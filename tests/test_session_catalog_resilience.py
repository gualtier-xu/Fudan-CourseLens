"""S04-B 会话与目录韧性：复用窗口过期/校验在途不得伪报登录失败。

边界闭集（stage-04 合同 §1-§5；SRC-SYNDROME-1 U2 防抖修订）：
- 复用窗口（CLIENT_REUSE_SECONDS）过期但已连接客户端仍在 → 补验在途且未超
  收敛上限时报 ready+revalidating（防抖，前端不闪「验证中」）；超限收敛回
  瞬时 checking；任何形态绝不报 fudan_login_required；
- 校验成功 → 回到 ready；确认失败（会话过期/登出/身份切换）→ 立即 fail-closed；
- 目录快照在瞬时态保持诚实 checking 形态，确认态维持 action_required + 空列表；
- _course_session_ready 与特权路由 401 一律不放宽。
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import urlopen

from path_utils import PROJECT_ROOT
from src.application import CatalogRefreshError, CLIENT_REUSE_SECONDS, CourseLensApplication
from src.runtime.http_api import FrontendSessionRegistry, make_handler
from src.runtime.network import NetworkSettings
from tests.http_services import http_services


def _wait_flag_reset(app: CourseLensApplication, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with app._lock:
            if not app._client_nudge_in_flight:
                return
        time.sleep(0.01)
    raise AssertionError("nudge flag was not released")


class LapsedWindowSnapshotTests(unittest.TestCase):
    """复用窗口过期 × 已连接客户端仍在：checking，而非 fudan_login_required。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        self.app.set_credentials("synthetic-user", "synthetic-password")
        self.app._set_login_status("ready", "icourse", "已连接", connected=True)
        self.app._client = object()
        self.app._client_last_verified_at = time.monotonic() - (CLIENT_REUSE_SECONDS + 1)

    def tearDown(self) -> None:
        self.app.close()
        self.temporary.cleanup()

    def test_lapsed_window_with_connected_client_is_never_login_required(self):
        """SRC-SYNDROME-1 U2 防抖：补验在途（阻塞 client 保证确定性）→
        ready+revalidating；无论防抖或 checking 形态，绝不报 fudan_login_required。"""
        release = threading.Event()
        calls: list[dict] = []

        def _blocked_client(**kwargs):
            calls.append(kwargs)
            release.wait(timeout=2.0)

        self.app.client = _blocked_client
        snapshot = self.app.authentication_snapshot()  # 触发补验（在途，确定性）
        self.assertEqual(snapshot["state"], "ready")
        self.assertEqual(snapshot["code"], "fudan_session_verified")
        self.assertTrue(snapshot.get("revalidating"))
        self.assertNotEqual(snapshot["code"], "fudan_login_required")
        self.assertFalse(snapshot["connected"])
        release.set()
        _wait_flag_reset(self.app)
        self.assertEqual(calls, [{"verify_before_reuse": True}])

    def test_snapshot_never_reports_login_required_while_client_held(self):
        """300 秒边界附近连续采样：已连接客户端在持有时绝不出现登录失败口径。"""
        self.app.client = Mock()
        for age in (CLIENT_REUSE_SECONDS - 5, CLIENT_REUSE_SECONDS, CLIENT_REUSE_SECONDS + 30):
            self.app._client_last_verified_at = time.monotonic() - age
            snapshot = self.app.authentication_snapshot()
            self.assertIn(snapshot["state"], {"ready", "checking"})
            self.assertNotIn(snapshot["code"], {"fudan_login_required", "fudan_session_expired"})
        _wait_flag_reset(self.app)

    def test_inflight_verification_keeps_ready_then_converges_to_checking(self):
        """SRC-SYNDROME-1 U2：补验在途保持 ready 形态（防抖，前端不闪「验证中」）；
        超过 45s 收敛上限回 honest checking；真失败仍走 error/degraded。"""
        release = threading.Event()

        def _blocked_client(**_kwargs):
            release.wait(timeout=2.0)

        self.app.client = _blocked_client
        first = self.app.authentication_snapshot()  # 触发后台校验（在途）
        self.assertEqual(first["state"], "ready")
        self.assertEqual(first["code"], "fudan_session_verified")
        self.assertTrue(first.get("revalidating"))
        time.sleep(0.05)
        second = self.app.authentication_snapshot()  # 校验仍在途
        self.assertEqual(second["state"], "ready")
        self.assertTrue(second.get("revalidating"))
        # 超过 REVALIDATE_READY_GRACE_SECONDS：收敛为诚实 checking
        with self.app._lock:
            self.app._client_revalidate_started_at = time.monotonic() - 50.0
        third = self.app.authentication_snapshot()
        self.assertEqual(third["state"], "checking")
        self.assertEqual(third["code"], "fudan_session_checking")
        self.assertNotIn("revalidating", third)
        release.set()
        _wait_flag_reset(self.app)

    def test_successful_renewal_returns_to_ready(self):
        def _renewing_client(**_kwargs):
            with self.app._lock:
                self.app._client_last_verified_at = time.monotonic()
            return self.app._client

        self.app.client = _renewing_client
        snapshot = self.app.authentication_snapshot()
        _wait_flag_reset(self.app)
        self.assertIn(snapshot["state"], {"ready", "checking"})
        renewed = self.app.authentication_snapshot()
        self.assertEqual(renewed["state"], "ready")
        self.assertEqual(renewed["code"], "fudan_session_verified")
        self.assertTrue(renewed["connected"])
        self.assertGreater(renewed["expires_at"], renewed["observed_at"])
        self.assertNotIn("revalidating", renewed)

    def test_confirmed_expiry_after_discard_stays_fail_closed(self):
        self.app._client = None
        self.app._set_login_status(
            "error", "icourse", "iCourse 会话已过期", error_code="fudan_session_expired",
        )
        snapshot = self.app.authentication_snapshot()
        self.assertEqual(snapshot["state"], "degraded")
        self.assertEqual(snapshot["code"], "fudan_session_expired")
        self.assertEqual(snapshot["actions"], ["login"])

    def test_nudge_spawn_failure_releases_inflight_flag(self):
        """线程启动失败必须释放在途旗标，否则保活永久失效（S04-B 相邻加固）。"""
        self.app.client = Mock()
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("synthetic spawn failure")):
            self.app.nudge_client_verification()
        with self.app._lock:
            self.assertFalse(self.app._client_nudge_in_flight)
        # 旗标已释放：下一次 nudge 可正常派发
        self.app.nudge_client_verification()
        _wait_flag_reset(self.app)


