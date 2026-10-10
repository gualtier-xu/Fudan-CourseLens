"""T4（夜14-R7 P1 → N15-R4 重排第 8 位）：task_store 暂停/重试/收口族直测。

六方法（retry_failed_task / acknowledge_pause / pause_all / resume_all /
checkpoint_for_shutdown / release_stuck_lifecycle_rows）此前 tests 全树零
直接调用，族内与 pause_task/resume_task/mark_terminal 覆盖不对称。全部走
真 TaskStore（临时目录 sqlite），running 行以既有黑盒先例
``update_task(state="running")`` 合成（test_unified_task_center 同款）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.runtime.task_store import TaskStore


class TaskStoreControlFamilyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temporary.name) / "state.db")

    def tearDown(self):
        self.temporary.cleanup()

    def _add(self, sub_id="1", **kwargs):
        task, _created = self.store.add_task("subtitle", "c1", sub_id, {}, **kwargs)
        return task

    def test_retry_failed_task_requeues_once_then_is_a_noop(self):
        task = self._add("retry-a")
        self.store.update_task(task["task_id"], state="running")
        self.assertIsNotNone(self.store.mark_terminal(task["task_id"], "failed", error="boom"))
        retried = self.store.retry_failed_task(task["task_id"])
        self.assertEqual(retried["state"], "queued")
        self.assertEqual(retried["error"], "")
        self.assertIsNone(retried.get("finished_at"))
        # 非 failed 行重试＝原样返回（幂等无副作用）。
        again = self.store.retry_failed_task(task["task_id"])
        self.assertEqual(again["state"], "queued")
        # 缺行返回 None（路由层转 404 语义）。
        self.assertIsNone(self.store.retry_failed_task("task:ghost"))

    def test_acknowledge_pause_finalizes_or_yields_to_resume_race(self):
        finalized = self._add("ack-a")
        self.store.pause_task(finalized["task_id"])
        self.assertEqual(self.store.acknowledge_pause(finalized["task_id"])["state"], "paused")

        # 竞态腿：running→pausing 后用户 resume 赢（resume_requested=1），
        # worker 收尾必须回队而非钉死 paused。
        racing = self._add("ack-b")
        self.store.update_task(racing["task_id"], state="running")
        self.assertEqual(self.store.pause_task(racing["task_id"])["state"], "pausing")
        self.assertEqual(self.store.resume_task(racing["task_id"])["state"], "pausing")
        self.assertEqual(self.store.acknowledge_pause(racing["task_id"])["state"], "queued")

        terminal = self._add("ack-c")
        self.store.mark_terminal(terminal["task_id"], "completed")
        self.assertEqual(
            self.store.acknowledge_pause(terminal["task_id"])["state"], "completed",
            "终态行绝不被 acknowledge 改写",
        )
        self.assertIsNone(self.store.acknowledge_pause("task:ghost"))

    def test_pause_all_sweeps_active_rows_sets_global_pause_and_is_idempotent(self):
        queued = self._add("pa-a")
        running = self._add("pa-b")
        self.store.update_task(running["task_id"], state="running")
        done = self._add("pa-c")
        self.store.mark_terminal(done["task_id"], "completed")

        affected = self.store.pause_all()
        self.assertIn(queued["task_id"], affected)
        self.assertIn(running["task_id"], affected)
        self.assertEqual(self.store.get_task(queued["task_id"])["state"], "paused")
        self.assertEqual(self.store.get_task(running["task_id"])["state"], "pausing")
        self.assertEqual(self.store.get_task(done["task_id"])["state"], "completed")
        # 全局暂停旗标在场：新任务缺省落 paused（不偷偷开跑）。
        later = self._add("pa-d")
        self.assertEqual(later["state"], "paused")
        # 幂等：第二遍零新受影响行。
        self.assertEqual(self.store.pause_all(), [])

    def test_resume_all_requeues_paused_flags_pausing_and_clears_global_pause(self):
        paused = self._add("ra-a")
        pausing = self._add("ra-b")
        self.store.update_task(pausing["task_id"], state="running")
        self.store.pause_all()

        affected = self.store.resume_all()
        self.assertIn(paused["task_id"], affected)
        self.assertIn(pausing["task_id"], affected)
        self.assertEqual(self.store.get_task(paused["task_id"])["state"], "queued")
        # pausing 行不直接跳 queued：置 resume_requested 等 worker 收尾裁决。
        self.assertEqual(self.store.get_task(pausing["task_id"])["state"], "pausing")
        # 全局旗标清除：新任务缺省回 queued。
        self.assertEqual(self._add("ra-c")["state"], "queued")

    def test_checkpoint_for_shutdown_parks_active_rows_once(self):
        queued = self._add("cp-a")
        running = self._add("cp-b")
        self.store.update_task(running["task_id"], state="running")
        done = self._add("cp-c")
        self.store.mark_terminal(done["task_id"], "failed")

        self.assertEqual(self.store.checkpoint_for_shutdown(), 2)
        self.assertEqual(self.store.get_task(queued["task_id"])["state"], "paused")
        self.assertEqual(self.store.get_task(running["task_id"])["state"], "paused")
        self.assertEqual(self.store.get_task(done["task_id"])["state"], "failed")
        # 幂等：已全部 parked，第二遍零行。
        self.assertEqual(self.store.checkpoint_for_shutdown(), 0)

    def test_release_stuck_lifecycle_rows_unlocks_only_stuck_rows(self):
        stuck_paused = self._add("rs-a")
        self.store.pause_task(stuck_paused["task_id"])
        done = self._add("rs-b")
        self.store.mark_terminal(done["task_id"], "completed")

        released = self.store.release_stuck_lifecycle_rows()
        self.assertEqual(
            set(released), {"tasks", "remote_runs", "automation_imports", "cleanup_gates"},
        )
        self.assertGreaterEqual(released["tasks"], 1)
        self.assertEqual(self.store.get_task(stuck_paused["task_id"])["state"], "canceled")
        self.assertEqual(
            self.store.get_task(done["task_id"])["state"], "completed",
            "终态历史一行不动",
        )
        self.assertEqual(released["remote_runs"], 0)


if __name__ == "__main__":
    unittest.main()
