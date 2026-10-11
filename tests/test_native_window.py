# P68（第六十八案）NATIVE-WINDOW-1 钉面：受管安装的产品形态是「真正应用程序」
# ——pywebview 原生壳窗口（标题/任务栏图标=CourseLens，关窗=退出应用），不再
# 借用浏览器标签页。窗口是薄壳：只承载既有 frontend URL，零业务逻辑入窗；本地
# 服务架构零变化。本文件钉决策面与接线面（假 webview 模块，套件内零真实 GUI）：
# CLI 旗标、受管默认、关窗→registry 显式 close 快路径（工作保护/宽限语义不变）、
# 浏览器降级面、serve 源序（ready→信号→窗口循环→join）。真机活体归执行包 U4。
from __future__ import annotations

import os
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.app import (
    _NATIVE_WINDOW_HEIGHT,
    _NATIVE_WINDOW_TITLE,
    _NATIVE_WINDOW_WIDTH,
    _open_native_window,
    _resolve_native_window,
    _webview2_runtime_available,
    main,
)
from src.runtime.http_api import FrontendSessionRegistry
from src.runtime.tray_manager import TrayIcon

ROOT = Path(__file__).resolve().parents[1]


class FakeEvent:
    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self, *args) -> None:
        for handler in list(self.handlers):
            handler(*args)


class FakeForm:
    """夜10-A：window.native 的桩（WinForms Form 面的最小形状）。

    记录 ResizeEnd 订阅并支持手动触发（E1 几何落盘钉）；无 Handle 属性
    → _window_handle 诚实返回 0 → 任务栏轮询不启动（零 COM）。
    """

    def __init__(self) -> None:
        self.resize_end_handlers: list = []
        self.WindowState = 0
        self.Bounds = SimpleNamespace(X=10, Y=20, Width=1000, Height=700)
        self.RestoreBounds = self.Bounds

    @property
    def Handle(self):  # 故意缺失 ToInt64：句柄解析走异常兜底 → 0
        return object()

    @property
    def ResizeEnd(self):
        return self

    def __iadd__(self, handler):
        self.resize_end_handlers.append(handler)
        return self

    def fire_resize_end(self) -> None:
        for handler in list(self.resize_end_handlers):
            handler(self, None)


class FakeWindow:
    def __init__(self) -> None:
        # 夜10-A：托盘/关窗决策接线需要 closing+shown 两个事件面。
        self.events = SimpleNamespace(
            closing=FakeEvent(), closed=FakeEvent(), loaded=FakeEvent(), shown=FakeEvent()
        )
        self._destroyed = threading.Event()
        self.native = None

    def destroy(self) -> None:
        self._destroyed.set()


class FakeWebviewModule(types.ModuleType):
    """套件内零真实 GUI：记录 create_window/start 参数，start 即模拟关窗。

    fire_closed_on_start=False 时 start() 阻塞到 destroy()（看门狗测试用），
    复刻真实 webview.start 阻塞至关窗的语义。
    """

    def __init__(self) -> None:
        super().__init__("webview")
        self.created: list[dict] = []
        self.started: list[dict] = []
        self.windows: list[FakeWindow] = []
        self.start_error: Exception | None = None
        self.fire_closed_on_start = True

    def create_window(self, title, url, **kwargs):
        window = FakeWindow()
        self.created.append({"title": title, "url": url, **kwargs})
        self.windows.append(window)
        return window

    def start(self, **kwargs):
        self.started.append(kwargs)
        if self.start_error is not None:
            raise self.start_error
        if self.fire_closed_on_start:
            for window in self.windows:
                window.events.loaded.fire()
                window.events.closed.fire()
            return
        for window in self.windows:
            window._destroyed.wait(timeout=10)


