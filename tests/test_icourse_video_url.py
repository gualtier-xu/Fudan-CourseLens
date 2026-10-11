import unittest
from datetime import date
from requests.cookies import RequestsCookieJar

from src.api.icourse import ICourseClient


class _FakeVPN:
    def __init__(self):
        self.session = type("Session", (), {})()
        self.session.cookies = RequestsCookieJar()
        self.session.cookies.set("PHPSESSID", "session-value")


class ICourseVideoUrlTests(unittest.TestCase):
    def test_list_authorized_courses_verifies_detail_before_returning(self):
        client = ICourseClient(vpn_session=object())
        client.list_user_courses = lambda **_kwargs: [
            {"course_id": "1", "title": "可访问", "teacher": "教师", "dept": "院系"},
            {"course_id": "2", "title": "无权限", "teacher": "教师", "dept": "院系"},
        ]
        client.get_course_detail = lambda course_id, student="": (
            {"title": "可访问", "teacher": "教师", "lectures": [{"sub_id": "11", "has_playback": True}]}
            if course_id == "1" else (_ for _ in ()).throw(RuntimeError("forbidden"))
        )

        result = client.list_authorized_courses()

        self.assertEqual([item["course_id"] for item in result], ["1"])
        self.assertEqual(result[0]["authorization_state"], "verified")
        self.assertEqual(result[0]["term"], "")

    def test_user_course_index_is_bounded_and_deduplicated(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"code": 0, "list": [{"section": "01", "course": [
                    {"id": "1", "title": "Course", "term_name": "Term"},
                    {"id": "1", "title": "Duplicate"},
                ]}]}

        class Vpn:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

        vpn = Vpn()
        client = ICourseClient(vpn)
        client._userinfo = {"id": "u", "account": "student", "tenant_id": "tenant"}
        client._authorization_headers = lambda **_kwargs: {"Authorization": "Bearer synthetic-token"}
        courses = client.list_user_courses(today=date(2026, 7, 21))

        self.assertEqual([item["course_id"] for item in courses], ["1"])
        self.assertEqual(len(vpn.calls), 14)
        self.assertEqual(vpn.calls[0][1]["timeout"], client.CATALOG_TIMEOUT)
        self.assertEqual(vpn.calls[0][1]["headers"], {"Authorization": "Bearer synthetic-token"})
        self.assertEqual(vpn.calls[0][1]["params"], {"month": "2025-07"})

    def test_bearer_token_is_extracted_from_percent_encoded_session_cookie(self):
        vpn = _FakeVPN()
        payload = '%7Bi%3A1%3Bs%3A6%3A%22_token%22%3Bi%3A2%3Bs%3A20%3A%22synthetic-token-value%22%3B%7D'
        vpn.session.cookies.set("platform_session", payload)
        client = ICourseClient(vpn)

        self.assertEqual(
            client._authorization_headers(required=True),
            {"Authorization": "Bearer synthetic-token-value"},
        )

    def test_accepts_video_list_mp4_url_with_query_string(self):
        client = ICourseClient(vpn_session=object())
        client.get_sub_info = lambda course_id, sub_id: {
            "_api_code": 0,
            "video_list": {
                "hd": {
                    "preview_url": "https://example.com/play/0/defaultnew/lecture.mp4?token=abc",
                },
            },
            "content": {
                "playback": {
                    "url": "https://example.com/play/1/defaultnew/lecture.mp4",
                },
            },
        }
        client.sign_video_url = lambda video_url, now=None: f"signed:{video_url}"

        self.assertEqual(
            client.get_video_url("36941", "652419"),
            "signed:https://example.com/play/0/defaultnew/lecture.mp4?token=abc",
        )

    def test_allows_nested_playback_url_when_browser_can_play_it(self):
        client = ICourseClient(vpn_session=object())
        client.get_sub_info = lambda course_id, sub_id: {
            "_api_code": 7001,
            "_api_msg": "video is not open",
            "content": {
                "playback": {
                    "url": "https://example.com/play/1/defaultnew/lecture.mp4",
                },
            },
        }
        client.sign_video_url = lambda video_url, now=None: f"signed:{video_url}"

        self.assertEqual(
            client.get_video_url("36941", "652419"),
            "signed:https://example.com/play/1/defaultnew/lecture.mp4",
        )

    def test_stream_params_use_direct_icourse_media_url_not_webvpn_rewrite(self):
        client = ICourseClient(vpn_session=_FakeVPN())
        media_url = "https://icourse.fudan.edu.cn/play/1/defaultnew/lecture.mp4?clientUUID=x&t=y"

        stream_url, headers = client.get_stream_params(media_url)

        self.assertEqual(stream_url, media_url)
        self.assertIn("Cookie: PHPSESSID=session-value", headers)
        self.assertIn("Accept-Encoding: identity;q=1, *;q=0", headers)
        self.assertIn("Range: bytes=0-", headers)


if __name__ == "__main__":
    unittest.main()
