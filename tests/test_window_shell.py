# 夜10-A APP-SHAPE-2 钉面（窗口壳域）：/api/v3/tasks 只读查询面、任务栏进度
# 映射、外链闭集校验、关窗决策（看门狗放行/托盘隐藏/在飞确认/几何落盘）、
# js 桥诚实失败、二次实例拉起的身份闭集。零真实 GUI/零真弹窗：弹窗与
# Win32 闪光全部注入或打桩。
from __future__ import annotations

import ctypes
import json
import os
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.runtime.window_shell import (
    ActiveTaskSnapshot,
    MediaRunWatcher,
    NativeShellApi,
    TaskbarProgressPoller,
    TBPF_INDETERMINATE,
    TBPF_NOPROGRESS,
    TBPF_NORMAL,
    TBPF_PAUSED,
    TerminalBadgeTracker,
    WindowClosePolicy,
    _overlay_icon_pixels,
    apply_titlebar_theme,
    create_terminal_overlay_icon,
    focus_running_instance_window,
    is_external_http_url,
    query_active_tasks,
    read_window_geometry,
    taskbar_state_for,
    tooltip_for,
    tray_status_line_for,
)
from src.runtime.tray_manager import (
    DARK_TASKBAR_VARIANT_SUFFIX,
    IMMERSIVE_COLOR_SET,
    NIN_BALLOONUSERCLICK,
    NIN_SELECT,
    THEME_REG_VALUE,
    WM_APP_TRAY,
    WM_LBUTTONDBLCLK,
    WM_LBUTTONUP,
    WM_RBUTTONUP,
    WM_SETTINGCHANGE,
    TrayIcon,
    apps_use_light_theme,
)
from src.runtime.window_state import (
    load_window_state,
    webview_cache_clear_marker_path,
)

ROOT = Path(__file__).resolve().parents[1]


def _envelope(data: dict) -> bytes:
    return json.dumps({"schema": "courselens.api.v3", "data": data}).encode("utf-8")


class _StubTasksHandler(BaseHTTPRequestHandler):
    """闭集桩：只回 /api/v3/tasks 的 envelope；一切失败路径=诚实的 unknown。"""

    payload = _envelope(
        {
            "tasks": [],
            "counts": {"active": 0, "failed": 0, "completed": 0},
        }
    )

    def log_message(self, format, *args):  # noqa: A002
        return

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)


class QueryActiveTasksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _StubTasksHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def _url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def test_empty_queue_is_known_zero(self) -> None:
        snapshot = query_active_tasks(self._url())
        self.assertFalse(snapshot.unknown)
        self.assertEqual(snapshot.active, 0)

    def test_running_fraction_and_paused_are_parsed(self) -> None:
        _StubTasksHandler.payload = _envelope(
            {
                "tasks": [
                    {"state": "running", "progress": {"processed_media_seconds": 60, "media_duration_seconds": 240}},
                    {"state": "running", "progress": {"processed_media_seconds": 30, "media_duration_seconds": 60}},
                    {"state": "queued", "progress": {}},
                    {"state": "paused", "progress": {}},
                    {"state": "completed", "progress": {}},
                ],
                "counts": {"active": 4, "failed": 0, "completed": 1},
            }
        )
        self.addCleanup(setattr, _StubTasksHandler, "payload", _envelope({"tasks": [], "counts": {"active": 0, "failed": 0, "completed": 0}}))
        snapshot = query_active_tasks(self._url())
        self.assertEqual(snapshot.active, 4)
        self.assertEqual(snapshot.running, 2)
        self.assertTrue(snapshot.any_paused)
        self.assertAlmostEqual(snapshot.fraction, 90 / 300)

    def test_course_gate_none_counts_degrade_to_unknown(self) -> None:
        _StubTasksHandler.payload = _envelope(
            {"tasks": [], "counts": {"active": None, "failed": None, "completed": None}}
        )
        self.addCleanup(setattr, _StubTasksHandler, "payload", _envelope({"tasks": [], "counts": {"active": 0, "failed": 0, "completed": 0}}))
        snapshot = query_active_tasks(self._url())
        self.assertTrue(snapshot.unknown)

    def test_connection_refused_is_unknown(self) -> None:
        # 端口 1（保留）连接必然失败：未知≠「没有任务」。
        self.assertTrue(query_active_tasks("http://127.0.0.1:1/", timeout=0.2).unknown)

    def test_malformed_envelope_is_unknown(self) -> None:
        _StubTasksHandler.payload = b"[]"
        self.addCleanup(setattr, _StubTasksHandler, "payload", _envelope({"tasks": [], "counts": {"active": 0, "failed": 0, "completed": 0}}))
        self.assertTrue(query_active_tasks(self._url()).unknown)

    def test_media_running_and_failed_ids_are_parsed(self) -> None:
        # SYSTRAY-IMPL-2：媒体长任务（字幕/总结）running 单独计数；failed 的
        # task_id 有序透出（新失败判定=消费方自持基线）。
        _StubTasksHandler.payload = _envelope(
            {
                "tasks": [
                    {"kind": "subtitle", "state": "running", "task_id": "t1", "progress": {}},
                    {"kind": "summary", "state": "running", "task_id": "t2", "progress": {}},
                    {"kind": "quiz", "state": "running", "task_id": "t3", "progress": {}},
                    {"kind": "subtitle", "state": "failed", "task_id": "t4", "progress": {}},
                    {"kind": "subtitle", "state": "failed", "task_id": "t0", "progress": {}},
                    {"kind": "summary", "state": "completed", "task_id": "t5", "progress": {}},
                ],
                "counts": {"active": 3, "failed": 2, "completed": 1},
            }
        )
        self.addCleanup(setattr, _StubTasksHandler, "payload", _envelope({"tasks": [], "counts": {"active": 0, "failed": 0, "completed": 0}}))
        snapshot = query_active_tasks(self._url())
        self.assertEqual(snapshot.running, 3)
        self.assertEqual(snapshot.media_running, 2, "quiz 不算媒体长任务")
        self.assertEqual(snapshot.failed_ids, ("t0", "t4"))

    def test_query_builds_the_kind_running_tuple(self) -> None:
        # P2-9 解析面：running 任务的 (kind, 数量) 按计数降序进快照。
        _StubTasksHandler.payload = _envelope(
            {
                "tasks": [
                    {"kind": "subtitle", "state": "running", "progress": {}},
                    {"kind": "subtitle", "state": "running", "progress": {}},
                    {"kind": "summary", "state": "running", "progress": {}},
                ],
                "counts": {"active": 3, "failed": 0, "completed": 0},
            }
        )
        self.addCleanup(setattr, _StubTasksHandler, "payload", _envelope({"tasks": [], "counts": {"active": 0, "failed": 0, "completed": 0}}))
        snapshot = query_active_tasks(self._url())
        self.assertEqual(snapshot.kind_running, (("subtitle", 2), ("summary", 1)))


