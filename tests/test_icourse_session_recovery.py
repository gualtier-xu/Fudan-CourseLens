from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from path_utils import PROJECT_ROOT
from src.application import CatalogRefreshError, CourseLensApplication
from src.api.icourse import ICourseClient, SessionUnverifiable
from src.api.webvpn import CHECKPOINT_COOKIE_HOST, CHECKPOINT_MAX_COOKIES, WebVPNSession
from src.runtime.network import NetworkSettings


class _VerifiedClient:
    def __init__(self):
        self.vpn = object()
        self.closed = False

    def check_alive(self):
        return True

    def close(self):
        self.closed = True


class _AliveSessionVPN:
    """已登录会话：userinfo 可用、带非 _token Cookie、无 PHP 序列化片段。

    month_family 决定月度目录请求的行为："ready" 回放健康索引/详情（修复后
    webvpn 路径的正例），"refuse" 抛连接错误（迫使路由尝试 direct 备选）。
    """

    def __init__(self, *, month_family="ready"):
        self.session = SimpleNamespace(cookies=[
            SimpleNamespace(
                name="SESS", value="alive-session-value",
                domain="icourse.fudan.edu.cn",
            )
        ])
        self.calls: list[tuple[str, dict]] = []
        assert month_family in ("ready", "refuse")
        self._month_family = month_family
        self._details = list(_course_detail_responses())

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "get-my-course-month" in url:
            if self._month_family == "refuse":
                raise requests.ConnectionError("synthetic webvpn failure")
            return _json_response({"code": 0, "list": [{"course": [
                {"id": "c1", "title": "t", "sub_id": "s1", "sub_duration": 60},
            ]}]})
        if "get-course-detail" in url:
            return self._details.pop(0) if self._details else _course_detail_responses()[0]
        if "infosimple" in url:
            return _json_response({"code": 0, "params": {"id": "u1", "account": "acc1"}})
        raise AssertionError("unexpected URL family")


def _json_response(payload, *, status=200):
    def raise_for_status():
        if status >= 400:
            raise requests.HTTPError(f"synthetic status {status}")

    return SimpleNamespace(status_code=status, raise_for_status=raise_for_status,
                           json=lambda: payload)


def _course_detail_responses():
    """两份健康详情响应：sub_list 携带一讲，回放用（有界合成数据）。"""

    def detail():
        return _json_response({"code": 0, "data": {
            "title": "synthetic-title", "realname": "synthetic-teacher",
            "sub_list": {"2026": {"09": {"01": [
                {"id": "s1", "playback_status": "1",
                 "sub_title": "2026-09-01第1节"},
            ]}}},
        }})

    return [detail(), detail()]


def _authenticated_client_without_bearer(vpn=None) -> ICourseClient:
    """真实 ICourseClient：真实 _authorization_headers，会话有效但无 bearer。"""
    client = ICourseClient.__new__(ICourseClient)
    client.vpn = vpn or _AliveSessionVPN()
    client.catalog_session = None
    client.base_url = "https://icourse.invalid"
    client._userinfo = {"id": "u1", "account": "acc1", "tenant_id": "t1"}
    client.last_catalog_diagnostics = {}
    client._index_months_ok = 0
    client._index_rows = 0
    return client


