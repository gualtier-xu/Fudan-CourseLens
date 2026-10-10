# AS9（第四十八案）启动两阶段绑定钉面 → P64（第六十四案）开窗时机迁移钉面。
# P56-U3①（第五十六案）：启动期 /api/health 答 JSON（ok=false, starting=true），
# 前端断连探测解析得出诚实失败、热替换后第一拍即恢复；其余 GET 仍启动页 HTML。
# P64：浏览器不再「绑定即开」——启动态期间零 webbrowser 调用，热替换+ready 后
# 恰一次开窗；失败兜底补开窗一次；-NoOpen 全程零开窗；就绪探测只认真 handler
# 的 ok:true。源码序钉 + 活端口行为钉双轨；受管启动脚本与信任校验零触碰（只读确认）。
from __future__ import annotations

import contextlib
import io
import json
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from src.app import (
    _STARTUP_ERROR_PAGE_HTML,
    _STARTUP_PAGE_GRACE_SECONDS,
    _STARTUP_PAGE_HTML,
    _health_endpoint_ok,
    _startup_state_handler,
    serve,
)
from src.runtime.lifecycle import ERROR_PORT_BUSY, ERROR_STARTUP_FAILED, LifecycleError

ROOT = Path(__file__).resolve().parents[1]


def _fetch(port: int, path: str = "/") -> tuple[int, str, dict[str, str]]:
    opener = build_opener(ProxyHandler({}))
    try:
        # 满载宽限：全量套件后段机器吃紧时 2s 本地回环也会超（T1 满载家族，
        # FN9 晨验既定指令=闪红直接改测内宽限不再豁免），统一 10s。
        response = opener.open(f"http://127.0.0.1:{port}{path}", timeout=10)
    except HTTPError as error:
        # 错误页（503 等）以 HTTPError 形态到达：同样读取 body/headers
        response = error
    with response:
        body = response.read().decode("utf-8")
        headers = {key.lower(): value for key, value in response.headers.items()}
        return int(response.status if hasattr(response, "status") else response.code), body, headers


def _free_port() -> int:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])
    listener.close()
    return port