class TaskbarMappingTests(unittest.TestCase):
    def test_unknown_maps_to_no_bar(self) -> None:
        self.assertIsNone(taskbar_state_for(ActiveTaskSnapshot(unknown=True)))

    def test_idle_maps_to_noprogress(self) -> None:
        self.assertEqual(taskbar_state_for(ActiveTaskSnapshot(unknown=False, active=0)), (TBPF_NOPROGRESS, 0, 0))

    def test_running_with_units_maps_to_normal_fraction(self) -> None:
        mapped = taskbar_state_for(
            ActiveTaskSnapshot(unknown=False, active=2, running=2, completed_units=90, total_units=300)
        )
        self.assertEqual(mapped[0], TBPF_NORMAL)
        self.assertEqual(mapped[1:], (90, 300))

    def test_running_without_units_maps_to_indeterminate(self) -> None:
        self.assertEqual(taskbar_state_for(ActiveTaskSnapshot(unknown=False, active=1, running=1))[0], TBPF_INDETERMINATE)

    def test_only_paused_work_maps_to_paused_color(self) -> None:
        self.assertEqual(taskbar_state_for(ActiveTaskSnapshot(unknown=False, active=1, any_paused=True))[0], TBPF_PAUSED)

    def test_poller_never_starts_without_a_handle(self) -> None:
        poller = TaskbarProgressPoller("http://127.0.0.1:1/")
        poller.start(0)
        self.assertIsNone(poller._thread)
        poller.stop()


class TerminalBadgeTrackerTests(unittest.TestCase):
    """SYSTRAY-IMPL-2 P2-6：终态徽章状态机矩阵（红点=新失败，绿勾一次性）。"""

    def _snap(self, **kwargs) -> ActiveTaskSnapshot:
        defaults = dict(unknown=False, active=0, running=0, media_running=0, failed_ids=())
        defaults.update(kwargs)
        return ActiveTaskSnapshot(**defaults)

    def test_unknown_never_moves_the_badge(self) -> None:
        tracker = TerminalBadgeTracker()
        self.assertEqual(tracker.next(self._snap(unknown=True)), "none")

    def test_pre_existing_failures_never_light_the_red_dot(self) -> None:
        # 启动前旧账（首拍已存在的 failed）不点亮红点——红点只报「新」失败。
        tracker = TerminalBadgeTracker()
        self.assertEqual(tracker.next(self._snap(failed_ids=("old",))), "none")
        self.assertEqual(tracker.next(self._snap(failed_ids=("old",))), "none")

    def test_new_failure_lights_red_exactly_once(self) -> None:
        tracker = TerminalBadgeTracker()
        tracker.next(self._snap(failed_ids=("old",)))  # 基线拍
        self.assertEqual(tracker.next(self._snap(failed_ids=("old", "new"))), "failed")
        self.assertEqual(tracker.next(self._snap(failed_ids=("old", "new"))), "none")

    def test_resolved_failure_clears_the_stale_red_dot(self) -> None:
        # 学生把失败任务处理掉（任务列表里不再出现）→ 红点销账。
        tracker = TerminalBadgeTracker()
        tracker.next(self._snap(failed_ids=("old",)))
        tracker.next(self._snap(failed_ids=("old", "new")))
        self.assertEqual(tracker.next(self._snap(failed_ids=("old",))), "clear")
        self.assertEqual(tracker.next(self._snap(failed_ids=("old",))), "none")

    def test_media_phase_completion_shows_green_once(self) -> None:
        tracker = TerminalBadgeTracker()
        self.assertEqual(tracker.next(self._snap(active=1, running=1, media_running=1)), "none")
        self.assertEqual(tracker.next(self._snap(active=2, running=1, media_running=1)), "none")
        self.assertEqual(tracker.next(self._snap()), "done")
        self.assertEqual(tracker.next(self._snap()), "none")  # 一次性：不逐拍重发

    def test_green_never_overrides_red_and_viewed_clears_both(self) -> None:
        tracker = TerminalBadgeTracker()
        tracker.next(self._snap(active=1, running=1, media_running=1))
        self.assertEqual(tracker.next(self._snap(active=1, failed_ids=("x",))), "failed")
        self.assertEqual(tracker.next(self._snap(active=0, failed_ids=("x",))), "none")  # 红在场：阶段完成不抢
        self.assertEqual(tracker.next(self._snap(), window_viewed=True), "clear")
        self.assertEqual(tracker.next(self._snap()), "none")  # 已销账，窗离开也不复燃

    def test_window_viewed_clears_the_badge_and_acknowledges_failures(self) -> None:
        tracker = TerminalBadgeTracker()
        tracker.next(self._snap(failed_ids=("old",)))
        tracker.next(self._snap(failed_ids=("old", "new")))
        self.assertEqual(
            tracker.next(self._snap(failed_ids=("old", "new")), window_viewed=True), "clear"
        )
        self.assertEqual(tracker.next(self._snap(failed_ids=("old", "new"))), "none")

    def test_non_media_phase_never_shows_green(self) -> None:
        tracker = TerminalBadgeTracker()
        tracker.next(self._snap(active=1, running=1))  # 秒级小任务，不算「一轮长活」
        self.assertEqual(tracker.next(self._snap()), "none")


