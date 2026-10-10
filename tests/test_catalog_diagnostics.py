"""Catalog payload diagnostics: closed-set reasons, counters, and leak negatives.

Locks the current ``catalog_payload_invalid`` fold behaviour (many unrelated
failure classes share one public code) and proves the new in-memory closed-set
diagnostics expose only counts, booleans, and closed labels — never URLs,
field values, or course data.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

from path_utils import PROJECT_ROOT
from src.api.icourse import CATALOG_DIAGNOSTIC_REASONS, ICourseClient
from src.application import (
    _CATALOG_DIAGNOSTIC_EXCEPTIONS,
    _CATALOG_DIAGNOSTIC_KEYS,
    _CATALOG_DIAGNOSTIC_ROUTE_CLASSES,
    _CATALOG_ERROR_CODES,
    CatalogRefreshError,
    CourseLensApplication,
    _catalog_error_code,
    _closed_catalog_diagnostics,
)


class _FakeResponse:
    def __init__(self, payload=None, *, status=200, json_error=False):
        self._payload = payload
        self.status_code = status
        self._json_error = json_error

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"synthetic status {self.status_code}")
            error.response = self
            raise error

    def json(self):
        if self._json_error:
            raise json.JSONDecodeError("synthetic", "<html>", 0)
        return self._payload


class _FakeVPN:
    """Routes by URL substring; replays the last queued response per family."""

    def __init__(self, *, userinfo=None, months=None, details=None):
        self.session = SimpleNamespace(cookies=[])
        self._userinfo = userinfo or _FakeResponse({"code": 0, "params": {}})
        self._months = list(months or [])
        self._details = list(details or [])
        self._month_replay: list[_FakeResponse] = []
        self._detail_replay: list[_FakeResponse] = []

    def _next(self, queue, replay):
        if queue:
            value = queue.pop(0)
            replay.append(value)
            return value
        return replay[-1] if replay else self._userinfo

    def get(self, url, **_kwargs):
        if "infosimple" in url:
            return self._userinfo
        if "get-my-course-month" in url:
            return self._next(self._months, self._month_replay)
        if "get-course-detail" in url:
            return self._next(self._details, self._detail_replay)
        raise AssertionError("unexpected URL family")


def _client(vpn: _FakeVPN) -> ICourseClient:
    client = ICourseClient.__new__(ICourseClient)
    client.vpn = vpn
    client.catalog_session = None
    client.base_url = "https://icourse.invalid"
    client._userinfo = {"id": "u1", "account": "acc1", "tenant_id": "t1"}
    client.last_catalog_diagnostics = {}
    client._index_months_ok = 0
    client._index_rows = 0
    client._authorization_headers = lambda *, required=False: {
        "Authorization": "Bearer synthetic-test-token"
    }
    return client


def _month_rows():
    """14 identical months, each carrying the same two candidate courses."""
    course = [
        {"id": "c1", "title": "t1", "sub_id": "s1", "sub_duration": 60},
        {"id": "c2", "title": "t2", "sub_id": "s2", "sub_duration": 90},
    ]
    return [_FakeResponse({"code": 0, "list": [{"course": course}]}) for _ in range(14)]


class CatalogErrorCodeFoldTests(unittest.TestCase):
    """Lock: the public code folds many unrelated exception classes together."""

    def test_non_requests_exceptions_fold_to_payload_invalid(self):
        for exc in (
            RuntimeError("index rejected the request"),
            json.JSONDecodeError("synthetic", "<html>", 0),
            AttributeError("'list' object has no attribute 'items'"),
            KeyError("data"),
            TypeError("not a dict"),
        ):
            self.assertEqual(_catalog_error_code(exc), "catalog_payload_invalid")

    def test_requests_timeout_and_connection_classify(self):
        self.assertEqual(_catalog_error_code(requests.Timeout("t")), "catalog_timeout")
        self.assertEqual(
            _catalog_error_code(requests.ConnectionError("c")),
            "catalog_route_unavailable",
        )

    def test_direct_session_codes_map_through_aliases(self):
        from src.api.icourse_direct import DirectICourseError

        self.assertEqual(
            _catalog_error_code(DirectICourseError("timeout")), "catalog_timeout"
        )
        self.assertEqual(
            _catalog_error_code(DirectICourseError("network_unavailable")),
            "catalog_route_unavailable",
        )


class CourseDetailEnvelopeTests(unittest.TestCase):
    def _client(self, responses):
        vpn = _FakeVPN(details=responses)
        return _client(vpn)

    def test_nonzero_code_annotates_reason_and_kind(self):
        client = self._client([_FakeResponse({"code": 200, "msg": "ok", "data": {}})])
        with self.assertRaises(RuntimeError) as caught:
            client.get_course_detail("c1", student="acc1")
        self.assertEqual(caught.exception.catalog_reason, "detail_code_rejected")
        self.assertEqual(caught.exception.catalog_code_kind, "200")

    def test_other_nonzero_code_kind(self):
        client = self._client([_FakeResponse({"code": 401, "msg": "denied"})])
        with self.assertRaises(RuntimeError) as caught:
            client.get_course_detail("c1", student="acc1")
        self.assertEqual(caught.exception.catalog_code_kind, "other")

    def test_html_body_annotates_json_invalid(self):
        client = self._client([_FakeResponse(json_error=True)])
        with self.assertRaises(ValueError) as caught:
            client.get_course_detail("c1", student="acc1")
        self.assertEqual(caught.exception.catalog_reason, "detail_json_invalid")

    def test_http_error_annotates_http_reason(self):
        client = self._client([_FakeResponse(status=503)])
        with self.assertRaises(requests.RequestException) as caught:
            client.get_course_detail("c1", student="acc1")
        self.assertEqual(caught.exception.catalog_reason, "detail_http_error")

    def test_detail_timeout_stays_unannotated(self):
        class _TimeoutResponse(_FakeResponse):
            def raise_for_status(self):
                raise requests.Timeout("synthetic deadline")

        client = self._client([_TimeoutResponse()])
        with self.assertRaises(requests.Timeout):
            client.get_course_detail("c1", student="acc1")


class BearerClassificationTests(unittest.TestCase):
    """The instant pre-request bearer failure must classify honestly."""

    def test_bearer_unavailable_carries_closed_code(self):
        from src.api.icourse import BearerUnavailableError

        exc = BearerUnavailableError()
        self.assertIsInstance(exc, RuntimeError)
        self.assertEqual(exc.code, "catalog_bearer_missing")
        self.assertEqual(exc.catalog_reason, "bearer_unavailable")
        self.assertEqual(_catalog_error_code(exc), "catalog_bearer_missing")

    def test_bearer_failure_surfaces_retry_action_not_relogin(self):
        """会话仍有效但载体不可读时（webvpn 路由失败 + direct 无 bearer），
        动作必须是重试而非重新认证，避免无效的重登循环。"""
        from src.api.icourse import BearerUnavailableError
        from src.application import CatalogRefreshError, _catalog_actions

        code = _catalog_error_code(BearerUnavailableError())
        self.assertEqual(code, "catalog_bearer_missing")
        self.assertEqual(_catalog_actions(code), ["refresh-catalog"])
        self.assertNotIn("login", _catalog_actions(code))
        self.assertEqual(CatalogRefreshError(code).code, "catalog_bearer_missing")

    def test_missing_bearer_no_longer_blocks_the_index_request(self):
        """梯子1（2026-09-07 有界真实探针）：webvpn 会话无 _token Cookie 时
        不再前置失败，月度索引照常发出且不带 Authorization。"""
        from src.api.icourse import ICourseClient as _Client

        observed: list[dict] = []
        vpn = _FakeVPN()
        original_get = vpn.get

        def counting_get(url, **kwargs):
            observed.append(dict(kwargs))
            return original_get(url, **kwargs)

        vpn.get = counting_get
        client = _client(vpn)
        client._authorization_headers = (
            ICourseClient._authorization_headers.__get__(client)
        )
        values = client.list_user_courses()
        # 请求已发出（旧的前置 bearer 边界已按梯子1移除）且零 Authorization。
        self.assertTrue(observed)
        for kwargs in observed:
            self.assertIn(kwargs.get("headers"), (None, {}))
        self.assertEqual(values, [])

    def test_route_aggregation_prefers_bearer_missing_over_payload_fold(self):
        from src.api.icourse import BearerUnavailableError
        from unittest.mock import Mock

        cache = PROJECT_ROOT / "runtime" / "cache"
        with tempfile.TemporaryDirectory(dir=cache) as temp:
            service = CourseLensApplication(Path(temp))
            try:
                base = Mock()
                base.list_authorized_courses.side_effect = BearerUnavailableError()
                base.check_alive.return_value = True
                base.with_catalog_session.side_effect = BearerUnavailableError()
                with patch.object(
                    service.network, "service_proxies", return_value=[""]
                ):
                    with self.assertRaises(CatalogRefreshError) as caught:
                        service._load_authorized_catalog(base)
                self.assertEqual(caught.exception.code, "catalog_bearer_missing")
            finally:
                service.close()


class DetailFailureClassifierTests(unittest.TestCase):
    """requests' JSONDecodeError is both ValueError and RequestException."""

    def test_requests_json_error_classifies_by_reason_not_transport(self):
        from src.api.icourse import ICourseClient as _Client

        exc = requests.exceptions.JSONDecodeError("synthetic", "<html>", 0)
        exc.catalog_reason = "detail_json_invalid"
        bucket, kind = _Client._classify_detail_failure(exc)
        self.assertEqual(bucket, "detail_fail_json")
        self.assertEqual(kind, "")

    def test_annotated_http_error_classifies_by_reason(self):
        from src.api.icourse import ICourseClient as _Client

        exc = requests.HTTPError("synthetic 503")
        exc.catalog_reason = "detail_http_error"
        self.assertEqual(
            _Client._classify_detail_failure(exc), ("detail_fail_http", "")
        )

    def test_plain_request_exceptions_stay_transport(self):
        from src.api.icourse import ICourseClient as _Client

        self.assertEqual(
            _Client._classify_detail_failure(requests.ConnectionError("c")),
            ("detail_fail_transport", ""),
        )
        self.assertEqual(
            _Client._classify_detail_failure(requests.Timeout("t")),
            ("detail_fail_transport_timeout", ""),
        )

    def test_shape_and_unknown_fall_through(self):
        from src.api.icourse import ICourseClient as _Client

        self.assertEqual(
            _Client._classify_detail_failure(AttributeError("x")),
            ("detail_fail_shape", ""),
        )
        self.assertEqual(
            _Client._classify_detail_failure(Exception("x")),
            ("detail_fail_other", ""),
        )


