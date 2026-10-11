"""T13 残余（夜14-R7/夜15-R4 台账）：ShutdownBudgets 算术 + 信号安装/恢复对称。

test_local_lifecycle.py 已盖实例锁/陈旧实例收割/关机宽限/无头闲置；本件只
补该文件族零直测的两小块：预算总账（关机超时上限的来源算术）与
``install_signal_handlers``/``restore_signal_handlers`` 的对称性——
恢复面若漏装/漏恢复，测试进程信号语义被产品污染而全量不红。
"""

from __future__ import annotations

import dataclasses
import signal
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.runtime.lifecycle import (
    LifecycleController,
    ShutdownBudgets,
    install_signal_handlers,
    restore_signal_handlers,
)


class ShutdownBudgetsTests(unittest.TestCase):
    def test_default_budget_totals_eleven_seconds(self):
        budgets = ShutdownBudgets()
        self.assertEqual(
            budgets.total,
            budgets.request_drain + budgets.live_sessions
            + budgets.task_checkpoint + budgets.service_stop,
        )
        self.assertEqual(budgets.total, 11.0)

    def test_custom_budgets_sum_and_dataclass_is_frozen(self):
        budgets = ShutdownBudgets(request_drain=0.5, live_sessions=2.0,
                                  task_checkpoint=1.5, service_stop=4.0)
        self.assertEqual(budgets.total, 8.0)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            budgets.service_stop = 99.0


class SignalHandlerSymmetryTests(unittest.TestCase):
    def test_install_requests_shutdown_and_restore_returns_previous_handlers(self):
        controller = LifecycleController()
        original_int = signal.getsignal(signal.SIGINT)
        original_term = signal.getsignal(signal.SIGTERM)
        try:
            previous = install_signal_handlers(controller)
            candidates = [signal.SIGINT, signal.SIGTERM]
            if hasattr(signal, "SIGBREAK"):
                candidates.append(signal.SIGBREAK)
            self.assertEqual(sorted(previous), sorted(int(item) for item in candidates))
            for number in candidates:
                installed = signal.getsignal(number)
                self.assertIsNot(installed, original_int, "安装后不再是原处理器")
            # 处理器本体：直接以信号号调用（不发真信号），关机请求闭集理由。
            installed_int = signal.getsignal(signal.SIGINT)
            installed_int(int(signal.SIGINT), None)
            self.assertTrue(controller.snapshot()["state"] == "draining")
            self.assertFalse(
                controller.request_shutdown("again"),
                "重复关机请求幂等拒绝",
            )
        finally:
            restore_signal_handlers(previous)
        self.assertIs(signal.getsignal(signal.SIGINT), original_int, "SIGINT 恢复原处理器")
        self.assertIs(signal.getsignal(signal.SIGTERM), original_term, "SIGTERM 恢复原处理器")


if __name__ == "__main__":
    unittest.main()
