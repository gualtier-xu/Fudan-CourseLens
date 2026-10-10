from __future__ import annotations

import json
import time
import unittest
from types import SimpleNamespace
from urllib.parse import quote
from unittest.mock import patch

from src.api.icourse import (
    ICourseClient,
    PptListError,
    ppt_record_order,
)
from src.api.icourse_direct import DirectICourseError, DirectICourseSession


class DirectICourseSessionTests(unittest.TestCase):
    def test_direct_session_explicitly_bypasses_environment_proxies(self):
        with patch("src.api.icourse_direct.curl_requests.Session") as session_cls:
            DirectICourseSession()
        self.assertEqual(
            session_cls.call_args.kwargs["proxies"],
            {"http": "", "https": ""},
        )
        self.assertFalse(session_cls.call_args.kwargs["trust_env"])

    def test_bearer_extraction_accepts_only_bounded_serialized_token(self):
        session = DirectICourseSession()
        try:
            serialized = '{i:1;s:6:"_token";i:2;s:21:"synthetic-bearer-token";}'
            session.session.cookies.set("_token", quote(serialized))
            self.assertEqual(session._extract_bearer(), "synthetic-bearer-token")
            session.session.cookies.clear()
            session.session.cookies.set("_token", "short")
            with self.assertRaisesRegex(DirectICourseError, "catalog_bearer_missing"):
                session._extract_bearer()
        finally:
            session.close()

    def test_request_targets_are_https_and_closed_to_idp_and_icourse(self):
        session = DirectICourseSession()
        try:
            session._validate_url("https://id.fudan.edu.cn/idp/authn/getJsPublicKey")
            session._validate_url("https://icourse.fudan.edu.cn/userapi/v1/infosimple")
            for value in (
                "http://icourse.fudan.edu.cn/userapi/v1/infosimple",
                "https://example.org/userapi/v1/infosimple",
                "https://user:password@icourse.fudan.edu.cn/",
            ):
                with self.assertRaisesRegex(DirectICourseError, "catalog_target_invalid"):
                    session._validate_url(value)
            with self.assertRaisesRegex(DirectICourseError, "catalog_target_invalid"):
                session._validate_url(
                    "https://id.fudan.edu.cn/idp/authn/getJsPublicKey",
                    icourse_only=True,
                )
        finally:
            session.close()

    def test_deadline_expires_closed(self):
        session = DirectICourseSession()
        try:
            session._deadline = time.monotonic() - 1
            with self.assertRaisesRegex(DirectICourseError, "timeout"):
                session._timeout()
        finally:
            session.close()

    def test_redirect_chain_rejects_an_unapproved_host(self):
        session = DirectICourseSession()
        response = SimpleNamespace(
            history=[],
            url="https://icourse.fudan.edu.cn/start",
            headers={"Location": "https://example.org/redirected"},
            close=lambda: None,
        )
        try:
            with self.assertRaisesRegex(DirectICourseError, "catalog_target_invalid"):
                session._validate_redirect_chain(response)
        finally:
            session.close()

    def test_adopted_bearer_is_kept_only_after_identity_match(self):
        class Response:
            @staticmethod
            def close():
                return None

            @staticmethod
            def json():
                return {
                    "code": 0,
                    "data": {"id": "synthetic-id", "account": "synthetic-account"},
                }

        session = DirectICourseSession()
        try:
            with patch.object(session, "get", return_value=Response()):
                session.adopt_authorization(
                    {"Authorization": "Bearer synthetic-bearer-token"},
                    {"id": "synthetic-id", "account": "synthetic-account"},
                )
            self.assertEqual(
                session.authorization_headers(),
                {"Authorization": "Bearer synthetic-bearer-token"},
            )
            with patch.object(session, "get", return_value=Response()):
                with self.assertRaisesRegex(DirectICourseError, "catalog_identity_mismatch"):
                    session.adopt_authorization(
                        {"Authorization": "Bearer replacement-bearer-token"},
                        {"id": "different-id", "account": "synthetic-account"},
                    )
            with self.assertRaisesRegex(DirectICourseError, "catalog_bearer_missing"):
                session.authorization_headers()
        finally:
            session.close()


