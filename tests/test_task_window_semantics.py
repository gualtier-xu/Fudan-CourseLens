"""CLIENT-STATE-R1：任务窗口语义钉（newest_first + 90 天终态保留窗）。

死任务残余根因：list_tasks 恒 ORDER BY sequence 升序，「最近 200」实取最
旧 200 → 旧终态任务长驻任务区、新任务不进窗口。本文件钉三层：

① 行为钉（真实 TaskStore）：newest_first 参数（默认升序零变化；True=最新
   在前）；终态 90 天保留窗（默认生效、参数化、可显式 None 关闭；活跃态
   不受窗限；finished_at 缺失回退 updated_at）。
② 契约钉：api_v3.tasks() 走最新优先窗口。
③ 源码语义钉：七个「最近窗口」调用点 newest_first=True 且 reversed 清零；
   恢复/证据调用点显式 terminal_retention_days=None（核真/旧用户证据语义
   不变）。
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.runtime import api_v3
from src.runtime.task_store import TaskStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DAY = 86400.0

HTTP_API_SOURCE = (PROJECT_ROOT / "src" / "runtime" / "http_api.py").read_text(encoding="utf-8")
APPLICATION_SOURCE = (PROJECT_ROOT / "src" / "application.py").read_text(encoding="utf-8")
API_V3_SOURCE = (PROJECT_ROOT / "src" / "runtime" / "api_v3.py").read_text(encoding="utf-8")
TASK_STORE_SOURCE = (PROJECT_ROOT / "src" / "runtime" / "task_store.py").read_text(encoding="utf-8")


class TaskWindowBehaviorPins(unittest.TestCase):
    """真实 TaskStore 上的 newest_first 与 90 天保留窗行为。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.store = TaskStore(Path(self.temporary.name) / "state.db")
        now = time.time()
        # 乱序 sequence 插入：排序正确性必须来自查询而非插入顺序。
        # (sequence, state, finished_at, updated_at)
        rows = [
            (1, "completed", now - 100 * DAY, now - 100 * DAY),  # 终态超窗
            (2, "completed", now - 1 * DAY, now - 1 * DAY),      # 终态在窗内
            (3, "failed", None, now - 100 * DAY),                # 终态超窗（finished_at 缺失→updated_at）
            (4, "queued", None, now - 200 * DAY),                # 活跃态：永不受窗限
            (5, "failed", now - 10 * DAY, now - 10 * DAY),       # 终态在窗内
        ]
        with sqlite3.connect(self.store.path) as db:
            for sequence, state, finished_at, updated_at in rows:
                db.execute(
                    "INSERT INTO tasks(task_id,kind,course_id,sub_id,config_key,state,"
                    "payload_json,sequence,created_at,updated_at,finished_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (f"task-{sequence}", "summary", "course", f"sub-{sequence}", "",
                     state, "{}", sequence, updated_at, updated_at, finished_at),
                )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_default_window_hides_stale_terminal_and_keeps_ascending(self):
        # 默认=升序（恢复扫描语义零变化）+ 90 天终态保留窗（旧死任务剔除）。
        tasks = self.store.list_tasks(limit=500)
        self.assertEqual([task["sequence"] for task in tasks], [2, 4, 5])

    def test_newest_first_returns_most_recent_first(self):
        tasks = self.store.list_tasks(limit=500, newest_first=True)
        self.assertEqual([task["sequence"] for task in tasks], [5, 4, 2])

    def test_newest_first_respects_limit_window(self):
        # 「最近 2」必须取最新 2 条（根因场景：旧序取到最旧 2 条）。
        tasks = self.store.list_tasks(limit=2, newest_first=True)
        self.assertEqual([task["sequence"] for task in tasks], [5, 4])

    def test_retention_optout_lists_full_history(self):
        tasks = self.store.list_tasks(limit=500, terminal_retention_days=None)
        self.assertEqual([task["sequence"] for task in tasks], [1, 2, 3, 4, 5])

    def test_retention_window_is_parameterized(self):
        # 7 天窗：1 天前的 completed 保留，10 天前的 failed 剔除，活跃态不受限。
        tasks = self.store.list_tasks(limit=500, terminal_retention_days=7.0)
        self.assertEqual([task["sequence"] for task in tasks], [2, 4])

    def test_retention_boundary_ninety_days(self):
        now = time.time()
        with sqlite3.connect(self.store.path) as db:
            for sequence, finished_at in (
                (101, now - 90 * DAY + 120.0),  # 窗界内侧 2 分钟→保留
                (102, now - 90 * DAY - 120.0),  # 窗界外侧 2 分钟→剔除
            ):
                db.execute(
                    "INSERT INTO tasks(task_id,kind,course_id,sub_id,config_key,state,"
                    "payload_json,sequence,created_at,updated_at,finished_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (f"task-{sequence}", "summary", "course", f"sub-{sequence}", "",
                     "completed", "{}", sequence, finished_at, finished_at, finished_at),
                )
        tasks = self.store.list_tasks(limit=500)
        sequences = [task["sequence"] for task in tasks]
        self.assertIn(101, sequences)
        self.assertNotIn(102, sequences)

    def test_active_state_never_excluded_by_window(self):
        # 200 天前的 queued 行仍可见——保留窗只作用于终态行。
        tasks = self.store.list_tasks(limit=500, terminal_retention_days=0.0)
        self.assertEqual([task["sequence"] for task in tasks], [4])


