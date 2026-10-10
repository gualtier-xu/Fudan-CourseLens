"""FAULT-MATRIX-1 维度二：睡眠/唤醒注入矩阵（电源维度）。

真实整机睡眠绝不在此执行（无人值守红线：不动宿主电源状态）。注入手法=
宿主恢复检测的物理等效注入：真实 CourseLensApplication 上伪造「墙钟超前
单调钟」的观察基线（Windows 睡眠期间单调钟不计时、墙钟继续走——产品正是
凭这一漂移判恢复），配合网络断注入（客户端替身失败），驱动既有入口
（connection_snapshot/client）走完整唤醒旅程。

阈值锚点（application.py）：_HOST_RESUME_DRIFT_SECONDS=120s，
_HOST_RESUME_IDLE_SECONDS=1800s。行为变化须同步改本文件钉。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.application import CourseLensApplication

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _SleepWakeJourneyBase(unittest.TestCase):
    """真实 Application + 真实 TaskStore 的唤醒旅程基座。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temporary.name)
        self.service = CourseLensApplication(self.root)
        self.service.set_credentials("student", "password")

    def tearDown(self) -> None:
        self.service.close()
        self.temporary.cleanup()

    def _install_verified_client(self) -> Mock:
        old_client = Mock()
        old_client.check_alive.return_value = True
        with self.service._lock:
            self.service._client = old_client
            self.service._client_last_verified_at = time.monotonic()
            self.service._client_generation = self.service._route_generation
        self.service._set_login_status("ready", "icourse", "已连接", connected=True)
        return old_client

    def _observe_jump(self, *, wall_delta: float, mono_delta: float = 1.0) -> None:
        """伪造一次「睡眠前」的观察基线（墙钟/单调钟差值即注入量）。"""
        with self.service._lock:
            self.service._host_activity_observed = (
                time.monotonic() - mono_delta,
                time.time() - wall_delta,
            )


class SleepWakeTaskJourneyTests(_SleepWakeJourneyBase):
    """睡眠唤醒期间在途任务的数据面旅程。"""

    def test_running_task_survives_eight_hour_sleep_without_zombie_purge(self):
        """8 小时睡眠：唤醒后任务行无损、无僵尸清扫、可继续推进到终态。"""
        task, _created = self.service.task_store.add_task(
            "subtitle", "1", "1-a", {"title": "跨睡眠任务"}
        )
        task_id = str(task["task_id"])
        self.service.task_store.update_task(task_id, state="running")

        self._install_verified_client()
        generation_before = self.service._route_generation
        # 睡眠 8 小时（墙钟超前单调钟 8h，漂移远超 120s 阈值）。
        self._observe_jump(wall_delta=8 * 3600.0)
        snapshot = self.service.connection_snapshot()

        self.assertTrue(snapshot is not None)
        self.assertGreater(self.service._route_generation, generation_before)
        # 任务行原样存活：唤醒路径绝不触碰任务状态（僵尸清扫只在启动链）。
        after = self.service.task_store.get_task(task_id)
        self.assertEqual(after["state"], "running")
        self.assertEqual(after["sub_id"], "1-a")
        # 僵尸清扫是启动链专属：唤醒观察点不产生僵尸记录。
        self.assertEqual(self.service._startup_zombie_task_ids, [])

        # 网络恢复后任务正常收口（学生回来看到的是完成，不是丢任务）。
        self.service.task_store.mark_terminal(task_id, "completed")
        self.assertEqual(
            self.service.task_store.get_task(task_id)["state"], "completed"
        )

    def test_three_sleep_wake_cycles_keep_generation_monotonic_and_tasks_intact(self):
        """连续三轮睡眠/唤醒：代际单调递增、任务面零损伤。"""
        task, _ = self.service.task_store.add_task(
            "subtitle", "1", "1-b", {"title": "多轮睡眠任务"}
        )
        task_id = str(task["task_id"])
        self._install_verified_client()

        generations = []
        for _cycle in range(3):
            self._observe_jump(wall_delta=3 * 3600.0)
            self.assertTrue(self.service._maybe_note_host_resume())
            generations.append(self.service._route_generation)
        self.assertEqual(generations, sorted(generations), "代际必须单调不减")
        self.assertEqual(len(set(generations)), 3, "每轮恢复都必须推进代际")

        after = self.service.task_store.get_task(task_id)
        self.assertEqual(after["state"], "queued")


