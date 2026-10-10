"""MAC-NIGHT-1 平台托盘/聚焦层钉面（零真机：AppKit 全桩注入）。

三份合同：
- NSStatusItem 托盘形状与 Windows TrayIcon 对齐（start 幂等/running 诚实/
  tooltip 127 上限/stop 收口/通知缺席=False），挂载序列逐形状钉死；
- focus_owner_process 平台守卫（非 macOS=诚实 False，绝不假激活）；
- macOS 入口（courselens_macos.py）钉面：ALLOW_DOWNLOADS 放行先于
  src.app 导入（MAC-2 R1 防回退钉）、数据目录 env 接线、不强行挂托盘；
- window_shell.focus_running_instance_window 的 darwin 分发：同一证据
  文件、同一身份闭集，激活交 src.platform.tray。
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from src.platform.tray import (
    MACOS_TRAY_BACKEND,
    MacOSStatusItemTray,
    focus_owner_process,
    status_item_script_pins,
)

ROOT = Path(__file__).resolve().parents[1]


class _FakeButton:
    def __init__(self) -> None:
        self.tooltips: list[str] = []

    def setToolTip_(self, text) -> None:
        self.tooltips.append(str(text))


class _FakeMenuItem:
    def __init__(self) -> None:
        self.title = ""
        self.action = ""
        self.target = None

    def setTitle_(self, title) -> None:
        self.title = str(title)

    def setTarget_(self, target) -> None:
        self.target = target

    def setAction_(self, action) -> None:
        self.action = str(action)

    @classmethod
    def new(cls) -> "_FakeMenuItem":
        return cls()

    @staticmethod
    def separatorItem():  # noqa: N802
        return "separator"


class _FakeMenu:
    def __init__(self) -> None:
        self.items: list = []

    def addItem_(self, item) -> None:
        self.items.append(item)


class _FakeStatusBar:
    def __init__(self, fake) -> None:
        self.fake = fake

    def statusItemWithLength_(self, length) -> None:
        self.fake.lengths.append(length)
        return self.fake.item

    def removeStatusItem_(self, item) -> None:
        self.fake.removed.append(item)


class _FakeAppKit:
    """形状合同桩：只承载挂载序列需要的面。"""

    NSVariableStatusItemLength = -1.0

    def __init__(self) -> None:
        self.lengths: list = []
        self.removed: list = []
        self.button = _FakeButton()
        self.item = types.SimpleNamespace(button=lambda: self.button, setMenu_=None)
        self.item.setMenu_ = lambda menu: self.menus.append(menu)
        self.menus: list = []
        self.NSStatusBar = types.SimpleNamespace(
            systemStatusBar=lambda: _FakeStatusBar(self)
        )
        self.NSMenu = types.SimpleNamespace(new=lambda: _FakeMenu())
        self.NSMenuItem = _FakeMenuItem

    @property
    def Foundation(self):  # 入口托盘处理器的命名空间探测面
        raise AttributeError("stub has no NSObject; menu target stays None")


def _tray(appkit: _FakeAppKit) -> MacOSStatusItemTray:
    return MacOSStatusItemTray(appkit=appkit)


class MacOSStatusItemTrayTests(unittest.TestCase):
    def test_start_mounts_status_item_with_two_fixed_items(self) -> None:
        appkit = _FakeAppKit()
        tray = _tray(appkit)
        self.assertTrue(tray.available)
        self.assertFalse(tray.running)
        self.assertTrue(tray.start(None, on_open=lambda: None, on_exit=lambda: None))
        self.assertTrue(tray.running)
        self.assertEqual(appkit.lengths, [appkit.NSVariableStatusItemLength])
        self.assertEqual(len(appkit.menus), 1)
        entries = appkit.menus[0].items
        titles = [entry if isinstance(entry, str) else entry.title for entry in entries]
        self.assertEqual(titles, ["打开 CourseLens", "separator", "退出 CourseLens"])
        actions = {
            entry.action for entry in entries if not isinstance(entry, str)
        }
        self.assertEqual(actions, {"courselensOpen:", "courselensExit:"})

    def test_start_is_idempotent(self) -> None:
        appkit = _FakeAppKit()
        tray = _tray(appkit)
        self.assertTrue(tray.start(None, on_open=lambda: None, on_exit=lambda: None))
        self.assertTrue(tray.start(None, on_open=lambda: None, on_exit=lambda: None))
        self.assertEqual(len(appkit.lengths), 1)

    def test_tooltip_is_capped_at_127_chars(self) -> None:
        appkit = _FakeAppKit()
        tray = _tray(appkit)
        tray.start(None, on_open=lambda: None, on_exit=lambda: None)
        button = appkit.item.button()
        tray.set_tooltip("长" * 300)
        self.assertEqual(button.tooltips[-1], "长" * 127)

    def test_set_tooltip_without_running_is_noop(self) -> None:
        tray = _tray(_FakeAppKit())
        self.assertFalse(tray.set_tooltip("x"))

    def test_stop_removes_the_status_item(self) -> None:
        appkit = _FakeAppKit()
        tray = _tray(appkit)
        tray.start(None, on_open=lambda: None, on_exit=lambda: None)
        tray.stop()
        self.assertEqual(appkit.removed, [appkit.item])
        self.assertFalse(tray.running)
        tray.stop()  # 未持有时是安全 no-op
        self.assertEqual(len(appkit.removed), 1)

    def test_unavailable_appkit_start_returns_false_honestly(self) -> None:
        tray = MacOSStatusItemTray(appkit=None)
        with mock.patch("src.platform.tray._load_appkit", return_value=None):
            self.assertFalse(tray.available)
            self.assertFalse(tray.start(None, on_open=lambda: None, on_exit=lambda: None))
        self.assertFalse(tray.running)

    def test_notify_without_notification_center_returns_false(self) -> None:
        appkit = _FakeAppKit()
        appkit.NSUserNotification = types.SimpleNamespace(new=lambda: "notification")
        appkit.NSUserNotificationCenter = types.SimpleNamespace(
            defaultUserNotificationCenter=lambda: None  # 未配通知中心（真实 mac 常态）
        )
        tray = _tray(appkit)
        self.assertFalse(tray.notify("标题", "内容"))

    def test_script_mount_sequence_pins(self) -> None:
        source = (ROOT / "src" / "platform" / "tray.py").read_text(encoding="utf-8")
        pins = status_item_script_pins(source)
        self.assertTrue(all(pins.values()), pins)
        self.assertIn(MACOS_TRAY_BACKEND, source)


class FocusOwnerProcessTests(unittest.TestCase):
    def test_non_macos_platform_returns_false(self) -> None:
        for platform in ("windows", "other"):
            with mock.patch("src.platform.tray.current_platform", return_value=platform):
                self.assertFalse(focus_owner_process(1234))

    def test_invalid_pid_returns_false(self) -> None:
        self.assertFalse(focus_owner_process(0))
        self.assertFalse(focus_owner_process(-5))


class WindowShellDarwinDispatchTests(unittest.TestCase):
    """二次实例聚焦的 darwin 分发：同一证据文件，激活交平台层。"""

    def test_darwin_dispatch_reads_owner_pid_and_calls_platform_focus(self) -> None:
        from src.runtime.window_shell import focus_running_instance_window

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "server-instance.json").write_text(
                json.dumps({"schema": "courselens.instance.v2", "pid": 4321}),
                encoding="utf-8",
            )
            with mock.patch("sys.platform", "darwin"), mock.patch(
                "src.platform.tray.focus_owner_process", return_value=True
            ) as focus:
                self.assertTrue(focus_running_instance_window(root))
            self.assertEqual(focus.call_args.args, (4321,))

    def test_darwin_dispatch_returns_false_on_unreadable_evidence(self) -> None:
        from src.runtime.window_shell import focus_running_instance_window

        with tempfile.TemporaryDirectory() as folder:
            with mock.patch("sys.platform", "darwin"), mock.patch(
                "src.platform.tray.focus_owner_process", return_value=True
            ) as focus:
                self.assertFalse(focus_running_instance_window(Path(folder) / "no-such"))
            focus.assert_not_called()

    def test_windows_dispatch_unchanged_on_windows_host(self) -> None:
        # Windows 宿主零变化钉：非 darwin 分支照旧走 EnumWindows 路径。
        if sys.platform != "win32":
            self.skipTest("Windows 宿主钉面")
        from src.runtime.window_shell import focus_running_instance_window

        with tempfile.TemporaryDirectory() as folder:
            with mock.patch(
                "src.runtime.window_shell._enumerate_top_level_windows",
                return_value=[],
            ):
                self.assertFalse(focus_running_instance_window(Path(folder)))


class MacosEntryPinTests(unittest.TestCase):
    """MAC-2 R1 防回退钉 + 入口接线顺序钉（源钉，双平台可跑）。"""

    def _entry_source(self) -> str:
        return (ROOT / "courselens_macos.py").read_text(encoding="utf-8")

    def test_allow_downloads_is_set_true_before_app_import(self) -> None:
        source = self._entry_source()
        self.assertIn('webview.settings["ALLOW_DOWNLOADS"] = True', source)
        # 顺序钉：放行必须发生在 src.app 导入之前（pywebview 的 settings
        # 在 webview.start 时读取；导入 src.app 会连带其窗口流程）。
        self.assertLess(
            source.index('webview.settings["ALLOW_DOWNLOADS"] = True'),
            source.index("from src.app import main as app_main"),
        )

    def test_allow_downloads_is_the_only_webview_side_effect_before_app(self) -> None:
        # R1 防回退钉的两个真实不变量：①放行值=True 且先于 src.app 导入；
        # ②入口代码对 webview 的动作只有这一处设置（零额外窗口/生命周期面
        # ——那些全部属于 src.app 的既有流程）。
        source = self._entry_source()
        code = source.split('"""', 2)[2]  # 去掉模块 docstring 后的纯代码面
        self.assertIn('webview.settings["ALLOW_DOWNLOADS"] = True', code)
        self.assertLess(
            code.index('webview.settings["ALLOW_DOWNLOADS"] = True'),
            code.index("from src.app import main as app_main"),
        )
        self.assertEqual(code.count("import webview"), 1)
        self.assertEqual(code.count("webview."), 1)

    def test_data_dir_env_wiring_keeps_explicit_override_first(self) -> None:
        source = self._entry_source()
        self.assertLess(
            source.index('os.environ.get("COURSELENS_DATA_DIR", "").strip()'),
            source.index("from src.platform.paths import data_dir as _platform_data_dir"),
        )

    def test_entry_runs_real_app_main_with_passthrough_argv(self) -> None:
        source = self._entry_source()
        self.assertIn("return app_main(argv)", source)

    def test_pywebview_settings_contract(self) -> None:
        # 依赖合同钉：本机 pywebview 必须暴露可就地覆写的 ALLOW_DOWNLOADS。
        try:
            import webview
        except Exception:
            self.skipTest("pywebview 未安装（打包/CI 环境按需验证）")
        self.assertIn("ALLOW_DOWNLOADS", webview.settings)
        original = webview.settings["ALLOW_DOWNLOADS"]
        try:
            webview.settings["ALLOW_DOWNLOADS"] = True
            self.assertTrue(webview.settings["ALLOW_DOWNLOADS"])
        finally:
            webview.settings["ALLOW_DOWNLOADS"] = original


if __name__ == "__main__":
    unittest.main()