class MediaRunWatcherTests(unittest.TestCase):
    """SYSTRAY-IMPL-2 P2-7：媒体长任务终态下降沿矩阵。"""

    def _snap(self, **kwargs) -> ActiveTaskSnapshot:
        defaults = dict(unknown=False, active=0, running=0, media_running=0, failed_ids=())
        defaults.update(kwargs)
        return ActiveTaskSnapshot(**defaults)

    def test_falling_edge_fires_exactly_once(self) -> None:
        watcher = MediaRunWatcher()
        self.assertIsNone(watcher.observe(self._snap(active=1, running=1, media_running=1)))
        self.assertIsNone(watcher.observe(self._snap(active=2, running=2, media_running=2)))
        self.assertEqual(watcher.observe(self._snap()), "completed")
        self.assertIsNone(watcher.observe(self._snap()))  # 0→0 不再发

    def test_partial_completion_waits_for_zero(self) -> None:
        watcher = MediaRunWatcher()
        watcher.observe(self._snap(active=2, running=2, media_running=2))
        self.assertIsNone(watcher.observe(self._snap(active=1, running=1, media_running=1)))
        self.assertEqual(watcher.observe(self._snap()), "completed")  # 只认归零拍

    def test_zero_to_zero_and_unknown_never_fire(self) -> None:
        watcher = MediaRunWatcher()
        self.assertIsNone(watcher.observe(self._snap()))
        self.assertIsNone(watcher.observe(self._snap()))
        # unknown 不动观测状态：服务抖动一拍，恢复后下降沿仍成立。
        watcher.observe(self._snap(active=1, running=1, media_running=1))
        self.assertIsNone(watcher.observe(self._snap(unknown=True)))
        self.assertEqual(watcher.observe(self._snap()), "completed")

    def test_new_failure_during_phase_means_failed_copy(self) -> None:
        watcher = MediaRunWatcher()
        watcher.observe(self._snap(active=1, running=1, media_running=1))
        self.assertEqual(
            watcher.observe(self._snap(failed_ids=("t9",))), "failed"
        )

    def test_pre_existing_failures_keep_the_completed_copy(self) -> None:
        watcher = MediaRunWatcher()
        watcher.observe(self._snap(failed_ids=("old",)))  # 启动前旧账=基线
        watcher.observe(self._snap(active=1, running=1, media_running=1))
        self.assertEqual(watcher.observe(self._snap(failed_ids=("old",))), "completed")

    def test_watcher_rearms_for_the_next_phase(self) -> None:
        watcher = MediaRunWatcher()
        watcher.observe(self._snap(active=1, running=1, media_running=1))
        self.assertEqual(watcher.observe(self._snap()), "completed")
        watcher.observe(self._snap(active=1, running=1, media_running=1))
        self.assertEqual(watcher.observe(self._snap()), "completed")  # 每轮各一枚


class TrayNotifyTests(unittest.TestCase):
    """SYSTRAY-IMPL-2 P2-7：notify 零副作用边界 + 气球点击=回窗口。"""

    def test_notify_without_running_icon_is_a_noop(self) -> None:
        # 未 start（图标没挂上）→ False 且绝不触碰 shell（托盘未挂=静默跳过）。
        tray = TrayIcon()
        self.assertFalse(tray.notify("任务跑完了", "字幕和总结都已经就绪。"))

    def test_balloon_click_opens_the_window(self) -> None:
        # 气球被点（NIN_SELECT / NIN_BALLOONUSERCLICK）= 打开窗口，通知无死胡同。
        for event in (NIN_SELECT, NIN_BALLOONUSERCLICK):
            tray = TrayIcon()
            seen: list = []
            tray._on_open = lambda: seen.append("open")
            tray._wndproc(0, WM_APP_TRAY, 0, event)
            self.assertEqual(seen, ["open"], f"事件 {event:#x} 应回窗口")

    def test_app_wires_the_media_falling_edge_source_pin(self) -> None:
        source = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
        self.assertIn("media_watcher = MediaRunWatcher()", source)
        self.assertIn('event == "completed"', source)
        self.assertIn("tray.notify(*_MEDIA_DONE_NOTIFY)", source)
        self.assertIn("tray.notify(*_MEDIA_FAILED_NOTIFY, kind=\"error\")", source)
        self.assertIn("_MEDIA_DONE_NOTIFY = (", source)
        self.assertIn("_MEDIA_FAILED_NOTIFY = (", source)


class TerminalOverlayIconTests(unittest.TestCase):
    """SYSTRAY-IMPL-2 P2-6：徽章像素矩阵（纯函数）+ GDI 活体 + 两步调用形态钉。"""

    def test_done_pixels_have_transparent_corners_green_body_white_check(self) -> None:
        pixels = _overlay_icon_pixels("done")
        self.assertEqual(len(pixels), 16)
        self.assertEqual(len(pixels[0]), 16)
        for x, y in ((0, 0), (15, 0), (0, 15), (15, 15)):
            self.assertEqual(pixels[y][x][3], 0, "四角必须透明")
        self.assertEqual(pixels[8][2], (86, 158, 32, 255), "非勾处圆身=实心绿")
        self.assertEqual(pixels[8][8][:3], (255, 255, 255), "勾笔画恰过圆心=白")
        whites = [
            (x, y) for y in range(16) for x in range(16)
            if pixels[y][x][:3] == (255, 255, 255)
        ]
        self.assertGreaterEqual(len(whites), 6, "绿勾必须有可见白色笔画")

    def test_failed_pixels_are_a_solid_red_dot(self) -> None:
        pixels = _overlay_icon_pixels("failed")
        red_center = pixels[8][8]
        self.assertEqual(red_center, (66, 80, 232, 255))
        whites = [
            1 for y in range(16) for x in range(16) if pixels[y][x][:3] == (255, 255, 255)
        ]
        self.assertEqual(whites, [], "红点=纯色圆点，无勾")

    @unittest.skipUnless(os.name == "nt", "GDI 活体")
    def test_gdi_creates_and_destroys_real_overlay_icons(self) -> None:
        import ctypes

        for kind in ("done", "failed"):
            hicon = create_terminal_overlay_icon(kind)
            self.assertTrue(hicon, f"{kind} 徽章 HICON 应能现画")
            self.assertTrue(ctypes.windll.user32.DestroyIcon(hicon))
        self.assertEqual(create_terminal_overlay_icon("nonsense"), 0, "未知种类=纯色点回退（红）≠失败")

    def test_taskbar_calls_use_the_two_step_vtbl_form_source_pin(self) -> None:
        # SYSTRAY-IMPL-2 修复钉：ITaskbarList3 方法必须两步调用（取地址再以
        # this 调用）——「原型类型直调」在 ctypes 抛 TypeError 被吞恒 False，
        # 进度条因此从未真正画出来过；禁止回退。
        source = (ROOT / "src" / "runtime" / "window_shell.py").read_text(encoding="utf-8")
        self.assertNotIn("self._proto_state(", source)
        self.assertNotIn("self._proto_value(", source)
        self.assertIn("_ITASKBAR3_VTBL_SET_OVERLAY_ICON = 11", source)
        self.assertIn("_ITASKBAR3_VTBL_SET_PROGRESS_STATE = 10", source)
        self.assertIn("_ITASKBAR3_VTBL_SET_PROGRESS_VALUE = 9", source)
        self.assertIn("def set_overlay_badge", source)
        self.assertIn("DestroyIcon", source)