class UserIndexDiagnosticsTests(unittest.TestCase):
    def test_index_code_rejection_annotates_reason(self):
        vpn = _FakeVPN(months=[_FakeResponse({"code": 401, "msg": "auth"})])
        client = _client(vpn)
        with self.assertRaises(RuntimeError) as caught:
            client.list_user_courses()
        self.assertEqual(caught.exception.catalog_reason, "index_code_rejected")
        self.assertEqual(client._index_months_ok, 0)

    def test_index_shape_invalid_annotates_reason_after_partial_scan(self):
        vpn = _FakeVPN()
        state = {"calls": 0}

        def get(url, **_kwargs):
            if "infosimple" in url:
                return vpn._userinfo
            state["calls"] += 1
            if state["calls"] <= 3:
                return _FakeResponse({"code": 0, "list": []})
            return _FakeResponse({"code": 0, "list": "bad-shape"})

        vpn.get = get
        client = _client(vpn)
        with self.assertRaises(RuntimeError) as caught:
            client.list_user_courses()
        self.assertEqual(caught.exception.catalog_reason, "index_payload_shape_invalid")
        self.assertEqual(client._index_months_ok, 3)

    def test_identity_incomplete_annotates_reason(self):
        client = _client(_FakeVPN())
        client._userinfo = {"id": "", "account": "", "tenant_id": ""}
        with self.assertRaises(RuntimeError) as caught:
            client.list_user_courses()
        self.assertEqual(caught.exception.catalog_reason, "identity_incomplete")