class _RealAppStubHandler(BaseHTTPRequestHandler):
    """热替换后的「真 handler」替身：卡面响应一行标记。"""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        payload = b"real-app"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class LiveTwoPhaseBindTests(unittest.TestCase):
    """活端口：启动态 handler 在 compose 前可响应；热替换后新请求走真 handler。"""

    def test_startup_page_serves_then_hot_swap_takes_over(self) -> None:
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _startup_state_handler(_STARTUP_PAGE_HTML, status=200)
        )
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        try:
            port = int(server.server_address[1])
            status, body, headers = _fetch(port)
            self.assertEqual(status, 200)
            self.assertIn("正在启动", body)
            self.assertIn("自动进入", body)
            self.assertEqual(headers.get("cache-control"), "no-store")

            # compose 完成后的热替换：实例属性按请求读取，新请求立即走真 handler
            server.RequestHandlerClass = _RealAppStubHandler
            status, body, _ = _fetch(port)
            self.assertEqual(status, 200)
            self.assertEqual(body, "real-app")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_startup_pages_are_zero_external_resource(self) -> None:
        for page in (_STARTUP_PAGE_HTML, _STARTUP_ERROR_PAGE_HTML):
            self.assertNotIn("http://", page)
            self.assertNotIn("https://", page)
            self.assertNotIn("<script", page)
            self.assertIn('charset="utf-8"', page)
        self.assertIn('http-equiv="refresh"', _STARTUP_PAGE_HTML, "启动页自刷新回到 /")
        self.assertNotIn('http-equiv="refresh"', _STARTUP_ERROR_PAGE_HTML, "错误页不循环刷新")
        self.assertIn("启动没有成功", _STARTUP_ERROR_PAGE_HTML)
        self.assertIn("再试一次", _STARTUP_ERROR_PAGE_HTML)
        # P64：启动页退为兜底面，文案收敛为「通常几秒」，不再预告十几秒
        self.assertIn("通常几秒内完成", _STARTUP_PAGE_HTML)
        self.assertNotIn("十几秒", _STARTUP_PAGE_HTML)

    def test_startup_health_answers_legible_json(self) -> None:
        """P56-U3①：启动期 /api/health 答 200 JSON（ok=false, starting=true）——
        前端断连探测解析得出诚实失败、热替换后第一拍即恢复；其余 GET 仍启动页 HTML。"""
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _startup_state_handler(_STARTUP_PAGE_HTML, status=200)
        )
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        try:
            port = int(server.server_address[1])
            status, body, headers = _fetch(port, "/api/health")
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(body),
                {"ok": False, "starting": True, "service": "fudan-courselens"},
            )
            self.assertIn("application/json", headers.get("content-type", ""))
            self.assertEqual(headers.get("cache-control"), "no-store")
            # 带查询串同样命中 JSON 分支
            status, body, _ = _fetch(port, "/api/health?probe=1")
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["starting"])
            # 其余 GET（含根路径）维持启动页 HTML
            status, body, headers = _fetch(port, "/")
            self.assertEqual(status, 200)
            self.assertIn("正在启动", body)
            self.assertIn("text/html", headers.get("content-type", ""))
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_error_state_health_stays_legible_and_post_stays_503(self) -> None:
        """错误态（503 兜底页）：/api/health 仍答 JSON（ok=false, starting=false，
        诚实区分于「启动中」）；POST 一律 503 的启动期语义不变。"""
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _startup_state_handler(_STARTUP_ERROR_PAGE_HTML, status=503)
        )
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        try:
            port = int(server.server_address[1])
            status, body, headers = _fetch(port, "/api/health")
            self.assertEqual(status, 503)
            payload = json.loads(body)
            self.assertEqual(payload["ok"], False)
            self.assertEqual(payload["starting"], False)
            self.assertIn("application/json", headers.get("content-type", ""))
            request = Request(f"http://127.0.0.1:{port}/api/health", data=b"{}", method="POST")
            with self.assertRaises(HTTPError) as raised:
                build_opener(ProxyHandler({})).open(request, timeout=10)
            self.assertEqual(raised.exception.code, 503)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