class TrayTooltipTests(unittest.TestCase):
    """夜10-A #9（E2）：托盘 tooltip 的文案映射与零副作用边界。"""

    def test_idle_and_unknown_keep_plain_name(self) -> None:
        self.assertEqual(tooltip_for(ActiveTaskSnapshot(unknown=True)), "CourseLens")
        self.assertEqual(tooltip_for(ActiveTaskSnapshot(unknown=False, active=0)), "CourseLens")

    def test_active_tasks_show_a_count_line(self) -> None:
        self.assertEqual(
            tooltip_for(ActiveTaskSnapshot(unknown=False, active=2)),
            "CourseLens\n2 个任务进行中",
        )

    def test_typed_tooltip_names_each_running_kind(self) -> None:
        # P2-9：「字幕转写 ×2、总结 ×1 进行中」形态（包面口径全称）。
        snapshot = ActiveTaskSnapshot(
            unknown=False, active=3,
            kind_running=(("subtitle", 2), ("summary", 1)),
        )
        self.assertEqual(tooltip_for(snapshot), "CourseLens\n字幕转写 ×2、总结 ×1 进行中")

    def test_typed_tooltip_orders_by_count_then_kind(self) -> None:
        # 计数降序、同数按 kind 名序——同一任务面恒同一文案（确定性）。
        snapshot = ActiveTaskSnapshot(
            unknown=False, active=4,
            kind_running=(("subtitle", 2), ("quiz", 1), ("summary", 1)),
        )
        self.assertEqual(
            tooltip_for(snapshot),
            "CourseLens\n字幕转写 ×2、测验 ×1、总结 ×1 进行中",
        )

    def test_unknown_kind_falls_back_to_the_raw_string(self) -> None:
        snapshot = ActiveTaskSnapshot(
            unknown=False, active=1, kind_running=(("brand_new_kind", 1),)
        )
        self.assertEqual(
            tooltip_for(snapshot), "CourseLens\nbrand_new_kind ×1 进行中"
        )

    def test_typed_tooltip_is_capped_at_127_chars(self) -> None:
        many = tuple((f"kind_{i}", 1) for i in range(40))
        snapshot = ActiveTaskSnapshot(unknown=False, active=40, kind_running=many)
        self.assertLessEqual(len(tooltip_for(snapshot)), 127)

    def test_set_tooltip_without_running_icon_is_a_noop(self) -> None:
        # 未 start（图标没挂上）→ False 且绝不触碰 shell（单测机零托盘副作用）。
        tray = TrayIcon()
        self.assertFalse(tray.set_tooltip("CourseLens\n1 个任务进行中"))

    def test_poller_passes_snapshots_to_callback(self) -> None:
        # on_snapshot 在轮询线程每拍收到任务面——接线面钉：回调可调用且 stop 幂等。
        seen: list = []

        def _cb(snapshot) -> None:
            seen.append(snapshot)

        poller = TaskbarProgressPoller("http://127.0.0.1:1/", on_snapshot=_cb)
        poller._on_snapshot(ActiveTaskSnapshot())
        self.assertEqual(len(seen), 1)
        poller.stop()  # 未启动时 stop 幂等

    def test_app_wires_the_snapshot_callback(self) -> None:
        source = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
        self.assertIn("on_snapshot=_on_task_snapshot", source)
        self.assertIn("tray.set_tooltip(text)", source)


class TrayStatusLineCopyTests(unittest.TestCase):
    """SYSTRAY-IMPL-2 P1-4：托盘右键菜单状态行文案矩阵（未知=整行不显示）。"""

    def test_unknown_hides_the_status_line(self) -> None:
        # 查询失败/课程门未过=诚实未知：空串让状态行消失，绝不伪装成空闲。
        self.assertEqual(tray_status_line_for(ActiveTaskSnapshot(unknown=True)), "")

    def test_active_tasks_show_a_running_count(self) -> None:
        self.assertEqual(
            tray_status_line_for(ActiveTaskSnapshot(unknown=False, active=3)),
            "3 个任务在跑",
        )

    def test_idle_shows_a_calm_clear_line(self) -> None:
        self.assertEqual(
            tray_status_line_for(ActiveTaskSnapshot(unknown=False, active=0)),
            "暂无进行中任务",
        )


@unittest.skipUnless(os.name == "nt", "Win32 菜单句柄面")
class TrayContextStatusLineTests(unittest.TestCase):
    """SYSTRAY-IMPL-2 P1-4：右键菜单构建矩阵——真实 Win32 菜单对象（构建即销毁，绝不 TrackPopupMenu）。"""

    MF_BYPOSITION = 0x400
    MF_GRAYED = 0x1
    MF_SEPARATOR = 0x800

    def _build(self, provider) -> int:
        tray = TrayIcon(status_provider=provider)
        menu = tray._build_context_menu()
        self.addCleanup(self._destroy, menu)
        return menu

    @staticmethod
    def _destroy(menu: int) -> None:
        import ctypes

        if menu:
            ctypes.windll.user32.DestroyMenu(menu)

    def _items(self, menu: int) -> list[tuple[str, int]]:
        import ctypes

        user32 = ctypes.windll.user32
        items: list[tuple[str, int]] = []
        for index in range(user32.GetMenuItemCount(menu)):
            length = user32.GetMenuStringW(menu, index, None, 0, self.MF_BYPOSITION)
            buffer = ctypes.create_unicode_buffer(max(length + 1, 1))
            user32.GetMenuStringW(menu, index, buffer, length + 1, self.MF_BYPOSITION)
            items.append((buffer.value, user32.GetMenuState(menu, index, self.MF_BYPOSITION)))
        return items

    def test_without_provider_the_menu_stays_two_fixed_items(self) -> None:
        items = self._items(self._build(None))
        self.assertEqual([text for text, _flags in items], ["打开 CourseLens", "退出 CourseLens"])

    def test_provider_text_becomes_a_grayed_first_line_with_separator(self) -> None:
        items = self._items(self._build(lambda: "2 个任务在跑"))
        self.assertEqual(len(items), 4)
        text0, flags0 = items[0]
        self.assertEqual(text0, "2 个任务在跑")
        self.assertTrue(flags0 & self.MF_GRAYED, "状态行必须灰显（不可点）")
        self.assertTrue(items[1][1] & self.MF_SEPARATOR, "状态行后必须有分隔线")
        self.assertEqual(items[2][0], "打开 CourseLens")
        self.assertEqual(items[3][0], "退出 CourseLens")

    def test_empty_provider_text_keeps_the_plain_menu(self) -> None:
        self.assertEqual(len(self._items(self._build(lambda: ""))), 2)
        self.assertEqual(len(self._items(self._build(lambda: "  "))), 2)

    def test_raising_provider_never_steals_the_menu_focus(self) -> None:
        # provider 抛错=状态行不显示，菜单照常打开（绝不因状态查询失焦）。
        def _boom() -> str:
            raise RuntimeError("查询炸了")

        items = self._items(self._build(_boom))
        self.assertEqual([text for text, _flags in items], ["打开 CourseLens", "退出 CourseLens"])

    def test_long_status_text_is_truncated_to_the_menu_cap(self) -> None:
        items = self._items(self._build(lambda: "长" * 300))
        self.assertEqual(len(items), 4)
        self.assertLessEqual(len(items[0][0]), 127)

    def test_app_wires_the_status_provider_source_pin(self) -> None:
        source = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
        self.assertIn("status_provider=_tray_status_line", source)
        self.assertIn("tray_status_line_for(query_active_tasks(url))", source)


