from __future__ import annotations

import unittest

from src.runtime.student_features import build_quiz_items, build_review_steps, evidence_answer, evidence_packet, validate_grounded_answer


class StudentFeatureApiTests(unittest.TestCase):
    def test_evidence_answer_is_grounded_or_declines(self):
        self.assertFalse(evidence_answer("q", [])["grounded"])
        value = evidence_answer("q", [{
            "result_id": "r1", "course_id": "1", "sub_id": "2", "snippet": "原文",
            "target": {"start_seconds": 3}, "source_hash": "a" * 64,
        }])
        self.assertTrue(value["grounded"])
        self.assertEqual(value["citations"][0]["start_seconds"], 3)

    def test_quiz_items_keep_timestamp_evidence(self):
        values = build_quiz_items(course_id="1", sub_id="2", segments=[{
            "start_ms": 1000, "end_ms": 3000, "text": "这里是一个足够长的课程原文。",
        }])
        self.assertEqual(values[0]["evidence"]["start_ms"], 1000)
        self.assertEqual(values[0]["question_type"], "short_answer")
        self.assertEqual(len(values[0]["evidence"]["text_hash"]), 64)

    def test_remote_answer_citations_are_restricted_to_the_evidence_packet(self):
        evidence = evidence_packet([{
            "result_id": "r1", "course_id": "1", "sub_id": "2",
            "snippet": "原文", "target": {"start_seconds": 3},
        }])
        value = validate_grounded_answer({"answer": "回答", "grounded": True, "citations": ["r1", "other"]}, evidence)
        self.assertTrue(value["grounded"])
        self.assertEqual([item["citation_id"] for item in value["citations"]], ["r1"])

    def test_evidence_packet_and_citations_prefer_contract_evidence_identity(self):
        evidence_id = "seg:0123456789ab"
        packet = evidence_packet([{
            "result_id": "r1", "course_id": "1", "sub_id": "2",
            "snippet": "原文", "target": {"start_seconds": 3}, "evidence_id": evidence_id,
        }])
        self.assertEqual(packet[0]["evidence_id"], evidence_id)
        self.assertEqual(packet[0]["citation_id"], "r1")
        self.assertEqual(packet[0]["start_ms"], 3000, "引用保留绝对锚点")
        value = validate_grounded_answer({"answer": "回答", "grounded": True, "citations": ["r1"]}, packet)
        self.assertTrue(value["grounded"])
        self.assertEqual(value["citations"][0]["evidence_id"], evidence_id)
        self.assertEqual(value["citations"][0]["citation_id"], "r1")

    def test_legacy_results_keep_citation_id_fallback_without_fake_identity(self):
        packet = evidence_packet([{
            "result_id": "r1", "course_id": "1", "sub_id": "2",
            "snippet": "原文", "target": {"start_seconds": 3},
        }])
        self.assertNotIn("evidence_id", packet[0])
        malformed = evidence_packet([{
            "result_id": "r2", "course_id": "1", "sub_id": "2",
            "snippet": "原文", "target": {"start_seconds": 3}, "evidence_id": "seg:NOPE",
        }])
        self.assertNotIn("evidence_id", malformed[0])
        value = validate_grounded_answer({"answer": "回答", "grounded": True, "citations": ["r1", "ghost"]}, packet)
        self.assertEqual([item["citation_id"] for item in value["citations"]], ["r1"], "引用仍被限制在证据包内")
        self.assertTrue(all("evidence_id" not in item for item in value["citations"]))

    def test_grounded_answer_keeps_every_valid_citation_count_matches_evidence(self):
        """DEFECT-2-FIX-R2 残余风险守卫钉（POLISH-O13）：validate_grounded_answer
        曾因 return 缩进滑移把引用截断到首项且既有钉全数漏网——既有用例的存活
        引用恰好 ≤1 条，截断态输出与期望不可区分。本钉以「多存活引用计数与证据
        源对照」守住该回归面：合法引用必须逐条按序存活、计数与证据包一致。"""
        evidence = evidence_packet([
            {"result_id": f"r{i}", "course_id": "1", "sub_id": "2",
             "snippet": f"原文{i}", "target": {"start_seconds": i}}
            for i in range(1, 4)
        ])
        value = validate_grounded_answer(
            {"answer": "回答", "grounded": True, "citations": ["r1", "r2", "r3"]}, evidence,
        )
        self.assertTrue(value["grounded"])
        self.assertEqual([item["citation_id"] for item in value["citations"]], ["r1", "r2", "r3"])
        self.assertEqual(len(value["citations"]), len(evidence), "存活引用数与证据源对照（防截断回归）")

    def test_grounded_answer_citation_cap_is_eight_and_order_stable(self):
        """引用帽=8 为设计闭集（student_features.validate_grounded_answer）：
        超帽按序截断、前 8 条逐条存活——帽值或顺序变化必须是有意识的改动。"""
        evidence = evidence_packet([
            {"result_id": f"r{i}", "course_id": "1", "sub_id": "2",
             "snippet": f"原文{i}", "target": {"start_seconds": i}}
            for i in range(1, 10)
        ])
        value = validate_grounded_answer(
            {"answer": "回答", "grounded": True, "citations": [f"r{i}" for i in range(1, 10)]}, evidence,
        )
        self.assertTrue(value["grounded"])
        self.assertEqual(len(value["citations"]), 8)
        self.assertEqual([item["citation_id"] for item in value["citations"]], [f"r{i}" for i in range(1, 9)], "超帽按序保留前 8 条")

    def test_review_steps_prioritize_unwatched_emphasis_and_keep_evidence(self):
        steps = build_review_steps(
            chapters=[{"title": "普通内容", "start_seconds": 1}, {"title": "考试重点", "start_seconds": 100}],
            quiz_items=[], exam_at=4_000_000_000, available_minutes=30, watched_seconds=2,
        )
        self.assertEqual(steps[0]["start_seconds"], 100)
        self.assertEqual(steps[0]["evidence"]["start_ms"], 100000)


if __name__ == "__main__":
    unittest.main()