class SleepWakeFalsePositiveTests(_SleepWakeJourneyBase):
    """时钟回拨与静默节拍绝不可误判为恢复（防误重建客户端）。"""

    def test_wall_clock_step_back_never_fires_resume(self):
        """用户校时/NTP 回拨：墙钟倒退不是恢复事件。"""
        self._install_verified_client()
        generation_before = self.service._route_generation
        self._observe_jump(wall_delta=-6 * 3600.0)
        self.assertFalse(self.service._maybe_note_host_resume())
        self.assertEqual(self.service._route_generation, generation_before)

    def test_short_wall_jump_below_drift_threshold_is_not_resume(self):
        """几分钟级的墙钟噪声（<120s 漂移阈值）不触发恢复。"""
        self._install_verified_client()
        self._observe_jump(wall_delta=30.0, mono_delta=10.0)
        self.assertFalse(self.service._maybe_note_host_resume())

    def test_resume_detection_failure_never_breaks_caller(self):
        """恢复检测内部异常必须静默：绝不打断调用方（设计契约）。"""
        self._observe_jump(wall_delta=4 * 3600.0)
        with patch.object(
            self.service.network,
            "note_resume",
            side_effect=RuntimeError("network unavailable"),
        ):
            # 返回 False 且零外溢——connection_snapshot 照常给出快照。
            self.assertFalse(self.service._maybe_note_host_resume())
        snapshot = self.service.connection_snapshot()
        self.assertIsInstance(snapshot, dict)


class SleepWakeNetworkBreakTests(_SleepWakeJourneyBase):
    """唤醒后网络未就绪的组合注入（睡眠+断网复合故障）。"""

    def test_wake_with_dead_network_rebuilds_client_on_next_action(self):
        """唤醒后旧客户端已死（网络断）：下一个动作强制重建，不挂旧引用。"""
        old_client = self._install_verified_client()
        old_client.check_alive.return_value = False  # 唤醒后连接已死

        self._observe_jump(wall_delta=8 * 3600.0)
        self.assertTrue(self.service._maybe_note_host_resume())

        new_client = Mock()
        with patch.object(
            self.service, "_login_with_retry", return_value=new_client
        ) as login:
            client = self.service.client()
        login.assert_called_once()
        self.assertIs(client, new_client)

    def test_wake_reverify_then_unreachable_ticks_never_storm(self):
        """唤醒→立即重验（契约）→仍失败→退避放大→探测不可达→不发航班。

        完整序列钉：唤醒事件把断网期放大的退避复位为一拍；第一个 tick
        立即发起重验（≤30s 契约）；若网络仍未恢复，重验失败退避翻倍，
        其后 tick 走无凭据探测——仍不可达则不再发起注定失败的航班。
        """
        self._install_verified_client()
        self.service._set_login_status(
            "error", "webvpn", "登录失败，请检查账号、密码或网络状态",
            error_code="timeout",
        )
        with self.service._lock:
            self.service._session_self_heal_backoff = 600.0

        # ① 复睡唤醒：退避复位为一拍（REALRUN-1 契约）。
        self._observe_jump(wall_delta=8 * 3600.0)
        self.assertTrue(self.service._maybe_note_host_resume())
        with self.service._lock:
            self.assertEqual(self.service._session_self_heal_backoff, 30.0)

        # ② 唤醒后第一个 tick：立即重验（网络仍断→失败）。
        self.service.client = Mock(side_effect=RuntimeError("still offline"))
        backoff = self.service._session_self_heal_once()
        self.service.client.assert_called_once_with(verify_before_reuse=True)
        self.assertEqual(backoff, 60.0, "重验失败：退避翻倍不风暴")

        # ③ 后续 tick：探测确认校园路径仍不可达——不再发起注定失败的航班。
        self.service.client.reset_mock()
        self.service._campus_path_recovered = lambda: False
        backoff = self.service._session_self_heal_once()
        self.service.client.assert_not_called()
        self.assertEqual(backoff, 120.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
