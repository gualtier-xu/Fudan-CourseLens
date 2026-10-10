"""一次性 CAS ticket 传输腿的执行式离线测试（先于实现编写，旧实现必须失败）。

对应 docs/fudan-login-ticket-transport-optimization-handoff.md §11：
1. 预热先于 ticket，且与 ticket 共用同一持久 curl 连接（TCP 连接计数证明复用）；
2. 预热失败发生在 ticket 发送前：ticket 请求计数为 0，可在 attempt 内换 transport；
3. 直连 route 显式空串代理：环境 http_proxy/HTTPS_PROXY 指向死端口也不被采用；
4. curl 腿收到 (connect, read) 二元 timeout，而不是 max() 压平的标量；
5. ticket 已发送后传输异常：验证恰一次、同 ticket 绝不重放（既有语义回归）；
6. 验证失败：异常携带闭集 code；下一 attempt 拿全新 ticket 并换 (route, transport)；
7. close() 幂等且吞掉一切内部异常；被放弃的 transport 恰好关闭一次；
8. 进程内 last-success 只影响排序，值是闭集类别，不落盘、不含账号数据；
9. 预热阶段与 route/transport 进入遥测闭集，未知类别一律丢弃。
全部 loopback/fake 驱动，零真实外联。
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

import requests

from path_utils import PROJECT_ROOT
from src import application
from src.api.webvpn import TICKET_TRANSPORTS, _DeadlineSession, WebVPNSession
from src.application import CourseLensApplication, LoginStageTelemetry
from src.runtime import config


# ---------------------------------------------------------------------------
# loopback：TCP 连接计数 + 请求顺序 + 按路径故障注入（reset = 立即断连）
# ---------------------------------------------------------------------------


class _FaultHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        with self.server.lock:
            self.server.connection_count += 1
            self.connection_id = self.server.connection_count

    def log_message(self, *args) -> None:  # 静默
        pass

    def do_GET(self) -> None:
        server = self.server
        path = self.path.split("?", 1)[0]
        with server.lock:
            server.requests.append((self.connection_id, path))
        if server.behaviors.get(path) == "reset":
            raise ConnectionResetError("synthetic cold-connection reset")
        if path == "/webvpn/login":
            self._send(302, location="/portal")
        elif path == "/portal":
            self._send(200, body=b"portal")
        else:
            self._send(302, location="/login")

    def _send(self, status: int, *, location: str = "", body: bytes = b"") -> None:
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


class FaultServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _FaultHandler)
        self.lock = threading.Lock()
        self.connection_count = 0
        self.requests: list[tuple[int, str]] = []
        self.behaviors: dict[str, str] = {}

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def recorded_paths(self) -> list[str]:
        with self.lock:
            return [path for _, path in self.requests]

    def connection_ids(self) -> list[int]:
        with self.lock:
            return [connection for connection, _ in self.requests]


class LoopbackCase(unittest.TestCase):
    """把 WEBVPN_BASE 指向 loopback，保证 curl 腿零真实外联。"""

    def setUp(self) -> None:
        self.server = FaultServer()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self._patcher = patch.object(config, "WEBVPN_BASE", self.server.base)
        self._patcher.start()
        self._ticket_url_patcher = patch.object(
            WebVPNSession,
            "_validate_ticket_redirect_url",
            side_effect=lambda value: value,
        )
        self._ticket_url_patcher.start()

    def tearDown(self) -> None:
        self._ticket_url_patcher.stop()
        self._patcher.stop()
        self.server.shutdown()
        self.server.server_close()


def make_loopback_vpn(server: FaultServer, **kwargs) -> WebVPNSession:
    """真实 _DeadlineSession + 直连 route（curl 腿真实执行）。"""
    vpn = WebVPNSession(**kwargs)
    vpn.session = _DeadlineSession(lambda: None)
    vpn.session.headers.update({"User-Agent": config.USER_AGENT})
    return vpn


class _RecordingJar:
    """curl cookie jar 替身：记录 set 调用，供腿前刷新断言。"""

    def __init__(self):
        self.set_calls: list[tuple[str, str]] = []
        self.jar = ()

    def set(self, name, value, domain=None, path="/"):
        self.set_calls.append((name, value))


class FakeTicketTransport:
    """可编程 curl transport 替身：记录 kwargs、按脚本返回/抛出。"""

    def __init__(self, script):
        self.script = list(script)
        self.get_calls: list[tuple[str, dict]] = []
        self.headers: dict = {}
        self.cookies = _RecordingJar()
        self.close_count = 0

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.close_count += 1


class FakeCurlResponse:
    def __init__(self, status=200, *, url="", location=""):
        self.status_code = status
        self.headers = {"Location": location} if location else {}
        self.url = url
        self.closed = False

    def close(self):
        self.closed = True


class TicketRedirectAllowlistTests(unittest.TestCase):
    def _vpn(self, script):
        vpn = WebVPNSession(proxy_url="")
        transport = FakeTicketTransport(script)
        install_fake_session(vpn, lambda transport="curl_h2": transport)
        vpn._ticket_transport = transport
        return vpn, transport

    def test_ticket_initial_url_must_be_allowlisted_https_before_request(self):
        vpn, transport = self._vpn([])
        try:
            with self.assertRaisesRegex(RuntimeError, "target is not allowed"):
                vpn._follow_ticket_redirects("http://webvpn.fudan.edu.cn/login?ticket=ST-x")
            self.assertEqual(transport.get_calls, [])
        finally:
            vpn.close()

    def test_ticket_redirect_to_unknown_host_is_rejected_before_next_request(self):
        vpn, transport = self._vpn([
            FakeCurlResponse(
                302,
                url="https://webvpn.fudan.edu.cn/login?ticket=ST-x",
                location="https://redirect.invalid/next",
            ),
        ])
        try:
            with self.assertRaisesRegex(RuntimeError, "target is not allowed"):
                vpn._follow_ticket_redirects("https://webvpn.fudan.edu.cn/login?ticket=ST-x")
            self.assertEqual(len(transport.get_calls), 1)
        finally:
            vpn.close()

    def test_ticket_redirect_cannot_downgrade_to_http(self):
        vpn, transport = self._vpn([
            FakeCurlResponse(
                302,
                url="https://webvpn.fudan.edu.cn/login?ticket=ST-x",
                location="http://webvpn.fudan.edu.cn/next",
            ),
        ])
        try:
            with self.assertRaisesRegex(RuntimeError, "target is not allowed"):
                vpn._follow_ticket_redirects("https://webvpn.fudan.edu.cn/login?ticket=ST-x")
            self.assertEqual(len(transport.get_calls), 1)
        finally:
            vpn.close()


def install_fake_session(vpn: WebVPNSession, factory) -> None:
    vpn.session = Mock(spec=_DeadlineSession)
    vpn.session.proxies = {}
    vpn.session.headers = {}
    vpn.session.cookies = ()
    vpn.session.ticket_session = factory


# ---------------------------------------------------------------------------
# 1 + 2：预热与持久连接（真实 curl transport + loopback）
# ---------------------------------------------------------------------------


class WarmupPersistenceTests(LoopbackCase):
    def test_warmup_precedes_ticket_on_same_persistent_connection(self):
        vpn = make_loopback_vpn(self.server, proxy_url="")
        try:
            ticket_url = f"{self.server.base}/webvpn/login?cas_login=true&ticket=ST-once"
            vpn._ensure_ticket_transport()
            status, redirects = vpn._follow_ticket_redirects(ticket_url)
            self.assertEqual((status, redirects), (200, 1))
            self.assertEqual(self.server.recorded_paths(), ["/", "/webvpn/login", "/portal"])
            self.assertEqual(len(set(self.server.connection_ids())), 1, "预热与票腿共用一条 TCP 连接")
        finally:
            vpn.close()

    def test_second_ensure_reuses_transport_without_new_probe(self):
        vpn = make_loopback_vpn(self.server, proxy_url="")
        try:
            first = vpn._ensure_ticket_transport()
            second = vpn._ensure_ticket_transport()
            self.assertIs(first, second)
            self.assertEqual(self.server.recorded_paths(), ["/"], "仅创建时预热一次")
        finally:
            vpn.close()

    def test_warmup_failure_keeps_ticket_request_count_at_zero(self):
        self.server.behaviors["/"] = "reset"
        vpn = make_loopback_vpn(self.server, proxy_url="")
        try:
            with self.assertRaises(requests.RequestException):
                vpn._ensure_ticket_transport()
            self.assertNotIn("/webvpn/login", self.server.recorded_paths())
            self.assertIsNone(vpn._ticket_transport)
        finally:
            vpn.close()

    def test_prepared_transport_failure_switches_backup_within_attempt(self):
        vpn = WebVPNSession(proxy_url="")
        primary = FakeTicketTransport([requests.ConnectionError("cold")])
        backup = FakeTicketTransport([FakeCurlResponse(302)])
        created = [primary, backup]
        install_fake_session(vpn, lambda transport="curl_h2": created.pop(0))
        try:
            with patch.object(config, "WEBVPN_BASE", "http://webvpn.invalid"):
                transport = vpn.prepare_ticket_transport()
            self.assertIs(transport, backup)
            self.assertEqual(primary.close_count, 1, "被放弃的 transport 恰好关闭一次")
            self.assertEqual(vpn._transport_class, "curl_h1")
        finally:
            vpn.close()

    def test_prepare_gives_up_after_single_backup_round(self):
        vpn = WebVPNSession(proxy_url="")
        transports = [
            FakeTicketTransport([requests.ConnectionError("cold-h2")]),
            FakeTicketTransport([requests.ConnectionError("cold-h1")]),
        ]
        install_fake_session(vpn, lambda transport="curl_h2": transports.pop(0))
        try:
            with patch.object(config, "WEBVPN_BASE", "http://webvpn.invalid"):
                with self.assertRaises(requests.ConnectionError):
                    vpn.prepare_ticket_transport()
            self.assertIsNone(vpn._ticket_transport, "两轮预热都失败后不得带着坏 transport 进入票腿")
        finally:
            vpn.close()

    def test_prepare_skips_warmup_for_scripted_sessions(self):
        vpn = WebVPNSession(proxy_url="")
        vpn.session = ScriptedSession([])
        self.assertIsNone(vpn.prepare_ticket_transport())
        vpn.close()


# ---------------------------------------------------------------------------
# 3：环境代理隔离（真实 curl transport + loopback + 死代理环境变量）
# ---------------------------------------------------------------------------


class ProxyIsolationTests(LoopbackCase):
    def setUp(self) -> None:
        super().setUp()
        # 预留后立刻关闭的端口：任何指向它的连接都会被立即拒绝
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.dead_port = probe.getsockname()[1]
        probe.close()

    def test_direct_route_ticket_transport_ignores_environment_proxy(self):
        vpn = make_loopback_vpn(self.server, proxy_url="")
        dead = f"http://127.0.0.1:{self.dead_port}"
        env = {
            "http_proxy": dead,
            "https_proxy": dead,
            "HTTP_PROXY": dead,
            "HTTPS_PROXY": dead,
            "all_proxy": dead,
        }
        try:
            with patch.dict(os.environ, env):
                transport = vpn._ensure_ticket_transport()
                self.assertEqual(transport.proxies.get("https"), "")
                status, _ = vpn._follow_ticket_redirects(
                    f"{self.server.base}/webvpn/login?ticket=ST-env"
                )
            self.assertEqual(status, 200)
        finally:
            vpn.close()


# ---------------------------------------------------------------------------
# 4：二元 timeout（可编程 transport 记录 kwargs）
# ---------------------------------------------------------------------------


class BinaryTimeoutTests(unittest.TestCase):
    def test_curl_leg_receives_connect_read_tuple_not_flattened_scalar(self):
        vpn = WebVPNSession(proxy_url="")
        recorder = FakeTicketTransport(
            [
                FakeCurlResponse(302, url="https://webvpn.fudan.edu.cn/login?ticket=ST-t", location="/portal"),
                FakeCurlResponse(200, url="https://webvpn.fudan.edu.cn/portal"),
            ]
        )
        install_fake_session(vpn, lambda transport="curl_h2": recorder)
        vpn._ticket_transport = recorder
        try:
            status, _ = vpn._follow_ticket_redirects("https://webvpn.fudan.edu.cn/login?ticket=ST-t")
            self.assertEqual(status, 200)
            self.assertTrue(recorder.get_calls)
            for _, kwargs in recorder.get_calls:
                self.assertEqual(
                    kwargs.get("timeout"),
                    vpn.TICKET_TIMEOUT,
                    "curl 票腿必须收到 (connect, read) 二元 timeout",
                )
        finally:
            vpn.close()


# ---------------------------------------------------------------------------
# 5 + 6：闭集错误 code（ScriptedSession 走 requests 腿路径）
# ---------------------------------------------------------------------------


class CookieSyncTests(unittest.TestCase):
    def test_requests_jar_cookies_refresh_into_transport_before_each_leg(self):
        # 两条票腿之间 requests 主会话可能经 Set-Cookie 轮换；
        # 每条腿开始前必须把当前 jar 刷新进持久 curl transport（双向显式同步）。
        vpn = WebVPNSession(proxy_url="")
        vpn.session = _DeadlineSession(lambda: None)
        vpn.session.headers.update({"User-Agent": config.USER_AGENT})
        portal = "https://webvpn.fudan.edu.cn/portal"
        recorder = FakeTicketTransport(
            [FakeCurlResponse(302, url=portal, location="/portal"), FakeCurlResponse(200, url=portal)]
        )
        vpn._ticket_transport = recorder
        vpn.session.cookies.set("rotated_session", "v2")
        try:
            status, _ = vpn._follow_ticket_redirects("https://webvpn.fudan.edu.cn/login?ticket=ST-c")
            self.assertEqual(status, 200)
            self.assertIn(("rotated_session", "v2"), recorder.cookies.set_calls)
        finally:
            vpn.close()


class ScriptedSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        pass


class ScriptedResponse:
    def __init__(self, status=200, *, url=""):
        self.status_code = status
        self.headers: dict = {}
        self.url = url

    def close(self):
        pass


class ErrorClosedSetTests(unittest.TestCase):
    def test_webvpn_ticket_transport_failure_carries_closed_code(self):
        vpn = WebVPNSession(proxy_url="")
        vpn.session = ScriptedSession(
            [
                requests.ConnectionError("reset"),
                ScriptedResponse(302, url=f"{config.WEBVPN_BASE}/login"),
            ]
        )
        try:
            with self.assertRaises(RuntimeError) as caught:
                vpn._establish_session(f"{config.WEBVPN_BASE}/login?ticket=ST-x")
            self.assertEqual(
                getattr(caught.exception, "code", ""),
                "webvpn_ticket_transport_failed",
            )
            self.assertEqual(len(vpn.session.calls), 2, "票 GET 恰一次 + 验证探针恰一次")
        finally:
            vpn.close()

    def test_icourse_ticket_transport_failure_carries_closed_code(self):
        vpn = WebVPNSession(proxy_url="")
        vpn.session = ScriptedSession(
            [
                requests.ConnectionError("reset"),
                ScriptedResponse(302, url=f"{config.ICOURSE_BASE}/login"),
            ]
        )
        try:
            with patch("src.api.webvpn.get_vpn_url", side_effect=lambda url: url):
                with self.assertRaises(RuntimeError) as caught:
                    vpn._establish_icourse_session(f"{config.ICOURSE_BASE}/cas?ticket=ST-y")
            self.assertEqual(
                getattr(caught.exception, "code", ""),
                "icourse_ticket_transport_failed",
            )
        finally:
            vpn.close()

    def test_verified_recovery_stays_code_free(self):
        events: list[str] = []
        vpn = WebVPNSession(
            step_callback=lambda step, details: events.append(step), proxy_url=""
        )
        vpn.session = ScriptedSession(
            [
                requests.ConnectionError("reset"),
                ScriptedResponse(200, url=f"{config.WEBVPN_BASE}/"),
            ]
        )
        try:
            vpn._establish_session(f"{config.WEBVPN_BASE}/login?ticket=ST-z")
            self.assertEqual(events[0], "webvpn_ticket_complete")
        finally:
            vpn.close()


# ---------------------------------------------------------------------------
# 6b：候选多样化（application 层，fake WebVPNSession）
# ---------------------------------------------------------------------------


class CandidateDiversityTests(unittest.TestCase):
    def setUp(self) -> None:
        application._LAST_TICKET_SUCCESS.update(route=None, transport=None)
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.service = CourseLensApplication(self.temporary.name)
        self.service.set_credentials("student", "password")
        self.ctor_kwargs: list[dict] = []

    def tearDown(self) -> None:
        self.service.close()
        self.temporary.cleanup()
        application._LAST_TICKET_SUCCESS.update(route=None, transport=None)

    def _install_vpn(self, outcomes):
        test = self

        class _RecordingVpn:
            def __init__(self, **kwargs):
                test.ctor_kwargs.append(dict(kwargs))
                self.session = Mock()

            def login(self, **_kwargs):
                outcome = outcomes.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome

            def authenticate_icourse(self, **_kwargs):
                outcome = outcomes.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome

            def close(self):
                pass

        return _RecordingVpn

    def _run(self, outcomes) -> None:
        vpn_cls = self._install_vpn(outcomes)
        with patch("src.api.webvpn.WebVPNSession", vpn_cls), patch.object(
            self.service.network, "service_proxies", return_value=["", "http://proxy-1"]
        ), patch("time.sleep"):
            self.service._login_with_retry(max_attempts=3)

    @staticmethod
    def _transport_failure() -> Exception:
        exc = RuntimeError("WebVPN ticket exchange failed (ConnectionError)")
        exc.code = "webvpn_ticket_transport_failed"
        return exc

    def test_ticket_transport_failure_is_terminal_after_ticket_consumption(self):
        # P0.2（2026-09-13 VPN-P0-BACKEND-1）：票腿传输失败意味着 ticket 已消费
        # 而会话未确认——立即终止，绝不换 transport、绝不换 route、绝不重试。
        # 票前预热阶段的 H2/H1 回退保留在 WebVPNSession.prepare_ticket_transport
        # 内部（发生在任何 ticket 产生之前），不受此终局语义影响。
        outcomes = [self._transport_failure(), None, None]
        with self.assertRaises(RuntimeError):
            self._run(outcomes)
        proxies = [kwargs.get("proxy_url") for kwargs in self.ctor_kwargs]
        transports = [kwargs.get("transport") for kwargs in self.ctor_kwargs]
        self.assertEqual(len(proxies), 1, "ticket 已消费：恰好一次尝试")
        self.assertEqual((proxies[0], transports[0]), ("", "curl_h2"))
        snapshot = self.service.authentication_snapshot()
        self.assertEqual(snapshot["code"], "webvpn_ticket_transport_failed")

    def test_non_transport_failure_rotates_route_not_transport(self):
        self._run([requests.ConnectionError("auth-phase reset"), None, None])
        proxies = [kwargs.get("proxy_url") for kwargs in self.ctor_kwargs]
        transports = [kwargs.get("transport") for kwargs in self.ctor_kwargs]
        self.assertEqual(proxies[:2], ["", "http://proxy-1"])
        self.assertEqual(transports[:2], ["curl_h2", "curl_h2"])

    def test_next_login_starts_from_last_success_combination(self):
        # 第一次登录：attempt 1 直连失败（非票腿类），attempt 2 代理 route 成功
        self._run([requests.ConnectionError("auth reset"), None, None])
        self.ctor_kwargs.clear()
        self._run([None, None])
        proxies = [kwargs.get("proxy_url") for kwargs in self.ctor_kwargs]
        transports = [kwargs.get("transport") for kwargs in self.ctor_kwargs]
        self.assertEqual((proxies[0], transports[0]), ("http://proxy-1", "curl_h2"))

    def test_last_success_memory_holds_closed_classes_only(self):
        self._run([None, None])
        memory = dict(application._LAST_TICKET_SUCCESS)
        self.assertTrue(set(memory) <= {"route", "transport"})
        self.assertIn(memory["route"], {"direct", "proxy"})
        self.assertIn(memory["transport"], TICKET_TRANSPORTS)


# ---------------------------------------------------------------------------
# 7：close 所有权
# ---------------------------------------------------------------------------


class CloseOwnershipTests(unittest.TestCase):
    def test_close_is_idempotent_and_swallows_internal_errors(self):
        vpn = WebVPNSession(proxy_url="")

        def broken_close():
            raise ZeroDivisionError("synthetic close failure")

        transport = FakeTicketTransport([])
        transport.close = broken_close
        vpn._ticket_transport = transport
        vpn.session = Mock(spec=_DeadlineSession)
        vpn.session.close = broken_close
        vpn.close()
        vpn.close()
        self.assertIsNone(vpn._ticket_transport)

    def test_scripted_vpn_without_transport_closes_cleanly(self):
        vpn = WebVPNSession(proxy_url="")
        vpn.session = ScriptedSession([])
        vpn.close()
        vpn.close()


# ---------------------------------------------------------------------------
# 9：遥测闭集
# ---------------------------------------------------------------------------


class TelemetryClosedSetTests(unittest.TestCase):
    def _record(self, details: dict) -> dict:
        recorded: list[dict] = []
        telemetry = LoginStageTelemetry.__new__(LoginStageTelemetry)
        telemetry._directory = None
        telemetry._path = None
        telemetry.attempt = 1
        telemetry._pruned = True
        telemetry._append = recorded.append  # type: ignore[method-assign]
        telemetry("webvpn_ticket_warmup", details)
        return recorded[0]

    def test_warmup_stage_with_route_transport_is_recorded(self):
        record = self._record(
            {
                "duration_ms": 12,
                "outcome": "ok",
                "status": 302,
                "route": "direct",
                "transport": "curl_h2",
            }
        )
        self.assertEqual(record["stage"], "webvpn_ticket_warmup")
        self.assertEqual(record["outcome"], "ok")
        self.assertEqual(record["route"], "direct")
        self.assertEqual(record["transport"], "curl_h2")
        self.assertTrue(set(record) <= LoginStageTelemetry.CLOSED_KEYS)

    def test_unknown_route_or_transport_values_are_dropped(self):
        record = self._record(
            {"duration_ms": 12, "outcome": "ok", "route": "http://leak", "transport": "weird"}
        )
        self.assertNotIn("route", record)
        self.assertNotIn("transport", record)


if __name__ == "__main__":
    unittest.main()