class _PassThroughClient:
    """client() 直通已有客户端：让刷新线程免于真实 check_alive 分支。"""

    def __init__(self, app, client):
        self.app = app
        self.client = client

    def __enter__(self):
        self.previous = self.app.client
        self.app.client = Mock(return_value=self.client)
        return self

    def __exit__(self, *_exc):
        self.app.client = self.previous
        return False


class CatalogResilienceTests(unittest.TestCase):
    """目录快照在瞬时/确认两态的诚实口径。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        self.app.set_credentials("synthetic-user", "synthetic-password")

        class _AliveClient:
            def __init__(self):
                self.vpn = object()
                self.closed = False

            def check_alive(self):
                return True

            def close(self):
                self.closed = True

        self.client = _AliveClient()
        with self.app._lock:
            self.app._client = self.client
            self.app._client_last_verified_at = time.monotonic()
        self.app._set_login_status("ready", "icourse", "synthetic verified session", connected=True)
        self.app.catalog_repository.upsert_course(
            "course-safe", "Synthetic", "Teacher", authorization_state="verified"
        )
        self.app._store_authorized_catalog([{"course_id": "course-safe"}])

    def tearDown(self) -> None:
        self.app.close()
        self.temporary.cleanup()

    def test_transient_refresh_keeps_catalog_in_checking_shape(self):
        """窗口过期 + 客户端仍在：目录快照是诚实 checking，绝不跳登录口径。

        P2-C 陈而可用：复验在途时同身份已验证目录继续可见并显式标注 stale；
        内容边界不变——只回放当前身份命中过的课程 ID，绝无跨账号泄漏。"""
        self.app.client = Mock()
        with self.app._lock:
            self.app._client_last_verified_at = time.monotonic() - (CLIENT_REUSE_SECONDS + 1)
        catalog = self.app.authorized_catalog_snapshot(page=1, page_size=10)
        self.assertEqual(catalog["state"], "checking")
        self.assertEqual(catalog["code"], "fudan_session_checking")
        self.assertTrue(catalog["refreshing"])
        self.assertEqual(catalog["actions"], [])
        self.assertTrue(catalog["stale"])
        self.assertEqual([item["course_id"] for item in catalog["courses"]], ["course-safe"])
        _wait_flag_reset(self.app)

    def test_manual_refresh_in_flight_never_reports_login_required(self):
        """手动刷新的 liveness 校验在途期间：目录保持 ready/checking，课程不消失。"""
        entered = threading.Event()
        release = threading.Event()

        def _slow_load(_client):
            entered.set()
            self.assertTrue(release.wait(timeout=3))
            return ([{"course_id": "course-safe", "lectures": []}], "webvpn", [])

        with _PassThroughClient(self.app, self.client):
            with patch.object(self.app, "_load_authorized_catalog", side_effect=_slow_load):
                accepted = self.app.refresh_authorized_catalog_async()
                self.assertEqual(accepted["state"], "ready")
                self.assertTrue(entered.wait(timeout=3))
                during = self.app.authorized_catalog_snapshot(page=1, page_size=10)
                self.assertIn(during["state"], {"ready", "checking"})
                self.assertNotEqual(during["code"], "fudan_login_required")
                self.assertEqual(
                    [item["course_id"] for item in during["courses"]],
                    ["course-safe"],
                    "刷新在途期间已验证目录不得消失",
                )
                release.set()

        thread = self.app._catalog_refresh_thread
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        settled = self.app.authorized_catalog_snapshot(page=1, page_size=10)
        self.assertEqual(settled["state"], "ready")

    def test_confirmed_logout_clears_catalog_immediately(self):
        self.app.logout_fudan()
        authentication = self.app.authentication_snapshot()
        self.assertEqual(authentication["state"], "action_required")
        self.assertEqual(authentication["code"], "fudan_login_required")
        catalog = self.app.authorized_catalog_snapshot(page=1, page_size=10)
        self.assertEqual(catalog["state"], "action_required")
        self.assertEqual(catalog["code"], "fudan_login_required")
        self.assertEqual(catalog["courses"], [])
        self.assertFalse(catalog.get("stale"))
        self.assertFalse(catalog["refreshing"])
        # P2-C：显式登出即身份边界事件——陈旧目录缓存被立即清除，绝不为
        # 下一个会话残留（即使同账号再次登录也走全新验证目录）。
        self.assertEqual(self.app._authorized_catalog_stale_courses_raw(), [])

    def test_confirmed_session_expiry_serves_stale_identity_scoped_catalog(self):
        """P2-C：会话确认过期但目录缓存命中 → degraded + 陈旧标注 + 课程可见，
        动作只保留登录入口（需要新授权的动作不下发）。"""
        with _PassThroughClient(self.app, self.client):
            with patch.object(
                self.app,
                "_load_authorized_catalog",
                side_effect=CatalogRefreshError("catalog_session_expired"),
            ):
                self.app.refresh_authorized_catalog_async()
                thread = self.app._catalog_refresh_thread
                thread.join(timeout=5)
        self.assertTrue(self.client.closed)
        authentication = self.app.authentication_snapshot()
        self.assertEqual(authentication["state"], "degraded")
        self.assertEqual(authentication["code"], "fudan_session_expired")
        catalog = self.app.authorized_catalog_snapshot(page=1, page_size=10)
        self.assertEqual(catalog["state"], "degraded")
        self.assertEqual(catalog["code"], "authorized_catalog_stale")
        self.assertTrue(catalog["stale"])
        self.assertEqual([item["course_id"] for item in catalog["courses"]], ["course-safe"])
        self.assertEqual(catalog["actions"], ["login"])

    def test_stale_view_never_crosses_identity_scope(self):
        """P2-C：换号后身份不命中 → 陈旧视图立即消失，绝不显示他人目录。"""
        self.assertEqual(
            [item["course_id"] for item in self.app._authorized_catalog_stale_courses_raw()],
            ["course-safe"],
        )
        self.app.set_credentials("synthetic-other", "synthetic-password")
        self.assertEqual(self.app._authorized_catalog_stale_courses_raw(), [])
        catalog = self.app.authorized_catalog_snapshot(page=1, page_size=10)
        self.assertEqual(catalog["state"], "action_required")
        self.assertEqual(catalog["courses"], [])


class CampusPreflightAndMetricsTests(unittest.TestCase):
    """P2-A 非阻塞预检 + P2-E 去标识计数器（全部合成，零真实网络）。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        self.app.set_credentials("synthetic-user", "synthetic-password")

    def tearDown(self) -> None:
        self.app.close()
        self.temporary.cleanup()

    def _armed_rebuild(self, client_factory):
        """Force client() into the rebuild path with synthetic login/restore."""
        self.app._restore_client_from_checkpoint = Mock(return_value=None)
        self.app._login_with_retry = Mock(return_value=client_factory())

    def test_preflight_skips_cold_first_flight_and_probes_async_after_connect(self):
        class _FakeClient:
            def __init__(self):
                self.vpn = object()

            def check_alive(self):
                return True

        # 冷启动首航：预检绝不武装、绝不探测（不加延迟，也绝不在单测内发探测）。
        def _forbid(service):
            raise AssertionError("cold first flight must not probe")

        with patch.object(self.app.network, "refresh_route_decision", side_effect=_forbid):
            self._armed_rebuild(_FakeClient)
            self.app.client()
        self.assertIsNone(self.app._preflight_campus_state)
        # 已有验证会话后的重建（再认证）：预检武装并异步探测一次。
        probe_calls = []

        def _record(service):
            probe_calls.append(service)
            return self.app.network._empty_route_evidence()

        with patch.object(self.app.network, "refresh_route_decision", side_effect=_record):
            with self.app._lock:
                self.app._session_ever_connected = True
                self.app._client = None
                self.app._client_last_verified_at = 0.0
            self._armed_rebuild(_FakeClient)
            self.app.client()
            deadline = time.monotonic() + 2.0
            while len(probe_calls) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(sorted(probe_calls), ["icourse", "webvpn"])
            self.assertIsNotNone(self.app._preflight_campus_state)
            # TTL 去重：同一代际内立刻再重建，不再重复探测。
            self._armed_rebuild(_FakeClient)
            with self.app._lock:
                self.app._client = None
                self.app._client_last_verified_at = 0.0
            self.app.client()
        self.assertEqual(sorted(probe_calls), ["icourse", "webvpn"])

    def test_campus_counters_are_closed_set_and_count_flights(self):
        """航班级计数走真实 _login_with_retry（合成 WebVPN，零真实网络）。"""
        from src.api import webvpn as webvpn_module
        from src.application import _LAST_TICKET_SUCCESS

        def _no_probe(url, proxy, *, timeout=None):
            return {"healthy": False, "latency_ms": None}

        names = set(CourseLensApplication.CAMPUS_METRIC_NAMES)
        counters = self.app.campus_metric_counters()
        self.assertEqual(set(counters), names)
        self.assertTrue(all(value == 0 for value in counters.values()))
        # 未知名称与零增量绝不落账。
        self.app._bump_campus_metrics(auth_flights_total=1, totally_unknown_metric=5)
        self.assertEqual(self.app.campus_metric_counters()["auth_flights_total"], 1)
        self.assertNotIn("totally_unknown_metric", self.app.campus_metric_counters())
        # 首航：auth_flights_total=1、reauth=0；已有会话后的第二航：reauth=1。
        # 预检的异步探测与 WebVPN 会话全程打桩：本测试绝不发真实网络请求。
        saved_last_success = dict(_LAST_TICKET_SUCCESS)
        fake_vpn = Mock()
        fake_vpn.AUTH_DEADLINE_SECONDS = 5.0
        fake_vpn.capture_session_checkpoint_cookies.return_value = []
        fake_vpn._transport_class = "synthetic"
        try:
            with patch.object(NetworkSettings, "_probe", side_effect=_no_probe), patch.object(
                webvpn_module, "WebVPNSession", return_value=fake_vpn,
            ):
                with self.app._lock:
                    self.app._session_ever_connected = False
                self.app._restore_client_from_checkpoint = Mock(return_value=None)
                self.app.client()  # 首航（真实 _login_with_retry）
                self.app.client()  # 复用窗口内：不新增航班
                self.assertEqual(self.app.campus_metric_counters()["auth_flights_total"], 2)
                with self.app._lock:
                    self.app._client = None
                    self.app._client_last_verified_at = 0.0
                    self.app._session_ever_connected = True
                self.app.client()  # 再认证航
                time.sleep(0.15)  # 等待预检守护线程结束（探测已被打桩）
        finally:
            _LAST_TICKET_SUCCESS.clear()
            _LAST_TICKET_SUCCESS.update(saved_last_success)
        counters = self.app.campus_metric_counters()
        self.assertEqual(counters["auth_flights_total"], 3)
        self.assertEqual(counters["reauth_flights"], 1)

    def test_tun_hint_counters_only_move_on_transitions(self):
        evidence = {"decision": "proxy", "direct_ok": False, "proxy_ok": True}
        with patch.object(self.app.network, "route_evidence", return_value=evidence):
            self.app._set_login_status(
                "error", "webvpn", "登录失败", error_code="timeout",
            )
            first = self.app.connection_snapshot()
            self.assertEqual(first["reason"], "possible_tun_interference")
            self.assertEqual(self.app.campus_metric_counters()["tun_hint_transitions"], 1)
            self.app.connection_snapshot()
            self.app.connection_snapshot()
            self.assertEqual(self.app.campus_metric_counters()["tun_hint_transitions"], 1)
            # 证据翻转为直连恢复 → 提示消失一次（tun_hint_clears）。
            recovered = {"decision": "direct", "direct_ok": True, "proxy_ok": None}
            with patch.object(self.app.network, "route_evidence", return_value=recovered):
                self.app.connection_snapshot()
        counters = self.app.campus_metric_counters()
        self.assertEqual(counters["tun_hint_transitions"], 1)
        self.assertEqual(counters["tun_hint_clears"], 1)

    def test_campus_diagnostics_payload_carries_closed_set_counters(self):
        def _probe_ok(url, proxy, *, timeout=None):
            return {"healthy": True, "latency_ms": 42}

        with patch.object(NetworkSettings, "_probe", side_effect=_probe_ok):
            value = self.app.campus_diagnostics()
        self.assertEqual(set(value["counters"]), set(CourseLensApplication.CAMPUS_METRIC_NAMES))
        self.assertTrue(all(isinstance(v, int) and v >= 0 for v in value["counters"].values()))
        text = json.dumps(value)
        for forbidden in ("http", "127.0.0.1", "password", "cookie", "token", "lck"):
            self.assertNotIn(forbidden, text.casefold())


