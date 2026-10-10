"""FAULT-MATRIX-1 维度一：磁盘满注入矩阵（SWEEP 十维之外的资源维度）。

注入手法（非管理员宿主的真实等效注入，绝不触碰真实数据卷）：
- SQLite 层：``PRAGMA max_page_count`` 收紧到当前页数 → 真实 SQLITE_FULL
  （与真实磁盘满同码路），对真实 TaskStore / learning.db 生效。
- JSON/文件层：对真实写入原语注入 ``OSError(errno.ENOSPC)``。

每个用例验证四件事：故障时诚实报错（不静默丢数据）、既有数据无损
（哈希/行数比对）、可恢复（解除注入后写回成功）、恢复后数据仍无损。

磁盘满候选发现（F-DISK-*）在本文件以钉测固化；行为变化须同步改钉。
"""

from __future__ import annotations

import errno
import hashlib
import json
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path
from unittest import mock

import pytest

from src.runtime.learning_store import LearningStore
from src.runtime.local_data_recovery import (
    CURRENT_SCHEMA_VERSION,
    ERROR_DISK_FULL,
    LocalDataRecoveryError,
)
from src.runtime.startup_migration import coordinate_local_data
from src.runtime.task_store import TaskStore


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_state_db(root: Path, task_count: int = 2) -> TaskStore:
    root.mkdir(parents=True, exist_ok=True)
    store = TaskStore(root / "state.db")
    for index in range(task_count):
        store.add_task("subtitle", "1", f"1-{index}", {"title": f"磁盘满注入任务 {index}"})
    return store


class _GrowthLimitedSqlite:
    """模块级 sqlite3 代理：新连接一律 max_page_count=当前页数（只限本模块）。"""

    def __init__(self):
        self._real = sqlite3

    def connect(self, *args, **kwargs):
        db = self._real.connect(*args, **kwargs)
        pages = int(db.execute("PRAGMA page_count").fetchone()[0])
        db.execute(f"PRAGMA max_page_count={pages}")
        return db

    def __getattr__(self, name):
        return getattr(self._real, name)


def _freeze_store_growth(store_module: str):
    """把磁盘满注入钉进目标模块的 sqlite 连接面（页分配失败=真实 SQLITE_FULL）。"""
    module = __import__(store_module, fromlist=["sqlite3"])
    return mock.patch.object(module, "sqlite3", _GrowthLimitedSqlite())


def _freeze_sqlite_growth(path: Path) -> None:
    """把 max_page_count 收紧到当前页数：后续任何增长=SQLITE_FULL。"""
    with closing(sqlite3.connect(path)) as db:
        pages = int(db.execute("PRAGMA page_count").fetchone()[0])
        db.execute(f"PRAGMA max_page_count={pages}")
        db.commit()


def _release_sqlite_growth(path: Path) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA max_page_count=2147483646")
        db.commit()


class TestSqliteDiskFull:
    """SQLITE_FULL（真实磁盘满码路）对任务存储的注入。"""

    def test_full_disk_insert_fails_honestly_and_existing_rows_survive(self, tmp_path):
        store = _seed_state_db(tmp_path)
        db_path = tmp_path / "state.db"
        before = {t["task_id"]: t for t in store.list_tasks()}
        assert len(before) == 2

        with _freeze_store_growth("src.runtime.task_store"):
            with pytest.raises(sqlite3.OperationalError) as excinfo:
                store.add_task(
                    "subtitle", "1", "1-new", {"title": "x", "blob": "y" * 262144}
                )
            assert "full" in str(excinfo.value).lower()

        after = {t["task_id"]: t for t in store.list_tasks()}
        assert set(after) == set(before), "故障期间的既有任务必须无损"

        store.add_task("subtitle", "1", "1-new", {"title": "x"})
        recovered = {t["task_id"] for t in store.list_tasks()}
        assert len(recovered) == 3, "解除注入后写回成功且原数据仍在"

    def test_full_disk_wal_checkpoint_keeps_database_queryable(self, tmp_path):
        store = _seed_state_db(tmp_path, task_count=1)
        with _freeze_store_growth("src.runtime.task_store"):
            with pytest.raises(sqlite3.OperationalError):
                store.add_task(
                    "summary", "1", "1-x", {"title": "x", "blob": "y" * 262144}
                )
            # 故障态下读路径必须仍然健康（学生还能浏览既有任务）。
            remaining = store.list_tasks()
            assert len(remaining) == 1 and remaining[0]["sub_id"] == "1-0"

    def test_learning_store_full_disk_keeps_progress_rows(self, tmp_path):
        root = tmp_path
        store = LearningStore(root / "learning.db")
        db_path = root / "learning.db"
        store.save_watch_progress(
            course_id="1", sub_id="1-a", position_seconds=42, duration_seconds=2000
        )
        # 先吃掉页内空闲（大 blob 占位），冻结后任何新行都必须分配新页。
        with closing(sqlite3.connect(db_path)) as db:
            db.execute(
                "INSERT INTO ai_artifacts(artifact_id,course_id,sub_id,kind,status,"
                "input_hash,prompt_version,content_json,created_at,updated_at) VALUES("
                "'blob-1','1','1-a','summary','ready','synthetic','v1',?,"
                "strftime('%s','now'), strftime('%s','now'))",
                (json.dumps({"body": "z" * (4 * 1024 * 1024)}),),
            )
            db.commit()

        with _freeze_store_growth("src.runtime.learning_store"):
            with pytest.raises(sqlite3.OperationalError):
                for index in range(2000):
                    store.save_watch_progress(
                        course_id="1",
                        sub_id=f"full-{index}",
                        position_seconds=1,
                        duration_seconds=2000,
                    )
        assert store.get_watch_progress("1-a")["position_seconds"] == 42, (
            "满盘写入失败的回滚不得伤及既有学习进度"
        )


