from __future__ import annotations

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor
from http.cookiejar import CookieJar

from src.runtime.http_api import make_handler
from src.runtime.live_room import LiveRoomError, ProxyResponse
from tests.http_services import http_services


class Live:
    def __init__(self):
        self.consumed = False
        self.opened_views = []

    def status(self, course_id):
        return {"state": "live" if course_id == "course" else "denied", "can_enter": course_id == "course"}

    def issue_grant(self, course_id):
        if course_id != "course":
            raise LiveRoomError("live_authorization_denied", 403)
        return {"grant": "g" * 32, "expires_at": 10}

    def consume_grant(self, grant, *, view="student"):
        if grant != "g" * 32 or self.consumed:
            raise LiveRoomError("live_grant_invalid", 403)
        self.consumed = True
        self.opened_views.append(view)
        return {
            "session_id": "s" * 32, "manifest_id": "m" * 24,
            "manifest_path": f"/api/v3/live-room/play/{'s' * 32}/manifest/{'m' * 24}",
            "expires_at": 20, "view": view,
        }

    def fetch(self, session_id, resource_id, *, cookie_session, range_header=""):
        if cookie_session != session_id:
            raise LiveRoomError("live_session_cookie_required", 403)
        return ProxyResponse(200, "application/vnd.apple.mpegurl", b"#EXTM3U\n")


class Media:
    """transcript/segments 端点测试替身：契约行为按 application.transcript_segments。"""

    def __init__(self):
        self.calls = []

    def transcript_segments(self, sub_id, since_ms=None):
        self.calls.append((sub_id, since_ms))
        if sub_id == "missing":
            raise FileNotFoundError("Lecture is not in the authorized catalog")
        if sub_id == "broken":
            return None
        if since_ms is None:
            return [{"start_ms": 0, "end_ms": 1200, "text": "hello"}]
        return [{"start_ms": 1200, "end_ms": 2400, "text": "world"}]


class LiveRoomApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = SimpleNamespace(authentication_snapshot=lambda: {"state": "ready"})
        services = http_services(source)
        cls.live = Live()
        services.live_room = cls.live
        cls.media = Media()
        services.media_session.transcript_segments = cls.media.transcript_segments
        frontend = Path(__file__).resolve().parents[1] / "frontend"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(services, frontend))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.cookies = CookieJar()
        cls.opener = build_opener(HTTPCookieProcessor(cls.cookies))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        # 类级共享替身：每测重置消费态，杜绝用例间串台
        self.live.consumed = False
        self.live.opened_views.clear()

    def request(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json"} if data else {},
        )
        return self.opener.open(request)

    def test_status_grant_session_cookie_and_manifest(self):
        with self.request("/api/v3/live-room/status?course_id=course") as response:
            self.assertEqual(json.load(response)["data"]["state"], "live")
        with self.request("/api/v3/live-room/grants", {"course_id": "course"}) as response:
            grant = json.load(response)["data"]["grant"]
        with self.request("/api/v3/live-room/sessions", {"grant": grant}) as response:
            value = json.load(response)["data"]
            cookie = response.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn("/manifest/", cookie)
        with self.request(value["manifest_path"]) as response:
            self.assertEqual(response.read(), b"#EXTM3U\n")
            self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/v3/live-room/sessions", {"grant": grant})
        self.assertEqual(caught.exception.code, 403)

    def test_sessions_view_param_closes_set_and_echoes(self):
        with self.request("/api/v3/live-room/grants", {"course_id": "course"}) as response:
            grant = json.load(response)["data"]["grant"]
        with self.request("/api/v3/live-room/sessions", {"grant": grant, "view": "teacher"}) as response:
            self.assertEqual(json.load(response)["data"]["view"], "teacher")
        self.assertEqual(self.live.opened_views, ["teacher"])

    def test_transcript_segments_incremental_and_closed_errors(self):
        with self.request("/api/v3/transcript/segments?sub_id=s1") as response:
            value = json.load(response)["data"]
        self.assertTrue(value["available"])
        self.assertEqual(value["segments"], [{"start_ms": 0, "end_ms": 1200, "text": "hello"}])
        # since_ms 增量：含尾水位语义在 icourse，路由只透传
        with self.request("/api/v3/transcript/segments?sub_id=s1&since_ms=1200") as response:
            value = json.load(response)["data"]
        self.assertEqual(self.media.calls[-1], ("s1", 1200))
        self.assertEqual(value["segments"], [{"start_ms": 1200, "end_ms": 2400, "text": "world"}])
        # 目录外讲次 → 404 闭集码；读取失败 → 503 transcript_unavailable
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/v3/transcript/segments?sub_id=missing")
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(json.loads(caught.exception.read())["error_code"], "transcript_lecture_unknown")
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/v3/transcript/segments?sub_id=broken")
        self.assertEqual(caught.exception.code, 503)
        self.assertEqual(json.loads(caught.exception.read())["error_code"], "transcript_unavailable")
        # 参数闭集：缺 sub_id=400；非整数 since_ms=400
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/v3/transcript/segments")
        self.assertEqual(caught.exception.code, 400)
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/v3/transcript/segments?sub_id=s1&since_ms=abc")
        self.assertEqual(caught.exception.code, 400)

    def test_missing_cookie_is_rejected(self):
        direct = build_opener()
        with self.assertRaises(HTTPError) as caught:
            direct.open(self.base + f"/api/v3/live-room/play/{'s' * 32}/manifest/{'m' * 24}")
        self.assertEqual(caught.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
