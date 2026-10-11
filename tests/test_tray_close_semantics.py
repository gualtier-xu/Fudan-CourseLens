"""TRAY-FIX-1 语义钉：关窗=收托盘、托盘图标生命周期与主窗口解耦、退出=真退场。

三面合围：
1. 行为钉——真 TrayIcon（注入假 user32/shell32）+ 真 WindowClosePolicy +
   假窗口：点 × → hide 被调、关窗被取消、Shell_NotifyIcon 全程只有 NIM_ADD
   没有 NIM_DELETE（托盘仍在位）；stop() 收口时才 NIM_DELETE（假窗+调用断言）。
2. 源钉——app.py 接线：_hide_to_tray 走 ensure+hide；_tray_exit 先确认再
   exit_requested+destroy；tray.stop 全文件恰一次（只在收口 finally）。
3. 形状钉——托盘宿主窗是独立顶层窗（非 message-only、非主窗句柄复用），
   SYSTRAY 家族既有断言（test_tray_struct_shapes.py）继续全绿。
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.runtime.tray_manager as tray_manager
from src.runtime.tray_manager import NIM_ADD, NIM_DELETE, NIF_ICON, NIF_MESSAGE, NIF_TIP, TrayIcon
from src.runtime.window_shell import ActiveTaskSnapshot, WindowClosePolicy


class _FakeWindow:
    def __init__(self) -> None:
        self.hidden = 0

    def hide(self) -> None:
        self.hidden += 1


def _flags_of(carg_arg) -> int:
    """Shell_NotifyIconW 的第二个实参是 byref 的 CArgObject：取回结构体 uFlags。"""
    return carg_arg._obj.uFlags


class TrayCloseSemanticsTests(unittest.TestCase):
    """关窗→hide→托盘在位（假窗 + Shell_NotifyIcon 调用断言）。"""

    def test_close_hides_window_and_leaves_the_tray_mounted(self) -> None:
        shell_calls: list[tuple[int, object]] = []

        def _record(msg, data):
            shell_calls.append((msg, data))
            return 1

        shell32 = mock.MagicMock()
        shell32.Shell_NotifyIconW.side_effect = _record
        user32 = mock.MagicMock()
        user32.RegisterClassW.return_value = 1
        user32.CreateWindowExW.return_value = 0x1234
        user32.RegisterWindowMessageW.return_value = 0xC000
        user32.GetSystemMetrics.return_value = 0
        user32.LoadIconW.return_value = 55
        kernel32 = mock.MagicMock()
        kernel32.GetCurrentThreadId.return_value = 42
        # GetModuleHandleW 的返回值要塞进 WNDCLASSW.hInstance（c_void_p 字段）：
        # 必须是真整数，MagicMock 会在赋值处抛 "cannot be converted to pointer"。
        kernel32.GetModuleHandleW.return_value = 0x00400000

        entered = threading.Event()

        def _get_message(_lp, _h, _a, _b) -> int:
            # 托盘线程停在消息循环里（模拟图标挂上后的常驻）；stop 置位
            # _stopped 后返回 0（=收到 WM_QUIT），复刻真收口路径。
            entered.set()
            return 0 if tray._stopped.wait(0.2) else 1

        user32.GetMessageW.side_effect = _get_message

        tray = TrayIcon()
        with mock.patch.object(tray_manager, "user32", user32), \
                mock.patch.object(tray_manager, "shell32", shell32), \
                mock.patch.object(tray_manager, "kernel32", kernel32):
            self.assertTrue(tray.available)
            self.assertTrue(
                tray.start(None, on_open=lambda: None, on_exit=lambda: None),
                "托盘 start 必须成功（图标挂上）",
            )
            self.assertTrue(tray.running)
            self.assertTrue(entered.wait(5.0), "托盘线程必须进消息循环")
            adds = [c for c in shell_calls if c[0] == NIM_ADD]
            self.assertEqual(len(adds), 1, "start 恰好 NIM_ADD 一次")
            # NIF_MESSAGE|NIF_ICON|NIF_TIP 全带（回调/图标/提示三件套）。
            self.assertEqual(_flags_of(adds[0][1]), NIF_MESSAGE | NIF_ICON | NIF_TIP)
            self.assertNotIn(
                NIM_DELETE, [c[0] for c in shell_calls],
                "托盘在位期绝不允许 NIM_DELETE",
            )

            # 真关窗决策链：默认语义（exit_on_close=False）→ 假窗被 hide、
            # 关窗被取消、托盘 still mounted。
            fake_window = _FakeWindow()

            def on_hide_to_tray() -> bool:
                # 与 app.py _hide_to_tray 同形：ensure_tray→hide。
                if not tray.running:
                    return False
                fake_window.hide()
                return True

            policy = WindowClosePolicy(
                exit_on_close=lambda: False,
                is_loaded=lambda: True,
                is_exit_requested=lambda: False,
                active_tasks=lambda: ActiveTaskSnapshot(unknown=False, active=0),
                on_hide_to_tray=on_hide_to_tray,
                on_save_geometry=lambda: None,
            )
            self.assertIs(policy.on_closing(), False, "关窗必须被取消（收进托盘）")
            self.assertEqual(fake_window.hidden, 1)
            self.assertNotIn(
                NIM_DELETE, [c[0] for c in shell_calls],
                "关窗收托盘后托盘必须原封不动（TRAY-FIX-1 核心）",
            )

            # 退出收口：stop → 消息循环退出 → NIM_DELETE 恰一次（诚实退场）。
            tray.stop()
            deletes = [c for c in shell_calls if c[0] == NIM_DELETE]
            self.assertEqual(len(deletes), 1, "stop 收口恰好 NIM_DELETE 一次")
            self.assertFalse(tray.running)


class TrayExitWiringSourcePins(unittest.TestCase):
    """app.py 接线源钉：退出语义诚实、托盘生命周期只归收口 finally。"""

    def _fn_body(self, source: str, name: str) -> str:
        fn_idx = source.index(f"def {name}(")
        return source[fn_idx:source.index("\n        def ", fn_idx)]

    def test_hide_to_tray_ensures_then_hides(self) -> None:
        source = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
        body = self._fn_body(source, "_hide_to_tray")
        self.assertIn("_ensure_tray()", body, "隐藏前必须确保托盘在位")
        self.assertIn("window.hide()", body)
        self.assertIn("_tray_first_hide_hint()", body, "首次收托盘的一次性提示必须接上")

    def test_tray_exit_confirms_then_requests_exit_and_destroys(self) -> None:
        source = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
        body = self._fn_body(source, "_tray_exit")
        self.assertIn("confirm_tray_exit_with_active_tasks", body,
                      "托盘退出的在飞确认必须先问")
        self.assertIn("return", body, "学生拒绝退出时必须原样留下")
        self.assertIn("exit_requested.set()", body)
        self.assertIn("window.destroy()", body)

    def test_tray_stop_lives_only_in_the_exit_finally(self) -> None:
        source = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
        self.assertEqual(
            source.count("tray.stop()"), 1,
            "tray.stop 全文件恰一次（收口 finally）：关窗=隐藏绝不拆托盘",
        )

    def test_close_policy_wired_with_exit_on_close_and_hide(self) -> None:
        source = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
        self.assertIn("exit_on_close=lambda: shell_api.exit_on_close", source)
        self.assertIn("on_hide_to_tray=_hide_to_tray", source)
        self.assertIn('initial_exit_on_close=initial_state["exit_on_close"]', source)

    def test_first_hide_hint_is_one_shot_and_persistent(self) -> None:
        source = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
        self.assertIn('tray_hint_pending = not bool(initial_state.get("tray_hint_shown"))', source)
        self.assertIn("save_window_state(data_dir, tray_hint_shown=True)", source)
        self.assertIn("_TRAY_FIRST_HIDE_NOTIFY", source)

    def test_tooltip_carries_version(self) -> None:
        source = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
        self.assertIn("tooltip_for(snapshot, base=_tray_tooltip_base())", source)
        self.assertIn("courselens-version.json", source)


if __name__ == "__main__":
    unittest.main()