class TrayClickEventMatrixTests(unittest.TestCase):
    """SYSTRAY-IMPL-1 P1-1：托盘左键单击/双击事件矩阵（零真实线程，直接打 _wndproc）。"""

    def _tray(self) -> tuple[TrayIcon, list]:
        tray = TrayIcon()
        seen: list = []
        tray._on_open = lambda: seen.append("open")
        return tray, seen

    def test_left_button_up_opens_exactly_once(self) -> None:
        tray, seen = self._tray()
        tray._wndproc(0, WM_APP_TRAY, 0, WM_LBUTTONUP)
        self.assertEqual(seen, ["open"])

    def test_double_click_still_opens(self) -> None:
        # 双击行为保留：单击落地后 DBLCLK 分支不动，show/restore 幂等。
        tray, seen = self._tray()
        tray._wndproc(0, WM_APP_TRAY, 0, WM_LBUTTONDBLCLK)
        self.assertEqual(seen, ["open"])

    @unittest.skipUnless(os.name == "nt", "右键菜单腿走真实 user32.CreatePopupMenu")
    def test_right_button_up_never_opens(self) -> None:
        # hwnd=0 时右键菜单路径早退（零真实菜单副作用），且绝不触发打开。
        tray, seen = self._tray()
        tray._wndproc(0, WM_APP_TRAY, 0, WM_RBUTTONUP)
        self.assertEqual(seen, [])

    @unittest.skipUnless(os.name == "nt", "兜底腿走真实 user32.DefWindowProcW")
    def test_press_and_non_tray_messages_never_open(self) -> None:
        # 按下（WM_LBUTTONDOWN）不算打开；非托盘回调消息不进本分支。
        tray, seen = self._tray()
        tray._wndproc(0, WM_APP_TRAY, 0, 0x0201)
        tray._wndproc(0, 0x9999, 0, WM_LBUTTONUP)
        self.assertEqual(seen, [])


class TrayIconLoadSourcePinTests(unittest.TestCase):
    """SYSTRAY-IMPL-1 P1-2 源钉：小图标按系统尺寸显式装载（高 DPI 不糊）。"""

    def test_load_icon_uses_small_icon_metrics_before_loadimage(self) -> None:
        source = (ROOT / "src" / "runtime" / "tray_manager.py").read_text(encoding="utf-8")
        fn_idx = source.index("def _load_icon(")
        body = source[fn_idx:source.index("def _icon_data(", fn_idx)]
        self.assertIn("GetSystemMetrics(SM_CXSMICON)", body)
        self.assertIn("GetSystemMetrics(SM_CYSMICON)", body)
        load_idx = body.index("LoadImageW(")
        self.assertLess(body.index("SM_CXSMICON"), load_idx)
        # 尺寸读不到（0）时保持原 cx=cy=0 行为的回退形态仍在。
        self.assertIn("0, 0, LR_LOADFROMFILE", body)