class _StaticAuthService:
    """闭集认证快照桩：http 门与内联信封的行为锁。"""

    state = "checking"
    code = "fudan_session_checking"

    def authentication_snapshot(self):
        return {
            "state": type(self).state,
            "source": "local",
            "observed_at": 0,
            "expires_at": 0,
            "code": type(self).code,
            "actions": [],
            "connected": False,
            "configured": True,
        }

    def authorized_catalog_snapshot(self, **_kwargs):
        checking = type(self).state == "checking"
        return {
            "state": "checking" if checking else "action_required",
            "source": "local",
            "observed_at": 0,
            "expires_at": 0,
            "code": type(self).code,
            "actions": [],
            "refreshing": checking,
            "courses": [],
            "course_count": None,
        }

    def nudge_client_verification(self):
        return None


class CourseGateHttpTests(unittest.TestCase):
    """未决校验期间：内联信封如实 checking；特权路由 401 不放宽。"""

    @classmethod
    def setUpClass(cls):
        frontend = PROJECT_ROOT / "frontend"
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(
                http_services(_StaticAuthService()), frontend,
                frontend_sessions=FrontendSessionRegistry(
                    lease_seconds=30, shutdown_grace_seconds=30, poll_seconds=1,
                ),
            ),
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _get(self, path: str):
        with urlopen(f"{self.base}{path}") as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read().decode("utf-8"))["data"]

    def _get_status(self, path: str):
        try:
            with urlopen(f"{self.base}{path}") as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # urllib 以 HTTPError 承载 4xx/5xx
            status = getattr(exc, "code", 0)
            body = getattr(exc, "read", lambda: b"")()
            return status, json.loads(body.decode("utf-8")) if body else {}

    def test_tasks_envelope_reports_checking_during_unsettled_verification(self):
        value = self._get("/api/v3/tasks")
        self.assertEqual(value["state"], "checking")
        self.assertEqual(value["code"], "fudan_session_checking")
        self.assertEqual(value["tasks"], [])
        self.assertNotEqual(value["code"], "fudan_login_required")

    def test_privileged_route_stays_fail_closed_during_checking(self):
        status, body = self._get_status("/api/v3/bookmarks?sub_id=s1")
        self.assertEqual(status, 401)
        self.assertEqual(body.get("error_code"), "fudan_login_required")

    def test_confirmed_failure_envelope_stays_action_required(self):
        _StaticAuthService.state = "degraded"
        _StaticAuthService.code = "fudan_session_expired"
        try:
            value = self._get("/api/v3/tasks")
            self.assertEqual(value["state"], "action_required")
            self.assertEqual(value["code"], "fudan_session_expired")
            self.assertEqual(value["actions"], ["login"])
        finally:
            _StaticAuthService.state = "checking"
            _StaticAuthService.code = "fudan_session_checking"