class ICourseSessionRecoveryTests(unittest.TestCase):
    def setUp(self):
        cache = PROJECT_ROOT / "runtime" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=cache)
        self.service = CourseLensApplication(Path(self.temp.name))

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def _authenticate(self, student_id: str = "synthetic-user") -> _VerifiedClient:
        self.service.set_credentials(student_id, "synthetic-password")
        client = _VerifiedClient()
        with self.service._lock:
            self.service._client = client
            self.service._client_last_verified_at = time.monotonic()
        self.service._set_login_status(
            "ready", "icourse", "synthetic verified session", connected=True
        )
        return client

    def _wait_for_refresh(self) -> None:
        thread = self.service._catalog_refresh_thread
        self.assertIsNotNone(thread)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def _expire_probe_clock(self) -> None:
        with self.service._lock:
            self.service._client_last_verified_at = 0.0

    def test_probe_transport_failure_keeps_verified_session(self):
        # F1-R：探活传输类失败（夜间抖动同族）不得把会话判死重建。
        client = self._authenticate()
        self._expire_probe_clock()

        def _blip():
            raise requests.exceptions.SSLError("synthetic probe blip")

        client.check_alive = _blip
        resolved = self.service.client()
        self.assertIs(resolved, client)
        self.assertFalse(client.closed)

    def test_probe_gateway_5xx_keeps_verified_session(self):
        # F1-R：探活网关 5xx（夜间 502 窗口同族）死活不可判，保留会话。
        client = self._authenticate()
        self._expire_probe_clock()

        def _gateway_502():
            raise SessionUnverifiable("probe gateway status 502")

        client.check_alive = _gateway_502
        resolved = self.service.client()
        self.assertIs(resolved, client)
        self.assertFalse(client.closed)

    def test_icourse_check_alive_three_state(self):
        vpn = Mock()
        client = ICourseClient(vpn)
        vpn.get.side_effect = requests.exceptions.SSLError("synthetic")
        with self.assertRaises(SessionUnverifiable):
            client.check_alive()
        vpn.get.side_effect = None
        vpn.get.return_value = _json_response({}, status=502)
        with self.assertRaises(SessionUnverifiable):
            client.check_alive()
        vpn.get.return_value = _json_response({"code": 302})
        self.assertFalse(client.check_alive())
        vpn.get.return_value = _json_response({"code": 0})
        self.assertTrue(client.check_alive())

    def test_authoritative_probe_death_still_rebuilds_session(self):
        # F1-R 不越界：权威死亡证据仍照旧重建（会话纪元语义保留）。
        client = self._authenticate()
        self._expire_probe_clock()
        client.check_alive = lambda: False
        fresh = _VerifiedClient()
        with (
            patch.object(self.service, "_restore_client_from_checkpoint", return_value=None),
            patch.object(self.service, "_preflight_campus_route", return_value=None),
            patch.object(self.service, "_login_with_retry", return_value=fresh),
        ):
            resolved = self.service.client()
        self.assertIs(resolved, fresh)
        self.assertTrue(client.closed)

    def test_catalog_failure_preserves_verified_session_and_same_identity_cache(self):
        client = self._authenticate()
        self.service.catalog_repository.upsert_course(
            "course-safe", "Synthetic", "Teacher", authorization_state="verified"
        )
        self.service._store_authorized_catalog([{"course_id": "course-safe"}])

        with patch.object(
            self.service,
            "_load_authorized_catalog",
            side_effect=CatalogRefreshError("catalog_route_unavailable"),
        ):
            accepted = self.service.refresh_authorized_catalog_async()
            self.assertEqual(accepted["state"], "ready")
            self._wait_for_refresh()

        authentication = self.service.authentication_snapshot()
        catalog = self.service.authorized_catalog_snapshot(page=1, page_size=10)
        self.assertEqual(authentication["state"], "ready")
        self.assertFalse(client.closed)
        self.assertEqual(catalog["state"], "degraded")
        self.assertEqual(catalog["code"], "catalog_route_unavailable")
        self.assertEqual(catalog["actions"], ["refresh-catalog", "diagnose-network"])
        self.assertEqual([item["course_id"] for item in catalog["courses"]], ["course-safe"])

    def test_confirmed_catalog_session_expiry_requires_login_and_clears_tokens(self):
        client = self._authenticate()
        with patch.object(
            self.service,
            "_load_authorized_catalog",
            side_effect=CatalogRefreshError("catalog_session_expired"),
        ):
            self.service.refresh_authorized_catalog_async()
            self._wait_for_refresh()

        authentication = self.service.authentication_snapshot()
        self.assertTrue(client.closed)
        self.assertEqual(authentication["state"], "degraded")
        self.assertEqual(authentication["code"], "fudan_session_expired")
        self.assertEqual(authentication["actions"], ["login"])

    def test_catalog_route_fallback_reuses_bearer_without_authentication(self):
        self._authenticate()
        direct = Mock()
        direct.close = Mock()
        fallback = Mock()
        fallback.list_authorized_courses.return_value = [{"course_id": "course-safe"}]
        base = Mock()
        base.list_authorized_courses.side_effect = requests.ConnectionError("synthetic")
        base.check_alive.return_value = True
        base.with_catalog_session.return_value = fallback

        with (
            patch.object(self.service.network, "service_proxies", return_value=[""]),
            patch("src.api.icourse_direct.DirectICourseSession", return_value=direct),
        ):
            values, route_class, failures = self.service._load_authorized_catalog(base)

        self.assertEqual(values, [{"course_id": "course-safe"}])
        self.assertEqual(route_class, "direct")
        self.assertEqual(failures, ["catalog_route_unavailable"])
        base.with_catalog_session.assert_called_once_with(direct)
        direct.close.assert_called_once_with()

    def test_identity_change_during_refresh_cannot_publish_old_catalog(self):
        client = self._authenticate("synthetic-first")
        self.service.catalog_repository.upsert_course(
            "existing", "Existing", authorization_state="verified"
        )
        self.service._store_authorized_catalog([{"course_id": "existing"}])

        def switch_identity(_client):
            self.service.set_credentials("synthetic-second", "synthetic-password")
            return ([{"course_id": "old-session-course", "lectures": []}], "webvpn", [])

        with patch.object(self.service, "_load_authorized_catalog", side_effect=switch_identity):
            with self.assertRaisesRegex(CatalogRefreshError, "catalog_identity_mismatch"):
                self.service.discover_authorized_courses()

        self.assertTrue(client.closed)
        self.assertNotIn(
            "old-session-course",
            {item["course_id"] for item in self.service.catalog_repository.courses()},
        )
        self.assertEqual(self.service.authorized_catalog_snapshot()["courses"], [])

    def test_old_refresh_cannot_overwrite_new_identity_status(self):
        self._authenticate("synthetic-first")
        entered = threading.Event()
        release = threading.Event()

        def delayed_failure(_client):
            entered.set()
            self.assertTrue(release.wait(timeout=3))
            raise CatalogRefreshError("catalog_route_unavailable")

        with patch.object(self.service, "_load_authorized_catalog", side_effect=delayed_failure):
            self.service.refresh_authorized_catalog_async()
            self.assertTrue(entered.wait(timeout=3))
            self.service.set_credentials("synthetic-second", "synthetic-password")
            release.set()
            self._wait_for_refresh()

        with self.service._lock:
            catalog_status = dict(self.service._catalog_status)
        self.assertEqual(catalog_status["state"], "idle")
        self.assertEqual(catalog_status["code"], "fudan_login_required")

    def test_platform_login_does_not_start_a_second_direct_password_cas(self):
        class FakeVPN:
            AUTH_DEADLINE_SECONDS = 30

            def __init__(self, **_kwargs):
                self.session = Mock()
                self.login_calls = 0
                self.icourse_calls = 0

            def begin_authentication(self, _seconds):
                return None

            def end_authentication(self):
                return None

            def login(self, **_kwargs):
                self.login_calls += 1

            def authenticate_icourse(self, **_kwargs):
                self.icourse_calls += 1

        self.service.set_credentials("synthetic-user", "synthetic-password")
        with (
            patch("src.api.webvpn.WebVPNSession", FakeVPN),
            patch("src.api.icourse_direct.DirectICourseSession.login") as direct_login,
        ):
            client = self.service._login_with_retry(max_attempts=1)

        self.assertIsNone(client.catalog_session)
        self.assertEqual(client.vpn.login_calls, 1)
        self.assertEqual(client.vpn.icourse_calls, 1)
        direct_login.assert_not_called()

    # --- 合同 §5-1（2026-09-07 梯子1修复）：webvpn 会话原生访问目录 ---

    def test_webvpn_route_without_serialized_bearer_reaches_the_catalog(self):
        """登录成功且无 _token Cookie 时，webvpn 路由不带 Authorization 完成
        月度索引与详情验证（有界真实探针 2026-09-07 证明上游接受该形态）。"""
        vpn = _AliveSessionVPN()
        client = _authenticated_client_without_bearer(vpn)
        values, route_class, failures = self.service._load_authorized_catalog(client)

        self.assertEqual(route_class, "webvpn")
        self.assertEqual(failures, [])
        self.assertEqual([item["course_id"] for item in values], ["c1"])
        self.assertEqual(values[0]["authorization_state"], "verified")
        month_calls = [
            kwargs for url, kwargs in vpn.calls if "get-my-course-month" in url
        ]
        self.assertTrue(month_calls)
        for kwargs in month_calls:
            self.assertIn(kwargs.get("headers"), (None, {}))
        detail_calls = [
            kwargs for url, kwargs in vpn.calls if "get-course-detail" in url
        ]
        self.assertTrue(detail_calls)
        for kwargs in detail_calls:
            self.assertIn(kwargs.get("headers"), (None, {}))

    def test_direct_route_still_requires_bearer_after_webvpn_failure(self):
        """webvpn 路由失败（会话仍存活）后，direct 备选仍强制 bearer：
        提取失败即 catalog_bearer_missing，且绝无第二次密码 CAS。"""
        vpn = _AliveSessionVPN(month_family="refuse")
        client = _authenticated_client_without_bearer(vpn)
        direct = Mock()
        direct.get = Mock()
        direct.login = Mock()
        direct.adopt_authorization = Mock()

        with (
            patch.object(self.service.network, "service_proxies", return_value=[""]),
            patch("src.api.icourse_direct.DirectICourseSession", return_value=direct),
        ):
            with self.assertRaises(CatalogRefreshError) as caught:
                self.service._load_authorized_catalog(client)

        self.assertEqual(caught.exception.code, "catalog_bearer_missing")
        # direct 会话的失败发生在 with_catalog_session 的授权提取处：
        # adopt 之前、任何 direct HTTP 之前，更不会退回第二次密码 CAS。
        direct.get.assert_not_called()
        direct.adopt_authorization.assert_not_called()
        direct.login.assert_not_called()
        # webvpn 路由确实先发出过目录请求（fail-closed 于上游传输失败）。
        self.assertTrue(
            any("get-my-course-month" in url for url, _kwargs in vpn.calls)
        )


    def test_identity_mismatch_route_is_terminal_without_second_path(self):
        """P0.2：身份不匹配与路径无关——立即终止路由循环，绝不换路径重试。"""
        self._authenticate()
        base = Mock()
        base.list_authorized_courses.side_effect = _CodedCatalogError(
            "catalog_identity_mismatch"
        )
        base.check_alive.return_value = True
        base.with_catalog_session.return_value = base

        with (
            patch.object(self.service.network, "service_proxies", return_value=["", "http://127.0.0.1:6268"]),
            patch("src.api.icourse_direct.DirectICourseSession"),
        ):
            with self.assertRaises(CatalogRefreshError) as caught:
                self.service._load_authorized_catalog(base)

        self.assertEqual(caught.exception.code, "catalog_identity_mismatch")
        base.with_catalog_session.assert_not_called()

    def test_transport_timeout_on_path_failure_invalidates_cached_decision(self):
        """路径候选（直连/代理）超时让决策缓存失效；webvpn 候选与路径无关。"""
        self._authenticate()
        base = Mock()
        base.list_authorized_courses.side_effect = _CodedCatalogError("catalog_timeout")
        base.check_alive.return_value = True
        base.with_catalog_session.return_value = base

        with patch.object(self.service.network, "note_route_failure") as note:
            with self.assertRaises(CatalogRefreshError):
                self.service._load_authorized_catalog(base)
        self.assertEqual(
            note.call_args_list,
            [unittest.mock.call("icourse", ""), unittest.mock.call("icourse", "http://127.0.0.1:6268")],
            "只有直连/代理候选的失败才触发决策失效",
        )

    def test_cooldown_suppression_keeps_decision_cache_stable(self):
        """P3.1：冷却窗口内的重复路径失败被抑制——决策缓存不被逐请求打掉。"""
        self._authenticate()
        # 上一episode刚翻转过：冷却已武装。
        self.assertTrue(self.service.network.note_route_failure("icourse", ""))
        # 决策缓存随后被无凭据探测重填（门户级直连健康）。
        with patch.object(
            NetworkSettings,
            "_probe",
            side_effect=lambda url, proxy, *, timeout=None: {
                "healthy": True, "latency_ms": 5,
            },
        ):
            self.service.network.refresh_route_decision("icourse")
        base = Mock()
        base.list_authorized_courses.side_effect = _CodedCatalogError("catalog_timeout")
        base.check_alive.return_value = True
        base.with_catalog_session.return_value = base

        with (
            patch.object(self.service.network, "service_proxies", return_value=["", "http://127.0.0.1:6268"]),
            patch("src.api.icourse_direct.DirectICourseSession"),
        ):
            with self.assertRaises(CatalogRefreshError):
                self.service._load_authorized_catalog(base)

        # 冷却抑制：重新缓存的决策没有被冷却窗口内的第二次失败打掉。
        self.assertTrue(
            self.service.network.route_decision_fresh("icourse"),
            "冷却窗口内的重复失败不得再次清除决策缓存",
        )


