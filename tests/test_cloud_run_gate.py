from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from path_utils import PROJECT_ROOT
from src.application import (
    CLOUD_RUN_LIMIT_DEFAULT,
    CLOUD_RUN_LIMIT_MAX,
    CLOUD_RUN_LIMIT_STATE_KEY,
    CourseLensApplication,
)
from src.remote.coordinator import RemoteTaskPaused


class CloudRunGateTests(unittest.TestCase):
    """⑩（RUNLOCK-1）：云端在飞并发闸——≤5 默认、可调 10、超出排队、排队可取消。"""

    def setUp(self):
        cache = PROJECT_ROOT / "runtime" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(tmp.cleanup)
        self.service = CourseLensApplication(Path(tmp.name))
        self.addCleanup(self.service.close)

    def test_limit_defaults_to_five_and_clamps_into_one_to_ten(self):
        self.assertEqual(CLOUD_RUN_LIMIT_DEFAULT, 5)
        self.assertEqual(CLOUD_RUN_LIMIT_MAX, 10)
        self.assertEqual(self.service._cloud_run_limit(), 5)
        store = self.service.task_store
        for raw, expected in ((10, 10), (3, 3), (15, 10), (0, 1), (-2, 1), ("7", 7)):
            store.set_app_state(CLOUD_RUN_LIMIT_STATE_KEY, raw)
            self.assertEqual(self.service._cloud_run_limit(), expected)
        store.set_app_state(CLOUD_RUN_LIMIT_STATE_KEY, "not-a-number")
        self.assertEqual(self.service._cloud_run_limit(), CLOUD_RUN_LIMIT_DEFAULT)

    def test_gate_queues_beyond_limit_with_honest_label(self):
        service = self.service
        service.task_store.set_app_state(CLOUD_RUN_LIMIT_STATE_KEY, 2)
        with service._cloud_run_slot("task-a"):
            with service._cloud_run_slot("task-b"):
                waits: list[int] = []
                entered = threading.Event()
                proceed = threading.Event()

                def waiter():
                    with service._cloud_run_slot("task-c", on_wait=waits.append):
                        entered.set()
                        proceed.wait(5)

                thread = threading.Thread(target=waiter, daemon=True)
                thread.start()
                try:
                    deadline = time.time() + 2.0
                    while not waits and time.time() < deadline:
                        time.sleep(0.02)
                    self.assertEqual(waits, [2])
                    self.assertFalse(entered.is_set())
                finally:
                    proceed.set()
            # task-b 释放一个槽位后，排队者必须被唤醒进入。
            thread.join(5)
            self.assertTrue(entered.is_set())
        self.assertEqual(service._cloud_runs_active, 0)

    def test_queued_task_stays_cancelable_and_never_leaks_slots(self):
        service = self.service
        service.task_store.set_app_state(CLOUD_RUN_LIMIT_STATE_KEY, 1)
        with service._cloud_run_slot("task-a"):
            waits: list[int] = []
            with self.assertRaises(RemoteTaskPaused):
                with service._cloud_run_slot(
                    "task-b",
                    cancel_requested=lambda: True,
                    on_wait=waits.append,
                ):
                    self.fail("a canceled queued task must not take a slot")
            self.assertEqual(waits, [1])
        self.assertEqual(service._cloud_runs_active, 0)

    def test_all_product_dispatch_sites_go_through_the_shared_gate(self):
        # 字幕/总结/疑问/质检四处派发（自动化与手动在队列层同源汇流）共用同一
        # 闸实例（P11 质检走同一 _cloud_run_slot，学生任务优先语义不变）；
        # echo 探针与 DeepSeek 直连调用不占云槽。
        source = (PROJECT_ROOT / "src" / "application.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("self._cloud_run_slot("), 4)
        self.assertIn("self._cloud_run_condition = threading.Condition()", source)


if __name__ == "__main__":
    unittest.main()