class NativeWindowDecisionTests(unittest.TestCase):
    """P68 决策面：显式旗标 > 受管默认 > 无头否决。"""

    def test_managed_install_defaults_to_window(self) -> None:
        with mock.patch.dict("os.environ", {"COURSELENS_INSTALL_ROOT": "x"}):
            self.assertTrue(_resolve_native_window(None, open_browser=True))

    def test_dev_checkout_defaults_to_browser(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(_resolve_native_window(None, open_browser=True))

    def test_explicit_flags_override_the_default(self) -> None:
        with mock.patch.dict("os.environ", {"COURSELENS_INSTALL_ROOT": "x"}):
            self.assertFalse(_resolve_native_window(False, open_browser=True))
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertTrue(_resolve_native_window(True, open_browser=True))

    def test_headless_never_opens_a_window(self) -> None:
        with mock.patch.dict("os.environ", {"COURSELENS_INSTALL_ROOT": "x"}):
            self.assertFalse(_resolve_native_window(None, open_browser=False))
            self.assertFalse(_resolve_native_window(True, open_browser=False))


class NativeWindowCliTests(unittest.TestCase):
    """CLI 旗标面：--window / --no-window / --no-open 经 main() 原样进 serve()。"""

    def _serve_kwargs(self, argv: list[str]) -> dict:
        recorded: dict = {}

        def fake_serve(**kwargs):
            recorded.update(kwargs)

        with mock.patch("src.app.serve", side_effect=fake_serve):
            self.assertEqual(main(argv), 0)
        return recorded

    def test_window_flag_reaches_serve(self) -> None:
        self.assertIs(self._serve_kwargs(["serve", "--window"])["native_window"], True)

    def test_no_window_flag_reaches_serve(self) -> None:
        self.assertIs(
            self._serve_kwargs(["serve", "--no-window"])["native_window"], False
        )

    def test_auto_is_undecided_by_default(self) -> None:
        self.assertIsNone(self._serve_kwargs(["serve"])["native_window"])

    def test_no_open_keeps_headless_semantics(self) -> None:
        kwargs = self._serve_kwargs(["serve", "--no-open"])
        self.assertFalse(kwargs["open_browser"])

    def test_unknown_argument_fails_closed(self) -> None:
        with self.assertRaises(SystemExit):
            main(["serve", "--windows"])


class NativeWindowWiringTests(unittest.TestCase):
    """接线面：窗口参数、关窗→registry 离场信号、降级路径。"""

    def setUp(self) -> None:
        self.webview = FakeWebviewModule()
        self._modules = mock.patch.dict(sys.modules, {"webview": self.webview})
        self._modules.start()
        self.addCleanup(self._modules.stop)
        self._tmp = tempfile.TemporaryDirectory(prefix="cl-p68-wiring-")
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def test_window_is_a_thin_shell_over_the_frontend_url(self) -> None:
        fallback: list[str] = []
        _open_native_window(
            "http://127.0.0.1:6268/",
            data_dir=self.data_dir,
            frontend_sessions=None,
            fallback_open=lambda: fallback.append("browser"),
        )
        self.assertEqual(fallback, [])
        self.assertEqual(len(self.webview.created), 1)
        created = self.webview.created[0]
        self.assertEqual(created["title"], _NATIVE_WINDOW_TITLE)
        self.assertEqual(_NATIVE_WINDOW_TITLE, "CourseLens")
        self.assertEqual(created["url"], "http://127.0.0.1:6268/")
        self.assertEqual(created["width"], _NATIVE_WINDOW_WIDTH)
        self.assertEqual(created["height"], _NATIVE_WINDOW_HEIGHT)
        self.assertTrue(created["resizable"])
        self.assertEqual(_NATIVE_WINDOW_WIDTH, 1280)
        self.assertEqual(_NATIVE_WINDOW_HEIGHT, 800)
        started = self.webview.started[0]
        # 用户数据目录钉在数据根内（绝不落 launcher 树，防信任遍历拒绝）；
        # private_mode=False 保主题 localStorage 持久性。
        self.assertEqual(
            started["storage_path"], str(self.data_dir / "webview")
        )
        self.assertIs(started["private_mode"], False)
        # 图标解析：开发头解析到 installer/ 下的 ico（受管运行时解析到版本
        # 槽根，见 _native_window_icon_path 的顺序）。
        self.assertEqual(
            started["icon"], str(ROOT / "installer" / "courselens-icon.ico")
        )

    def test_window_close_drives_the_graceful_shutdown_chain(self) -> None:
        # 全链行为钉：窗口 closed → close_all_for_native_window → 显式 close
        # 快路径 → 关停回调（工作保护/宽限语义在 registry 行为钉里另测）。
        fired = threading.Event()
        registry = FrontendSessionRegistry(
            lease_seconds=300.0,
            shutdown_grace_seconds=60.0,
            close_grace_seconds=0.3,
            poll_seconds=0.05,
        )
        self.addCleanup(registry.stop)
        registry.set_shutdown_callback(fired.set, should_keep_alive=lambda: False)
        registry.update("page-1", "open")
        fallback: list[str] = []
        _open_native_window(
            "http://127.0.0.1:6268/",
            data_dir=self.data_dir,
            frontend_sessions=registry,
            fallback_open=lambda: fallback.append("browser"),
        )
        window = self.webview.windows[0]
        self.assertEqual(len(window.events.closed.handlers), 1)
        self.assertEqual(fallback, [])
        # 模拟 WebView2 销毁窗口且 pagehide 信标未发：closed 事件给出确定性
        # 离场信号，随后既有收班链在 close 宽限内接管。
        window.events.closed.fire()
        self.assertTrue(fired.wait(5.0), "关窗必须在 close 宽限内触发关停")

    def test_webview_import_failure_degrades_to_browser(self) -> None:
        fallback: list[str] = []
        with mock.patch.dict(sys.modules, {"webview": None}):
            _open_native_window(
                "http://127.0.0.1:6268/",
                data_dir=self.data_dir,
                frontend_sessions=None,
                fallback_open=lambda: fallback.append("browser"),
            )
        self.assertEqual(fallback, ["browser"])
        self.assertEqual(self.webview.created, [])

    def test_webview_start_failure_degrades_to_browser(self) -> None:
        self.webview.start_error = RuntimeError("WebView2 runtime missing")
        fallback: list[str] = []
        _open_native_window(
            "http://127.0.0.1:6268/",
            data_dir=self.data_dir,
            frontend_sessions=None,
            fallback_open=lambda: fallback.append("browser"),
        )
        self.assertEqual(fallback, ["browser"])

    def test_missing_webview2_runtime_never_opens_a_window(self) -> None:
        # 预检：覆盖目录缺失=运行时缺失（官方查找顺序第一优先）。pywebview
        # 会静默开 IE 内核坏窗，这里必须在建窗前就回浏览器。
        with mock.patch.dict(
            "os.environ",
            {"WEBVIEW2_BROWSER_EXECUTABLE_FOLDER": r"C:\nonexistent-wv2-p68"},
        ):
            self.assertFalse(_webview2_runtime_available())
            fallback: list[str] = []
            _open_native_window(
                "http://127.0.0.1:6268/",
                data_dir=self.data_dir,
                frontend_sessions=None,
                fallback_open=lambda: fallback.append("browser"),
            )
        self.assertEqual(fallback, ["browser"])
        self.assertEqual(self.webview.created, [])

    def test_override_folder_with_runtime_counts_as_available(self) -> None:
        runtime_dir = self.data_dir / "wv2-runtime"
        runtime_dir.mkdir()
        (runtime_dir / "msedewebview2.exe").write_bytes(b"MZ")
        with mock.patch.dict(
            "os.environ",
            {"WEBVIEW2_BROWSER_EXECUTABLE_FOLDER": str(runtime_dir)},
        ):
            self.assertTrue(_webview2_runtime_available())

    def test_blank_webview_window_is_replaced_by_the_browser(self) -> None:
        # 加载看门狗：WebView2 环境创建失败是异步白窗（不抛异常）——页面
        # 迟迟不 loaded 就销毁窗口并回浏览器，绝不留一个白窗给学生。
        self.webview.fire_closed_on_start = False
        fallback: list[str] = []
        with mock.patch(
            "src.app._NATIVE_WINDOW_LOAD_WATCHDOG_SECONDS", 0.2
        ):
            _open_native_window(
                "http://127.0.0.1:6268/",
                data_dir=self.data_dir,
                frontend_sessions=None,
                fallback_open=lambda: fallback.append("browser"),
            )
        self.assertEqual(fallback, ["browser"])
        self.assertTrue(self.webview.windows[0]._destroyed.is_set())


class NativeWindowCloseFastPathTests(unittest.TestCase):
    """close_all_for_native_window 行为钉：快路径/工作保护/活页面自愈。"""

    def _registry(self, **kwargs) -> FrontendSessionRegistry:
        defaults = dict(
            lease_seconds=300.0,
            shutdown_grace_seconds=60.0,
            close_grace_seconds=0.3,
            poll_seconds=0.05,
        )
        defaults.update(kwargs)
        registry = FrontendSessionRegistry(**defaults)
        self.addCleanup(registry.stop)
        return registry

    def test_close_all_takes_the_explicit_close_fast_path(self) -> None:
        fired = threading.Event()
        registry = self._registry()
        registry.set_shutdown_callback(fired.set, should_keep_alive=lambda: False)
        registry.update("page-1", "open")
        registry.update("page-2", "open")
        registry.close_all_for_native_window()
        self.assertTrue(fired.wait(5.0), "关窗离场必须在 close 宽限内触发关停")

    def test_active_work_still_protects_the_service(self) -> None:
        fired = threading.Event()
        keep_alive = True
        # 再武装语义与浏览器路径一致：工作保护解除后按 shutdown 宽限（此处取
        # 小值便于观察）计时，而不是关窗快路径的 0.3s。
        registry = self._registry(
            close_grace_seconds=0.3, shutdown_grace_seconds=0.4
        )
        registry.set_shutdown_callback(
            fired.set, should_keep_alive=lambda: keep_alive
        )
        registry.update("page-1", "open")
        registry.close_all_for_native_window()
        self.assertFalse(
            fired.wait(1.0), "在飞工作期间关窗不得触发关停（工作保护语义不变）"
        )
        keep_alive = False
        self.assertTrue(
            fired.wait(5.0), "工作到达安全态后仍须走既有关停链"
        )

    def test_live_browser_page_reregisters_and_cancels_the_exit(self) -> None:
        fired = threading.Event()
        registry = self._registry(close_grace_seconds=0.5)
        registry.set_shutdown_callback(fired.set, should_keep_alive=lambda: False)
        registry.update("browser-page", "open")
        registry.close_all_for_native_window()
        # 真实场景：原生窗口关闭时另一个浏览器页还开着——它的下一次心跳
        # 会重新登记并取消离场宽限（宽限 0.5s，心跳 0.25s，双倍余量防抖）。
        time.sleep(0.25)
        registry.update("browser-page", "heartbeat")
        self.assertFalse(fired.wait(1.0), "活着的浏览器页必须取消关窗离场")


class ServeSourceOrderP68Pins(unittest.TestCase):
    """源序钉：窗口循环在 ready+信号安装之后、join 之前；浏览器线程仅兜底。"""

    def setUp(self) -> None:
        self.source = (ROOT / "src" / "app.py").read_text(encoding="utf-8")

    def test_window_loop_runs_after_ready_and_before_join(self) -> None:
        serve_body = self.source[
            self.source.index("def serve("):self.source.index("def main(")
        ]
        ready = serve_body.index('controller.transition("ready", "serving")')
        signals = serve_body.index("install_signal_handlers(controller)")
        window = serve_body.index("_open_native_window(")
        join = serve_body.index("server_thread.join()")
        self.assertLess(ready, signals)
        self.assertLess(signals, window, "P68：原生窗口在信号安装后由主线程打开")
        self.assertLess(window, join)

    def test_browser_thread_only_starts_outside_window_mode(self) -> None:
        serve_body = self.source[
            self.source.index("def serve("):self.source.index("def main(")
        ]
        branch = serve_body[serve_body.index("if open_window:"):]
        self.assertLess(
            branch.index("elif open_browser:"),
            branch.index("courselens-open-browser"),
            "浏览器开窗线程必须收进非窗口模式的分支",
        )


class AppShapeNight10WiringTests(unittest.TestCase):
    """夜10-A APP-SHAPE-2 接线钉：几何还原/最小尺寸/js 桥/更新重启退场。

    注意：桩内绝不触发真实托盘——SYSTRAY-IMPL-2 P1-5 起窗口 shown 即挂托盘
    （常驻与偏好解耦），setUp 已把 ``TrayIcon.start`` 打桩，维持「套件内零
    真实托盘线程」纪律；托盘/任务栏的 Win32 行为归真机活体验证。
    """

    def setUp(self) -> None:
        self.webview = FakeWebviewModule()
        self._modules = mock.patch.dict(sys.modules, {"webview": self.webview})
        self._modules.start()
        self.addCleanup(self._modules.stop)
        self._tray_start = mock.patch.object(
            TrayIcon, "start", lambda self, *args, **kwargs: False
        )
        self._tray_start.start()
        self.addCleanup(self._tray_start.stop)
        self._tmp = tempfile.TemporaryDirectory(prefix="cl-n10a-wiring-")
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def test_window_is_created_with_min_size_and_js_bridge(self) -> None:
        import inspect

        _open_native_window(
            "http://127.0.0.1:6268/",
            data_dir=self.data_dir,
            frontend_sessions=None,
            fallback_open=lambda: None,
        )
        created = self.webview.created[0]
        self.assertEqual(created["min_size"], (900, 600))
        self.assertIn("js_api", created)
        api = created["js_api"]
        self.assertEqual(api.get_window_features()["native_window"], True)
        # 夜10 真机缺陷钉：句柄 getter 必须零参可调（传带参函数会让
        # _hwnd() 静默抛错、标题栏主题永远 ok:false）。
        self.assertEqual(len(inspect.signature(api._hwnd_getter).parameters), 0)
        self.assertEqual(api._hwnd(), 0)  # 桩窗无 native → 诚实 0
        # TRAY-FIX-1：偏好回调随「关闭时退出」语义撤销，_bind 只剩句柄 getter。
        self.assertEqual(len(inspect.signature(api._bind).parameters), 1)

    def test_saved_geometry_is_restored_and_clamped(self) -> None:
        from src.runtime.window_state import save_window_state

        save_window_state(
            self.data_dir,
            geometry={"x": -5000, "y": -5000, "width": 9999, "height": 9999, "maximized": True},
            exit_on_close=True,
        )
        _open_native_window(
            "http://127.0.0.1:6268/",
            data_dir=self.data_dir,
            frontend_sessions=None,
            fallback_open=lambda: None,
        )
        created = self.webview.created[0]
        self.assertEqual(created["maximized"], True)
        self.assertEqual(created["width"], 9999)
        # 出格几何被钳回虚拟桌面原点（测试主机虚拟桌面必然含 (0,0)）。
        self.assertGreaterEqual(created["x"], -9999 + 80)
        self.assertLessEqual(created["x"], 0)
        # 托盘偏好进了 js 桥——前端开关的初始态由此而来（TRAY-FIX-1 起为
        # 「关闭时退出」开关）。
        self.assertTrue(created["js_api"].get_window_features()["exit_on_close"])

    def test_resize_end_saves_geometry_without_a_handle(self) -> None:
        # 夜10-A #8（E1）：用户拖动/缩放结束 → 几何立刻落盘（不等关窗）。
        _open_native_window(
            "http://127.0.0.1:6268/",
            data_dir=self.data_dir,
            frontend_sessions=None,
            fallback_open=lambda: None,
        )
        window = self.webview.windows[0]
        window.native = FakeForm()
        window.events.shown.fire()
        form = window.native
        self.assertEqual(len(form.resize_end_handlers), 1, "shown 后必须订阅 ResizeEnd")
        form.fire_resize_end()
        from src.runtime.window_state import load_window_state

        geometry = load_window_state(self.data_dir)["geometry"]
        self.assertEqual(geometry["width"], 1000)
        self.assertEqual(geometry["x"], 10)

    def test_no_native_form_still_opens_window_and_skips_subscription(self) -> None:
        # 桩/其他平台无 native：订阅静默跳过，开窗链路零影响。
        _open_native_window(
            "http://127.0.0.1:6268/",
            data_dir=self.data_dir,
            frontend_sessions=None,
            fallback_open=lambda: None,
        )
        self.assertIs(self.webview.windows[0].native, None)
        # shown 触发不抛（内部 try/except 兜住）。
        self.webview.windows[0].events.shown.fire()

    def test_webview_start_exception_still_stops_poller_and_tray(self) -> None:
        # 夜10-A #12（E3）：start 抛异常（WebView2 初始化等）也必须收口
        # taskbar 轮询与托盘——此前异常路径漏停，托盘图标可能滞留。
        self.webview.start_error = RuntimeError("boom")
        fallback: list[str] = []
        _open_native_window(
            "http://127.0.0.1:6268/",
            data_dir=self.data_dir,
            frontend_sessions=None,
            fallback_open=lambda: fallback.append("browser"),
        )
        self.assertEqual(fallback, ["browser"])

    def test_webview_start_finally_stops_poller_and_tray_source_pin(self) -> None:
        # 源钉（与上方行为钉互补）：finally 里 taskbar/tray 各自 stop。
        source = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
        start_idx = source.rindex("webview.start(")
        window_tail = source[start_idx:start_idx + 600]
        self.assertIn("finally:", window_tail)
        self.assertLess(window_tail.index("finally:"), window_tail.index("taskbar.stop()"))
        self.assertIn("tray.stop()", window_tail)

    def test_app_user_model_id_set_before_window_creation_source_pin(self) -> None:
        # SYSTRAY-IMPL-1 P1-3 源钉：显式 AUMID 必须在 webview.create_window
        # 之前设定——shell 的任务栏归属在首窗创建时定死，晚了无效。
        source = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
        fn_idx = source.index("def _open_native_window(")
        call_idx = source.index("_set_app_user_model_id()", fn_idx)
        create_idx = source.index("webview.create_window(", fn_idx)
        self.assertLess(call_idx, create_idx)
        self.assertIn("Fudan.FudanCourseLens.Desktop", source)

    def test_shown_always_ensures_the_tray_source_pin(self) -> None:
        # SYSTRAY-IMPL-2 P1-5：托盘常驻与偏好解耦——shown 即挂，偏好不得再
        # 作挂载闸；「被动状态面+右键退出入口」是恒在面，不随 checkbox 拆挂。
        source = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
        fn_idx = source.index("def _on_shown(")
        body = source[fn_idx:source.index("window.events.shown +=", fn_idx)]
        self.assertIn("_ensure_tray()", body)
        self.assertNotIn(
            "minimize_to_tray", body,
            "shown 即挂托盘：_on_shown 内不得再出现偏好挂载闸",
        )

    def test_tray_stop_appear_exactly_once_in_the_exit_finally_source_pin(self) -> None:
        # TRAY-FIX-1（SYSTRAY-IMPL-2 P1-5 延续）：托盘生命周期只归窗口收口
        # finally 管——关窗现在只是隐藏（绝不拆托盘），偏好回调已整体撤销；
        # 全文件 tray.stop() 恰好出现一次=托盘常驻不被任何偏好/关窗路径触碰。
        source = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("tray.stop()"), 1,
                         "tray.stop 只允许出现在窗口收口 finally（托盘常驻红线）")
        self.assertIn(
            "tray.stop()", source[source.rindex("webview.start("):],
            "窗口收口 finally 的托盘 stop 链必须仍在（夜10-A #12 源钉延续）",
        )
        # shown 即挂托盘的恒在面不受影响（P1-5）。
        fn_idx = source.index("def _on_shown(")
        body = source[fn_idx:source.index("window.events.shown +=", fn_idx)]
        self.assertIn("_ensure_tray()", body)
        self.assertNotIn(
            "minimize_to_tray", body,
            "shown 即挂托盘：_on_shown 内不得再出现偏好挂载闸",
        )

    def test_shutdown_request_closes_the_window_for_update_restart(self) -> None:
        # T7 闭合环（接线级）：exit_requested 点亮 → 窗口销毁 → start 返回 →
        # 进程可退出交还 launcher。loaded 先点火，避免看门狗抢跑。
        self.webview.fire_closed_on_start = False
        exit_event = threading.Event()
        outcome: dict = {}

        def run() -> None:
            _open_native_window(
                "http://127.0.0.1:6268/",
                data_dir=self.data_dir,
                frontend_sessions=None,
                fallback_open=lambda: None,
                exit_requested=exit_event,
            )
            outcome["returned"] = True

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        deadline = time.monotonic() + 5.0
        while not self.webview.windows and time.monotonic() < deadline:
            time.sleep(0.01)
        window = self.webview.windows[0]
        window.events.loaded.fire()
        exit_event.set()
        self.assertTrue(window._destroyed.wait(5.0), "关停请求必须自动退场（更新重启闭合）")
        worker.join(timeout=5.0)
        self.assertTrue(outcome.get("returned"))

    def test_window_close_still_drives_the_registry_without_exit_event(self) -> None:
        # 既有语义回归钉：没有 exit_requested 时关窗照走 registry 快路径。
        fired = threading.Event()
        registry = FrontendSessionRegistry(
            lease_seconds=300.0,
            shutdown_grace_seconds=60.0,
            close_grace_seconds=0.3,
            poll_seconds=0.05,
        )
        self.addCleanup(registry.stop)
        registry.set_shutdown_callback(fired.set, should_keep_alive=lambda: False)
        registry.update("page-1", "open")
        _open_native_window(
            "http://127.0.0.1:6268/",
            data_dir=self.data_dir,
            frontend_sessions=registry,
            fallback_open=lambda: None,
        )
        self.webview.windows[0].events.closed.fire()
        self.assertTrue(fired.wait(5.0))


