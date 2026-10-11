# AS10（第五十案）陈旧租约自愈钉面：暂停优先/恰一次自愈/有界退避/诚实排队态/
# 世代重置。51 真值链与 38 显式暂停语义零触碰（回归面由既有钉承担）。
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from src.application import (
    REMOTE_BUSY_BACKOFF_SECONDS,
    REMOTE_BUSY_HONEST_STAGE,
    REMOTE_BUSY_STATE_KEY_PREFIX,
    CourseLensApplication,
)
from src.runtime.task_store import USER_PAUSE_INTENT_KEY, TaskStore


class StaleLeaseRecoveryTests(unittest.TestCase):
    def _service(self, db_path: Path) -> CourseLensApplication:
        service = CourseLensApplication.__new__(CourseLensApplication)
        service.task_store = TaskStore(db_path)
        service.github_app = mock.Mock()
        service.github_app.cleanup_stale_job_token_lease.return_value = None
        return service

    def _add_running_task(self, service: CourseLensApplication) -> str:
        task, _ = service.task_store.add_task("subtitle", "course-1", "sub-1", {})
        task_id = str(task["task_id"])
        service.task_store.update_task(task_id, state="running", started_at=time.time() - 60)
        return task_id

    def test_first_busy_cycle_attempts_cleanup_once_and_requeues_without_sleep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task_id = self._add_running_task(service)
            with mock.patch("src.application.time.sleep") as sleep_mock:
                service._handle_remote_supervisor_busy(task_id, kind="subtitle")
            service.github_app.cleanup_stale_job_token_lease.assert_called_once_with(
                task_id=task_id, task_store=service.task_store
            )
            sleep_mock.assert_not_called()
            task = service.task_store.get_task(task_id)
            self.assertEqual(task["state"], "queued")
            cycle = service.task_store.get_app_state(f"{REMOTE_BUSY_STATE_KEY_PREFIX}{task_id}")
            self.assertEqual(cycle["count"], 1)

    def test_cleanup_refusal_still_requeues_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task_id = self._add_running_task(service)
            service.github_app.cleanup_stale_job_token_lease.side_effect = RuntimeError(
                "job_token_lease_active: another live lease"
            )
            with mock.patch("src.application.time.sleep"):
                service._handle_remote_supervisor_busy(task_id, kind="subtitle")
            task = service.task_store.get_task(task_id)
            self.assertEqual(task["state"], "queued", "前置拒绝不吞任务，照常排队")

    def test_repeated_busy_cycles_backoff_bounded_then_honest_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task_id = self._add_running_task(service)
            sleeps: list[float] = []
            with mock.patch("src.application.time.sleep", side_effect=sleeps.append):
                service._handle_remote_supervisor_busy(task_id, kind="subtitle")  # cycle 1
                service._handle_remote_supervisor_busy(task_id, kind="subtitle")  # cycle 2
                service._handle_remote_supervisor_busy(task_id, kind="subtitle")  # cycle 3
                service._handle_remote_supervisor_busy(task_id, kind="subtitle")  # cycle 4
            self.assertEqual(
                sleeps,
                [REMOTE_BUSY_BACKOFF_SECONDS[0], REMOTE_BUSY_BACKOFF_SECONDS[1],
                 REMOTE_BUSY_BACKOFF_SECONDS[2]],
                "第 2/3/4 周期按 30/60/120 有界退避，第 1 周期不睡",
            )
            # 第 3 周期起任务卡为诚实排队态：无罐头 ETA、有等待文案
            task = service.task_store.get_task(task_id)
            self.assertEqual(task["state"], "queued")
            progress = dict(task.get("progress") or {})
            self.assertEqual(progress.get("stage"), "waiting_cloud_idle")
            self.assertIn("已自动排队", str(progress.get("label")))
            self.assertIsNone(progress.get("percent"))

    def test_new_run_generation_resets_busy_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task_id = self._add_running_task(service)
            with mock.patch("src.application.time.sleep"):
                service._handle_remote_supervisor_busy(task_id, kind="summary")
                service._handle_remote_supervisor_busy(task_id, kind="summary")
            cycle_key = f"{REMOTE_BUSY_STATE_KEY_PREFIX}{task_id}"
            self.assertEqual(service.task_store.get_app_state(cycle_key)["count"], 2)
            # 任务重新开跑：started_at 前进 = 新世代，计数归零并重获自愈机会
            service.task_store.update_task(task_id, state="running", started_at=time.time())
            service.github_app.cleanup_stale_job_token_lease.reset_mock()
            with mock.patch("src.application.time.sleep") as sleep_mock:
                service._handle_remote_supervisor_busy(task_id, kind="summary")
            self.assertEqual(service.task_store.get_app_state(cycle_key)["count"], 1)
            service.github_app.cleanup_stale_job_token_lease.assert_called_once()
            sleep_mock.assert_not_called()

    def test_pause_intent_wins_over_busy_requeue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task_id = self._add_running_task(service)
            service.task_store.pause_task(task_id)  # 38 语义：写入暂停意图
            with mock.patch("src.application.time.sleep") as sleep_mock:
                service._handle_remote_supervisor_busy(task_id, kind="subtitle")
            sleep_mock.assert_not_called()
            service.github_app.cleanup_stale_job_token_lease.assert_not_called()
            task = service.task_store.get_task(task_id)
            self.assertEqual(task["state"], "paused", "暂停意图优先于重排，绝不置回 queued")

    def test_source_replaces_all_three_legacy_5s_requeues(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "src" / "application.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("time.sleep(5.0)", source, "5s 永动重排必须整体退役")
        self.assertEqual(
            source.count("_handle_remote_supervisor_busy(task_id, kind="),
            4,
            "字幕/总结/解答/质检四处 busy 分支全部走共用处置（P11 同一漏斗）",
        )
        self.assertIn("cleanup_stale_job_token_lease", source)


class BusyCycleClearedOnTerminalPin(unittest.TestCase):
    """生成规则单元：任务终态即清 busy 退避计数（app_state 不随任务累积）。"""

    def test_mark_terminal_clears_busy_cycle_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = CourseLensApplication.__new__(CourseLensApplication)
            service.task_store = TaskStore(Path(tmp) / "state.db")
            service.github_app = mock.Mock()
            task, _ = service.task_store.add_task("subtitle", "course-1", "sub-1", {})
            task_id = str(task["task_id"])
            service.task_store.update_task(task_id, state="running", started_at=time.time())
            with mock.patch("src.application.time.sleep"):
                service._handle_remote_supervisor_busy(task_id, kind="subtitle")
            key = f"{REMOTE_BUSY_STATE_KEY_PREFIX}{task_id}"
            self.assertIsNotNone(service.task_store.get_app_state(key))
            service.task_store.mark_terminal(task_id, "completed")
            self.assertEqual(service.task_store.get_app_state(key, None), None)


class FallbackBudgetEchoPin(unittest.TestCase):
    """O2：get_automation_profile 无行回显兜底与现行默认预算同值（AS3 对齐）。"""

    def test_fallback_profile_echoes_current_default_budget(self) -> None:
        from src.runtime.automation import DEFAULT_BUDGET

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            profile = store.get_automation_profile("default")
        self.assertEqual(profile["budget"], DEFAULT_BUDGET)


if __name__ == "__main__":
    unittest.main()