class _CodedCatalogError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class CatalogRouteHealthTests(unittest.TestCase):
    """P3.1：目录读验证路径 → 记录 icourse 最后验证路由；身份不匹配清场；
    交替路径失败在冷却窗口内不逐episode翻转。"""

    PROXY = "http://127.0.0.1:6268"

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        self.app.set_credentials("synthetic-user", "synthetic-password")

    def tearDown(self) -> None:
        self.app.close()
        self.temporary.cleanup()

    def _failing_client(self, outcome_factory) -> Mock:
        client = Mock()
        client.check_alive.return_value = True
        client.with_catalog_session.return_value = client
        client.list_authorized_courses.side_effect = outcome_factory
        return client

    def _run_catalog(self, client):
        import requests

        with (
            patch.object(
                self.app.network, "service_proxies", return_value=["", self.PROXY]
            ),
            patch("src.api.icourse_direct.DirectICourseSession"),
        ):
            return self.app._load_authorized_catalog(client)

    def test_direct_catalog_success_records_icourse_verified_route(self):
        import requests

        client = self._failing_client(
            [
                requests.ConnectionError("webvpn session path down"),
                [{"course_id": "synthetic-course"}],
            ]
        )
        verified, route_class, failures = self._run_catalog(client)
        self.assertEqual(route_class, "direct")
        record = self.app.network._route_health.get("icourse")
        self.assertIsNotNone(record)
        self.assertEqual(record["verified_route"], "direct")

    def test_webvpn_catalog_route_writes_no_independent_route_record(self):
        client = self._failing_client([[{"course_id": "synthetic-course"}]])
        verified, route_class, failures = self._run_catalog(client)
        self.assertEqual(route_class, "webvpn")
        self.assertNotIn("icourse", self.app.network._route_health)

    def test_catalog_identity_mismatch_clears_route_health(self):
        self.app.network.note_route_failure("icourse", "")
        client = self._failing_client(
            _CodedCatalogError("catalog_identity_mismatch")
        )
        with self.assertRaises(CatalogRefreshError):
            self._run_catalog(client)
        self.assertEqual(self.app.network._route_health, {})

    def test_alternating_path_failures_suppress_flap_across_episodes(self):
        counters = self.app.campus_metric_counters
        before = counters()["route_flap_suppressed"]
        client = self._failing_client(
            _CodedCatalogError("catalog_timeout")
        )
        with self.assertRaises(CatalogRefreshError):
            self._run_catalog(client)
        # 第一episode：直连失败获准翻转，代理失败被抑制（+1）。
        self.assertEqual(counters()["route_flap_suppressed"], before + 1)
        with self.assertRaises(CatalogRefreshError):
            self._run_catalog(client)
        # 冷却未过，第二个episode的两次失败全部被抑制（再 +2）。
        self.assertEqual(counters()["route_flap_suppressed"], before + 3)
        record = self.app.network._route_health.get("icourse")
        self.assertIsNotNone(record)
        self.assertEqual(record["failed_route"], "direct")