class TestStartupOnFullDisk:
    """启动迁移链路（coordinate_local_data）在磁盘满下的行为。"""

    def test_manifest_write_enospc_at_startup_surfaces_stable_recovery_code(
        self, tmp_path
    ):
        """F-DISK-1：启动期磁盘满必须给出闭集恢复码而非裸 OSError。

        错误页/启动脚本按 LocalDataRecoveryError 的稳定码引导（恢复 UX 通道）；
        裸 OSError 只能落兜底「启动失败」，学生无从知道「清出磁盘空间」。
        """
        _seed_state_db(tmp_path)
        LearningStore(tmp_path / "learning.db")
        enospc = OSError(errno.ENOSPC, "No space left on device")

        def full_disk_atomic_json(path, payload):
            raise enospc

        with mock.patch(
            "src.runtime.local_data_recovery._atomic_json",
            side_effect=full_disk_atomic_json,
        ):
            with pytest.raises(LocalDataRecoveryError) as excinfo:
                coordinate_local_data(tmp_path)
        assert excinfo.value.code == ERROR_DISK_FULL
        assert "free up space" in excinfo.value.instruction.lower()
        # 既有数据未被动过（写发生在任何备份/迁移之前）。
        assert (tmp_path / "state.db").is_file()

    def test_backup_write_enospc_mid_migration_fails_closed_and_recovers(
        self, tmp_path
    ):
        """备份中途磁盘满：fail-closed + 数据无损 + 解除后可重跑成功。"""
        _seed_state_db(tmp_path)
        LearningStore(tmp_path / "learning.db")
        state_before = _sha256(tmp_path / "state.db")

        call_count = {"n": 0}
        real_backup = __import__(
            "src.runtime.local_data_recovery", fromlist=["_backup_database"]
        )._backup_database

        def failing_backup(source, target):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_backup(source, target)

        with mock.patch(
            "src.runtime.local_data_recovery._backup_database",
            side_effect=failing_backup,
        ):
            with pytest.raises(LocalDataRecoveryError) as excinfo:
                coordinate_local_data(tmp_path)
        assert excinfo.value.code == ERROR_DISK_FULL
        assert _sha256(tmp_path / "state.db") == state_before, "迁移失败不得改动原库"

        # 解除注入（磁盘腾出空间）后重跑：成功且数据仍在。
        result = coordinate_local_data(tmp_path)
        assert result.status == "completed"
        assert result.schema_version == CURRENT_SCHEMA_VERSION
        tasks = TaskStore(tmp_path / "state.db").list_tasks()
        assert [t["sub_id"] for t in tasks] == ["1-0", "1-1"]


class TestFileLevelEnospc:
    """JSON/凭据文件层 ENOSPC 注入：原子语义保全旧文件。"""

    def test_credentials_replace_failure_keeps_existing_file(self, tmp_path):
        """os.replace 注入 ENOSPC：旧凭据文件必须原封不动。"""
        from credentials import CredentialStore

        scratch = Path(__file__).resolve().parents[1] / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            storage_path = Path(directory) / "credentials.json"
            storage = CredentialStore(storage_path)
            storage.save("student-a", "old-secret")
            original_bytes = storage_path.read_bytes()

            def full_replace(source, target, *args, **kwargs):
                raise OSError(errno.ENOSPC, "No space left on device")

            with mock.patch("credentials.os.replace", side_effect=full_replace):
                with pytest.raises(OSError) as excinfo:
                    storage.save("student-a", "new-secret")
            assert excinfo.value.errno == errno.ENOSPC
            assert storage_path.read_bytes() == original_bytes, "替换失败不得损坏旧凭据"
            # 解除后重写成功。
            storage.save("student-a", "new-secret")
            student, secret = storage.load("student-a")
            assert (student, secret) == ("student-a", "new-secret")

    def test_update_atomic_json_replace_failure_keeps_old_payload(self, tmp_path):
        from src.update import durability

        target = tmp_path / "release-state.json"
        durability.atomic_json(target, {"v": 1}, boundary="b")
        before = json.loads(target.read_text(encoding="utf-8"))

        real_replace = durability.durable_replace

        def full_replace(source, destination):
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(durability, "durable_replace", side_effect=full_replace):
            with pytest.raises(OSError):
                durability.atomic_json(target, {"v": 2}, boundary="b")
        assert json.loads(target.read_text(encoding="utf-8")) == before
        # 解除后正常写回 + 残留 tmp 可被既有清扫收敛。
        durability.atomic_json(target, {"v": 2}, boundary="b")
        assert json.loads(target.read_text(encoding="utf-8"))["v"] == 2
        durability.cleanup_atomic_temps(tmp_path)
        leftovers = list(tmp_path.rglob(".*.tmp"))
        assert leftovers == [], "清扫后不得残留原子写临时文件"
