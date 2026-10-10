from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.runtime.student_features import (
    assessment_attempts,
    assessment_items_from_quizzes,
    build_review_steps,
    create_bookmark,
    delete_bookmark,
    ensure_student_feature_schema,
    list_bookmarks,
    save_quiz_items,
    validate_quiz_items,
)


class StudentFeatureTests(unittest.TestCase):
    def test_bookmark_and_review_steps_are_local_and_timestamped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            ensure_student_feature_schema(path)
            bookmark = create_bookmark(path, course_id="1", sub_id="2", start_ms=1000, end_ms=2000)
            self.assertEqual(bookmark["start_ms"], 1000)
            db = sqlite3.connect(path)
            try:
                self.assertEqual(db.execute("select count(*) from bookmarks").fetchone()[0], 1)
            finally:
                db.close()
        steps = build_review_steps(
            chapters=[{"title": "基础", "start_seconds": 30}],
            quiz_items=[{"quiz_id": "q1", "question": "重点?"}],
            exam_at=9999999999,
            available_minutes=30,
        )
        self.assertTrue(steps)
        self.assertEqual(steps[0]["kind"], "watch")

    def test_bookmark_delete_is_physical_and_idempotent_unknown_404(self):
        """PLAYER-UX-1④：删除=物理移除；未知 id KeyError（路由层映射 404）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            ensure_student_feature_schema(path)
            first = create_bookmark(path, course_id="c1", sub_id="s1", start_ms=1000, end_ms=1000, note="没听懂")
            create_bookmark(path, course_id="c1", sub_id="s1", start_ms=9000, end_ms=9000, note="没听懂")
            result = delete_bookmark(path, bookmark_id=first["bookmark_id"])
            self.assertEqual(result, {"bookmark_id": first["bookmark_id"], "deleted": True})
            remaining = list_bookmarks(path, sub_id="s1")
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["start_ms"], 9000)
            with self.assertRaises(KeyError):
                delete_bookmark(path, bookmark_id=first["bookmark_id"])
            with self.assertRaises(KeyError):
                delete_bookmark(path, bookmark_id="no-such-id")


class AssessmentProjectionTests(unittest.TestCase):
    """N7A：本地出题 → AssessmentItem 形状投影；作答记录与题目定义彼此独立。"""

    def _store(self, path):
        ensure_student_feature_schema(path)
        evidence = {
            "start_ms": 1000, "end_ms": 4000, "text": "链式聚合动力学",
            "text_hash": hashlib.sha256("链式聚合动力学".encode("utf-8")).hexdigest(),
            "prompt_version": "quiz-recall-v2",
        }
        items = validate_quiz_items([{
            "quiz_id": "q1", "course_id": "c1", "sub_id": "s1",
            "question_type": "short_answer", "question": "用自己的话复述要点。",
            "answer": "链式聚合动力学", "evidence": evidence,
        }])
        save_quiz_items(path, items)
        return items

    def test_quiz_projects_to_assessment_shape_with_ai_generated_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            self._store(path)
            projected = assessment_items_from_quizzes(path, course_id="c1")
            self.assertEqual(len(projected), 1)
            item = projected[0]
            self.assertEqual(item["item_id"], "quiz:q1")
            self.assertEqual(item["kind"], "quiz")
            self.assertEqual(item["answer_source"], "ai_generated",
                             "本地出题不是官方答案，绝不冒充 official")
            self.assertEqual(item["status"], "answer_available")
            self.assertEqual(item["document_id"], "",
                             "quiz 没有文档身份，不发明合同 cka: 引用")
            self.assertEqual(item["evidence_refs"][0]["kind"], "transcript")
            self.assertEqual(item["evidence_refs"][0]["start_ms"], 1000)

    def test_attempts_stay_independent_from_the_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            self._store(path)
            before = assessment_items_from_quizzes(path, course_id="c1")
            self.assertEqual(assessment_attempts(path), [])
            with closing(sqlite3.connect(path)) as db, db:
                db.execute(
                    "INSERT INTO quiz_attempts(attempt_id,quiz_id,answer,correct,confidence,created_at)"
                    " VALUES('a1','q1','我的复述',0,3,1.0)"
                )
            attempts = assessment_attempts(path, quiz_id="q1")
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["correct"], 0)
            self.assertEqual(assessment_items_from_quizzes(path, course_id="c1"), before,
                             "写作答不得改变题目投影")

    def test_projection_filters_by_course_and_sub_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            self._store(path)
            self.assertEqual(len(assessment_items_from_quizzes(path, course_id="c1")), 1)
            self.assertEqual(assessment_items_from_quizzes(path, course_id="c2"), [])
            self.assertEqual(assessment_items_from_quizzes(path, course_id="c1", sub_id="s9"), [])


if __name__ == "__main__":
    unittest.main()
