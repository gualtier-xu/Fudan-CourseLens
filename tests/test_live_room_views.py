"""LIVE-BE-1 U3 · 直播四路 view 维度钉（S2 后端半边，设计纸 §九 S2/C1-C10）。

钉五件事：
1. available_views_from_live_url 从平台 live_url 嵌套键推 URL-free 闭集
   view id，正典序恒定；非 .m3u8 分支不算可用。
2. status() 对 observe 产出的 available_views 做闭集过滤+正典序透传；
   未提供不出键，显式空表如实透出，畸形类型软化为空表。
3. consume_grant 默认视角=学生画面（既有语义不变）；四路皆可绑定。
4. 闭集外视角在消费授予前即拒（grant 保留可原地重试）。
5. 切视角=重建直播流：各 grant 各自独立会话、各绑其 view，互不串台。
"""
from __future__ import annotations

import unittest

from src.runtime.live_room import (
    DEFAULT_LIVE_VIEW,
    LIVE_VIEWS,
    LiveRoomError,
    LiveRoomService,
    UpstreamRequest,
    available_views_from_live_url,
)


def public_dns(host, port, **_kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


FULL_LIVE_URL = {
    "output": {
        "m3u8": "https://media.example/t.m3u8",
        "m3u8_audio": "https://media.example/ta.m3u8",
    },
    "output_student": {
        "m3u8": "https://media.example/s.m3u8",
        "m3u8_audio": "https://media.example/sa.m3u8",
    },
}


class Response:
    def __init__(self):
        self.status_code = 200
        self.headers = {
            "Content-Type": "application/vnd.apple.mpegurl",
            "Content-Length": "20",
        }
        self.peer_ip = "93.184.216.34"

    def iter_content(self, _size):
        yield b"#EXTM3U\nsegment.ts\n"

    def close(self):
        pass


class Session:
    def get(self, url, **kwargs):
        return Response()


class LiveRoomViewTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.observation = {"state": "live", "observed_at": 1000.0, "expires_at": 1020.0}
        self.opened_views = []
        self.service = LiveRoomService(
            authorize=lambda _course_id: True,
            identity_scope=lambda: "scope",
            observe=lambda _course_id: self.observation,
            open_upstream=self._record_open,
            clock=lambda: self.now[0],
            resolver=public_dns,
            grant_ttl=5,
            playback_ttl=30,
        )

    def _record_open(self, _course_id, *, view=DEFAULT_LIVE_VIEW):
        self.opened_views.append(view)
        return UpstreamRequest(Session(), "https://media.example/master.m3u8")

    def _consume(self, view=None):
        grant = self.service.issue_grant("course")["grant"]
        if view is None:
            return self.service.consume_grant(grant)
        return self.service.consume_grant(grant, view=view)

    def test_available_views_derived_url_free_in_canonical_order(self):
        self.assertEqual(
            available_views_from_live_url(FULL_LIVE_URL),
            ("teacher", "student", "teacher_audio", "student_audio"),
        )
        self.assertEqual(
            list(available_views_from_live_url(FULL_LIVE_URL)),
            [view for view, _, _ in LIVE_VIEWS],
        )
        self.assertEqual(
            available_views_from_live_url({"output": {"m3u8": "https://media.example/t.m3u8"}}),
            ("teacher",),
        )
        self.assertEqual(available_views_from_live_url({"output": {"m3u8": "https://x/t.flv"}}), ())
        self.assertEqual(available_views_from_live_url({"output": {"m3u8": "   "}}), ())
        self.assertEqual(available_views_from_live_url({"output": "not-a-dict"}), ())
        self.assertEqual(available_views_from_live_url(None), ())
        self.assertEqual(available_views_from_live_url("junk"), ())
        for view in available_views_from_live_url(FULL_LIVE_URL):
            self.assertNotIn("http", view)
            self.assertNotIn("/", view)

    def test_status_passthrough_filters_to_closed_set_in_canonical_order(self):
        self.observation["available_views"] = [
            "student_audio", "junk_view", "teacher", "student_audio",
        ]
        value = self.service.status("course")
        self.assertEqual(value["available_views"], ["teacher", "student_audio"])
        self.observation["available_views"] = []
        self.assertEqual(self.service.status("course")["available_views"], [])
        self.observation["available_views"] = "teacher"  # 畸形类型：软化为空表
        self.assertEqual(self.service.status("course")["available_views"], [])
        del self.observation["available_views"]
        self.assertNotIn("available_views", self.service.status("course"))

    def test_default_view_is_student_and_all_four_views_construct(self):
        self.assertEqual(DEFAULT_LIVE_VIEW, "student")
        session = self._consume()
        self.assertEqual(self.service._playbacks[session["session_id"]].view, "student")
        for view, _, _ in LIVE_VIEWS:
            with self.subTest(view=view):
                bound = self._consume(view=view)
                self.assertEqual(self.service._playbacks[bound["session_id"]].view, view)

    def test_unknown_view_rejected_before_grant_consumed(self):
        grant = self.service.issue_grant("course")["grant"]
        with self.assertRaises(LiveRoomError) as caught:
            self.service.consume_grant(grant, view="invented")
        self.assertEqual(caught.exception.code, "live_view_unknown")
        retry = self.service.consume_grant(grant, view="teacher_audio")
        self.assertEqual(self.service._playbacks[retry["session_id"]].view, "teacher_audio")

    def test_switch_view_rebuilds_as_independent_session(self):
        first = self._consume(view="teacher")
        second = self._consume(view="teacher_audio")
        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertNotEqual(first["manifest_id"], second["manifest_id"])
        self.assertEqual(self.service._playbacks[first["session_id"]].view, "teacher")
        self.assertEqual(self.service._playbacks[second["session_id"]].view, "teacher_audio")

    def test_consume_grant_passes_view_to_upstream_opener_and_echoes_it(self):
        session = self._consume(view="teacher_audio")
        self.assertEqual(self.opened_views, ["teacher_audio"])
        self.assertEqual(session["view"], "teacher_audio")

    def test_default_consume_opens_default_view(self):
        session = self._consume()
        self.assertEqual(self.opened_views, [DEFAULT_LIVE_VIEW])
        self.assertEqual(session["view"], DEFAULT_LIVE_VIEW)

    def test_unknown_view_never_reaches_upstream_opener(self):
        grant = self.service.issue_grant("course")["grant"]
        with self.assertRaises(LiveRoomError) as caught:
            self.service.consume_grant(grant, view="invented")
        self.assertEqual(caught.exception.code, "live_view_unknown")
        self.assertEqual(self.opened_views, [], "闭集外视角在取流航班前即拒")


if __name__ == "__main__":
    unittest.main()