class AppShapeNight10ServePins(unittest.TestCase):
    """serve 源序钉（夜10-A）：退出事件组合回调、二次实例闪光、缓存清理时点。"""

    def setUp(self) -> None:
        self.source = (ROOT / "src" / "app.py").read_text(encoding="utf-8")

    def _serve_body(self) -> str:
        return self.source[self.source.index("def serve("):self.source.index("def main(")]

    def test_shutdown_callback_lights_the_window_exit_event(self) -> None:
        body = self._serve_body()
        self.assertIn("window_exit = threading.Event()", body)
        self.assertIn("window_exit.set()", body)
        self.assertIn("controller.set_shutdown_callback(_on_shutdown_requested)", body)
        self.assertIn("exit_requested=window_exit", body)

    def test_instance_active_flash_is_interactive_only(self) -> None:
        body = self._serve_body()
        self.assertIn("ERROR_INSTANCE_ACTIVE", body)
        # 无头实例（每日任务）绝不抢学生焦点：闪光必须以 open_browser 为门。
        self.assertLess(
            body.index('getattr(exc, "code", "") == ERROR_INSTANCE_ACTIVE'),
            body.index("focus_running_instance_window(root)"),
        )
        flash_line = body[body.index("if getattr(exc,"):body.index("focus_running_instance_window(root)")]
        self.assertIn("open_browser", flash_line)

    def test_webview_profile_purge_runs_after_lock_and_before_bind(self) -> None:
        body = self._serve_body()
        purge = body.index("_purge_webview_profile_if_requested(root)")
        bind = body.index("_ExclusiveBindHTTPServer(")
        lock = body.index("instance.acquire()")
        self.assertLess(lock, purge, "清理必须在实例锁到手后（无并发使用档案）")
        self.assertLess(purge, bind, "清理必须在窗口档案被 WebView2 占用前")