class NoCredentialRefreshTerminalTests(unittest.TestCase):
    """LV1-4（LIVE-VALIDATE-1）：无凭据刷新必须落等待登录终态。

    病理：refresh 起手把登录态置 connecting，无凭据失败后无人推进——
    认证面永挂 checking（actions=[] 无任何按钮）、目录镜像 checking、
    self-heal 只认 error 不认 connecting，学生只剩重启一条路。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        # 网络无关钉：预检桩掉（真实链在无凭据时应于 _login_with_retry 秒抛）
        self.app._preflight_campus_route = Mock()

    def tearDown(self) -> None:
        self.app.close()
        self.temporary.cleanup()

    def test_refresh_without_credentials_lands_terminal_login_required(self):
        snapshot = self.app.refresh_authorized_catalog_async()
        thread = self.app._catalog_refresh_thread
        self.assertIsNotNone(thread)
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "无凭据刷新线程必须很快终局")
        auth = self.app.authentication_snapshot()
        self.assertNotEqual(
            auth["state"], "checking",
            "登录态不得永挂 checking（connecting 无人推进）",
        )
        self.assertIn("login", auth["actions"], "必须给学生登录动作入口")
        # 登录状态落 idle 终态（等待登录），不是 connecting/retrying
        self.assertEqual(self.app._login_status.get("state"), "idle")

    def test_catalog_error_code_carries_login_required_not_payload_invalid(self):
        error = ValueError("Please set student ID and UIS password first.")
        error.code = "fudan_login_required"
        from src.application import _catalog_error_code
        self.assertEqual(_catalog_error_code(error), "fudan_login_required")


if __name__ == "__main__":
    unittest.main()