class ServeStartupTimingTests(unittest.TestCase):
    """serve() 级行为钉（P64）：启动态零开窗，ready 后恰一次；失败兜底补开窗。"""

    def _serve_in_thread(self, **kwargs):
        outcome: list = []

        def runner() -> None:
            try:
                serve(**kwargs)
                outcome.append(("ok", None))
            except Exception as exc:  # noqa: BLE001 - 捕获断言用
                outcome.append(("error", exc))

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        return thread, outcome

    def test_browser_opens_after_ready_not_during_startup(self) -> None:
        compose_entered = threading.Event()
        compose_release = threading.Event()

        def gated_compose(root, *, lifecycle_controller=None):
            compose_entered.set()
            compose_release.wait(timeout=15)
            return mock.MagicMock()

        services = mock.MagicMock()
        services.lifecycle.close.return_value = {"timed_out": False}
        instance = mock.MagicMock()
        browser_calls: list[str] = []
        browser_opened = threading.Event()
        with (
            tempfile.TemporaryDirectory() as folder,
            mock.patch("src.app.InstanceLock", return_value=instance),
            mock.patch("src.app.coordinate_local_data"),
            mock.patch("src.app.compose_services", side_effect=gated_compose),
            mock.patch("src.app.make_handler", return_value=mock.MagicMock()),
            mock.patch("src.app.install_signal_handlers", return_value={}),
            mock.patch("src.app._TracedLifecycleController") as controller_cls,
            mock.patch("src.app._health_endpoint_ok", return_value=True),
            mock.patch(
                "src.app.webbrowser.open",
                side_effect=lambda url: (browser_calls.append(url), browser_opened.set())[1] or True,
            ),
        ):
            controller = mock.MagicMock()
            controller.snapshot.return_value = {"state": "stopped"}
            shutdown_callbacks: list = []
            controller.set_shutdown_callback.side_effect = shutdown_callbacks.append
            controller_cls.return_value = controller
            thread, outcome = self._serve_in_thread(
                data_dir=folder, port=0, open_browser=True, stop_when_frontend_closes=False,
            )
            try:
                self.assertTrue(compose_entered.wait(timeout=5), "compose 应已进入")
                time.sleep(0.5)
                self.assertEqual(
                    browser_calls, [],
                    "启动态期间（绑定→数据协调→compose 全程）绝不调用 webbrowser（P64）",
                )
                self.assertFalse(
                    instance.publish.called,
                    "浏览器打开前实例端口必须先 publish（开窗在 ready 之后）",
                )
            finally:
                compose_release.set()
                self.assertTrue(browser_opened.wait(timeout=10), "热替换+ready 后必须开窗")
                deadline = time.monotonic() + 5
                while not shutdown_callbacks and time.monotonic() < deadline:
                    time.sleep(0.02)
                for callback in shutdown_callbacks:
                    callback()
                thread.join(timeout=10)
            self.assertTrue(outcome and outcome[0][0] == "ok", f"serve 应正常收班: {outcome}")
            self.assertEqual(instance.publish.call_count, 1, "端口最终照常 publish")
            time.sleep(0.5)
            self.assertEqual(len(browser_calls), 1, "开窗恰一次（P64）")

    def test_compose_failure_serves_error_page_within_grace_then_fails(self) -> None:
        compose_entered = threading.Event()

        def failing_compose(root, *, lifecycle_controller=None):
            compose_entered.set()
            raise RuntimeError("boom: 注入的启动失败")

        instance = mock.MagicMock()
        port = _free_port()
        with (
            tempfile.TemporaryDirectory() as folder,
            mock.patch("src.app.InstanceLock", return_value=instance),
            mock.patch("src.app.coordinate_local_data"),
            mock.patch("src.app.compose_services", side_effect=failing_compose),
            mock.patch("src.app._TracedLifecycleController") as controller_cls,
            mock.patch("src.app._STARTUP_PAGE_GRACE_SECONDS", 2.0),
            mock.patch("src.app.webbrowser.open", return_value=True) as browser_open,
        ):
            controller = mock.MagicMock()
            controller.snapshot.return_value = {"state": "failed"}
            controller_cls.return_value = controller
            thread, outcome = self._serve_in_thread(
                data_dir=folder, port=port, open_browser=True, stop_when_frontend_closes=False,
            )
            try:
                self.assertTrue(compose_entered.wait(timeout=5))
                deadline = time.monotonic() + 5
                page: tuple[int, str, dict[str, str]] | None = None
                while time.monotonic() < deadline:
                    try:
                        page = _fetch(port)
                        break
                    except Exception:  # noqa: BLE001 - 循环未起时连接被拒，重试
                        time.sleep(0.05)
                self.assertIsNotNone(page, "宽限窗内启动端口必须可响应")
                self.assertEqual(page[0], 503, "错误页以 503 诚实表态")
                self.assertIn("启动没有成功", page[1])
                self.assertEqual(page[2].get("cache-control"), "no-store")
            finally:
                thread.join(timeout=15)
            self.assertTrue(outcome, "serve 应在宽限窗后退出")
            self.assertEqual(outcome[0][0], "error")
            self.assertIsInstance(outcome[0][1], LifecycleError)
            self.assertEqual(outcome[0][1].code, ERROR_STARTUP_FAILED)
            self.assertFalse(instance.publish.called, "失败路径绝不 publish 实例端口")
            # P64：浏览器不再绑定即开，失败兜底必须补开窗恰一次（学生不盯黑窗）
            browser_open.assert_called_once()

    def test_port_busy_still_fails_closed(self) -> None:
        instance = mock.MagicMock()
        with (
            tempfile.TemporaryDirectory() as folder,
            mock.patch("src.app.InstanceLock", return_value=instance),
            mock.patch("src.app.coordinate_local_data"),
            mock.patch("src.app._ExclusiveBindHTTPServer", side_effect=OSError("occupied")),
        ):
            with self.assertRaises(LifecycleError) as raised:
                serve(data_dir=folder, port=43210, open_browser=False, stop_when_frontend_closes=False)
        self.assertEqual(raised.exception.code, ERROR_PORT_BUSY)
        self.assertFalse(instance.publish.called)

    def test_reserved_port_hint_printed_for_access_denied(self) -> None:
        """N9-A2：winerror 10013（Hyper-V/WSL 动态排除段）时打印人话提示，仍按 PORT_BUSY fail-closed。
        P67 U3b：端口无监听者时才走系统保留文案——占用者探测钉死为空，测试不再随环境漂移。"""
        instance = mock.MagicMock()
        denied = OSError("win32 access denied")
        denied.winerror = 10013
        with (
            tempfile.TemporaryDirectory() as folder,
            mock.patch("src.app.InstanceLock", return_value=instance),
            mock.patch("src.app.coordinate_local_data"),
            mock.patch("src.app._ExclusiveBindHTTPServer", side_effect=denied),
            mock.patch("src.app._describe_port_occupier", return_value=""),
        ):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer), self.assertRaises(LifecycleError) as raised:
                serve(data_dir=folder, port=43211, open_browser=False, stop_when_frontend_closes=False)
        self.assertEqual(raised.exception.code, ERROR_PORT_BUSY)
        self.assertIn("动态排除段", buffer.getvalue())
        self.assertIn("换个端口", buffer.getvalue())
        self.assertFalse(instance.publish.called)

    def test_occupied_port_hint_names_the_listener(self) -> None:
        """P67 U3b (DEF-1 文案分岔)：真实占用（含 SO_REUSEADDR 对普通监听者以
        10013 失败的形态，P67B AtlasCore 实占 6268 被误诊为系统保留）必须
        具名占用者并给可操作出路；10048 同走占用文案；闭集码不变。"""
        for winerror in (10013, 10048):
            with self.subTest(winerror=winerror):
                instance = mock.MagicMock()
                denied = OSError("occupied")
                denied.winerror = winerror
                with (
                    tempfile.TemporaryDirectory() as folder,
                    mock.patch("src.app.InstanceLock", return_value=instance),
                    mock.patch("src.app.coordinate_local_data"),
                    mock.patch("src.app._ExclusiveBindHTTPServer", side_effect=denied),
                    mock.patch("src.app._describe_port_occupier", return_value="AtlasCore.exe"),
                ):
                    buffer = io.StringIO()
                    with contextlib.redirect_stdout(buffer), self.assertRaises(LifecycleError) as raised:
                        serve(data_dir=folder, port=43211, open_browser=False, stop_when_frontend_closes=False)
                self.assertEqual(raised.exception.code, ERROR_PORT_BUSY)
                self.assertIn("AtlasCore.exe", buffer.getvalue())
                self.assertIn("自动换一个空闲端口", buffer.getvalue())
                self.assertNotIn("动态排除段", buffer.getvalue())
                self.assertFalse(instance.publish.called)

    def test_no_open_never_touches_webbrowser(self) -> None:
        compose_release = threading.Event()

        def gated_compose(root, *, lifecycle_controller=None):
            compose_release.wait(timeout=5)
            return mock.MagicMock()

        instance = mock.MagicMock()
        with (
            tempfile.TemporaryDirectory() as folder,
            mock.patch("src.app.InstanceLock", return_value=instance),
            mock.patch("src.app.coordinate_local_data"),
            mock.patch("src.app.compose_services", side_effect=gated_compose),
            mock.patch("src.app.make_handler", return_value=mock.MagicMock()),
            mock.patch("src.app.install_signal_handlers", return_value={}),
            mock.patch("src.app._TracedLifecycleController") as controller_cls,
            mock.patch("src.app.webbrowser.open") as browser_open,
        ):
            controller = mock.MagicMock()
            controller.snapshot.return_value = {"state": "stopped"}
            shutdown_callbacks: list = []
            controller.set_shutdown_callback.side_effect = shutdown_callbacks.append
            controller_cls.return_value = controller
            thread, outcome = self._serve_in_thread(
                data_dir=folder, port=0, open_browser=False, stop_when_frontend_closes=False,
            )
            time.sleep(0.3)
            compose_release.set()
            deadline = time.monotonic() + 5
            while not shutdown_callbacks and time.monotonic() < deadline:
                time.sleep(0.02)
            for callback in shutdown_callbacks:
                callback()
            thread.join(timeout=10)
            self.assertTrue(outcome and outcome[0][0] == "ok", f"无头路径正常收班: {outcome}")
            browser_open.assert_not_called()


