from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.runtime.student_features import (
    create_bookmark,
    ensure_student_feature_schema,
    list_bookmarks,
    list_quiz_items,
    list_review_plans,
    save_quiz_items,
    save_review_plan,
    set_bookmark_resolution,
    update_bookmark_explanation,
    update_bookmark_task,
)


class StudentFeatureWorkflowTests(unittest.TestCase):
    def test_bookmark_tracks_resolution_separately_from_explanation_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            bookmark = create_bookmark(
                path, course_id="1", sub_id="2", start_ms=1000, end_ms=3000,
                evidence=[{"citation_id": "r1", "text": "证据"}], input_hash="a" * 64,
            )
            queued = update_bookmark_task(
                path, bookmark_id=bookmark["bookmark_id"], task_id="task-1",
                explanation_state="queued", increment_attempt=True,
            )
            self.assertEqual(queued["explanation_state"], "queued")
            self.assertEqual(queued["attempt"], 1)
            resolved = set_bookmark_resolution(path, bookmark_id=bookmark["bookmark_id"], resolved=True)
            self.assertEqual(resolved["resolution_status"], "resolved")
            self.assertEqual(resolved["explanation_state"], "queued")
            reopened = set_bookmark_resolution(path, bookmark_id=bookmark["bookmark_id"], resolved=False)
            self.assertEqual(reopened["resolution_status"], "open")

    def test_bookmark_quiz_and_review_plan_are_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            ensure_student_feature_schema(path)

            bookmark = create_bookmark(
                path, course_id="1", sub_id="2", start_ms=1000, end_ms=3000, note="why"
            )
            explained = update_bookmark_explanation(
                path,
                bookmark_id=bookmark["bookmark_id"],
                explanation={"answer": "grounded", "citations": [{"start_seconds": 1.0}]},
            )
            self.assertEqual(explained["status"], "explained")
            self.assertEqual(list_bookmarks(path)[0]["explanation"]["answer"], "grounded")

            quiz = {
                "quiz_id": "quiz-1", "course_id": "1", "sub_id": "2",
                "question_type": "short_answer", "question": "Q", "answer": "A",
                "explanation": "E", "difficulty": "medium",
                "evidence": {"start_ms": 1000, "end_ms": 3000},
            }
            save_quiz_items(path, [quiz])
            self.assertEqual(list_quiz_items(path, sub_id="2")[0]["evidence"]["start_ms"], 1000)

            plan = save_review_plan(
                path,
                title="Review",
                exam_at=2_000_000_000,
                available_minutes=60,
                scope={"course_id": "1", "sub_id": "2"},
                steps=[{"kind": "watch", "minutes": 20}],
            )
            self.assertEqual(list_review_plans(path)[0]["plan_id"], plan["plan_id"])


if __name__ == "__main__":
    unittest.main()


class ExamSpecializationMetaTests(unittest.TestCase):
    """U10 期末特化：retention 0.9 + max_interval_days=剩余天数/2（纯本地）。"""

    def test_plan_carries_retention_target_and_half_remaining_interval(self):
        import time as _time
        from src.runtime.student_features import ensure_student_feature_schema, save_review_plan

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            ensure_student_feature_schema(path)
            now = _time.time()
            exam_at = now + 10 * 86400
            value = save_review_plan(
                path, title="期末复习", exam_at=exam_at, available_minutes=45,
                scope={}, steps=[{"order": 1, "kind": "watch", "title": "x"}],
            )
            self.assertEqual(value["retention_target"], 0.9)
            self.assertEqual(value["max_interval_days"], 5)
            # 已过期：间隔下限 1，不再为负
            past = save_review_plan(
                path, title="补复习", exam_at=now - 86400, available_minutes=45,
                scope={}, steps=[],
            )
            self.assertEqual(past["max_interval_days"], 1)