class TrayDarkTaskbarVariantAssetTests(unittest.TestCase):
    """SYSTRAY-IMPL-3：深色任务栏托盘变体资产与真实装载。

    资产腿=installer/ 下变体与基础图标帧目录逐位同构（同尺寸/位深/偏移/
    长度，未来资产重导出若漂移即红）；装载腿=真实 LoadImageW 按系统小图标
    尺寸装载变体出真句柄（真句柄用完 DestroyIcon 收口）。
    """

    def test_variant_lives_next_to_base_with_identical_frame_directory(self) -> None:
        base = ROOT / "installer" / "courselens-icon.ico"
        variant = ROOT / "installer" / "courselens-icon-darktaskbar.ico"
        self.assertTrue(base.is_file(), "base product icon must stay in installer/")
        self.assertTrue(variant.is_file(), "dark-taskbar variant must travel beside the base icon")

        def frame_directory(path: Path) -> list:
            raw = path.read_bytes()
            self.assertEqual(raw[:4], b"\x00\x00\x01\x00", f"{path.name} is not an ICO container")
            _reserved, _type, count = struct.unpack("<HHH", raw[:6])
            return [struct.unpack("<BBBBHHII", raw[6 + i * 16 : 6 + (i + 1) * 16]) for i in range(count)]

        self.assertEqual(frame_directory(variant), frame_directory(base))

    @unittest.skipUnless(os.name == "nt", "real Win32 required for the icon loading leg")
    def test_variant_loads_at_system_small_icon_size(self) -> None:
        from src.runtime import tray_manager as tm

        self.assertIsNotNone(tm.user32, "real Win32 required for the icon loading leg")
        tray = TrayIcon()
        variant = ROOT / "installer" / "courselens-icon-darktaskbar.ico"
        handle = tray._load_icon(str(variant))
        try:
            self.assertTrue(handle, "the dark-taskbar variant must load via LoadImageW")
            self.assertFalse(tray._hicon_is_shared, "variant must not fall back to IDI_APPLICATION")
        finally:
            if handle:
                tm.user32.DestroyIcon(handle)


if __name__ == "__main__":
    unittest.main()
