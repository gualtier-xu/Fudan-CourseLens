"""WP-3（LOCALRUNTIME-POLISH-1 U1）：tasks/remote_runs 双索引钉。

N9-H #4 卡销项：任务抽屉与远端对账热路径此前全表 SCAN+TEMP B-TREE。
索引列以真实查询为准（list_tasks ORDER BY sequence / list_remote_runs
ORDER BY updated_at DESC），30k 行合成库实测 3.27ms→0.49ms 与
16.64ms→0.23ms（EXPLAIN 前后证存 WP-3 结果文件）。
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.runtime.task_store import TaskStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TaskStoreIndexPins(unittest.TestCase):
    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.store = TaskStore(Path(self.temporary.name) / "state.db")
        with sqlite3.connect(self.store.path) as db:
            # 乱序插入：排序正确性必须来自索引/查询而非插入顺序。
            for sequence in (7, 1, 9, 3, 5):
                db.execute(
                    "INSERT INTO tasks(task_id,kind,course_id,sub_id,config_key,state,"
                    "payload_json,sequence,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (f"task-{sequence}", "summary", "course", f"sub-{sequence}", "",
                     "completed", "{}", sequence, 100.0 + sequence, 100.0 + sequence),
                )
            for offset, updated_at in enumerate((50.0, 90.0, 10.0, 70.0, 30.0)):
                db.execute(
                    "INSERT INTO remote_runs(task_id,repository,workflow,run_id,attempt,"
                    "remote_state,updated_at,last_error) VALUES(?,?,?,?,?,?,?,?)",
                    (f"task-{offset + 1}", "owner/repo", "echo.yml", 100 + offset, 1,
                     "imported", updated_at, ""),
                )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _index_names(self) -> set[str]:
        with sqlite3.connect(self.store.path) as db:
            return {
                str(row[0])
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")
            }

    def test_new_indexes_exist(self):
        names = self._index_names()
        self.assertIn("idx_tasks_sequence", names)
        self.assertIn("idx_remote_runs_updated", names)

    def test_hot_plans_use_indexes_without_sort(self):
        with sqlite3.connect(self.store.path) as db:
            drawer = " ".join(
                row[-1] for row in db.execute(
                    "EXPLAIN QUERY PLAN SELECT * FROM tasks ORDER BY sequence LIMIT 200"
                )
            )
            recon = " ".join(
                row[-1] for row in db.execute(
                    "EXPLAIN QUERY PLAN SELECT * FROM remote_runs ORDER BY updated_at DESC LIMIT 100"
                )
            )
        self.assertIn("idx_tasks_sequence", drawer)
        self.assertNotIn("TEMP B-TREE", drawer)
        self.assertIn("idx_remote_runs_updated", recon)
        self.assertNotIn("TEMP B-TREE", recon)

    def test_list_tasks_returns_sequence_order_with_limit(self):
        # CLIENT-STATE-R1：本测试钉索引排序语义，与终态保留窗无关——种子为
        # 远古 completed 行，显式关窗以保持原判定面。
        tasks = self.store.list_tasks(limit=3, terminal_retention_days=None)
        self.assertEqual([task["sequence"] for task in tasks], [1, 3, 5])

    def test_list_remote_runs_orders_by_updated_at_desc(self):
        runs = self.store.list_remote_runs(limit=10)
        self.assertEqual([run["updated_at"] for run in runs], [90.0, 70.0, 50.0, 30.0, 10.0])
        self.assertEqual(
            [run["updated_at"] for run in self.store.list_remote_runs(limit=10, workflow="echo.yml")],
            [90.0, 70.0, 50.0, 30.0, 10.0],
        )
        self.assertEqual(self.store.list_remote_runs(limit=10, workflow="subtitle.yml"), [])

    def test_schema_reopen_is_idempotent(self):
        TaskStore(self.store.path)
        self.assertIn("idx_tasks_sequence", self._index_names())
        self.assertIn("idx_remote_runs_updated", self._index_names())
        self.assertEqual(len(self.store.list_tasks(limit=500, terminal_retention_days=None)), 5)

    def test_dispatch_lookup_uses_lecture_index(self):
        # 夜10-C T7 第二层审计钉：add_task/find_active 的活跃查找必须走
        # idx_tasks_lecture（kind+sub_id+config_key 等值+state IN），绝不退化
        # 成全表 SCAN（30k 行实测索引路径 0.01ms vs 全扫 14ms）。
        with sqlite3.connect(self.store.path) as db:
            plan = " ".join(
                row[-1] for row in db.execute(
                    "EXPLAIN QUERY PLAN SELECT * FROM tasks "
                    "WHERE kind='summary' AND sub_id='sub-1' AND config_key='' "
                    "AND state IN ('queued','running','pausing','paused') "
                    "ORDER BY sequence DESC LIMIT 1"
                )
            )
        self.assertIn("idx_tasks_lecture", plan)
        self.assertNotIn("SCAN tasks", plan)


if __name__ == "__main__":
    unittest.main()