class ListAuthorizedCoursesDiagnosticsTests(unittest.TestCase):
    def test_all_detail_failures_fold_to_payload_invalid_with_counts(self):
        vpn = _FakeVPN(
            months=_month_rows(),
            details=[_FakeResponse({"code": 200, "msg": "ok", "data": {}})],
        )
        client = _client(vpn)
        with self.assertRaises(RuntimeError) as caught:
            client.list_authorized_courses(deadline_seconds=30)
        self.assertEqual(
            str(caught.exception), "iCourse course detail verification failed"
        )
        self.assertEqual(
            _catalog_error_code(caught.exception), "catalog_payload_invalid"
        )
        diag = client.last_catalog_diagnostics
        self.assertEqual(diag["candidates"], 2)
        self.assertEqual(diag["detail_attempted"], 2)
        self.assertEqual(diag["detail_failed"], 2)
        self.assertEqual(diag["detail_fail_code_rejected"], 2)
        self.assertEqual(diag["code_kind_200"], 2)
        self.assertEqual(diag["months_ok"], 14)

    def test_schema_drift_counts_as_shape_failure(self):
        drifted = {
            "code": 0,
            "data": {"title": "t", "realname": "r", "sub_list": [1, 2]},
        }
        vpn = _FakeVPN(months=_month_rows(), details=[_FakeResponse(drifted)])
        client = _client(vpn)
        with self.assertRaises(RuntimeError):
            client.list_authorized_courses(deadline_seconds=30)
        diag = client.last_catalog_diagnostics
        self.assertEqual(diag["detail_fail_shape"], 2)
        self.assertEqual(diag.get("code_kind_200", 0), 0)

    def test_detail_timeout_counts_as_transport_failure(self):
        class _TimeoutResponse(_FakeResponse):
            def raise_for_status(self):
                raise requests.Timeout("synthetic deadline")

        vpn = _FakeVPN(months=_month_rows(), details=[_TimeoutResponse()])
        client = _client(vpn)
        with self.assertRaises(RuntimeError):
            client.list_authorized_courses(deadline_seconds=30)
        diag = client.last_catalog_diagnostics
        self.assertEqual(diag["detail_fail_transport_timeout"], 2)

    def test_index_failure_mid_scan_reports_months_ok(self):
        vpn = _FakeVPN()
        state = {"calls": 0}

        def get(url, **_kwargs):
            if "infosimple" in url:
                return vpn._userinfo
            state["calls"] += 1
            if state["calls"] <= 2:
                return _FakeResponse({"code": 0, "list": []})
            return _FakeResponse({"code": 500, "msg": "boom"})

        vpn.get = get
        client = _client(vpn)
        with self.assertRaises(RuntimeError) as caught:
            client.list_authorized_courses(deadline_seconds=30)
        self.assertEqual(caught.exception.catalog_reason, "index_code_rejected")
        diag = client.last_catalog_diagnostics
        self.assertEqual(diag["months_ok"], 2)
        self.assertEqual(diag["candidates"], 0)

    def test_partial_detail_success_returns_verified_courses(self):
        def detail(index):
            return _FakeResponse({
                "code": 0,
                "data": {
                    "title": f"t{index}",
                    "realname": "r",
                    "sub_list": {"2026": {"09": {"01": [{"id": f"s{index}", "playback_status": "1"}]}}},
                },
            })

        vpn = _FakeVPN(
            months=_month_rows(),
            details=[detail(1), _FakeResponse({"code": 500, "msg": "boom"})],
        )
        client = _client(vpn)
        authorized = client.list_authorized_courses(deadline_seconds=30)
        self.assertEqual([item["course_id"] for item in authorized], ["c1"])
        diag = client.last_catalog_diagnostics
        self.assertEqual(diag["detail_ok"], 1)
        self.assertEqual(diag["detail_failed"], 1)


