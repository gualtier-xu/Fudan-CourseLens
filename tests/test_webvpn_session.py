import unittest
from unittest.mock import Mock, patch

import requests

from src.api.webvpn import WebVPNSession


class FakeResponse:
    def __init__(self, status=200, *, location="", url="", payload=None, text=""):
        self.status_code = status
        self.headers = {"Location": location} if location else {}
        self.url = url
        self._payload = payload if payload is not None else {}
        self.text = text
        self.history = []
        self.closed = False

    def close(self):
        self.closed = True

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if not response.url:
            response.url = url
        return response


def make_vpn(*responses, callback=None):
    vpn = WebVPNSession(step_callback=callback)
    vpn.session = FakeSession(responses)
    return vpn


class WebVPNSessionTests(unittest.TestCase):
    def test_explicit_proxy_is_used_by_requests_and_ticket_transports(self):
        vpn = WebVPNSession(proxy_url="http://127.0.0.1:6268")
        try:
            self.assertEqual(vpn.session.proxies["https"], "http://127.0.0.1:6268")
            ticket = vpn.session.ticket_session()
            try:
                self.assertEqual(ticket.proxies["https"], "http://127.0.0.1:6268")
            finally:
                ticket.close()
        finally:
            vpn.session.close()

    def test_timetable_targets_are_closed_and_always_webvpn_wrapped(self):
        response = FakeResponse(200)
        vpn = make_vpn(response)
        with patch("src.api.webvpn.get_vpn_url", return_value="https://webvpn.fudan.edu.cn/http/encrypted") as convert:
            returned = vpn.get_allowed("http://yjsxktest.fudan.sh.cn/yjsxkapp/load.do")
        self.assertIs(returned, response)
        convert.assert_called_once_with("http://yjsxktest.fudan.sh.cn/yjsxkapp/load.do")
        self.assertEqual(vpn.session.calls[0][0], "https://webvpn.fudan.edu.cn/http/encrypted")
        with self.assertRaisesRegex(ValueError, "not allowed"):
            vpn.get_allowed("https://example.com/timetable")
        with self.assertRaisesRegex(ValueError, "not allowed"):
            vpn.get_allowed("https://user:secret@fdjwgl.fudan.edu.cn/student")

    def test_ticket_redirects_are_bounded_and_relative(self):
        first = FakeResponse(302, location="/ticket/next")
        second = FakeResponse(303, location="../portal")
        final = FakeResponse(200)
        vpn = make_vpn(first, second, final)

        status, redirects = vpn._follow_ticket_redirects(
            "https://webvpn.fudan.edu.cn/cas/ticket"
        )

        self.assertEqual((status, redirects), (200, 2))
        self.assertEqual([call[0] for call in vpn.session.calls], [
            "https://webvpn.fudan.edu.cn/cas/ticket",
            "https://webvpn.fudan.edu.cn/ticket/next",
            "https://webvpn.fudan.edu.cn/portal",
        ])
        # 非流式：curl_cffi 0.15.0 的 stream=True 走 duphandle 独立连接缓存，
        # 会破坏预热连接复用；票腿以有界小正文换取整腿复用同一条已预热连接。
        self.assertTrue(all(call[1]["stream"] is False for call in vpn.session.calls))
        self.assertTrue(all(call[1]["allow_redirects"] is False for call in vpn.session.calls))
        self.assertTrue(all(call[1]["timeout"] == vpn.TICKET_TIMEOUT for call in vpn.session.calls))
        self.assertTrue(first.closed and second.closed and final.closed)

    def test_ticket_timeout_verifies_cookie_state_without_replaying_ticket(self):
        portal = FakeResponse(200, url="https://webvpn.fudan.edu.cn/")
        events = []
        vpn = make_vpn(requests.exceptions.ReadTimeout(), portal, callback=lambda step, details: events.append((step, details)))

        vpn._establish_session("https://webvpn.fudan.edu.cn/once")

        ticket_calls = [call for call in vpn.session.calls if call[0] == "https://webvpn.fudan.edu.cn/once"]
        self.assertEqual(len(ticket_calls), 1)
        self.assertTrue(portal.closed)
        self.assertEqual(events[0][0], "webvpn_ticket_complete")
        self.assertNotIn("url", events[0][1])

    def test_failed_timeout_never_reuses_single_use_ticket(self):
        portal_login = FakeResponse(302, location="/login")
        vpn = make_vpn(requests.exceptions.ConnectTimeout(), portal_login)

        with self.assertRaisesRegex(RuntimeError, "ConnectTimeout"):
            vpn._establish_session("https://webvpn.fudan.edu.cn/once")

        self.assertEqual(sum(call[0] == "https://webvpn.fudan.edu.cn/once" for call in vpn.session.calls), 1)
        self.assertEqual(len(vpn.session.calls), 2)

    def test_login_page_pseudo_success_is_rejected(self):
        response = FakeResponse(200, url="https://webvpn.fudan.edu.cn/login")
        vpn = make_vpn(response)

        with self.assertRaisesRegex(RuntimeError, "login page"):
            vpn._follow_ticket_redirects("https://webvpn.fudan.edu.cn/once")
        self.assertTrue(response.closed)

    def test_redirect_limit_is_enforced(self):
        responses = [FakeResponse(302, location=f"/hop/{index}") for index in range(7)]
        vpn = make_vpn(*responses)

        with self.assertRaisesRegex(RuntimeError, "redirect limit"):
            vpn._follow_ticket_redirects("https://webvpn.fudan.edu.cn/once")
        self.assertEqual(len(vpn.session.calls), 7)

    def test_icourse_verification_uses_short_timeout_and_closes_response(self):
        response = FakeResponse(200, payload={"code": 0})
        vpn = make_vpn(response)

        with patch("src.api.webvpn.get_vpn_url", return_value="https://vpn.invalid/user"):
            self.assertTrue(vpn._verify_icourse_session())

        self.assertEqual(vpn.session.calls[0][1]["timeout"], (3, 5))
        self.assertTrue(vpn.session.calls[0][1]["stream"])
        self.assertTrue(response.closed)

    def test_icourse_ticket_timeout_verifies_cookie_state_without_replay(self):
        verified = FakeResponse(200, payload={"code": 0})
        events = []
        vpn = make_vpn(
            requests.exceptions.ReadTimeout(),
            verified,
            callback=lambda step, details: events.append((step, details)),
        )

        vpn._establish_icourse_session("https://icourse.fudan.edu.cn/once")

        self.assertEqual(
            sum(call[0] == "https://icourse.fudan.edu.cn/once" for call in vpn.session.calls),
            1,
        )
        self.assertTrue(verified.closed)
        self.assertEqual(events[0][0], "icourse_ticket_complete")
        self.assertNotIn("url", events[0][1])

    def test_icourse_evil_raw_ticket_is_rejected_before_webvpn_wrap_or_request(self):
        vpn = WebVPNSession()
        session = Mock()
        session.get.side_effect = [
            FakeResponse(200, url="https://webvpn.fudan.edu.cn/cas?lck=ok"),
            FakeResponse(200, payload={"data": "public-key"}),
        ]
        session.post.side_effect = [
            FakeResponse(200, payload={
                "data": [{"moduleCode": "userAndPwd", "authChainCode": "chain"}],
            }),
            FakeResponse(200, payload={"code": "200", "loginToken": "token"}),
            FakeResponse(200, text='locationValue = "https://evil.invalid/?ticket=ST-x"'),
        ]
        vpn.session = session
        vpn._establish_icourse_session = Mock()

        with (
            patch.object(vpn, "_verify_webvpn_session", return_value=True),
            patch.object(vpn, "_encrypt_password", return_value="encrypted"),
            patch("src.api.webvpn.get_vpn_url", side_effect=lambda url: url) as convert,
            self.assertRaisesRegex(RuntimeError, "target is not allowed"),
        ):
            vpn.authenticate_icourse("student", "password")

        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(session.post.call_count, 3)
        vpn._establish_icourse_session.assert_not_called()
        self.assertNotIn(
            "https://evil.invalid/?ticket=ST-x",
            [call.args[0] for call in convert.call_args_list],
        )

    def test_ticket_redirect_non_default_https_port_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "target is not allowed"):
            WebVPNSession._validate_ticket_redirect_url(
                "https://webvpn.fudan.edu.cn:8443/login?ticket=ST-x"
            )

if __name__ == "__main__":
    unittest.main()
