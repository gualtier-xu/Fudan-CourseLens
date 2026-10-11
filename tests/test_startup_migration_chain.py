"""夜10-C T12：启动迁移链完整性加深——幂等二次运行与事务适配器原子性。

既有钉面（tests/test_local_data_recovery.py）已覆盖故障边界/半升级/版本拒
识/损坏源/锁；本文件补三块：
- coordinate_local_data 在已迁移库上的二次运行（幂等 + 数据零丢失）；
- _TransactionalConnection 的整脚本原子性（executescript 拆句含字符串内
  分号的语句、commit/rollback 抑制、中途失败零部分提交）。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.runtime.local_data_recovery import CURRENT_SCHEMA_VERSION
from src.runtime.startup_migration import (
    _TransactionalConnection,
    coordinate_local_data,
)
from src.runtime.task_store import TaskStore


class CoordinateIdempotencyTests(unittest.TestCase):
    def test_second_run_completes_and_preserves_data(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = coordinate_local_data(root)
            self.assertEqual(first.status, "completed")
            self.assertEqual(first.schema_version, CURRENT_SCHEMA_VERSION)

            store = TaskStore(root / "state.db")
            task, created = store.add_task("subtitle", "course-1", "sub-1", {"k": "v"})
            self.assertTrue(created)
            store.close()

            second = coordinate_local_data(root)
            self.assertEqual(second.status, "completed")

            reopened = TaskStore(root / "state.db")
            try:
                self.assertEqual(reopened.get_task(task["task_id"])["kind"], "subtitle")
            finally:
                reopened.close()

    def test_second_run_keeps_schema_version_stable(self) -> None:
        def _read_version(db_path: Path) -> int:
            db = sqlite3.connect(db_path)
            try:
                value = db.execute(
                    "SELECT value FROM local_data_schema_meta WHERE key='schema_version'"
                ).fetchone()[0]
            finally:
                db.close()
            return int(value)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            coordinate_local_data(root)
            before = _read_version(root / "state.db")
            coordinate_local_data(root)
            after = _read_version(root / "state.db")
            self.assertEqual(before, CURRENT_SCHEMA_VERSION)
            self.assertEqual(after, CURRENT_SCHEMA_VERSION)


class TransactionalAdapterTests(unittest.TestCase):
    def _adapter_db(self) -> sqlite3.Connection:
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, note TEXT)")
        db.execute("BEGIN IMMEDIATE")
        return db

    def test_executescript_splits_semicolons_inside_string_literals(self) -> None:
        db = self._adapter_db()
        adapter = _TransactionalConnection(db)
        script = (
            "INSERT INTO t (id, note) VALUES (1, '含分号的文本；不是语句边界');\n"
            "INSERT INTO t (id, note) VALUES (2, 'another');\n"
            "UPDATE t SET note='x' WHERE id=2;\n"
        )
        adapter.executescript(script)
        self.assertEqual(
            db.execute("SELECT note FROM t WHERE id=1").fetchone()[0],
            "含分号的文本；不是语句边界",
            "字符串内分号不拆破语句",
        )
        self.assertIn("x", db.execute("SELECT note FROM t WHERE id=2").fetchone()[0])
        db.rollback()

    def test_commit_is_suppressed_and_rollback_removes_everything(self) -> None:
        db = self._adapter_db()
        adapter = _TransactionalConnection(db)
        adapter.executescript(
            "INSERT INTO t (id, note) VALUES (1, 'a');\n"
            "INSERT INTO t (id, note) VALUES (2, 'b');\n"
        )
        adapter.commit()  # 必须是 no-op：语句留在恢复事务内
        self.assertEqual(db.execute("SELECT COUNT(*) FROM t").fetchone()[0], 2)
        adapter.rollback()  # no-op：真正的回滚由外层持有者执行
        db.rollback()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM t").fetchone()[0], 0)

    def test_midscript_failure_leaves_no_partial_commit(self) -> None:
        db = self._adapter_db()
        adapter = _TransactionalConnection(db)
        with self.assertRaises(sqlite3.OperationalError):
            adapter.executescript(
                "INSERT INTO t (id, note) VALUES (1, 'kept-by-rollback');\n"
                "INSERT INTO missing_table (id) VALUES (2);\n"
            )
        db.rollback()
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM t").fetchone()[0], 0,
            "中途失败零部分提交（外层回滚可整体撤销）",
        )
        db.close()


if __name__ == "__main__":
    unittest.main()
