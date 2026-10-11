from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.runtime.smart_playback import (
    CLASSIFIER_VERSION,
    classify_segments,
    classify_text,
    list_timeline,
    macro_f1,
    save_timeline,
    timeline_input_hash,
)


class SmartPlaybackTests(unittest.TestCase):
    def test_synthetic_label_set_meets_macro_f1_guardrail(self):
        samples = [
            ("这个结论是期末考试重点，也是必考考点", "exam"),
            ("下面看一道例题，我们计算一下结果", "example"),
            ("课后作业请在截止时间前提交", "homework"),
            ("现在开始点名，请学号末尾三位的同学回答", "roll_call"),
            ("课程通知：下周调课到另一间教室", "administrative"),
            ("听得到吗，刚才网络卡了一下", "chat"),
            ("翻到下一页，看这张幻灯片", "slide_transition"),
            ("先给出矩阵的定义，再说明它的性质", "knowledge"),
        ]
        expected = [label for _text, label in samples]
        predicted = [classify_text(text)["label"] for text, _label in samples]
        self.assertGreaterEqual(macro_f1(expected, predicted), 0.80)

    def test_exam_keyword_has_high_confidence_and_evidence(self):
        value = classify_text("这是考试重点和必考考点")
        self.assertEqual(value["label"], "exam")
        self.assertGreaterEqual(value["confidence"], 0.93)
        self.assertIn("考试重点", value["matched_terms"])

    def test_adjacent_same_label_segments_are_coalesced(self):
        values = classify_segments(
            course_id="course-1",
            sub_id="lecture-1",
            segments=[
                {"start_ms": 0, "end_ms": 5000, "text": "先给出定义"},
                {"start_ms": 6000, "end_ms": 10000, "text": "再说明性质"},
                {"start_ms": 20000, "end_ms": 24000, "text": "这是考试重点"},
            ],
        )
        self.assertEqual(len(values), 2)
        self.assertEqual(values[0]["start_ms"], 0)
        self.assertEqual(values[0]["end_ms"], 10000)
        self.assertEqual(values[1]["label"], "exam")

    def test_persisted_timeline_is_invalidated_by_transcript_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            source = [{"start_ms": 0, "end_ms": 5000, "text": "矩阵的定义"}]
            classified = classify_segments(course_id="c", sub_id="s", segments=source)
            saved = save_timeline(
                path,
                course_id="c",
                sub_id="s",
                transcript_segments=source,
                classified=classified,
            )
            self.assertEqual(saved["meta"]["classifier_version"], CLASSIFIER_VERSION)
            self.assertEqual(len(saved["segments"]), 1)
            changed_hash = timeline_input_hash([
                {"start_ms": 0, "end_ms": 5000, "text": "完全不同的字幕"},
            ])
            stale = list_timeline(path, sub_id="s", current_input_hash=changed_hash)
            self.assertTrue(stale["stale"])
            self.assertEqual(stale["segments"], [])

    def test_input_hash_is_deterministic_order_sensitive_and_whitespace_free(self):
        source = [
            {"start_ms": 0, "end_ms": 5000, "text": "先给出 定义"},
            {"start_ms": 6000, "end_ms": 10000, "text": "再说明性质"},
        ]
        self.assertEqual(timeline_input_hash(source), timeline_input_hash([
            {"start_ms": 0, "end_ms": 5000, "text": "先给出\n  定义"},
            {"start_ms": 6000, "end_ms": 10000, "text": "再说明性质"},
        ]), "连续空白折叠为单空格后文本等价则同哈希")
        self.assertNotEqual(
            timeline_input_hash(source),
            timeline_input_hash(list(reversed(source))),
            "段序参与哈希（时序错排必须判脏）",
        )
        self.assertEqual(
            timeline_input_hash(source),
            timeline_input_hash(
                source + [{"start_ms": 1, "end_ms": 2, "text": "   "}, {"start_ms": 2, "end_ms": 3}],
            ),
            "纯空白/缺文本段不进哈希",
        )

    def test_save_timeline_gate_keeps_out_of_set_labels_out_of_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            classified = [
                {"segment_id": "keep", "start_ms": 0, "end_ms": 100,
                 "label": "knowledge", "confidence": 0.9, "evidence": {}},
                {"segment_id": "drop", "start_ms": 200, "end_ms": 300,
                 "label": "made_up_label", "confidence": 0.9, "evidence": {}},
            ]
            saved = save_timeline(
                path, course_id="c", sub_id="s",
                transcript_segments=[{"start_ms": 0, "end_ms": 100, "text": "定义"}],
                classified=classified,
            )
            self.assertEqual(
                [row["segment_id"] for row in saved["segments"]], ["keep"],
                "闭集外标签永不落库",
            )
            self.assertEqual(
                saved["meta"]["segment_count"], 2,
                "现行为钉：segment_count 记分类输出条数（含被门剔除者）",
            )

    def test_classifier_default_bands_split_short_and_substantive_text(self):
        short = classify_text("好的")
        self.assertEqual(short["label"], "knowledge")
        self.assertEqual(short["confidence"], 0.55)
        self.assertEqual(short["reason"], "short_uncertain")
        long_text = classify_text("这一段讲解的是矩阵的相似对角化过程")
        self.assertEqual(long_text["label"], "knowledge")
        self.assertEqual(long_text["confidence"], 0.68)
        self.assertEqual(long_text["reason"], "substantive_default")

    def test_list_timeline_without_rows_returns_honest_default_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            empty = list_timeline(path, sub_id="s")
            self.assertFalse(empty["stale"])
            self.assertEqual(empty["segments"], [])
            self.assertEqual(empty["meta"]["segment_count"], 0)
            self.assertEqual(empty["meta"]["classifier_version"], CLASSIFIER_VERSION)


if __name__ == "__main__":
    unittest.main()
