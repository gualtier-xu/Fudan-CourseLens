from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.runtime.task_store import TaskStore

import unittest

from src.application import CourseLensApplication
from src.runtime.http_api import TASK_ERROR_CODES, _task_error_code


class EnqueueParkedTaskTests(unittest.TestCase):
    def test_parked_recovery_task_is_rejected_with_closed_code(self):
        parked = {"state": "paused", "error": "remote_recovery_material_unavailable"}
        with self.assertRaisesRegex(RuntimeError, "task_already_active"):
            CourseLensApplication._reject_reused_parked_task(parked)

    def test_active_and_terminal_reused_tasks_pass_through(self):
        for task in (
            {"state": "running", "error": ""},
            {"state": "paused", "error": ""},
            {"state": "completed", "error": ""},
        ):
            CourseLensApplication._reject_reused_parked_task(task)

    def test_task_already_active_is_a_mapped_closed_code(self):
        self.assertIn("task_already_active", TASK_ERROR_CODES)
        self.assertEqual(_task_error_code("task_already_active"), "task_already_active")

    def test_remote_runner_lost_is_in_the_closed_code_table(self):
        """⑫（LOG1）：runner 中途失联专属闭集码入表，异常 code 直通分类。"""
        self.assertIn("remote_runner_lost", TASK_ERROR_CODES)
        self.assertEqual(_task_error_code("remote_runner_lost"), "remote_runner_lost")


if __name__ == "__main__":
    unittest.main()


class _RunnerLostNarrow:
    """⑫ 窄适配器：只挂自动重试方法与 control_task 桩，不装配整个应用。"""

    _auto_retry_runner_lost_once = CourseLensApplication._auto_retry_runner_lost_once

    def __init__(self, root: Path):
        self.task_store = TaskStore(root / "state.db")
        self.retries = []

    def control_task(self, task_id, action):
        self.retries.append((str(task_id), str(action)))
        return {"task_id": task_id, "state": "queued"}


class RunnerLostAutoRetryTests(unittest.TestCase):
    def test_auto_retry_runs_exactly_once_per_task(self):
        """⑫：runner 掉线自动重试恰一次——第二次掉线放行终态失败。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = _RunnerLostNarrow(Path(tmp.name))
        self.assertTrue(app._auto_retry_runner_lost_once("task-1"))
        self.assertEqual(app.retries, [("task-1", "retry")])
        self.assertFalse(app._auto_retry_runner_lost_once("task-1"), "同任务只自动重试一次")
        self.assertEqual(app.retries, [("task-1", "retry")])
        self.assertTrue(app._auto_retry_runner_lost_once("task-2"), "不同任务各享一次")
        self.assertEqual(app.retries, [("task-1", "retry"), ("task-2", "retry")])
        self.assertEqual(len(app.task_store.get_app_state("remote_runner_lost_retry.v1", [])), 2)

    def test_retry_channel_failure_returns_false(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = _RunnerLostNarrow(Path(tmp.name))
        app.control_task = self._fail
        self.assertFalse(app._auto_retry_runner_lost_once("task-x"), "重试通道故障不算已重试")

    @staticmethod
    def _fail(_task_id, _action):
        raise ValueError("task_retry_requires_failed_state")