class ApiV3TasksContractPins(unittest.TestCase):
    """api_v3.tasks() 契约=最新优先 + 保留窗（与任务抽屉同语义）。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.store = TaskStore(Path(self.temporary.name) / "state.db")
        now = time.time()
        with sqlite3.connect(self.store.path) as db:
            for sequence, state, finished_at in (
                (1, "completed", now - 100 * DAY),
                (2, "failed", now - 1 * DAY),
                (3, "queued", None),
            ):
                db.execute(
                    "INSERT INTO tasks(task_id,kind,course_id,sub_id,config_key,state,"
                    "payload_json,sequence,created_at,updated_at,finished_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (f"task-{sequence}", "summary", "course", f"sub-{sequence}", "",
                     state, "{}", sequence, now, now, finished_at),
                )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_tasks_returns_newest_first_windowed(self):
        values = api_v3.tasks(SimpleNamespace(task_store=self.store))
        self.assertEqual([task["sequence"] for task in values], [3, 2])


class TaskWindowSourcePins(unittest.TestCase):
    """七个最近窗口调用点 + 两个恢复/证据 opt-out 的源码语义钉。"""

    def test_list_tasks_signature_pins(self):
        self.assertIn("TERMINAL_RETENTION_DAYS = 90.0", TASK_STORE_SOURCE)
        self.assertIn(
            "terminal_retention_days: float | None = TERMINAL_RETENTION_DAYS",
            TASK_STORE_SOURCE,
        )
        self.assertIn('order = "ORDER BY sequence DESC" if newest_first else "ORDER BY sequence"',
                      TASK_STORE_SOURCE)

    def test_http_api_drawer_and_sse_pins(self):
        # GET /api/v3/tasks（抽屉主数据源）+ SSE tasks 快照：最新优先，reversed 清零。
        self.assertEqual(HTTP_API_SOURCE.count("list_tasks(limit=200, newest_first=True)"), 2)
        self.assertNotIn("reversed(service.tasks.repository.list_tasks", HTTP_API_SOURCE)

    def test_application_shell_and_summary_pins(self):
        # app_shell_snapshot 开机壳快照 + _active_summary_sub_ids 活动摘要判定。
        self.assertEqual(
            APPLICATION_SOURCE.count("self.task_store.list_tasks(limit=200, newest_first=True)"), 2,
        )
        self.assertNotIn("reversed(self.task_store.list_tasks", APPLICATION_SOURCE)

    def test_application_recovery_and_reuse_pins(self):
        # paused 恢复扫描 + quality_judge failed 复用查找。
        self.assertIn(
            'states=("paused",), limit=200, newest_first=True', APPLICATION_SOURCE,
        )
        self.assertIn(
            'kinds=("quality_judge",), limit=200, newest_first=True', APPLICATION_SOURCE,
        )

    def test_application_courseware_adjacent_pins(self):
        # 相邻缺陷同修：latest 语义 + 活动窗口取最新。
        self.assertIn(
            'kinds=("courseware_pdf",), limit=50, newest_first=True', APPLICATION_SOURCE,
        )
        self.assertIn(
            'kinds=("courseware_pdf",), states=ACTIVE_STATES, limit=20, newest_first=True',
            APPLICATION_SOURCE,
        )

    def test_application_recovery_optout_pins(self):
        # 恢复/证据路径显式关窗：存量 failed 启动核真 + 旧用户证据判定。
        self.assertIn(
            'states=("failed",), limit=200, terminal_retention_days=None,',
            APPLICATION_SOURCE,
        )
        self.assertIn(
            "list_tasks(limit=1, terminal_retention_days=None)", APPLICATION_SOURCE,
        )

    def test_api_v3_tasks_pin(self):
        self.assertIn("list_tasks(limit=100, newest_first=True)", API_V3_SOURCE)


if __name__ == "__main__":
    unittest.main()
