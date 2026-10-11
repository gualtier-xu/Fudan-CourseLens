"""T13 残余（夜14-R7/夜15-R4 台账）：托盘 ctypes ABI 形状钉。

``NOTIFYICONDATAW``/``WNDCLASSW`` 是与 win32 的二进制合同：字段重排/缓冲
缩水在纯 Python 单测里不可见（直到真机上 Explorer 静默丢托盘）。本件把
结构体形状按 win32 常量钉死（R7 定级「低值」——作为回归基线一次钉平）。
真托盘行为面（菜单/主题刷新）归 test_window_shell.py 既有钉，不在此重复。
"""

from __future__ import annotations

import ctypes
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.runtime import tray_manager


@unittest.skipUnless(os.name == "nt", "win32 tray ABI shape pins require Windows")
class NotifyIconDataShapeTests(unittest.TestCase):
    def test_field_order_matches_the_win32_abi(self):
        self.assertEqual(
            [name for name, _ in tray_manager.NOTIFYICONDATAW._fields_],
            [
                "cbSize", "hWnd", "uID", "uFlags", "uCallbackMessage", "hIcon",
                "szTip", "dwState", "dwStateMask", "szInfo", "uVersion",
                "szInfoTitle", "dwInfoFlags", "guidItem", "hBalloonIcon",
            ],
        )

    def test_text_buffers_keep_the_documented_capacities(self):
        self.assertEqual(tray_manager.NOTIFYICONDATAW.szTip.size, 256)
        self.assertEqual(tray_manager.NOTIFYICONDATAW.szInfo.size, 512)
        self.assertEqual(tray_manager.NOTIFYICONDATAW.szInfoTitle.size, 128)
        self.assertEqual(tray_manager.NOTIFYICONDATAW.guidItem.size, 16)

    def test_struct_size_is_a_stable_abi_baseline(self):
        # x64 实测基线（2026-10-07，含 V2 hBalloonIcon 尾字段）；字段增删/
        # 重排即红。与 Shell_NotifyIconW 的 NOTIFYICONDATAW 布局一致。
        self.assertEqual(ctypes.sizeof(tray_manager.NOTIFYICONDATAW), 976)

    def test_tooltip_flags_constants_are_the_win32_values(self):
        self.assertEqual(tray_manager.NIF_TIP, 0x4)
        self.assertEqual(tray_manager.NIF_INFO, 0x10)
        self.assertEqual(tray_manager.NIIF_INFO, 0x1)
        self.assertEqual(tray_manager.NIIF_ERROR, 0x3)
        self.assertEqual(tray_manager.NIN_BALLOONUSERCLICK, 0x405)


@unittest.skipUnless(os.name == "nt", "win32 tray ABI shape pins require Windows")
class WindowClassShapeTests(unittest.TestCase):
    def test_wndproc_signature_is_lresult_callback_hwnd_uint_wparam_lparam(self):
        # ctypes 按签名生成互异的 WINFUNCTYPE 子类：恰配签名的回调可赋入
        # 字段，任何签名漂移（增删参/换宽度）都会 TypeError——即 ABI 钉本体。
        wndproc = ctypes.WINFUNCTYPE(
            ctypes.c_longlong, ctypes.c_void_p, ctypes.c_uint,
            ctypes.c_size_t, ctypes.c_longlong,
        )(lambda hwnd, msg, wparam, lparam: 0)
        instance = tray_manager.WNDCLASSW()
        instance.lpfnWndProc = wndproc
        self.assertEqual(wndproc.restype, ctypes.c_longlong)
        wrong = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_void_p)(lambda hwnd: 0)
        with self.assertRaises(TypeError):
            instance.lpfnWndProc = wrong

    def test_class_name_and_menu_name_are_wide_strings(self):
        self.assertIn("c_wchar_p", str(tray_manager.WNDCLASSW.lpszClassName))
        self.assertIn("c_wchar_p", str(tray_manager.WNDCLASSW.lpszMenuName))
        instance = tray_manager.WNDCLASSW()
        instance.lpszClassName = "CourseLensTrayHost"
        self.assertEqual(instance.lpszClassName, "CourseLensTrayHost")

    def test_window_class_size_is_a_stable_abi_baseline(self):
        self.assertEqual(ctypes.sizeof(tray_manager.WNDCLASSW), 72)

    def test_theme_probe_registry_coordinates_are_pinned(self):
        self.assertEqual(
            tray_manager.THEME_REG_SUBKEY,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        self.assertEqual(tray_manager.THEME_REG_VALUE, "AppsUseLightTheme")


if __name__ == "__main__":
    unittest.main()