class HealthGateProbeTests(unittest.TestCase):
    """P64 就绪探测钉：启动态/错误态 health 不算就绪，真 handler 的 ok:true 才放行开窗。"""

    def _probe_against(self, handler: type[BaseHTTPRequestHandler]) -> bool:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        try:
            port = int(server.server_address[1])
            return _health_endpoint_ok(f"http://127.0.0.1:{port}/")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_startup_state_health_is_not_ready(self) -> None:
        self.assertFalse(
            self._probe_against(_startup_state_handler(_STARTUP_PAGE_HTML, status=200)),
            "启动态 200（ok=false）不得放行开窗",
        )

    def test_error_state_health_is_not_ready(self) -> None:
        self.assertFalse(
            self._probe_against(_startup_state_handler(_STARTUP_ERROR_PAGE_HTML, status=503)),
            "错误态 503 不得放行开窗",
        )

    def test_real_ok_health_is_ready(self) -> None:
        class OkHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def do_GET(self) -> None:  # noqa: N802
                payload = json.dumps({"ok": True, "service": "fudan-courselens"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.assertTrue(self._probe_against(OkHandler), "真 handler ok:true 才放行开窗")

    def test_dead_port_is_not_ready(self) -> None:
        port = _free_port()
        self.assertFalse(_health_endpoint_ok(f"http://127.0.0.1:{port}/"))


class ServeSourceOrderPins(unittest.TestCase):
    """源码序钉：P64 开窗时序与退役项（防回归）。"""

    def setUp(self) -> None:
        self.source = (ROOT / "src" / "app.py").read_text(encoding="utf-8")

    def test_browser_opens_only_after_ready(self) -> None:
        """P64 序钉：绑定→协调→compose→热替换→ready→开窗；启动态窗口零 webbrowser。"""
        serve_body = self.source[self.source.index("def serve("):self.source.index("def main(")]
        bind_index = serve_body.index("(host, port)")
        coordinate_index = serve_body.index("coordinate_local_data(root)")
        compose_index = serve_body.index("services = compose_services(root")
        swap_index = serve_body.index("server.RequestHandlerClass = make_handler(")
        ready_index = serve_body.index('controller.transition("ready", "serving")')
        open_index = serve_body.index("target=_open_browser_when_ready")
        self.assertLess(coordinate_index, compose_index, "迁移检查先于应用组装（语义不变）")
        self.assertLess(compose_index, swap_index, "compose 完成才热替换真 handler")
        self.assertLess(swap_index, ready_index, "热替换先于 ready 转移")
        self.assertLess(ready_index, open_index, "P64：开窗线程必须在 ready 转移之后才启动")
        startup_window = serve_body[bind_index:swap_index]
        self.assertNotIn(
            "webbrowser", startup_window,
            "绑定→热替换之间零 webbrowser 调用（启动态页退为兜底，P64）",
        )
        self.assertIn("server_thread.start()", serve_body)
        self.assertNotIn("        server.serve_forever(poll_interval=0.1)\n", serve_body,
                         "serve_forever 不再占用主线程")

    def test_ready_probe_is_direct_loopback_and_fallback_opens(self) -> None:
        """就绪探测直连 loopback（http.client）不经系统代理；失败兜底保留补开窗。"""
        self.assertIn("http.client.HTTPConnection", self.source)
        self.assertNotIn("ProxyHandler", self.source,
                         "就绪探测不得走 urllib 代理栈（直连 loopback）")
        self.assertIn(
            "_open_browser_when_ready(url, timeout=0.0)", self.source,
            "失败兜底必须补开窗一次（P64）",
        )

    def test_grace_constant_is_bounded(self) -> None:
        self.assertIsInstance(_STARTUP_PAGE_GRACE_SECONDS, float)
        self.assertLessEqual(_STARTUP_PAGE_GRACE_SECONDS, 120.0)


if __name__ == "__main__":
    unittest.main()