class _FakePptResponse:
    def __init__(self, payload, *, content=None, headers=None):
        self._payload = payload
        self.headers = headers or {}
        self.content = content if content is not None else json.dumps(payload).encode("utf-8")

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeVpn:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get(self, url, params=None, **_kwargs):
        self.calls.append(dict(params or {}))
        if self.pages:
            return _FakePptResponse(self.pages.pop(0))
        return _FakePptResponse({"code": 0, "list": []})


def _ppt_row(row_id, created_sec, pptimgurl="https://media.example.edu/s.jpg", content=None):
    body = content if content is not None else json.dumps({
        "pptimgurl": pptimgurl, "created": created_sec * 1000,
    })
    return {"id": row_id, "created_sec": created_sec, "content": body}


class GetPptListStreamingTests(unittest.TestCase):
    def _client(self, vpn):
        return ICourseClient(vpn)

    def test_walks_full_pages_until_the_server_short_page(self):
        page_one = [_ppt_row(index, index) for index in range(3)]
        page_two = [_ppt_row(index + 10, index + 10) for index in range(1)]
        vpn = _FakeVpn([{"code": 0, "list": page_one}, {"code": 0, "list": page_two}])
        items = self._client(vpn).get_ppt_list("c", "s", per_page=3)
        self.assertEqual(len(items), 4)
        self.assertEqual([call["page"] for call in vpn.calls], [1, 2])
        self.assertEqual([item["created_sec"] for item in items], [0, 1, 2, 10])

    def test_ties_break_deterministically_by_original_id(self):
        page = [_ppt_row("b", 50), _ppt_row("a", 50), _ppt_row("c", 10)]
        vpn = _FakeVpn([{"code": 0, "list": page}])
        items = self._client(vpn).get_ppt_list("c", "s", per_page=100)
        self.assertEqual(
            [item["original_id"] for item in items],
            ["c", "a", "b"],
        )
        self.assertEqual(
            [ppt_record_order(item) for item in items],
            sorted(ppt_record_order(item) for item in items),
        )

    def test_malformed_rows_are_skipped_without_failing_the_lecture(self):
        page = [
            "not-a-dict",
            {"id": 2, "created_sec": 2, "content": "{broken json"},
            {"id": 3, "created_sec": 3, "content": json.dumps(["not", "dict"])},
            {"id": 4, "created_sec": 4, "content": json.dumps({"pptthumb": "only"})},
            _ppt_row(5, 5),
        ]
        vpn = _FakeVpn([{"code": 0, "list": page}])
        items = self._client(vpn).get_ppt_list("c", "s", per_page=100)
        self.assertEqual([item["original_id"] for item in items], ["5"])

    def test_repeated_page_signature_fails_closed_as_stalled(self):
        page_one = [_ppt_row(1, 1), _ppt_row(2, 2)]
        vpn = _FakeVpn([
            {"code": 0, "list": page_one},
            {"code": 0, "list": list(page_one)},
        ])
        with self.assertRaisesRegex(PptListError, "ppt_pagination_stalled"):
            self._client(vpn).get_ppt_list("c", "s", per_page=2)

    def test_record_storm_budget_raises_closed_code(self):
        page = [_ppt_row(index, index) for index in range(3)]
        vpn = _FakeVpn([{"code": 0, "list": page}])
        iterator = self._client(vpn).iter_ppt_records("c", "s", per_page=100, max_records=2)
        collected = 0
        with self.assertRaisesRegex(PptListError, "ppt_record_storm"):
            for _item in iterator:
                collected += 1
        self.assertEqual(collected, 2)

    def test_oversized_response_raises_closed_code_before_parsing(self):
        vpn = _FakeVpn([])
        vpn.get = lambda url, params=None, **_kwargs: _FakePptResponse(
            {"code": 0, "list": []},
            content=b"x" * 64,
            headers={"Content-Length": str(64)},
        )
        iterator = self._client(vpn).iter_ppt_records("c", "s", max_page_bytes=32)
        with self.assertRaisesRegex(PptListError, "ppt_response_too_large"):
            list(iterator)

    def test_payload_shape_errors_raise_closed_codes(self):
        for payload in (None, {"code": 1}, {"code": 0, "list": "not-a-list"}, [1, 2]):
            vpn = _FakeVpn([payload])
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(PptListError, "ppt_payload_invalid"):
                    self._client(vpn).get_ppt_list("c", "s")


if __name__ == "__main__":
    unittest.main()
