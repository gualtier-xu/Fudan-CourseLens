"""FAULT-MATRIX-1 维度三：版本迁移循环注入矩阵（版本/降级维度）。

场景=0.1.0→0.1.1→降回→再升级的数据目录生命周期：升级幂等性、
未来版本 fail-closed（LOCAL_DATA_E_VERSION_TOO_NEW）、迁移中断的事务
回滚与自动恢复、全循环数据无损。全部在合成数据目录进行，绝不触碰
用户真实数据。

真实迁移回调（startup_migration._state_migration / _learning_migration）
+ 真实 coordinate_local_data；故障经 migrate_local_data 的 fault 钩子在
官方边界注入。
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.runtime.local_data_recovery import (
    BACKUP_DIRECTORY,
    CURRENT_SCHEMA_VERSION,
    ERROR_VERSION_TOO_NEW,
    LocalDataRecoveryError,
    migrate_local_data,
)
from src.runtime.learning_store import LearningStore
from src.runtime.startup_migration import (
    LEARNING_REQUIRED_TABLES,
    STATE_REQUIRED_TABLES,
    coordinate_local_data,
)
from src.runtime.task_store import TaskStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _schema_version_of(db_path: Path) -> int:
    with closing(sqlite3.connect(db_path)) as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='local_data_schema_meta'"
        ).fetchone()
        if not exists:
            return 0
        row = db.execute(
            "SELECT value FROM local_data_schema_meta WHERE key='schema_version'"
        ).fetchone()
        return int(row[0]) if row else 0


def _build_v0_dir(root: Path) -> None:
    """v0 数据目录=真实 schema 但无 local_data_schema_meta（0.1.0 前形态）。"""
    root.mkdir(parents=True, exist_ok=True)
    LearningStore(root / "learning.db")
    tasks = TaskStore(root / "state.db")
    tasks.set_app_state("faultmatrix.proof", "v0-row")
    tasks.add_task("subtitle", "1", "1-a", {"title": "迁移循环任务"})


def _seed_row_counts(root: Path) -> dict:
    counts = {}
    for name, table in (
        ("state.db", "app_state"),
        ("state.db", "tasks"),
        ("learning.db", "watch_progress"),
    ):
        with closing(sqlite3.connect(root / name)) as db:
            counts[f"{name}:{table}"] = db.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
    return counts


class _VersionLoopBase(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile as _tempfile

        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = _tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _mark_future_version(self) -> None:
        """模拟「被更高版本客户端写过」：schema_version 抬到当前+1。"""
        for name in ("state.db", "learning.db"):
            with closing(sqlite3.connect(self.root / name)) as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS local_data_schema_meta "
                    "(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
                )
                db.execute(
                    "INSERT INTO local_data_schema_meta(key,value) "
                    "VALUES('schema_version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(CURRENT_SCHEMA_VERSION + 1),),
                )
                db.commit()

    def _clear_future_version(self) -> None:
        """模拟「降回/清掉未来标记」：schema_version 回到当前版本。"""
        for name in ("state.db", "learning.db"):
            with closing(sqlite3.connect(self.root / name)) as db:
                db.execute(
                    "INSERT INTO local_data_schema_meta(key,value) "
                    "VALUES('schema_version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(CURRENT_SCHEMA_VERSION),),
                )
                db.commit()


class UpgradeIdempotencyTests(_VersionLoopBase):
    """升级幂等：v0→v1→再开→再开，数据与版本稳定。"""

    def test_v0_to_v1_upgrade_then_repeated_opens_are_idempotent(self):
        _build_v0_dir(self.root)
        before = _seed_row_counts(self.root)

        first = coordinate_local_data(self.root)
        self.assertEqual(first.status, "completed")
        self.assertEqual(first.schema_version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(_schema_version_of(self.root / "state.db"), 1)
        self.assertEqual(_schema_version_of(self.root / "learning.db"), 1)
        after_first = _seed_row_counts(self.root)
        self.assertEqual(after_first, before, "升级不得增删业务行")

        # 幂等：同一目录反复打开（模拟反复启动）。
        for _round in range(3):
            again = coordinate_local_data(self.root)
            self.assertEqual(again.status, "completed")
            self.assertEqual(again.schema_version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(_seed_row_counts(self.root), before, "循环打开零数据漂移")

        # 备份集有界（MAX_BACKUP_SETS=3）：目录不被反复启动撑爆。
        backup_root = self.root / BACKUP_DIRECTORY
        completed_sets = [
            d for d in backup_root.iterdir()
            if (d / "manifest.json").is_file()
        ]
        self.assertLessEqual(len(completed_sets), 3)


class DowngradeFailClosedTests(_VersionLoopBase):
    """降级兼容：未来版本数据目录对旧客户端必须 fail-closed 且无损。"""

    def test_future_version_data_is_rejected_with_data_untouched(self):
        _build_v0_dir(self.root)
        coordinate_local_data(self.root)
        self._mark_future_version()
        hashes = {
            name: _sha256(self.root / name) for name in ("state.db", "learning.db")
        }

        with self.assertRaises(LocalDataRecoveryError) as ctx:
            coordinate_local_data(self.root)
        self.assertEqual(ctx.exception.code, ERROR_VERSION_TOO_NEW)
        # F-VER-1 修复钉：降级场景的指引必须是「升级应用」，绝不指向备份
        # 恢复（恢复旧备份会毁掉新版本写入的数据）。
        self.assertIn("newer version of courselens", ctx.exception.instruction.lower())
        self.assertIn("update courselens", ctx.exception.instruction.lower())
        self.assertNotIn("backup", ctx.exception.instruction.lower())

        # fail-closed：原库逐字节未动。
        for name, digest in hashes.items():
            self.assertEqual(_sha256(self.root / name), digest)

    def test_downgrade_then_reupgrade_roundtrip_preserves_data(self):
        """0.1.0→(0.1.1 写过)→降回 0.1.0 清标记→再升级：全循环数据无损。"""
        _build_v0_dir(self.root)
        coordinate_local_data(self.root)
        self._mark_future_version()
        with self.assertRaises(LocalDataRecoveryError):
            coordinate_local_data(self.root)

        # 降回：标记回到当前版本后，旧客户端照常打开。
        self._clear_future_version()
        result = coordinate_local_data(self.root)
        self.assertEqual(result.status, "completed")
        tasks = TaskStore(self.root / "state.db").list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["sub_id"], "1-a")
        with closing(sqlite3.connect(self.root / "state.db")) as db:
            value = db.execute(
                "SELECT value_json FROM app_state WHERE key='faultmatrix.proof'"
            ).fetchone()[0]
        self.assertIn("v0-row", value)


class MigrationCrashConsistencyTests(_VersionLoopBase):
    """迁移中断：事务回滚+备份自动恢复+重跑成功。"""

    def test_crash_during_state_callback_rolls_back_and_next_open_recovers(self):
        _build_v0_dir(self.root)
        before = _seed_row_counts(self.root)

        from src.runtime.startup_migration import (
            _learning_migration,
            _state_migration,
        )

        def crashing_fault(boundary: str) -> None:
            if boundary == "migration.callback.state.db":
                raise RuntimeError("synthetic crash mid-migration")

        with self.assertRaises(LocalDataRecoveryError) as ctx:
            migrate_local_data(
                self.root,
                state_migration=_state_migration,
                learning_migration=_learning_migration,
                required_tables={
                    "state.db": STATE_REQUIRED_TABLES,
                    "learning.db": LEARNING_REQUIRED_TABLES,
                },
                fault=crashing_fault,
            )
        self.assertIn(
            ctx.exception.code,
            {"LOCAL_DATA_E_MIGRATION_FAILED", "LOCAL_DATA_E_RECOVERY_REQUIRED"},
        )
        # 原库未被半迁移污染：sqlite backup 恢复是逻辑等价（页布局可不同），
        # 故按行数与内容比对，不比字节。
        self.assertEqual(_seed_row_counts(self.root), before)
        with closing(sqlite3.connect(self.root / "state.db")) as db:
            value = db.execute(
                "SELECT value_json FROM app_state WHERE key='faultmatrix.proof'"
            ).fetchone()[0]
        self.assertIn("v0-row", value)

        # 下一次正常打开：自动从未完成备份恢复并完成迁移。
        recovered = coordinate_local_data(self.root)
        self.assertEqual(recovered.status, "completed")
        self.assertEqual(_seed_row_counts(self.root), before)

    def test_full_lifecycle_loop_upgrade_future_reject_recover(self):
        """完整生命周期环：升级→未来标记拒绝→恢复→再升级，四拍数据不丢。"""
        _build_v0_dir(self.root)
        before = _seed_row_counts(self.root)

        # 拍 1：v0→v1 升级。
        self.assertEqual(coordinate_local_data(self.root).status, "completed")
        # 拍 2：未来版本拒绝（fail-closed）。
        self._mark_future_version()
        with self.assertRaises(LocalDataRecoveryError):
            coordinate_local_data(self.root)
        # 拍 3：清标记（降回语义）。
        self._clear_future_version()
        # 拍 4：再升级成功。
        self.assertEqual(coordinate_local_data(self.root).status, "completed")
        self.assertEqual(_seed_row_counts(self.root), before, "四拍循环零数据丢失")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
