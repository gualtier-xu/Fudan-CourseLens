"""Evidence-consumer honesty checks for quizzes, review plans, and Q&A citations.

Synthetic data only: these tests cover evidence identity, hash separation,
non-fabrication, and tamper rejection in the local learning consumers.  They
make no claim about learning efficacy.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from src.runtime.student_features import (
    build_quiz_items,
    build_review_steps,
    ensure_student_feature_schema,
    evidence_answer,
    list_quiz_items,
    save_quiz_items,
    validate_grounded_answer,
    validate_quiz_items,
)

DOC_SOURCE_HASH = "b" * 64
EVIDENCE_ID_A = "seg:0123456789ab"
EVIDENCE_ID_B = "seg:ffffffffffff"
CHAPTER_EVIDENCE_ID = "chk:0123456789ab"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class QuizIdentityTests(unittest.TestCase):
    def test_quiz_identity_is_stable_under_input_reordering(self):
        segments = [
            {"start_ms": 1000, "end_ms": 3000, "text": "第一段足够长的课程原文内容。"},
            {"start_ms": 5000, "end_ms": 8000, "text": "第二段足够长的课程原文内容。"},
        ]
        forward = build_quiz_items(course_id="1", sub_id="2", segments=segments)
        backward = build_quiz_items(course_id="1", sub_id="2", segments=list(reversed(segments)))
        self.assertEqual({item["quiz_id"] for item in forward}, {item["quiz_id"] for item in backward})

    def test_quiz_identity_changes_when_evidence_changes(self):
        base = build_quiz_items(course_id="1", sub_id="2", segments=[{
            "start_ms": 1000, "end_ms": 3000, "text": "原始的课程原文内容足够长。",
        }])
        changed = build_quiz_items(course_id="1", sub_id="2", segments=[{
            "start_ms": 1000, "end_ms": 3000, "text": "修改后的课程原文内容足够长。",
        }])
        self.assertNotEqual(base[0]["quiz_id"], changed[0]["quiz_id"])

    def test_evidence_id_dominates_quiz_identity(self):
        same_text = "同一段足够长的课程原文内容。"
        items = build_quiz_items(course_id="1", sub_id="2", segments=[
            {"start_ms": 1000, "end_ms": 3000, "text": same_text, "evidence_id": EVIDENCE_ID_A},
            {"start_ms": 1000, "end_ms": 3000, "text": same_text, "evidence_id": EVIDENCE_ID_B},
        ])
        self.assertEqual(len(items), 2)
        self.assertEqual({item["evidence"]["evidence_id"] for item in items}, {EVIDENCE_ID_A, EVIDENCE_ID_B})

    def test_absolute_anchors_are_preserved_in_milliseconds(self):
        items = build_quiz_items(course_id="1", sub_id="2", segments=[{
            "start_ms": 123_456, "end_ms": 130_000, "text": "锚点保持毫秒绝对值的段落。",
        }])
        self.assertEqual(items[0]["evidence"]["start_ms"], 123_456)
        self.assertEqual(items[0]["evidence"]["end_ms"], 130_000)

    def test_legacy_seconds_segments_still_build(self):
        items = build_quiz_items(course_id="1", sub_id="2", segments=[{
            "start_seconds": 12.5, "end_seconds": 15, "text": "旧字段秒数片段，内容足够长。",
        }])
        self.assertEqual(items[0]["evidence"]["start_ms"], 12_500)
        self.assertEqual(items[0]["evidence"]["end_ms"], 15_000)


class HonestPromptTests(unittest.TestCase):
    def test_local_builder_emits_recall_prompts_without_invented_options(self):
        segments = [
            {"start_ms": 1000, "end_ms": 3000, "text": "普通段落，讲述基本概念的内容足够长。"},
            {"start_ms": 60_000, "end_ms": 65_000, "text": "考试重点：这里强调期末必考的关键结论。"},
        ]
        items = build_quiz_items(course_id="1", sub_id="2", segments=segments)
        self.assertTrue(items)
        for item in items:
            self.assertEqual(item["question_type"], "short_answer")
            self.assertNotIn("A.", item["question"])
            self.assertNotIn("B.", item["question"])
            self.assertEqual(item["answer"], item["evidence"]["text"])

    def test_stem_never_quotes_the_original_text(self):
        """C⑨：题干只给章节标题+提问——原文 80 字曾直接进题干，免答即抄。"""
        chapters = [{"chapter_id": "C0", "title": "聚合反应原理", "start_ms": 0, "end_ms": 600_000}]
        items = build_quiz_items(
            course_id="1", sub_id="2",
            segments=[{"start_ms": 65_000, "end_ms": 70_000, "text": "这段原文内容绝不允许出现在题干里。"}],
            chapters=chapters,
        )
        self.assertTrue(items)
        question = items[0]["question"]
        self.assertNotIn("绝不允许出现在题干里", question)
        self.assertIn("聚合反应原理", question, "章节标题进题干")
        self.assertIn("复述", question, "提问语义保留")
        self.assertEqual(items[0]["answer"], items[0]["evidence"]["text"], "原文只在答案/证据里")

    def test_stem_falls_back_to_anchor_without_chapters(self):
        items = build_quiz_items(
            course_id="1", sub_id="2",
            segments=[{"start_ms": 65_000, "end_ms": 70_000, "text": "没有章节信息时的原文内容也足够长。"}],
            chapters=[],
        )
        self.assertIn("01:05", items[0]["question"], "无章节时以锚点定位")
        self.assertNotIn("没有章节信息时的原文内容", items[0]["question"])

    def test_difficulty_semantics_are_unchanged(self):
        emphasis = build_quiz_items(course_id="1", sub_id="2", segments=[
            {"start_ms": 1000, "end_ms": 3000, "text": "考试重点：期末必考的关键结论内容。"},
        ])
        plain = build_quiz_items(course_id="1", sub_id="2", segments=[
            {"start_ms": 1000, "end_ms": 3000, "text": "普通段落，讲述基本概念的内容足够长。"},
        ])
        self.assertEqual(emphasis[0]["difficulty"], "hard")
        self.assertEqual(plain[0]["difficulty"], "medium")

    def test_document_fingerprint_and_text_digest_stay_separate(self):
        text = "携带文档指纹的课程原文内容。"
        items = build_quiz_items(course_id="1", sub_id="2", segments=[{
            "start_ms": 1000, "end_ms": 3000, "text": text, "source_hash": DOC_SOURCE_HASH,
        }])
        evidence = items[0]["evidence"]
        self.assertEqual(evidence["text_hash"], _digest(text))
        self.assertEqual(evidence["source_hash"], DOC_SOURCE_HASH)
        self.assertNotEqual(evidence["source_hash"], evidence["text_hash"])

    def test_incoming_text_digest_is_not_mistaken_for_document_fingerprint(self):
        text = "旧生产者把原文摘要塞进 source_hash 的片段。"
        items = build_quiz_items(course_id="1", sub_id="2", segments=[{
            "start_ms": 1000, "end_ms": 3000, "text": text, "source_hash": _digest(text),
        }])
        self.assertNotIn("source_hash", items[0]["evidence"])
        self.assertEqual(items[0]["evidence"]["text_hash"], _digest(text))


class ValidateQuizItemsTests(unittest.TestCase):
    def _item(self) -> dict:
        text = "被引用的课程原文内容足够长。"
        return {
            "quiz_id": "quiz-x", "course_id": "1", "sub_id": "2",
            "question_type": "short_answer", "question": "Q", "answer": text,
            "evidence": {
                "start_ms": 1000, "end_ms": 3000, "text": text,
                "text_hash": _digest(text), "prompt_version": "quiz-recall-v1",
                "evidence_id": EVIDENCE_ID_A, "source_hash": DOC_SOURCE_HASH,
            },
        }

    def test_tampered_text_is_rejected(self):
        item = self._item()
        item["evidence"]["text"] = "被替换后的篡改文本。"
        self.assertEqual(validate_quiz_items([item]), [])

    def test_malformed_text_hash_is_rejected(self):
        item = self._item()
        item["evidence"]["text_hash"] = "nothex"
        self.assertEqual(validate_quiz_items([item]), [])

    def test_malformed_document_fingerprint_is_rejected_in_new_format(self):
        item = self._item()
        item["evidence"]["source_hash"] = "deadbeef"
        self.assertEqual(validate_quiz_items([item]), [])

    def test_legacy_rows_with_text_digest_source_hash_stay_readable(self):
        text = "旧格式测验行，source_hash 曾是原文摘要。"
        legacy = {
            "quiz_id": "quiz-legacy", "course_id": "1", "sub_id": "2",
            "question_type": "short_answer", "question": "Q", "answer": text,
            "evidence": {"start_ms": 1000, "end_ms": 3000, "text": text,
                         "source_hash": _digest(text)},
        }
        kept = validate_quiz_items([legacy])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["evidence"]["source_hash"], _digest(text))
        tampered = dict(legacy)
        tampered["evidence"] = dict(legacy["evidence"], text="篡改后的不同文本内容。")
        self.assertEqual(validate_quiz_items([tampered]), [])

    def test_malformed_evidence_id_is_dropped_not_minted_and_safe_fields_survive(self):
        item = self._item()
        item["evidence"]["evidence_id"] = "seg:NOPE"
        kept = validate_quiz_items([item])
        self.assertEqual(len(kept), 1)
        self.assertNotIn("evidence_id", kept[0]["evidence"])
        self.assertEqual(kept[0]["evidence"]["source_hash"], DOC_SOURCE_HASH)
        self.assertEqual(kept[0]["evidence"]["text_hash"], _digest(item["evidence"]["text"]))
        self.assertEqual(kept[0]["evidence"]["prompt_version"], "quiz-recall-v1")


class ReviewStepEvidenceTests(unittest.TestCase):
    def test_chapter_timing_prefers_start_ms_and_accepts_legacy_seconds(self):
        steps = build_review_steps(
            chapters=[
                {"chapter_id": "C0001", "title": "新章节", "start_ms": 90_000, "end_ms": 120_000},
                {"title": "旧章节", "start_seconds": 30},
            ],
            quiz_items=[], exam_at=4_000_000_000, available_minutes=60,
        )
        by_title = {step["title"]: step for step in steps if step["kind"] == "watch"}
        self.assertEqual(by_title["新章节"]["start_ms"], 90_000)
        self.assertEqual(by_title["新章节"]["start_seconds"], 90.0)
        self.assertEqual(by_title["旧章节"]["start_seconds"], 30.0)
        self.assertEqual(by_title["旧章节"]["evidence"]["start_ms"], 30_000)

    def test_watch_steps_carry_chapter_identity_not_a_generic_object(self):
        chapter = {
            "chapter_id": "C0002", "title": "重点章节", "summary": "概要",
            "start_ms": 10_000, "end_ms": 20_000,
            "source_refs": [{"kind": "segment", "id": EVIDENCE_ID_A}],
            "evidence_id": CHAPTER_EVIDENCE_ID,
        }
        steps = build_review_steps(
            chapters=[chapter], quiz_items=[], exam_at=4_000_000_000, available_minutes=30,
        )
        evidence = steps[0]["evidence"]
        self.assertEqual(evidence["chapter_id"], "C0002")
        self.assertEqual(evidence["end_ms"], 20_000)
        self.assertEqual(evidence["source_refs"], [{"kind": "segment", "id": EVIDENCE_ID_A}])
        self.assertEqual(evidence["evidence_id"], CHAPTER_EVIDENCE_ID)

    def test_malformed_chapter_evidence_id_is_not_propagated(self):
        chapter = {"title": "章节", "start_ms": 10_000, "evidence_id": "chk:NOPE"}
        steps = build_review_steps(
            chapters=[chapter], quiz_items=[], exam_at=4_000_000_000, available_minutes=30,
        )
        self.assertNotIn("evidence_id", steps[0]["evidence"])

    def test_quiz_steps_carry_quiz_evidence_identity_not_a_generic_object(self):
        text = "错题对应的课程原文内容。"
        quiz = {
            "quiz_id": "quiz-1", "question": "Q",
            "evidence": {"start_ms": 5_000, "end_ms": 8_000, "text": text,
                         "text_hash": _digest(text), "evidence_id": EVIDENCE_ID_B},
        }
        steps = build_review_steps(
            chapters=[], quiz_items=[quiz], exam_at=4_000_000_000, available_minutes=30,
        )
        evidence = steps[0]["evidence"]
        self.assertEqual(evidence["source"], "quiz")
        self.assertEqual(evidence["start_ms"], 5_000)
        self.assertEqual(evidence["end_ms"], 8_000)
        self.assertEqual(evidence["evidence_id"], EVIDENCE_ID_B)
        self.assertEqual(evidence["text_hash"], _digest(text))


class GroundedAnswerTests(unittest.TestCase):
    def test_evidence_answer_keeps_refusal_and_preserves_evidence_id(self):
        declined = evidence_answer("q", [])
        self.assertFalse(declined["grounded"])
        self.assertEqual(declined["citations"], [])
        value = evidence_answer("q", [{
            "result_id": "r1", "course_id": "1", "sub_id": "2", "snippet": "原文",
            "target": {"start_seconds": 3}, "source_hash": _digest("原文"),
            "evidence_id": EVIDENCE_ID_A,
        }])
        self.assertTrue(value["grounded"])
        citation = value["citations"][0]
        self.assertEqual(citation["evidence_id"], EVIDENCE_ID_A)
        self.assertEqual(citation["start_seconds"], 3)

    def test_generated_quiz_evidence_cannot_become_authoritative_evidence(self):
        # 新格式测验证据的 source_hash 是文档指纹，无法通过被引原文的内容摘要校验
        text = "测验引用的原文内容。"
        quiz_evidence = {
            "citation_id": "quiz-1", "course_id": "1", "sub_id": "2",
            "start_ms": 1000, "end_ms": 3000, "text": text,
            "text_hash": _digest(text), "source_hash": DOC_SOURCE_HASH,
            "evidence_id": EVIDENCE_ID_A,
        }
        answer = {"answer": text, "grounded": True, "citations": ["quiz-1"]}
        value = validate_grounded_answer(answer, [quiz_evidence])
        self.assertFalse(value["grounded"])
        self.assertEqual(value["citations"], [])


class QuizPersistenceTests(unittest.TestCase):
    def test_quiz_roundtrip_keeps_additive_evidence_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            ensure_student_feature_schema(path)
            text = "入库后再读出的证据字段保持完整。"
            items = build_quiz_items(course_id="1", sub_id="2", segments=[{
                "start_ms": 1000, "end_ms": 3000, "text": text,
                "evidence_id": EVIDENCE_ID_A, "source_hash": DOC_SOURCE_HASH,
            }])
            save_quiz_items(path, items)
            rows = list_quiz_items(path, sub_id="2")
            self.assertEqual(rows[0]["quiz_id"], items[0]["quiz_id"])
            evidence = rows[0]["evidence"]
            self.assertEqual(evidence["evidence_id"], EVIDENCE_ID_A)
            self.assertEqual(evidence["source_hash"], DOC_SOURCE_HASH)
            self.assertEqual(evidence["text_hash"], _digest(text))
            self.assertEqual(evidence["start_ms"], 1000)
            self.assertEqual(evidence["end_ms"], 3000)


if __name__ == "__main__":
    unittest.main()