class ApplicationDiagnosticsPublicationTests(unittest.TestCase):
    def setUp(self):
        cache = PROJECT_ROOT / "runtime" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=cache)
        self.service = CourseLensApplication(Path(self.temp.name))

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def _authenticate(self, client):
        self.service.set_credentials("synthetic-user", "synthetic-password")
        with self.service._lock:
            self.service._client = client
            self.service._client_last_verified_at = time.monotonic()
        self.service._set_login_status("ready", "icourse", "synthetic", connected=True)

    def _wait_for_refresh(self):
        thread = self.service._catalog_refresh_thread
        self.assertIsNotNone(thread)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def _failing_client(self, diagnostics):
        def _raise(deadline_seconds):
            raise RuntimeError("iCourse course detail verification failed")

        return SimpleNamespace(
            check_alive=lambda: True,
            last_catalog_diagnostics=dict(diagnostics),
            list_authorized_courses=_raise,
        )

    def _degraded_snapshot_with(self, diagnostics):
        failing = self._failing_client(diagnostics)
        self._authenticate(failing)
        with patch.object(self.service, "client", return_value=failing):
            with patch.object(self.service.network, "service_proxies", return_value=[]):
                self.service.refresh_authorized_catalog_async()
                self._wait_for_refresh()
        return self.service.authorized_catalog_snapshot(page=1, page_size=10)

    def test_degraded_snapshot_publishes_closed_diagnostics(self):
        snapshot = self._degraded_snapshot_with({
            "months_ok": 14,
            "months_total": 14,
            "rows": 30,
            "candidates": 2,
            "detail_attempted": 2,
            "detail_ok": 0,
            "detail_failed": 2,
            "detail_fail_code_rejected": 2,
            "code_kind_200": 2,
        })
        self.assertEqual(snapshot["state"], "degraded")
        self.assertEqual(snapshot["code"], "catalog_payload_invalid")
        diagnostics = snapshot["diagnostics"]
        self.assertEqual(diagnostics["candidates"], 2)
        self.assertEqual(diagnostics["detail_fail_code_rejected"], 2)
        self.assertEqual(diagnostics["code_kind_200"], 2)
        self.assertEqual(diagnostics["months_ok"], 14)

    def test_mixed_route_success_publishes_winner_diagnostics(self):
        """webvpn 路由失败、direct 路由成功时：ready 发布只携带获胜路由的
        诊断记录，失败路由仅以闭集代码进入 partial_failures，不混入计数。"""
        verified = [{"course_id": "c1", "title": "t1", "lectures": []}]
        wrapped = SimpleNamespace(last_catalog_diagnostics={})

        def _succeed(deadline_seconds):
            wrapped.last_catalog_diagnostics = {
                "months_ok": 14, "months_total": 15, "rows": 30,
                "candidates": 1, "detail_attempted": 1,
                "detail_ok": 1, "detail_failed": 0,
            }
            return verified

        wrapped.list_authorized_courses = _succeed
        base = SimpleNamespace(
            check_alive=lambda: True,
            last_catalog_diagnostics={},
            with_catalog_session=lambda direct: wrapped,
        )

        def _fail_webvpn(deadline_seconds):
            base.last_catalog_diagnostics = {
                "candidates": 2, "detail_failed": 2,
                "detail_fail_code_rejected": 2,
            }
            raise RuntimeError("webvpn route payload failure")

        base.list_authorized_courses = _fail_webvpn
        self._authenticate(base)
        with patch.object(self.service, "client", return_value=base):
            with patch.object(self.service.network, "service_proxies", return_value=[""]):
                results = self.service.discover_authorized_courses()
        self.assertEqual([item["course_id"] for item in results], ["c1"])
        snapshot = self.service.authorized_catalog_snapshot(page=1, page_size=10)
        self.assertEqual(snapshot["state"], "ready")
        self.assertEqual(snapshot["code"], "authorized_catalog_verified")
        self.assertEqual(self.service._catalog_status["route_class"], "direct")
        self.assertEqual(snapshot["partial_failures"], ["catalog_payload_invalid"])
        diagnostics = snapshot["diagnostics"]
        self.assertEqual(diagnostics["route_class"], "direct")
        self.assertEqual(diagnostics["months_ok"], 14)
        self.assertEqual(diagnostics["candidates"], 1)
        self.assertEqual(diagnostics["detail_ok"], 1)
        # 失败路由的计数与错误封装不得混入获胜路由的发布记录。
        self.assertNotIn("detail_fail_code_rejected", diagnostics)
        self.assertNotIn("code", diagnostics)
        self.assertNotIn("exception_class", diagnostics)
        # 发布消费一次：pending 不得残留或二次发布。
        self.assertEqual(self.service._pending_catalog_diagnostics, {})

    def test_action_required_snapshot_hides_diagnostics_when_auth_not_ready(self):
        """非 ready（action_required）快照不得发布任何目录诊断：
        已发布的 degraded 诊断与未消费的 pending 记录都不可见。"""
        snapshot = self._degraded_snapshot_with({
            "candidates": 1,
            "detail_failed": 1,
            "detail_fail_code_rejected": 1,
        })
        self.assertIn("diagnostics", snapshot)
        with self.service._lock:
            self.service._client = None
            self.service._client_last_verified_at = 0.0
        self.service._stash_catalog_route_diagnostics({"candidates": 9})
        hidden = self.service.authorized_catalog_snapshot(page=1, page_size=10)
        self.assertEqual(hidden["state"], "action_required")
        self.assertNotIn("diagnostics", hidden)
        self.assertEqual(hidden["courses"], [])

    def test_publish_without_diagnostics_carries_no_key_and_consumes_once(self):
        """空诊断的发布不得携带 diagnostics 键；pending 记录消费一次即清空，
        且 reset（登出路径）必须清空 pending，后续发布不得复活旧记录。"""
        self.service._stash_catalog_route_diagnostics({
            "route_class": "webvpn", "code": "catalog_timeout", "candidates": 3,
        })
        self.service._set_catalog_status("ready", "authorized_catalog_verified")
        self.assertNotIn("diagnostics", self.service._catalog_status)
        consumed = self.service._consume_catalog_route_diagnostics()
        self.assertEqual(consumed["route_class"], "webvpn")
        self.assertEqual(consumed["code"], "catalog_timeout")
        self.assertEqual(consumed["candidates"], 3)
        self.assertEqual(self.service._consume_catalog_route_diagnostics(), {})
        self.service._stash_catalog_route_diagnostics({"candidates": 5})
        self.service._reset_catalog_status()
        self.assertEqual(self.service._pending_catalog_diagnostics, {})
        self.assertNotIn("diagnostics", self.service._catalog_status)

    def test_snapshot_diagnostics_never_carry_long_or_course_strings(self):
        snapshot = self._degraded_snapshot_with({
            "candidates": 1,
            "detail_failed": 1,
            "detail_fail_code_rejected": 1,
        })
        diagnostics = snapshot["diagnostics"]
        allowed_labels = (
            set(_CATALOG_DIAGNOSTIC_ROUTE_CLASSES)
            | set(_CATALOG_DIAGNOSTIC_EXCEPTIONS)
            | set(CATALOG_DIAGNOSTIC_REASONS)
            | set(_CATALOG_ERROR_CODES)
            | {"", "other"}
        )
        label_keys = {"route_class", "code", "exception_class", "reason"}
        for key, value in diagnostics.items():
            self.assertIn(key, _CATALOG_DIAGNOSTIC_KEYS)
            if key in label_keys:
                self.assertIsInstance(value, str)
                self.assertIn(value, allowed_labels)
            else:
                self.assertIsInstance(value, (int, bool))

    def test_diagnostics_normalizer_is_closed_and_value_free(self):
        raw = {
            "route_class": "webvpn",
            "code": "catalog_payload_invalid",
            "exception_class": "RuntimeError",
            "reason": "detail_code_rejected",
            "elapsed_ms": 1234,
            "candidates": 2,
            "url": "https://secret.example/x?ticket=y",
            "course_title": "should disappear",
            "unknown_label": "something",
        }
        closed = _closed_catalog_diagnostics(raw)
        self.assertTrue(set(closed).issubset(_CATALOG_DIAGNOSTIC_KEYS))
        self.assertNotIn("url", closed)
        self.assertNotIn("course_title", closed)
        self.assertNotIn("unknown_label", closed)
        self.assertEqual(closed["exception_class"], "RuntimeError")
        self.assertEqual(closed["reason"], "detail_code_rejected")
        self.assertEqual(closed["code"], "catalog_payload_invalid")
        clamped = _closed_catalog_diagnostics({
            "elapsed_ms": -5,
            "candidates": 10**12,
        })
        self.assertEqual(clamped["elapsed_ms"], 0)
        self.assertEqual(clamped["candidates"], 1_000_000)
        self.assertEqual(
            _closed_catalog_diagnostics({"candidates": "course-title-leak"}), {}
        )
        self.assertEqual(_closed_catalog_diagnostics("garbage"), {})
        self.assertEqual(_closed_catalog_diagnostics(None), {})

    def test_identity_mismatch_still_blocks_publication(self):
        failing = self._failing_client({"candidates": 1})
        self._authenticate(failing)

        def load(_client):
            with self.service._lock:
                self.service._catalog_generation += 1
            return [{"course_id": "c1", "lectures": []}], "webvpn", []

        with patch.object(self.service, "client", return_value=failing):
            with patch.object(
                self.service, "_load_authorized_catalog", side_effect=load
            ):
                with self.assertRaises(CatalogRefreshError):
                    self.service.discover_authorized_courses()
        snapshot = self.service.authorized_catalog_snapshot()
        self.assertNotEqual(snapshot["code"], "authorized_catalog_verified")
        self.assertEqual(snapshot["courses"], [])


if __name__ == "__main__":
    unittest.main()
