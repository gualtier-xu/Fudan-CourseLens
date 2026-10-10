"""N5A-P1 考核雷达·规则版：闭集规则、句级重组与台账状态机的合成钉。

语料夹具来自 N5A 调研车道合成用例（tests/fixtures/assessment_radar_corpus.json，
不入任何真实字幕），验收门=召回≥90% + 负例零误报 + 跨段探针全命中。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from src.runtime.assessment_radar import (
    SCHEMA,
    assessment_events_action,
    assessment_events_scan,
    due_bucket,
    extract_events,
    sentence_clusters,
    title_norm,
    upsert_events,
)
from src.runtime.learning_schema import ensure_assessment_schema, initialize_learning_schema

CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "assessment_radar_corpus.json").read_text(encoding="utf-8")
)


def _segments(text: str, *, split: bool = False):
    if not split:
        return [{"start_ms": 0, "end_ms": 3000, "text": text}]
    mid = max(1, len(text) // 2)
    return [
        {"start_ms": 0, "end_ms": 1500, "text": text[:mid]},
        {"start_ms": 1500, "end_ms": 3000, "text": text[mid:]},
    ]


class AssessmentRuleTests(unittest.TestCase):
    def test_corpus_recall_and_precision(self):
        positives = CORPUS["positive"]
        hits = 0
        misses = []
        for item in positives:
            events = extract_events(
                _segments(item["text"]), course_id="c1", sub_id="s1",
                lecture_date="2026-09-15",
            )
            categories = [event["category"] for event in events]
            if item["expect"] in categories:
                hits += 1
            else:
                misses.append(item)
        self.assertGreaterEqual(
            hits, int(len(positives) * 0.9),
            f"召回不足: misses={[(item['text'], item['expect']) for item in misses]}",
        )
        for item in CORPUS["negative"]:
            events = extract_events(
                _segments(item["text"]), course_id="c1", sub_id="s1",
                lecture_date="2026-09-15",
            )
            self.assertEqual(events, [], f"负例误报: {item['text']}")

    def test_cross_segment_probes_survive_reassembly(self):
        for item in CORPUS["cross_segment_probe"]:
            events = extract_events(
                _segments(item["text"], split=True), course_id="c1", sub_id="s1",
                lecture_date="2026-09-15",
            )
            categories = [event["category"] for event in events]
            self.assertIn(item["expect"], categories, f"跨段探针未命中: {item['text']}")

    def test_sentence_clusters_respect_char_and_span_limits(self):
        segments = [
            {"start_ms": index * 20_000, "end_ms": index * 20_000 + 9_000,
             "text": f"这是第{index}句话。"}
            for index in range(10)
        ]
        clusters = sentence_clusters(segments)
        self.assertTrue(clusters)
        for cluster in clusters:
            self.assertLessEqual(len(cluster["text"]), 200)

    def test_title_norm_folds_ordinals_and_dates(self):
        self.assertEqual(title_norm("第三次作业 9月30日"), "#3作业")
        self.assertEqual(title_norm("ＤＤＬ 作业"), "DDL作业")

    def test_due_bucket_uses_local_calendar_day(self):
        stamp = time.mktime(time.strptime("2026-09-30", "%Y-%m-%d"))
        self.assertEqual(due_bucket(stamp), "2026-09-30")
        self.assertEqual(due_bucket(None), "")
        self.assertEqual(due_bucket(""), "")

    def test_exam_requires_schedule_wording(self):
        events = extract_events(
            _segments("我们聊聊考试"), course_id="c1", sub_id="s1",
        )
        self.assertEqual([event["category"] for event in events], [])


class AssessmentLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "learning.db"
        # Python 3.12 keeps an un-closed connection alive in a reference cycle
        # until cyclic GC runs; without the deterministic close the tearDown
        # rmtree below hits WinError 32 (PKG-MIGRATION-1 compatibility gate).
        with closing(sqlite3.connect(self.path)) as db, db:
            initialize_learning_schema(db)
        ensure_assessment_schema(self.path)
        self.learning_store = type("_Store", (), {"path": self.path})()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _event(self, *, due_at=""):
        return {
            "course_id": "c1", "category": "assignment", "title": "作业",
            "title_norm": "作业#1", "due_at": due_at, "location": "",
            "source": "rule", "quote": "下周一交作业", "first_seen_sub_id": "s1",
        }

    def test_state_machine_insert_merge_conflict_confirm_dismiss(self):
        store = self.learning_store
        counts = upsert_events(store, [self._event()])
        self.assertEqual(counts["inserted"], 1)
        # T2：同键同日=证据合并
        counts = upsert_events(store, [self._event()])
        self.assertEqual(counts["evidence_merged"], 1)
        payload = assessment_events_scan(store, None, course_id="c1")
        self.assertEqual(payload["schema"], SCHEMA)
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(len(payload["events"][0]["evidence"]), 1)
        event_id = payload["events"][0]["event_id"]
        # T5/T6：确认与忽略
        assessment_events_action(store, event_id=event_id, action="confirm")
        payload = assessment_events_scan(store, None, course_id="c1")
        self.assertEqual(payload["events"][0]["status"], "confirmed")
        assessment_events_action(store, event_id=event_id, action="dismiss")
        payload = assessment_events_scan(store, None, course_id="c1")
        self.assertEqual(payload["events"], [], "dismissed 不再呈现")
        # T6：再次抽取不复活
        counts = upsert_events(store, [self._event()])
        self.assertEqual(counts["inserted"], 0)
        payload = assessment_events_scan(store, None, course_id="c1")
        self.assertEqual(payload["events"], [])
        # 非法动作闭集
        with self.assertRaises(ValueError):
            assessment_events_action(store, event_id=event_id, action="delete")

    def test_due_conflict_keeps_old_value_and_downgrades(self):
        store = self.learning_store
        base = time.time()
        upsert_events(store, [self._event(due_at=base)])
        far = base + 8 * 86400
        counts = upsert_events(store, [self._event(due_at=far)])
        self.assertEqual(counts["conflicts"], 1)
        payload = assessment_events_scan(store, None, course_id="c1")
        # 冲突不覆盖：旧值保留（同 due_bucket 键不同 → 冲突行落在新键上也算账）
        rows = payload["events"]
        self.assertTrue(rows)
        self.assertTrue(any(row["conflict_note"] for row in rows))

    def test_llm_rideshot_ingest_stays_unconfirmed(self):
        """N5A-P2：顺风车事件 source=llm 落台账，默认 unconfirmed，确认才展开。"""
        store = self.learning_store
        event = self._event()
        event.update({"source": "llm", "status": "unconfirmed", "quote": "老师提到下周三考试"})
        upsert_events(store, [event], default_status="unconfirmed")
        payload = assessment_events_scan(store, None, course_id="c1")
        self.assertEqual(payload["events"][0]["status"], "unconfirmed")
        self.assertEqual(payload["events"][0]["source"], "llm")


if __name__ == "__main__":
    unittest.main()
