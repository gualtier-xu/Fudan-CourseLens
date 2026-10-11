"""RR-BOOKMARK-1 回归钉：空白/缺正文书签拒绝创建（WINIT-1 实测误建 1 枚）。

缺陷：POST /api/v3/bookmarks 对未知讲次/零可依据内容（字幕、章节、讲义全空）
的请求照单落库——书签创建成功、时间轴出标记，但解释链对零证据书签恒
needs_context/bookmark_evidence_unavailable，学生的「没听懂」标记实际是废品。

钉三件事：
① 缺 course_id/sub_id → 拒绝（bookmark_request_invalid），不落库；
② 讲次零证据 → 创建时即拒绝（复用 explain 链同源闭集码
   bookmark_evidence_unavailable），不再等解释时才发现；
③ 有字幕正文的讲次 → 创建成功且证据非空（既有行为不劣化）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.application import CourseLensApplication
from src.runtime.student_features import list_bookmarks


class _GateApplication(CourseLensApplication):
    """窄适配器：真实 create_question_bookmark 校验链 + 桩段源，不装配整个应用。"""

    def __init__(self, root: Path, segments: list[dict]):
        self._segments = segments
        self.learning_store = SimpleNamespace(
            path=root / "learning.db",
            find_ai_artifact=lambda *args, **kwargs: None,
        )

    def subtitle_segments(self, sub_id: str) -> dict:
        return {"segments": self._segments}


class BookmarkCreationGateTests(unittest.TestCase):
    def test_missing_course_or_sub_id_is_rejected_without_persisting(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = _GateApplication(Path(tmp.name), [{"start_ms": 0, "end_ms": 1000, "text": "正文一句"}])
        for course_id, sub_id in (("", "s1"), ("c1", ""), ("  ", "s1"), ("c1", None)):
            with self.subTest(course_id=course_id, sub_id=sub_id):
                with self.assertRaises(ValueError) as rejected:
                    app.create_question_bookmark(course_id, sub_id, 0, 0, note="没听懂")
                self.assertEqual(str(rejected.exception), "bookmark_request_invalid")
        self.assertEqual(list_bookmarks(Path(tmp.name) / "learning.db"), [])

    def test_contentless_lecture_is_rejected_without_persisting(self):
        """WINIT-1 主案：讲次零可依据内容 → 创建即拒，不落库。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = _GateApplication(Path(tmp.name), [])
        with self.assertRaises(ValueError) as rejected:
            app.create_question_bookmark("c1", "s1", 61_000, 61_000, note="没听懂")
        self.assertEqual(str(rejected.exception), "bookmark_evidence_unavailable")
        self.assertEqual(list_bookmarks(Path(tmp.name) / "learning.db"), [])

    def test_lecture_with_transcript_still_creates_bookmark(self):
        """有字幕正文的讲次照常创建，且证据非空（既有行为不劣化）。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = _GateApplication(Path(tmp.name), [
            {"start_ms": 60_000, "end_ms": 75_000, "text": "概率分布、期望和方差是考试重点"},
        ])
        bookmark = app.create_question_bookmark("c1", "s1", 61_000, 61_000, note="没听懂")
        self.assertTrue(str(bookmark.get("bookmark_id") or ""))
        self.assertEqual(bookmark["sub_id"], "s1")
        self.assertTrue(bookmark.get("evidence"), "有正文讲次的证据不得为空")
        stored = list_bookmarks(Path(tmp.name) / "learning.db", sub_id="s1")
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["bookmark_id"], bookmark["bookmark_id"])


if __name__ == "__main__":
    unittest.main()