class _CodedCatalogError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _CheckpointVPN(WebVPNSession):
    """零网络检查点测试宿主：只改 logged_in，其余继承真实实现。"""

    def __init__(self, *, logged_in: bool = True):
        super().__init__()
        self.logged_in = logged_in


def _synthetic_cookies():
    return [
        {"name": "webvpn_session", "value": "synthetic-restore-cookie-a", "domain": CHECKPOINT_COOKIE_HOST, "path": "/"},
        {"name": "tenant_synthetic_icourse", "value": "synthetic-restore-cookie-b", "domain": CHECKPOINT_COOKIE_HOST, "path": "/"},
    ]


class SessionCheckpointCaptureTests(unittest.TestCase):
    """V5 捕获层：最小、主机限定、闭集封顶。"""

    def test_capture_only_returns_webvpn_host_cookies(self):
        vpn = _CheckpointVPN()
        jar = vpn.session.cookies
        jar.set("webvpn_session", "synthetic-a", domain=CHECKPOINT_COOKIE_HOST, path="/")
        jar.set("proxied_icourse_session", "synthetic-b", domain=CHECKPOINT_COOKIE_HOST, path="/")
        jar.set("cross_domain_cookie", "synthetic-c", domain="icourse.fudan.edu.cn", path="/")
        jar.set("another_cross_domain", "synthetic-d", domain="id.fudan.edu.cn", path="/")
        captured = vpn.capture_session_checkpoint_cookies()
        # jar 迭代序由实现决定（stdlib cookiejar 按 key 排序），断言只钉
        # 集合语义：恰两只主机 cookie，值/域/路径精确，跨域零混入。
        self.assertEqual(
            {item["name"]: item for item in captured},
            {
                "webvpn_session": {
                    "name": "webvpn_session", "value": "synthetic-a",
                    "domain": CHECKPOINT_COOKIE_HOST, "path": "/",
                },
                "proxied_icourse_session": {
                    "name": "proxied_icourse_session", "value": "synthetic-b",
                    "domain": CHECKPOINT_COOKIE_HOST, "path": "/",
                },
            },
        )

    def test_capture_requires_logged_in_session(self):
        vpn = _CheckpointVPN(logged_in=False)
        vpn.session.cookies.set("webvpn_session", "synthetic-a", domain=CHECKPOINT_COOKIE_HOST, path="/")
        self.assertEqual(vpn.capture_session_checkpoint_cookies(), [])

    def test_capture_drops_crlf_values_and_caps_count(self):
        vpn = _CheckpointVPN()
        jar = vpn.session.cookies
        jar.set("crlf_cookie", "bad\r\nvalue", domain=CHECKPOINT_COOKIE_HOST, path="/")
        jar.set("ok_cookie", "synthetic-ok", domain=CHECKPOINT_COOKIE_HOST, path="/")
        # bulk 前缀取 zz_，使 ok_cookie 在 key 排序与插入序下都先于全部
        # bulk 候选——断言不依赖 jar 迭代序，封顶语义不变。
        for index in range(CHECKPOINT_MAX_COOKIES + 4):
            jar.set(f"zz_bulk_{index}", f"synthetic-{index}", domain=CHECKPOINT_COOKIE_HOST, path="/")
        captured = vpn.capture_session_checkpoint_cookies()
        names = {item["name"] for item in captured}
        self.assertEqual(len(captured), CHECKPOINT_MAX_COOKIES)
        self.assertNotIn("crlf_cookie", names, "CRLF 值必须被剔除")
        self.assertIn("ok_cookie", names, "CRLF 值必须被剔除且不计入封顶")
        self.assertTrue(
            all("\r" not in item["value"] and "\n" not in item["value"] for item in captured)
        )

    def test_capture_emits_no_telemetry(self):
        callback = Mock()
        vpn = _CheckpointVPN()
        vpn._step_callback = callback
        vpn.session.cookies.set("webvpn_session", "synthetic-a", domain=CHECKPOINT_COOKIE_HOST, path="/")
        self.assertTrue(vpn.capture_session_checkpoint_cookies())
        callback.assert_not_called()