class TrayThemeIconTests(unittest.TestCase):
    """SYSTRAY-IMPL-3：托盘图标深浅色自适应（零真实线程/零真实 Shell 挂载）。

    口径矩阵注入注册表读数（patch apps_use_light_theme）；WM_SETTINGCHANGE
    分发用真实宽字符缓冲地址打 lParam（缓冲在测试帧内保活）；装载腿用真实
    installer ICO 字节出真句柄，用完 DestroyIcon 收口。
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ico = (ROOT / "installer" / "courselens-icon.ico").read_bytes()
        self.base = Path(tmp.name) / "courselens-icon.ico"
        self.variant = Path(tmp.name) / "courselens-icon-darktaskbar.ico"
        self.base.write_bytes(ico)
        self.variant.write_bytes(ico)

    def _patch_theme(self, value: bool | None):
        return mock.patch("src.runtime.tray_manager.apps_use_light_theme", return_value=value)

    def _tray(self) -> TrayIcon:
        tray = TrayIcon()
        tray._icon_path = str(self.base)
        tray._active_icon_path = str(self.base)
        return tray

    # --- 选择矩阵 ------------------------------------------------------------

    def test_dark_registry_selects_the_sibling_variant(self) -> None:
        with self._patch_theme(False):
            tray = self._tray()
            self.assertEqual(tray._resolve_icon_path(str(self.base)), str(self.variant))

    def test_light_registry_keeps_the_base_icon(self) -> None:
        # 浅色形态=现役图标本身（浅底观感已取证达标），不引入重复资产。
        with self._patch_theme(True):
            tray = self._tray()
            self.assertEqual(tray._resolve_icon_path(str(self.base)), str(self.base))

    def test_unknown_registry_falls_back_to_the_base_icon(self) -> None:
        with self._patch_theme(None):
            tray = self._tray()
            self.assertEqual(tray._resolve_icon_path(str(self.base)), str(self.base))

    def test_missing_variant_falls_back_to_the_base_icon(self) -> None:
        self.variant.unlink()
        with self._patch_theme(False):
            tray = self._tray()
            self.assertEqual(tray._resolve_icon_path(str(self.base)), str(self.base))

    def test_no_base_icon_resolves_to_nothing(self) -> None:
        tray = self._tray()
        self.assertIsNone(tray._resolve_icon_path(None))

    # --- 注册表口径读取 ------------------------------------------------------

    @unittest.skipUnless(os.name == "nt", "winreg 仅 Windows 存在")
    def test_registry_reader_maps_light_and_dark_dword(self) -> None:
        import winreg

        with mock.patch("winreg.QueryValueEx", return_value=(0, winreg.REG_DWORD)):
            self.assertIs(apps_use_light_theme(), False)
        with mock.patch("winreg.QueryValueEx", return_value=(1, winreg.REG_DWORD)):
            self.assertIs(apps_use_light_theme(), True)

    @unittest.skipUnless(os.name == "nt", "winreg 仅 Windows 存在")
    def test_registry_reader_fails_closed_to_none(self) -> None:
        with mock.patch("winreg.OpenKey", side_effect=OSError("no key")):
            self.assertIsNone(apps_use_light_theme())
        with mock.patch("winreg.QueryValueEx", return_value=("dark", 1)):
            self.assertIsNone(apps_use_light_theme())

    # --- WM_SETTINGCHANGE 分发 -----------------------------------------------

    @unittest.skipUnless(os.name == "nt", "装载腿=真实 LoadImageW/DestroyIcon")
    def test_theme_broadcast_hot_swaps_to_the_variant(self) -> None:
        import src.runtime.tray_manager as tm

        tray = self._tray()
        buf = ctypes.create_unicode_buffer("ImmersiveColorSet")
        with self._patch_theme(False):
            code = tray._wndproc(0, WM_SETTINGCHANGE, 0, ctypes.addressof(buf))
        self.assertEqual(code, 0)
        self.assertEqual(tray._active_icon_path, str(self.variant))
        self.assertTrue(tray._hicon)
        self.assertFalse(tray._hicon_is_shared)
        tm.user32.DestroyIcon(tray._hicon)
        tray._hicon = 0

    def test_non_theme_broadcast_and_null_lparam_are_noops(self) -> None:
        tray = self._tray()
        buf = ctypes.create_unicode_buffer("SomethingElse")
        self.assertEqual(tray._wndproc(0, WM_SETTINGCHANGE, 0, ctypes.addressof(buf)), 0)
        self.assertEqual(tray._wndproc(0, WM_SETTINGCHANGE, 0, 0), 0)
        self.assertEqual(tray._active_icon_path, str(self.base))
        self.assertEqual(tray._hicon, 0)

    def test_unknown_registry_broadcast_keeps_the_mounted_icon(self) -> None:
        tray = self._tray()
        buf = ctypes.create_unicode_buffer("ImmersiveColorSet")
        with self._patch_theme(None):
            tray._wndproc(0, WM_SETTINGCHANGE, 0, ctypes.addressof(buf))
        self.assertEqual(tray._active_icon_path, str(self.base))
        self.assertEqual(tray._hicon, 0)

    @unittest.skipUnless(os.name == "nt", "装载腿=真实 LoadImageW（损坏变体仍触发装载）")
    def test_corrupt_variant_never_downgrades_the_mounted_icon(self) -> None:
        # 装载失败落到共享兜底图标时，保留旧图标不换——主题切换绝不丢托盘入口。
        tray = self._tray()
        self.variant.write_bytes(b"not an ico")
        buf = ctypes.create_unicode_buffer("ImmersiveColorSet")
        with self._patch_theme(False):
            tray._wndproc(0, WM_SETTINGCHANGE, 0, ctypes.addressof(buf))
        self.assertEqual(tray._active_icon_path, str(self.base))
        self.assertEqual(tray._hicon, 0)
        self.assertFalse(tray._hicon_is_shared)

    @unittest.skipUnless(os.name == "nt", "装载腿=真实 LoadImageW/DestroyIcon 换柄")
    def test_theme_flip_forth_and_back_swaps_handles_and_releases_the_old(self) -> None:
        import src.runtime.tray_manager as tm

        tray = self._tray()
        dark_buf = ctypes.create_unicode_buffer("ImmersiveColorSet")
        with self._patch_theme(False):
            tray._wndproc(0, WM_SETTINGCHANGE, 0, ctypes.addressof(dark_buf))
        dark_handle = tray._hicon
        real_destroy = tm.user32.DestroyIcon
        light_buf = ctypes.create_unicode_buffer("ImmersiveColorSet")
        with self._patch_theme(True), mock.patch.object(
            tm.user32, "DestroyIcon", side_effect=lambda h: real_destroy(h)
        ) as destroyed:
            tray._wndproc(0, WM_SETTINGCHANGE, 0, ctypes.addressof(light_buf))
        self.assertEqual(tray._active_icon_path, str(self.base))
        self.assertTrue(tray._hicon)
        self.assertNotEqual(tray._hicon, dark_handle)
        self.assertIn(dark_handle, [call.args[0] for call in destroyed.call_args_list])
        tm.user32.DestroyIcon(tray._hicon)
        tray._hicon = 0

    # --- 源钉 ----------------------------------------------------------------

    def test_theme_adaptation_constants_and_branch_source_pins(self) -> None:
        self.assertEqual(WM_SETTINGCHANGE, 0x001A)
        self.assertEqual(THEME_REG_VALUE, "AppsUseLightTheme")
        self.assertEqual(IMMERSIVE_COLOR_SET, "ImmersiveColorSet")
        self.assertEqual(DARK_TASKBAR_VARIANT_SUFFIX, "-darktaskbar")
        source = (ROOT / "src" / "runtime" / "tray_manager.py").read_text(encoding="utf-8")
        self.assertIn('THEME_REG_SUBKEY = r"Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize"', source)
        wndproc_idx = source.index("def _wndproc(")
        branch_idx = source.index("if message == WM_SETTINGCHANGE:", wndproc_idx)
        branch = source[branch_idx:source.index("if message == WM_CLOSE", branch_idx)]
        self.assertIn("self._is_theme_change(lparam)", branch)
        self.assertIn("self._refresh_theme_icon()", branch)

    def test_taskbar_created_reselects_the_variant_before_remount_source_pin(self) -> None:
        source = (ROOT / "src" / "runtime" / "tray_manager.py").read_text(encoding="utf-8")
        idx = source.index('getattr(self, "_wm_taskbar_created", None)')
        segment = source[idx:source.index("if message == WM_SETTINGCHANGE", idx)]
        self.assertLess(segment.index("NIM_DELETE"), segment.index("self._refresh_theme_icon()"))
        self.assertLess(segment.index("self._refresh_theme_icon()"), segment.index("self._add_icon()"))

    @unittest.skipUnless(os.name == "nt", "收口腿打桩真实 user32.PostThreadMessageW")
    def test_stop_posts_wm_quit_through_user32_call_pin(self) -> None:
        # 活体抓出的真雷：PostThreadMessageW 在 user32（kernel32 无此函数，
        # 旧代码对真托盘线程收口必 AttributeError——桩测试全绿掩盖了它）。
        import src.runtime.tray_manager as tm

        tray = TrayIcon()
        thread = threading.Thread(target=lambda: None, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        tray._thread = thread
        tray._thread_id = 12345
        with mock.patch.object(tm.user32, "PostThreadMessageW", return_value=1) as posted:
            tray.stop()
        self.assertEqual(posted.call_args.args, (12345, tm.WM_QUIT, 0, 0))
        self.assertIsNone(tray._thread)

    def test_win32_entry_points_declare_64bit_safe_argtypes_source_pin(self) -> None:
        # 同一活体抓出的相邻缺陷：DefWindowProcW 不声明 argtypes 时 lParam 按
        # 32 位 c_int 收，凡携指针的广播（WM_GETMINMAXINFO/SettingChange 族）
        # 在兜底处 OverflowError 且回调异常被吞。
        source = (ROOT / "src" / "runtime" / "tray_manager.py").read_text(encoding="utf-8")
        self.assertIn("user32.PostThreadMessageW", source)
        self.assertNotIn("kernel32.PostThreadMessageW", source)
        self.assertIn("DefWindowProcW.argtypes", source)


class ExternalUrlClosedSetTests(unittest.TestCase):
    def test_http_and_https_only(self) -> None:
        self.assertTrue(is_external_http_url("https://www.fudan.edu.cn/"))
        self.assertFalse(is_external_http_url("javascript:alert(1)"))
        self.assertFalse(is_external_http_url("file:///C:/Windows"))
        self.assertFalse(is_external_http_url("data:text/html,x"))
        self.assertFalse(is_external_http_url("not a url"))

    def test_same_origin_is_not_external(self) -> None:
        self.assertFalse(is_external_http_url("http://127.0.0.1:6268/api/x", origin="http://127.0.0.1:6268"))
        self.assertTrue(is_external_http_url("http://127.0.0.1:9999/", origin="http://127.0.0.1:6268"))
        self.assertTrue(is_external_http_url("https://127.0.0.1:6268/", origin="http://127.0.0.1:6268"))


class WindowClosePolicyTests(unittest.TestCase):
    """关窗决策全矩阵（TRAY-FIX-1 后）：看门狗放行 > 退出请求放行 >
    默认收托盘 > 显式退出链（在飞确认）> 落盘放行。"""

    def _policy(self, *, exit_on_close=False, loaded=True, exit_requested=False,
                snapshot=ActiveTaskSnapshot(unknown=False, active=0), confirm=True,
                tray_available=True) -> tuple[WindowClosePolicy, dict]:
        record: dict = {"saved": 0, "hidden": 0, "confirm_count": 0}

        def on_hide_to_tray() -> bool:
            if not tray_available:
                return False
            record["hidden"] += 1
            return True

        def confirm_exit(count: int) -> bool:
            record["confirm_count"] += 1
            record["confirm_count_value"] = count
            return confirm

        policy = WindowClosePolicy(
            exit_on_close=lambda: exit_on_close,
            is_loaded=lambda: loaded,
            is_exit_requested=lambda: exit_requested,
            active_tasks=lambda: snapshot,
            confirm_exit=confirm_exit,
            on_hide_to_tray=on_hide_to_tray,
            on_save_geometry=lambda: record.__setitem__("saved", record["saved"] + 1),
        )
        return policy, record

    def test_unloaded_window_never_intercepts(self) -> None:
        # 看门狗销毁路径：即使托盘在位、任务在飞，也必须放行回浏览器。
        policy, record = self._policy(loaded=False,
                                      snapshot=ActiveTaskSnapshot(unknown=False, active=3))
        self.assertIsNone(policy.on_closing())
        self.assertEqual(record, {"saved": 0, "hidden": 0, "confirm_count": 0})

    def test_exit_request_path_allows_close_without_dialog(self) -> None:
        policy, record = self._policy(
            exit_requested=True,
            snapshot=ActiveTaskSnapshot(unknown=False, active=2),
        )
        self.assertIsNone(policy.on_closing())
        self.assertEqual(record["confirm_count"], 0)

    def test_default_close_hides_to_tray_and_cancels(self) -> None:
        # TRAY-FIX-1 核心语义：默认（未勾「关闭时退出」）点 × =收进托盘；
        # 不走退出链、不问在飞确认（任务在托盘后面照常跑）。
        policy, record = self._policy(
            snapshot=ActiveTaskSnapshot(unknown=False, active=2),
        )
        self.assertFalse(policy.on_closing())
        self.assertEqual(record["hidden"], 1)
        self.assertEqual(record["confirm_count"], 0)
        self.assertEqual(record["saved"], 0)

    def test_tray_unavailable_falls_through_to_normal_close_chain(self) -> None:
        # 托盘起不来就不隐藏：绝不能让学生失去窗口入口（回落普通关窗链）。
        policy, record = self._policy(tray_available=False)
        self.assertIsNone(policy.on_closing())
        self.assertEqual(record["hidden"], 0)
        self.assertEqual(record["saved"], 1)

    def test_tray_unavailable_with_active_tasks_still_confirms(self) -> None:
        policy, record = self._policy(
            tray_available=False,
            snapshot=ActiveTaskSnapshot(unknown=False, active=2),
        )
        self.assertIsNone(policy.on_closing())
        self.assertEqual(record["confirm_count"], 1)
        self.assertEqual(record["confirm_count_value"], 2)

    def test_exit_on_close_opt_in_uses_the_old_close_chain(self) -> None:
        # 勾了「关闭窗口时退出」=显式退出语义：在飞确认 + 落盘 + 放行。
        policy, record = self._policy(
            exit_on_close=True,
            snapshot=ActiveTaskSnapshot(unknown=False, active=2),
        )
        self.assertIsNone(policy.on_closing())
        self.assertEqual(record["hidden"], 0)
        self.assertEqual(record["confirm_count"], 1)
        self.assertEqual(record["saved"], 1)

    def test_exit_on_close_with_declined_confirmation_cancels_close(self) -> None:
        policy, record = self._policy(
            exit_on_close=True,
            snapshot=ActiveTaskSnapshot(unknown=False, active=2), confirm=False,
        )
        self.assertFalse(policy.on_closing())
        self.assertEqual(record["saved"], 0)

    def test_exit_on_close_zero_tasks_closes_without_dialog(self) -> None:
        policy, record = self._policy(exit_on_close=True)
        self.assertIsNone(policy.on_closing())
        self.assertEqual(record["confirm_count"], 0)
        self.assertEqual(record["saved"], 1)

    def test_unknown_task_state_skips_the_dialog(self) -> None:
        policy, record = self._policy(exit_on_close=True,
                                      snapshot=ActiveTaskSnapshot(unknown=True))
        self.assertIsNone(policy.on_closing())
        self.assertEqual(record["confirm_count"], 0)

    def test_zero_confirm_fast_path_never_opens_a_dialog(self) -> None:
        from src.runtime.window_shell import confirm_exit_with_active_tasks

        self.assertTrue(confirm_exit_with_active_tasks(0))

    def test_confirm_copy_ends_with_a_yesno_answerable_question(self) -> None:
        """文案走查钉（N15-W1）：按钮是系统「是/否」，正文结尾必须是能被
        是/否直接回答的问句；任务数占位与安全收尾承诺在场。"""
        import inspect

        from src.runtime.window_shell import confirm_exit_with_active_tasks

        source = inspect.getsource(confirm_exit_with_active_tasks)
        self.assertIn("现在就要关窗吗？", source)
        self.assertIn("{count} 个任务没跑完", source)
        self.assertIn("安全收尾", source)

    def test_tray_exit_confirm_copy_and_fast_path(self) -> None:
        """TRAY-FIX-1：托盘「退出」的在飞确认与关窗确认同族同纪律。"""
        import inspect

        from src.runtime.window_shell import confirm_tray_exit_with_active_tasks

        self.assertTrue(confirm_tray_exit_with_active_tasks(0))
        source = inspect.getsource(confirm_tray_exit_with_active_tasks)
        self.assertIn("现在就要退出吗？", source)
        self.assertIn("{count} 个任务没跑完", source)
        self.assertIn("安全收尾", source)


class NativeShellApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cl-n10a-api-")
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self.api = NativeShellApi(self.data_dir, app_origin="http://127.0.0.1:6268")

    def test_open_external_rejects_the_closed_set_before_touching_a_browser(self) -> None:
        with mock.patch("src.runtime.window_shell.webbrowser.open") as opener:
            result = self.api.open_external("javascript:alert(1)")
        self.assertFalse(result["ok"])
        opener.assert_not_called()

    def test_open_external_routes_http_links_to_the_system_browser(self) -> None:
        with mock.patch("src.runtime.window_shell.webbrowser.open", return_value=True) as opener:
            result = self.api.open_external("https://www.fudan.edu.cn/")
        self.assertTrue(result["ok"])
        opener.assert_called_once_with("https://www.fudan.edu.cn/")

    def test_exit_on_close_updates_state_and_persists(self) -> None:
        # TRAY-FIX-1：开关只落盘，不再有托盘回调（托盘 shown 即挂、恒在）。
        self.api._bind(lambda: 0)
        result = self.api.set_exit_on_close(True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["exit_on_close"])
        self.assertTrue(self.api.exit_on_close)
        self.assertTrue(load_window_state(self.data_dir)["exit_on_close"])
        result = self.api.set_exit_on_close(False)
        self.assertEqual(result["exit_on_close"], False)

    def test_window_features_shape_is_stable(self) -> None:
        features = self.api.get_window_features()
        self.assertTrue(features["native_window"])
        self.assertEqual(features["features_version"], 2)
        self.assertFalse(features["exit_on_close"])
        self.assertIn("webview_cache_bytes", features)
        self.assertTrue(features["can_clear_webview_cache"])

    def test_cache_clear_request_writes_the_marker(self) -> None:
        result = self.api.request_webview_cache_clear()
        self.assertTrue(result["ok"])
        self.assertTrue(result["applies_on_next_start"])
        self.assertTrue(webview_cache_clear_marker_path(self.data_dir).exists())

    def test_titlebar_theme_requires_a_handle(self) -> None:
        self.assertFalse(self.api.set_titlebar_theme(True)["ok"])
        self.assertFalse(apply_titlebar_theme(0, dark=True))


class GeometryReaderTests(unittest.TestCase):
    def test_maximized_window_uses_restore_bounds(self) -> None:
        form = SimpleNamespace(
            WindowState=2,
            Bounds=SimpleNamespace(X=0, Y=0, Width=1920, Height=1040),
            RestoreBounds=SimpleNamespace(X=100, Y=50, Width=1280, Height=800),
        )
        geometry = read_window_geometry(SimpleNamespace(native=form))
        self.assertEqual(geometry, {"x": 100, "y": 50, "width": 1280, "height": 800, "maximized": True})

    def test_normal_window_uses_current_bounds(self) -> None:
        form = SimpleNamespace(
            WindowState=0,
            Bounds=SimpleNamespace(X=8, Y=9, Width=1100, Height=700),
            RestoreBounds=None,
        )
        self.assertEqual(
            read_window_geometry(SimpleNamespace(native=form)),
            {"x": 8, "y": 9, "width": 1100, "height": 700, "maximized": False},
        )

    def test_missing_native_is_skipped(self) -> None:
        self.assertIsNone(read_window_geometry(SimpleNamespace()))
        self.assertIsNone(read_window_geometry(SimpleNamespace(native=SimpleNamespace(WindowState="junk"))))


class FocusRunningInstanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cl-n10a-focus-")
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def test_owner_pid_and_exact_title_are_required(self) -> None:
        (self.data_dir / "server-instance.json").write_text(
            json.dumps({"pid": 4242, "port": 6268}), encoding="utf-8"
        )
        windows = [
            (101, 4242, "CourseLens", True),      # 命中
            (102, 999, "CourseLens", True),       # 标题同 pid 异——绝不动
            (103, 4242, "CourseLens - 编辑", True),  # 前缀相近——绝不动
        ]
        with mock.patch("src.runtime.window_shell._flash_and_foreground", return_value=True) as flash:
            self.assertTrue(focus_running_instance_window(self.data_dir, enumerate_windows=lambda: windows))
        flash.assert_called_once_with(101)

    def test_missing_evidence_is_a_clean_no(self) -> None:
        self.assertFalse(focus_running_instance_window(self.data_dir))
        (self.data_dir / "server-instance.json").write_text("{}", encoding="utf-8")
        self.assertFalse(focus_running_instance_window(self.data_dir))
        (self.data_dir / "server-instance.json").write_text("not json", encoding="utf-8")
        self.assertFalse(focus_running_instance_window(self.data_dir))

    def test_no_matching_window_is_a_clean_no(self) -> None:
        (self.data_dir / "server-instance.json").write_text(
            json.dumps({"pid": 4242}), encoding="utf-8"
        )
        with mock.patch("src.runtime.window_shell._flash_and_foreground") as flash:
            self.assertFalse(focus_running_instance_window(self.data_dir, enumerate_windows=lambda: []))
        flash.assert_not_called()


if __name__ == "__main__":
    unittest.main()
