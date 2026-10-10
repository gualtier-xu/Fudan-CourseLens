"""Closed-set catalog probes: shape labels, bounded counts, and leak negatives.

Locks the contract §4B/§4C probe invariants:
- outputs carry only booleans, closed labels, and bounded counts;
- the index/detail probes never send an Authorization header;
- the orchestrator keeps the bounded request budget (2 identity + 1 index +
  at most 1 detail) and fails closed at the first boundary.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from urllib.parse import quote, urlparse

import requests

from src.api.catalog_probe import (
    BEARER_SHAPES,
    DAY_COUNT_CAP,
    DECODE_LAYER_CAP,
    DOMAIN_KINDS,
    LENGTH_BUCKETS,
    LECTURE_COUNT_CAP,
    PROBE_SCHEMA_VERSION,
    ROW_COUNT_CAP,
    TOKEN_SHAPE_SUMMARIES,
    classify_bearer_shape,
    cookie_jar_shape,
    probe_detail_without_authorization,
    probe_identity,
    probe_index_without_authorization,
    run_closed_catalog_probe,
)
from src.runtime import config

_PHP = '{i:1;s:6:"_token";i:2;s:21:"synthetic-bearer-token";}'
_JWT = "eyJhbGciOiJI.eyJzdWIiOiIxMjM0NTY3ODkw.c2lnbmF0dXJlLXNlY3JldA"
_HEX32 = "0123456789abcdef0123456789abcdef"


class _FakeResponse:
    def __init__(self, payload=None, *, status=200, json_error=False,
                 content_type="application/json"):
        self._payload = payload
        self.status_code = status
        self._json_error = json_error
        self.headers = {"Content-Type": content_type} if content_type else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"synthetic status {self.status_code}")
            error.response = self
            raise error

    def json(self):
        if self._json_error:
            raise json.JSONDecodeError("synthetic", "<html>", 0)
        return self._payload


class _RecordingVPN:
    """按 URL 族回放响应，并记录每次 get 的 kwargs（含是否带 headers）。"""

    def __init__(self, *, userinfo=None, months=None, details=None,
                 raise_exc=None):
        self.session = SimpleNamespace(cookies=[])
        self.calls: list[tuple[str, dict]] = []
        self._userinfo = userinfo or _FakeResponse({
            "code": 0, "params": {"id": "u1", "account": "acc1"},
        })
        self._months = list(months or [])
        self._details = list(details or [])
        self._raise_exc = raise_exc

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self._raise_exc is not None:
            raise self._raise_exc
        if "infosimple" in url:
            return self._userinfo
        if "get-my-course-month" in url:
            return self._months.pop(0) if self._months else _FakeResponse({"code": 0, "list": []})
        if "get-course-detail" in url:
            return self._details.pop(0) if self._details else _FakeResponse({"code": 0, "data": {}})
        raise AssertionError("unexpected URL family")


class _FakeClient:
    base_url = "https://icourse.invalid"
    CATALOG_TIMEOUT = (1, 2)

    def __init__(self, vpn):
        self.vpn = vpn


class ClassifyBearerShapeTests(unittest.TestCase):
    def _check_schema(self, facts):
        self.assertEqual(
            set(facts), {"shape", "decode_layers", "length_bucket", "has_crlf"}
        )
        self.assertIn(facts["shape"], BEARER_SHAPES)
        self.assertIn(facts["length_bucket"], LENGTH_BUCKETS)
        self.assertTrue(0 <= facts["decode_layers"] <= DECODE_LAYER_CAP)
        self.assertIsInstance(facts["has_crlf"], bool)

    def test_php_serialized_shape_at_multiple_decode_layers(self):
        self._check_schema(facts := classify_bearer_shape(_PHP))
        self.assertEqual(facts["shape"], "php_serialized")
        self.assertEqual(facts["decode_layers"], 0)
        once = classify_bearer_shape(quote(_PHP))
        self._check_schema(once)
        self.assertEqual(once["shape"], "php_serialized")
        self.assertEqual(once["decode_layers"], 1)
        twice = classify_bearer_shape(quote(quote(_PHP)))
        self.assertEqual(twice["shape"], "php_serialized")
        self.assertEqual(twice["decode_layers"], 2)

    def test_jwt_json_and_opaque_shapes(self):
        self.assertEqual(classify_bearer_shape(_JWT)["shape"], "jwt_like")
        json_value = quote('{"_token":"synthetic-bearer-token"}')
        facts = classify_bearer_shape(json_value)
        self.assertEqual(facts["shape"], "json")
        self.assertEqual(facts["decode_layers"], 1)
        self.assertEqual(classify_bearer_shape(_HEX32)["shape"], "opaque")
        self.assertEqual(classify_bearer_shape(_HEX32)["length_bucket"], "16_64")

    def test_unknown_shapes_cover_short_long_and_crlf(self):
        short = classify_bearer_shape("short")
        self.assertEqual(short["shape"], "unknown")
        self.assertEqual(short["length_bucket"], "lt16")
        long_value = classify_bearer_shape("A" * 4097)
        self.assertEqual(long_value["shape"], "unknown")
        self.assertEqual(long_value["length_bucket"], "gt4096")
        crlf = classify_bearer_shape("A" * 32 + "\r\n" + "B" * 32)
        self.assertEqual(crlf["shape"], "unknown")
        self.assertTrue(crlf["has_crlf"])

    def test_length_bucket_boundaries(self):
        for length, bucket in (
            (15, "lt16"), (16, "16_64"), (64, "16_64"), (65, "65_256"),
            (256, "65_256"), (257, "257_1024"), (1024, "257_1024"),
            (1025, "1025_4096"), (4096, "1025_4096"), (4097, "gt4096"),
        ):
            facts = classify_bearer_shape("A" * length)
            self.assertEqual(facts["length_bucket"], bucket)

    def test_facts_never_carry_the_value(self):
        for value in (_PHP, _JWT, _HEX32, quote(_PHP)):
            dumped = json.dumps(classify_bearer_shape(value))
            self.assertNotIn("synthetic", dumped)
            self.assertNotIn("bearer-token", dumped)


class CookieJarShapeTests(unittest.TestCase):
    def _host(self, base: str) -> str:
        return urlparse(base).hostname

    def test_counts_tokens_and_domain_kinds_without_names(self):
        jar = [
            SimpleNamespace(name="_token", value=_PHP,
                            domain=self._host(config.ICOURSE_BASE)),
            SimpleNamespace(name="SESSIONID", value="opaque-session-id",
                            domain="." + self._host(config.IDP_BASE)),
            SimpleNamespace(name="_token", value=_JWT,
                            domain=self._host(config.WEBVPN_BASE)),
            SimpleNamespace(name="stray", value="x", domain="example.org"),
        ]
        shape = cookie_jar_shape(jar)
        self.assertEqual(shape["cookie_total"], 4)
        self.assertTrue(shape["token_cookie_present"])
        self.assertEqual(shape["token_cookie_count"], 2)
        self.assertEqual(shape["token_shape"], "mixed")
        self.assertEqual(shape["token_shapes"], ["jwt_like", "php_serialized"])
        self.assertEqual(shape["domain_kind_icourse"], 1)
        self.assertEqual(shape["domain_kind_idp"], 1)
        self.assertEqual(shape["domain_kind_webvpn"], 1)
        self.assertEqual(shape["domain_kind_other"], 1)

    def test_missing_token_reports_missing_summary(self):
        shape = cookie_jar_shape([
            SimpleNamespace(name="SESSIONID", value="x",
                            domain=self._host(config.ICOURSE_BASE)),
        ])
        self.assertFalse(shape["token_cookie_present"])
        self.assertEqual(shape["token_shape"], "missing")
        self.assertEqual(shape["token_shapes"], [])
        self.assertEqual(cookie_jar_shape([])["cookie_total"], 0)

    def test_output_schema_is_closed_and_value_free(self):
        shape = cookie_jar_shape([
            SimpleNamespace(name="_token", value=_PHP,
                            domain=self._host(config.ICOURSE_BASE)),
            SimpleNamespace(name="secret-name", value="secret-value",
                            domain="example.org"),
        ])
        allowed_keys = {
            "cookie_total", "token_cookie_present", "token_cookie_count",
            "token_shape", "token_shapes",
            "domain_kind_icourse", "domain_kind_idp",
            "domain_kind_webvpn", "domain_kind_other",
        }
        self.assertEqual(set(shape), allowed_keys)
        self.assertIn(shape["token_shape"], TOKEN_SHAPE_SUMMARIES)
        self.assertTrue(set(shape["token_shapes"]) <= BEARER_SHAPES)
        self.assertTrue(set(DOMAIN_KINDS) >= {"icourse", "idp", "webvpn", "other"})
        dumped = json.dumps(shape)
        self.assertNotIn("secret-name", dumped)
        self.assertNotIn("secret-value", dumped)
        self.assertNotIn("example.org", dumped)
        self.assertNotIn("synthetic", dumped)


class ProbeIndexTests(unittest.TestCase):
    def _month_payload(self, rows=None):
        return {"code": 0, "list": [{"course": rows or [
            {"id": "c-secret-1", "title": "secret-title"},
            {"id": "c2"},
        ]}]}

    def test_success_observations_and_no_authorization_header(self):
        vpn = _RecordingVPN(months=[_FakeResponse(self._month_payload())])
        client = _FakeClient(vpn)
        result = probe_index_without_authorization(client, month="2026-09")
        self.assertTrue(result["request_sent"])
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["status_kind"], "ok_2xx")
        self.assertEqual(result["content_type_kind"], "json")
        self.assertEqual(result["json_top_type"], "dict")
        self.assertEqual(result["code_kind"], "zero")
        self.assertTrue(result["list_present"])
        self.assertEqual(result["day_count"], 1)
        self.assertEqual(result["row_count"], 2)
        for _url, kwargs in vpn.calls:
            self.assertNotIn("headers", kwargs)

    def test_unauthorized_html_and_invalid_json_kinds(self):
        vpn = _RecordingVPN(months=[
            _FakeResponse({"code": 401}, status=401),
            _FakeResponse(json_error=True, content_type="text/html"),
            _FakeResponse(json_error=True, content_type="application/json"),
        ])
        client = _FakeClient(vpn)
        denied = probe_index_without_authorization(client, month="2026-09")
        self.assertEqual(denied["status_kind"], "unauthorized")
        self.assertEqual(denied["code_kind"], "other")
        html = probe_index_without_authorization(client, month="2026-09")
        self.assertEqual(html["content_type_kind"], "html")
        self.assertEqual(html["json_top_type"], "not_json")
        invalid = probe_index_without_authorization(client, month="2026-09")
        self.assertEqual(invalid["json_top_type"], "invalid")

    def test_transport_failures_classify_into_closed_outcomes(self):
        for exc, outcome in (
            (requests.Timeout("t"), "timeout"),
            (requests.ConnectionError("c"), "connection_error"),
            (requests.RequestException("r"), "request_error"),
        ):
            vpn = _RecordingVPN(raise_exc=exc)
            result = probe_index_without_authorization(
                _FakeClient(vpn), month="2026-09"
            )
            self.assertFalse(result["request_sent"])
            self.assertEqual(result["outcome"], outcome)

    def test_invalid_month_fails_closed_without_request(self):
        vpn = _RecordingVPN()
        result = probe_index_without_authorization(_FakeClient(vpn), month="9999-9")
        self.assertEqual(result["outcome"], "request_error")
        self.assertEqual(vpn.calls, [])

    def test_counts_are_capped(self):
        payload = {"code": 0, "list": [
            {"course": [{"id": f"c{i}"} for i in range(ROW_COUNT_CAP + 5)]}
            for _ in range(DAY_COUNT_CAP + 5)
        ]}
        vpn = _RecordingVPN(months=[_FakeResponse(payload)])
        result = probe_index_without_authorization(_FakeClient(vpn), month="2026-09")
        self.assertEqual(result["day_count"], DAY_COUNT_CAP)
        self.assertEqual(result["row_count"], ROW_COUNT_CAP)
        self.assertTrue(result["counts_capped"])


class ProbeDetailTests(unittest.TestCase):
    def test_success_observations_and_no_authorization_header(self):
        payload = {"code": 0, "data": {
            "title": "secret-title", "realname": "secret-teacher",
            "sub_list": {"2026": {"09": {"07": [
                {"id": f"s{i}"} for i in range(LECTURE_COUNT_CAP + 3)
            ]}}},
        }}
        vpn = _RecordingVPN(details=[_FakeResponse(payload)])
        result = probe_detail_without_authorization(_FakeClient(vpn), "c-secret-1")
        self.assertTrue(result["request_sent"])
        self.assertEqual(result["code_kind"], "zero")
        self.assertTrue(result["data_present"])
        self.assertTrue(result["title_present"])
        self.assertTrue(result["sub_list_present"])
        self.assertEqual(result["lecture_count"], LECTURE_COUNT_CAP)
        self.assertTrue(result["counts_capped"])
        for _url, kwargs in vpn.calls:
            self.assertNotIn("headers", kwargs)
        dumped = json.dumps(result)
        self.assertNotIn("c-secret-1", dumped)
        self.assertNotIn("secret-title", dumped)
        self.assertNotIn("secret-teacher", dumped)

    def test_empty_course_id_fails_closed_without_request(self):
        vpn = _RecordingVPN()
        result = probe_detail_without_authorization(_FakeClient(vpn), " ")
        self.assertEqual(result["outcome"], "request_error")
        self.assertEqual(vpn.calls, [])

    def test_error_json_envelope(self):
        vpn = _RecordingVPN(details=[_FakeResponse({"code": 401, "msg": "denied"}, status=200)])
        result = probe_detail_without_authorization(_FakeClient(vpn), "c1")
        self.assertEqual(result["status_kind"], "ok_2xx")
        self.assertEqual(result["code_kind"], "other")
        self.assertFalse(result["data_present"])


class ProbeIdentityTests(unittest.TestCase):
    def test_baseline_match_and_mismatch_are_closed(self):
        vpn = _RecordingVPN()
        client = _FakeClient(vpn)
        kind, identity = probe_identity(client)
        self.assertEqual(kind, "baseline")
        self.assertEqual(identity["id"], "u1")
        self.assertEqual(probe_identity(client, identity)[0], "match")
        self.assertEqual(
            probe_identity(client, {"id": "other", "account": "acc1"})[0],
            "mismatch",
        )

    def test_unavailable_identity_is_closed(self):
        vpn = _RecordingVPN(
            userinfo=_FakeResponse({"code": 500}, status=500)
        )
        kind, identity = probe_identity(_FakeClient(vpn))
        self.assertEqual(kind, "unavailable")
        self.assertIsNone(identity)


class RunClosedCatalogProbeTests(unittest.TestCase):
    def test_happy_path_stays_within_request_budget(self):
        month = _FakeResponse({"code": 0, "list": [{"course": [
            {"id": "c-secret-1"}, {"id": "c-secret-2"},
        ]}]})
        detail = _FakeResponse({"code": 0, "data": {
            "title": "secret-title",
            "sub_list": {"2026": {"09": {"07": [{"id": "s1"}]}}},
        }})
        vpn = _RecordingVPN(months=[month], details=[detail])
        result = run_closed_catalog_probe(_FakeClient(vpn), month="2026-09")
        self.assertEqual(result["schema"], PROBE_SCHEMA_VERSION)
        self.assertEqual(result["route"], "webvpn")
        self.assertEqual(result["stopped"], "")
        self.assertEqual(result["identity"]["identity_kind"], "baseline")
        self.assertEqual(result["identity_after"]["identity_kind"], "match")
        self.assertEqual(result["index"]["row_count"], 2)
        self.assertTrue(result["detail"]["data_present"])
        # 预算：2 次身份 + 1 次索引 + 1 次详情，全部无 Authorization。
        self.assertEqual(len(vpn.calls), 4)
        for _url, kwargs in vpn.calls:
            self.assertNotIn("headers", kwargs)

    def test_zero_rows_skips_detail_without_extra_requests(self):
        month = _FakeResponse({"code": 0, "list": []})
        vpn = _RecordingVPN(months=[month])
        result = run_closed_catalog_probe(_FakeClient(vpn), month="2026-09")
        self.assertEqual(result["stopped"], "")
        self.assertEqual(result["detail"], {"skipped": "no_candidates"})
        self.assertEqual(len(vpn.calls), 3)

    def test_index_rejection_fails_closed_before_detail_and_recheck(self):
        month = _FakeResponse({"code": 401, "msg": "denied"})
        vpn = _RecordingVPN(months=[month])
        result = run_closed_catalog_probe(_FakeClient(vpn), month="2026-09")
        self.assertEqual(result["stopped"], "index_rejected")
        self.assertEqual(result["detail"], {})
        self.assertEqual(result["identity_after"], {"identity_kind": ""})
        self.assertEqual(len(vpn.calls), 2)

    def test_unavailable_baseline_identity_stops_all_probes(self):
        vpn = _RecordingVPN(
            userinfo=_FakeResponse(json_error=True, content_type="text/html")
        )
        result = run_closed_catalog_probe(_FakeClient(vpn), month="2026-09")
        self.assertEqual(result["stopped"], "identity_unavailable")
        self.assertEqual(result["index"], {})
        self.assertEqual(len(vpn.calls), 1)

    def test_post_probe_identity_mismatch_fails_closed(self):
        month = _FakeResponse({"code": 0, "list": [{"course": [{"id": "c1"}]}]})
        detail = _FakeResponse({"code": 0, "data": {"title": "t"}})
        vpn = _RecordingVPN(months=[month], details=[detail])

        identity_calls = {"n": 0}

        original_get = vpn.get

        def alternating_identity(url, **kwargs):
            if "infosimple" in url:
                identity_calls["n"] += 1
                if identity_calls["n"] == 2:
                    return _FakeResponse({
                        "code": 0, "params": {"id": "intruder", "account": "acc2"},
                    })
            return original_get(url, **kwargs)

        vpn.get = alternating_identity
        result = run_closed_catalog_probe(_FakeClient(vpn), month="2026-09")
        self.assertEqual(result["identity_after"]["identity_kind"], "mismatch")
        self.assertEqual(result["stopped"], "identity_mismatch_after_probe")

    def test_full_result_never_carries_values(self):
        month = _FakeResponse({"code": 0, "list": [{"course": [
            {"id": "c-secret-1", "title": "secret-title"},
        ]}]})
        detail = _FakeResponse({"code": 0, "data": {"title": "secret-title"}})
        vpn = _RecordingVPN(months=[month], details=[detail])
        result = run_closed_catalog_probe(_FakeClient(vpn), month="2026-09")
        dumped = json.dumps(result, sort_keys=True)
        for forbidden in (
            "c-secret-1", "secret-title", "Authorization", "Bearer",
            "u1", "acc1", "icourse.invalid",
        ):
            self.assertNotIn(forbidden, dumped)
        for section in ("identity", "cookies", "index", "detail", "identity_after"):
            self.assertIsInstance(result[section], dict)


if __name__ == "__main__":
    unittest.main()