class SessionCheckpointRestoreTests(unittest.TestCase):
    """V5 恢复层：安装回会话、双探针裁决、短路失败、形状问题不出网即拒。"""

    def test_restore_installs_cookies_into_session(self):
        vpn = _CheckpointVPN()
        vpn._verify_webvpn_session = Mock(return_value=True)
        vpn._verify_icourse_session = Mock(return_value=True)
        self.assertTrue(vpn.restore_session_checkpoint(_synthetic_cookies()))
        jar = vpn.session.cookies
        self.assertEqual(jar.get("webvpn_session"), "synthetic-restore-cookie-a")
        self.assertEqual(jar.get("tenant_synthetic_icourse"), "synthetic-restore-cookie-b")
        vpn._verify_webvpn_session.assert_called_once()
        vpn._verify_icourse_session.assert_called_once()

    def test_restore_short_circuits_when_webvpn_probe_rejects(self):
        vpn = _CheckpointVPN()
        vpn._verify_webvpn_session = Mock(return_value=False)
        vpn._verify_icourse_session = Mock(return_value=True)
        self.assertFalse(vpn.restore_session_checkpoint(_synthetic_cookies()))
        vpn._verify_webvpn_session.assert_called_once()
        vpn._verify_icourse_session.assert_not_called()

    def test_restore_rejects_empty_or_malformed_payload_without_probes(self):
        vpn = _CheckpointVPN()
        vpn._verify_webvpn_session = Mock()
        vpn._verify_icourse_session = Mock()
        for bad in ([], "not-a-list", [{"name": "", "value": "x", "domain": "d", "path": "/"}]):
            with self.subTest(bad=bad):
                self.assertFalse(vpn.restore_session_checkpoint(bad))
        vpn._verify_webvpn_session.assert_not_called()
        vpn._verify_icourse_session.assert_not_called()

    def test_restore_of_captured_cookies_round_trips_in_memory(self):
        vpn = _CheckpointVPN()
        vpn.session.cookies.set("webvpn_session", "synthetic-roundtrip", domain=CHECKPOINT_COOKIE_HOST, path="/")
        captured = vpn.capture_session_checkpoint_cookies()

        fresh = _CheckpointVPN()
        fresh._verify_webvpn_session = Mock(return_value=True)
        fresh._verify_icourse_session = Mock(return_value=True)
        self.assertTrue(fresh.restore_session_checkpoint(captured))
        self.assertEqual(fresh.session.cookies.get("webvpn_session"), "synthetic-roundtrip")


if __name__ == "__main__":
    unittest.main()
