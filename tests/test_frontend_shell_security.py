from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from src.application import CourseLensApplication
from path_utils import PROJECT_ROOT


class FrontendShellSecurityTests(unittest.TestCase):
    def setUp(self):
        cache = PROJECT_ROOT / "runtime" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=cache)
        self.service = CourseLensApplication(Path(self.temp.name))

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def _authenticate(self, student_id: str) -> None:
        self.service.set_credentials(student_id, "synthetic-password", remember=False)
        with self.service._lock:
            self.service._client = object()
            self.service._client_last_verified_at = time.monotonic()
        self.service._set_login_status(
            "ready", "courses", "synthetic verified session", connected=True
        )

    def test_unauthenticated_status_and_app_shell_do_not_leak_courses(self):
        self.service.catalog_repository.upsert_course("course-secret", "不可泄露课程", "教师")
        self.service.catalog_repository.upsert_lecture(
            "course-secret", {"sub_id": "lecture-secret", "sub_title": "不可泄露课次"}
        )

        catalog = self.service.public_catalog_snapshot()
        shell = self.service.app_shell_snapshot()

        self.assertEqual(catalog.get("courses"), {})
        self.assertEqual(catalog.get("lectures"), {})
        self.assertEqual(shell["authentication"]["state"], "action_required")
        self.assertIsNone(shell["catalog"]["course_count"])
        self.assertIsNone(shell["tasks"]["active"])
        self.assertNotIn("不可泄露课程", str(shell))

    def test_search_startup_does_not_index_the_unscoped_catalog(self):
        self.service.catalog_repository.upsert_course("course-secret", "Private course")
        self.service.catalog_repository.upsert_lecture(
            "course-secret", {"sub_id": "lecture-secret", "sub_title": "Private lecture"}
        )
        self.service.start_search_index()
        deadline = time.monotonic() + 5
        while self.service.search_index.status()["state"] == "indexing" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.service.search_index.status()["state"], "action_required")
        with self.service.learning_store._connect() as database:
            count = database.execute("SELECT COUNT(*) FROM search_catalog").fetchone()[0]
        self.assertEqual(count, 0)

    def test_authorized_catalog_cache_is_isolated_by_verified_identity(self):
        self.service.catalog_repository.upsert_course("course-a", "账号甲课程", "教师甲")
        self.service.catalog_repository.upsert_course("course-b", "账号乙课程", "教师乙")

        self._authenticate("synthetic-a")
        self.service._store_authorized_catalog([
            {"course_id": "course-a"},
            {"course_id": "course-a"},
        ])
        first = self.service.authorized_catalog_snapshot(page=1, page_size=20)
        self.assertEqual([item["course_id"] for item in first["courses"]], ["course-a"])
        self.assertEqual(first["course_count"], 1)

        self._authenticate("synthetic-b")
        second = self.service.authorized_catalog_snapshot(page=1, page_size=20)
        self.assertEqual(second["courses"], [])
        self.assertEqual(second["state"], "degraded")
        self.assertNotIn("账号甲课程", str(second))

    def test_catalog_pagination_deduplication_and_logout(self):
        self._authenticate("synthetic-pagination")
        values = []
        for index in range(27):
            course_id = f"course-{index:02d}"
            self.service.catalog_repository.upsert_course(
                course_id,
                f"课程 {index:02d}",
                f"教师 {index % 3}",
                term="2026-2027-1",
                department="测试院系",
                authorization_state="verified",
            )
            values.extend(({"course_id": course_id}, {"course_id": course_id}))
        self.service._store_authorized_catalog(values)

        first = self.service.authorized_catalog_snapshot(page=1, page_size=10)
        third = self.service.authorized_catalog_snapshot(page=3, page_size=10)
        self.assertEqual(first["course_count"], 27)
        self.assertEqual(first["page_count"], 3)
        self.assertEqual(len(first["courses"]), 10)
        self.assertEqual(len(third["courses"]), 7)
        self.assertEqual(len({item["course_id"] for item in first["courses"] + third["courses"]}), 17)

        logout = self.service.logout_fudan()
        self.assertEqual(logout["state"], "action_required")
        self.assertEqual(self.service.public_catalog_snapshot().get("courses"), {})

    def test_authorized_catalog_never_exposes_paths_urls_or_credentials(self):
        self._authenticate("synthetic-boundary")
        self.service.catalog_repository.upsert_course("course-safe", "Synthetic", "Teacher")
        self.service.catalog_repository.upsert_lecture("course-safe", {
            "sub_id": "lecture-safe", "sub_title": "Lecture", "has_playback": True,
            "vtt_path": "artifacts/private.vtt", "srt_path": "artifacts/private.srt",
            "raw_sensevoice_path": "artifacts/private.json",
            "video_url": "https://private.invalid/course",
            "cookie": "must-not-leak", "authorization": "must-not-leak",
        })
        self.service._store_authorized_catalog([{"course_id": "course-safe"}])

        snapshot = self.service.authorized_catalog_snapshot(page=1, page_size=10)
        lecture = snapshot["courses"][0]["lectures"][0]
        self.assertEqual(lecture["sub_id"], "lecture-safe")
        self.assertTrue(lecture["can_stream"])
        serialized = str(lecture).casefold()
        for marker in ("_path", "url", "cookie", "authorization", "must-not-leak"):
            self.assertNotIn(marker, serialized)

    def test_verified_refresh_atomically_removes_stale_unscoped_catalog(self):
        self._authenticate("synthetic-refresh")
        self.service.catalog_repository.upsert_course("stale", "Stale")
        self.service.catalog_repository.upsert_lecture(
            "stale", {"sub_id": "stale-lecture", "has_playback": True}
        )
        client = type("Client", (), {})()
        client.check_alive = lambda: True
        client.list_authorized_courses = lambda **_kwargs: [{
            "course_id": "current",
            "title": "Current",
            "teacher": "Teacher",
            "term": "2026",
            "department": "Department",
            "authorization_state": "verified",
            "lectures": [{
                "sub_id": "current-lecture",
                "sub_title": "Lecture",
                "has_playback": True,
            }],
        }]
        with self.service._lock:
            self.service._client = client
        self.service.client = lambda: client

        result = self.service.discover_authorized_courses()

        self.assertEqual([item["course_id"] for item in result], ["current"])
        self.assertEqual(
            [item["course_id"] for item in self.service.catalog_repository.courses()],
            ["current"],
        )
        self.assertIsNone(self.service.catalog_repository.get_lecture("stale-lecture"))


if __name__ == "__main__":
    unittest.main()
