from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.runtime.analytics import (
    analytics_summary,
    delete_analytics_term,
    estimate_fun_metrics,
    record_watch_event,
    set_analytics_enabled,
)


class LocalAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "learning.db"

    def tearDown(self):
        self.temp.cleanup()

    def test_recording_is_opt_in_and_stops_when_disabled(self):
        self.assertFalse(record_watch_event(
            self.path, course_id="1", sub_id="2", term="2026春",
            position_seconds=10, duration_seconds=100, playback_rate=1, completed=False,
        ))
        set_analytics_enabled(self.path, True)
        self.assertTrue(record_watch_event(
            self.path, course_id="1", sub_id="2", term="2026春",
            position_seconds=10, duration_seconds=100, playback_rate=1, completed=False,
            occurred_at=1_700_000_000,
        ))
        self.assertTrue(record_watch_event(
            self.path, course_id="1", sub_id="2", term="2026春",
            position_seconds=30, duration_seconds=100, playback_rate=1.5, completed=False,
            occurred_at=1_700_000_020,
        ))
        summary = analytics_summary(self.path, term="2026春")["summary"]
        self.assertEqual(summary["event_count"], 2)
        self.assertGreater(summary["watch_seconds"], 0)
        set_analytics_enabled(self.path, False)
        self.assertFalse(record_watch_event(
            self.path, course_id="1", sub_id="2", term="2026春",
            position_seconds=40, duration_seconds=100, playback_rate=1, completed=False,
        ))

    def test_term_delete_is_scoped(self):
        set_analytics_enabled(self.path, True)
        for index, term in enumerate(("2026春", "2026秋")):
            record_watch_event(
                self.path, course_id="1", sub_id=str(index), term=term,
                position_seconds=10, duration_seconds=100, playback_rate=1, completed=False,
                occurred_at=1_700_000_000 + index * 10,
            )
        self.assertEqual(delete_analytics_term(self.path, "2026春"), 1)
        self.assertEqual(analytics_summary(self.path, term="2026春")["summary"]["event_count"], 0)
        self.assertEqual(analytics_summary(self.path, term="2026秋")["summary"]["event_count"], 1)

    def test_fun_metrics_are_explicit_estimates_with_evidence(self):
        value = estimate_fun_metrics([{
            "course_id": "1", "sub_id": "2", "segments": [
                {"start_ms": 1000, "end_ms": 2000, "text": "同学们注意，下面开始点名签到"},
                {"start_ms": 3000, "end_ms": 4000, "text": "同学们注意，哪位同学来回答"},
            ],
        }])
        self.assertTrue(value["estimated"])
        self.assertEqual(value["label"], "AI 估算")
        self.assertEqual(value["roll_call_count"], 1)
        self.assertEqual(value["student_answer_count"], 1)
        self.assertTrue(value["roll_call_evidence"])


if __name__ == "__main__":
    unittest.main()
