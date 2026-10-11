from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace

from src.runtime.live_room import (
    LiveRoomError, LiveRoomService, UpstreamRequest, bounded_range,
    validate_https_url,
)


def public_dns(host, port, **_kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


class Response:
    def __init__(self, body=b"", *, content_type="application/vnd.apple.mpegurl", status=200, headers=None):
        self.status_code = status
        self.body = body
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(body)), **(headers or {})}
        self.closed = False
        self.peer_ip = "93.184.216.34"

    def iter_content(self, _size):
        yield self.body

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class SlippingSession:
    """Upstream stand-in that advances the fake clock inside get()."""

    def __init__(self, before_response):
        self.before_response = before_response
        self.response = Response(b"#EXTM3U\nsegment.ts\n")
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        self.before_response()
        return self.response


class LiveRoomServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.allowed = {"course"}
        self.observation = {
            "state": "live", "observed_at": 1000.0, "expires_at": 1020.0,
            "starts_at": "2026-07-28T18:00:00+08:00",
        }
        self.session = Session([Response(b"#EXTM3U\nsegment.ts\n")])
        self.service = LiveRoomService(
            authorize=lambda course_id: course_id in self.allowed,
            identity_scope=lambda: "scope",
            observe=lambda _course_id: self.observation,
            open_upstream=lambda _course_id, **_kwargs: UpstreamRequest(
                self.session, "https://media.example/master.m3u8",
                {"Cookie": "secret", "Origin": "https://course.example", "X-Unsafe": "drop"},
            ),
            clock=lambda: self.now[0],
            resolver=public_dns,
            grant_ttl=5,
            playback_ttl=30,
        )

    def _enter(self):
        grant = self.service.issue_grant("course")
        return self.service.consume_grant(grant["grant"])

    def test_status_closed_set_and_fresh_live_evidence(self):
        value = self.service.status("course")
        self.assertEqual(value["state"], "live")
        self.assertTrue(value["can_enter"])
        self.observation["state"] = "invented"
        self.assertEqual(self.service.status("course")["state"], "unknown")
        self.observation.update(state="live", expires_at=999.0)
        self.assertEqual(self.service.status("course")["state"], "stale")

    def test_malformed_observation_timestamps_fail_closed_not_crash(self):
        # 夜10-C T18：上游负载畸形时间戳曾以裸 ValueError 炸出 status()。
        self.observation.update(observed_at="not-a-number", expires_at="also-bad")
        value = self.service.status("course")
        self.assertEqual(value["state"], "stale", "坏时间戳按缺失处理→stale 接管")
        self.observation.update(observed_at=None, expires_at=None)
        self.assertEqual(self.service.status("course")["state"], "stale")
        self.observation.update(observed_at=1000.0, expires_at=1020.0)
        self.assertEqual(self.service.status("course")["state"], "live", "合法值恢复")

    def test_all_non_live_states_fail_closed(self):
        for state in ("upcoming", "ended", "offline", "stale", "unknown"):
            with self.subTest(state=state):
                self.observation.update(state=state, expires_at=1020.0)
                value = self.service.status("course")
                self.assertEqual(value["state"], state)
                self.assertFalse(value["can_enter"])
                with self.assertRaises(LiveRoomError):
                    self.service.issue_grant("course")
        self.assertEqual(self.service.status("other")["state"], "denied")

    def test_grant_is_one_time_short_lived_and_concurrency_safe(self):
        grant = self.service.issue_grant("course")
        outcomes = []
        lock = threading.Lock()

        def consume():
            try:
                self.service.consume_grant(grant["grant"])
                value = "ok"
            except LiveRoomError:
                value = "rejected"
            with lock:
                outcomes.append(value)

        threads = [threading.Thread(target=consume) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(outcomes.count("rejected"), 7)

        expired = self.service.issue_grant("course")
        self.now[0] += 6
        with self.assertRaisesRegex(LiveRoomError, "live_grant_invalid"):
            self.service.consume_grant(expired["grant"])

    def test_revocation_and_identity_partition_apply_after_entry(self):
        entry = self._enter()
        self.allowed.clear()
        with self.assertRaisesRegex(LiveRoomError, "live_authorization_revoked"):
            self.service.fetch(
                entry["session_id"], entry["manifest_id"],
                cookie_session=entry["session_id"],
            )

    def test_successful_fetch_slides_idle_window(self):
        self.service._open_upstream = lambda _course_id, **_kwargs: UpstreamRequest(
            Session([
                Response(b"#EXTM3U\nsegment.ts\n"),
                Response(b"#EXTM3U\nsegment.ts\n"),
            ]), "https://media.example/master.m3u8", {},
        )
        entry = self._enter()
        self.assertEqual(entry["expires_at"], 1030.0)
        self.now[0] = 1025.0
        self.service.fetch(
            entry["session_id"], entry["manifest_id"],
            cookie_session=entry["session_id"],
        )
        self.assertEqual(
            self.service._playbacks[entry["session_id"]].expires_at, 1055.0,
        )
        # 44s after creation — beyond the fixed 30s window that shipped
        # before — the actively polled session is still alive, and its
        # registered resources slid with it instead of expiring early.
        self.now[0] = 1044.0
        response = self.service.fetch(
            entry["session_id"], entry["manifest_id"],
            cookie_session=entry["session_id"],
        )
        self.assertTrue(response.body)

    def test_idle_session_dies_without_renewal(self):
        entry = self._enter()
        self.now[0] = 1031.0
        with self.assertRaisesRegex(LiveRoomError, "live_session_expired"):
            self.service.fetch(
                entry["session_id"], entry["manifest_id"],
                cookie_session=entry["session_id"],
            )
        self.assertNotIn(entry["session_id"], self.service._playbacks)

    def test_absolute_cap_ends_session_despite_active_renewal(self):
        session = Session([Response(b"#EXTM3U\nsegment.ts\n") for _ in range(4)])
        service = LiveRoomService(
            authorize=lambda course_id: course_id in self.allowed,
            identity_scope=lambda: "scope",
            observe=lambda _course_id: self.observation,
            open_upstream=lambda _course_id, **_kwargs: UpstreamRequest(
                session, "https://media.example/master.m3u8", {},
            ),
            clock=lambda: self.now[0],
            resolver=public_dns,
            grant_ttl=5,
            playback_ttl=30,
            playback_absolute_ttl=90,
        )
        grant = service.issue_grant("course")
        entry = service.consume_grant(grant["grant"])
        self.assertEqual(entry["expires_at"], 1030.0)
        for moment in (1020.0, 1040.0, 1060.0, 1080.0):
            self.now[0] = moment
            service.fetch(
                entry["session_id"], entry["manifest_id"],
                cookie_session=entry["session_id"],
            )
        # Renewal never crosses the absolute deadline set at creation.
        self.assertEqual(
            service._playbacks[entry["session_id"]].expires_at, 1090.0,
        )
        self.now[0] = 1091.0
        with self.assertRaisesRegex(LiveRoomError, "live_session_expired"):
            service.fetch(
                entry["session_id"], entry["manifest_id"],
                cookie_session=entry["session_id"],
            )

    def test_midflight_expiry_crossing_does_not_revive_session(self):
        deadline_box: list[float] = []

        def slip_clock():
            self.now[0] = deadline_box[0] + 30.0

        self.service._open_upstream = lambda _course_id, **_kwargs: UpstreamRequest(
            SlippingSession(slip_clock), "https://media.example/master.m3u8", {},
        )
        entry = self._enter()
        deadline_box.append(entry["expires_at"])
        self.now[0] = deadline_box[0] - 5.0
        response = self.service.fetch(
            entry["session_id"], entry["manifest_id"],
            cookie_session=entry["session_id"],
        )
        # The request was admitted while alive, so it completes — but the
        # session crossed its own expiry mid-flight and stays dead.
        self.assertTrue(response.body)
        self.assertEqual(
            self.service._playbacks[entry["session_id"]].expires_at,
            deadline_box[0],
        )
        self.now[0] = deadline_box[0] + 31.0
        with self.assertRaisesRegex(LiveRoomError, "live_session_expired"):
            self.service.fetch(
                entry["session_id"], entry["manifest_id"],
                cookie_session=entry["session_id"],
            )

    def test_purged_session_cannot_be_touched_back_alive(self):
        deadline_box: list[float] = []

        def slip_and_purge():
            self.now[0] = deadline_box[0] + 30.0
            self.observation.update(
                observed_at=self.now[0], expires_at=self.now[0] + 20.0,
            )
            # 另一个入口取新 grant 的公开路径会先做过期清扫：把已过期会话摘出注册表
            self.service.issue_grant("course")

        self.service._open_upstream = lambda _course_id, **_kwargs: UpstreamRequest(
            SlippingSession(slip_and_purge), "https://media.example/master.m3u8", {},
        )
        entry = self._enter()
        deadline_box.append(entry["expires_at"])
        self.now[0] = deadline_box[0] - 5.0
        response = self.service.fetch(
            entry["session_id"], entry["manifest_id"],
            cookie_session=entry["session_id"],
        )
        self.assertTrue(response.body)
        self.assertNotIn(entry["session_id"], self.service._playbacks)

    def test_upstream_transport_failure_closes_as_live_upstream_unreachable(self):
        """U17③/A2：requests 裸异常（连接/超时/SSL）收编闭集码族，
        tee 可见行 flush 上屏，绝不让 500 runtime_failed 吃掉真实原因。"""
        import requests

        class ExplodingSession:
            def get(self, *_args, **_kwargs):
                raise requests.ConnectionError("boom")

        self.service._open_upstream = lambda _course_id, **_kwargs: UpstreamRequest(
            ExplodingSession(), "https://media.example/master.m3u8", {},
        )
        entry = self._enter()
        with self.assertRaises(LiveRoomError) as caught:
            self.service.fetch(
                entry["session_id"], entry["manifest_id"],
                cookie_session=entry["session_id"],
            )
        self.assertEqual(caught.exception.code, "live_upstream_unreachable")
        self.assertEqual(caught.exception.status, 502)

    def test_hls_rewrites_urls_and_does_not_leak_upstream_or_headers(self):
        entry = self._enter()
        response = self.service.fetch(
            entry["session_id"], entry["manifest_id"],
            cookie_session=entry["session_id"],
        )
        text = response.body.decode()
        self.assertIn(f"/api/v3/live-room/play/{entry['session_id']}/resource/", text)
        self.assertNotIn("media.example", text)
        self.assertNotIn("secret", text)
        sent = self.session.calls[0][1]["headers"]
        self.assertEqual(sent["Cookie"], "secret")
        self.assertNotIn("X-Unsafe", sent)

    def test_manifest_rewrites_variant_map_and_key_uris(self):
        body = (
            b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nvariant.m3u8\n"
            b'#EXT-X-MAP:URI="init.mp4"\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\nsegment.ts\n'
        )
        self.session = Session([Response(body)])
        self.service._open_upstream = lambda _course_id, **_kwargs: UpstreamRequest(
            self.session, "https://media.example/master.m3u8", {},
        )
        entry = self._enter()
        text = self.service.fetch(
            entry["session_id"], entry["manifest_id"], cookie_session=entry["session_id"],
        ).body.decode()
        self.assertEqual(text.count("/api/v3/live-room/play/"), 4)
        for upstream in ("variant.m3u8", "init.mp4", "key.bin", "segment.ts", "media.example"):
            self.assertNotIn(upstream, text)

    def test_cookie_range_type_size_redirect_and_disconnect_controls(self):
        entry = self._enter()
        with self.assertRaisesRegex(LiveRoomError, "live_session_cookie_required"):
            self.service.fetch(entry["session_id"], entry["manifest_id"], cookie_session="")
        self.assertEqual(bounded_range("bytes=10-999999999"), f"bytes=10-{10 + 8 * 1024 * 1024 - 1}")
        with self.assertRaises(LiveRoomError):
            bounded_range("bytes=1-2,4-5")

        bad_session = Session([Response(b"x", content_type="video/x-flv")])
        self.service._open_upstream = lambda _course_id, **_kwargs: UpstreamRequest(
            bad_session, "https://media.example/master.m3u8", {},
        )
        entry = self._enter()
        with self.assertRaisesRegex(LiveRoomError, "live_content_type_rejected"):
            self.service.fetch(entry["session_id"], entry["manifest_id"], cookie_session=entry["session_id"])

        oversized = Response(b"", headers={"Content-Length": str(2 * 1024 * 1024 + 1)})
        self.service._open_upstream = lambda _course_id, **_kwargs: UpstreamRequest(
            Session([oversized]), "https://media.example/master.m3u8", {},
        )
        entry = self._enter()
        with self.assertRaisesRegex(LiveRoomError, "live_response_too_large"):
            self.service.fetch(entry["session_id"], entry["manifest_id"], cookie_session=entry["session_id"])

    def test_ssrf_http_private_port_userinfo_dns_and_cross_origin_redirects(self):
        for url in (
            "http://media.example/a.m3u8",
            "https://user:pass@media.example/a.m3u8",
            "https://media.example:8443/a.m3u8",
        ):
            with self.subTest(url=url), self.assertRaises(LiveRoomError):
                validate_https_url(url, public_dns)

        def private_dns(host, port, **_kwargs):
            return [(2, 1, 6, "", ("127.0.0.1", port))]
        with self.assertRaisesRegex(LiveRoomError, "live_private_address_rejected"):
            validate_https_url("https://media.example/a.m3u8", private_dns)

        redirected = Session([
            Response(status=302, headers={"Location": "https://other.example/next.m3u8"}),
        ])
        self.service._open_upstream = lambda _course_id, **_kwargs: UpstreamRequest(
            redirected, "https://media.example/master.m3u8",
            {"Cookie": "secret", "Origin": "https://course.example", "Referer": "https://course.example/"},
        )
        entry = self._enter()
        with self.assertRaisesRegex(LiveRoomError, "live_cross_origin_redirect_rejected"):
            self.service.fetch(entry["session_id"], entry["manifest_id"], cookie_session=entry["session_id"])
        self.assertEqual(len(redirected.calls), 1)

        rebound = Response(b"#EXTM3U\n")
        rebound.peer_ip = "8.8.8.8"
        self.service._open_upstream = lambda _course_id, **_kwargs: UpstreamRequest(
            Session([rebound]), "https://media.example/master.m3u8", {},
        )
        entry = self._enter()
        with self.assertRaisesRegex(LiveRoomError, "live_dns_rebinding_rejected"):
            self.service.fetch(entry["session_id"], entry["manifest_id"], cookie_session=entry["session_id"])

    def test_network_observation_closes_to_offline(self):
        self.service._observe = lambda _course_id: (_ for _ in ()).throw(TimeoutError())
        self.assertEqual(self.service.status("course")["state"], "offline")

    def test_observation_failure_stays_closed_set(self):
        # SRC-SYNDROME-1 U2：观测层真异常仍收编 closed-set（新增闭集打印行），码不变
        self.service._observe = lambda _course_id: (_ for _ in ()).throw(RuntimeError("synthetic"))
        value = self.service.status("course")
        self.assertEqual(value["state"], "unknown")
        self.assertEqual(value["code"], "live_observation_failed")

    def test_consume_grant_wraps_bare_upstream_failure(self):
        # SRC-SYNDROME-1 U2：取流会话航班的裸异常（登录失败族）不得逃逸成 500
        def _failing_open(_course_id, **_kwargs):
            raise RuntimeError("synthetic session flight failure")

        self.service._open_upstream = _failing_open
        grant = self.service.issue_grant("course")
        with self.assertRaises(LiveRoomError) as caught:
            self.service.consume_grant(grant["grant"])
        self.assertEqual(caught.exception.code, "live_session_unavailable")


if __name__ == "__main__":
    unittest.main()
