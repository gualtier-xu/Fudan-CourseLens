"""D7 学习洞察事件流：闭集校验、幂等写入、按讲次读取与一键抹除。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.runtime.student_features import (
    clear_watch_events,
    insert_watch_events,
    list_watch_events,
)


class WatchEventsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self._tmp.name) / "learning.sqlite3")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_insert_accepts_closed_set_and_skips_invalid_rows(self) -> None:
        inserted = insert_watch_events(
            self.path,
            course_id="course-1",
            sub_id="sub-1",
            events=[
                {"event": "seek_back", "position_ms": 12000, "playback_rate": 1.0, "occurred_at": 100.0},
                {"event": "pause", "position_ms": 14000, "playback_rate": 1.0, "occurred_at": 101.0},
                # 非法项逐条跳过：闭集外事件 / 非法倍率 / 畸形数值
                {"event": "fast_forward", "position_ms": 15000, "playback_rate": 1.0},
                {"event": "replay", "position_ms": 16000, "playback_rate": 0},
                {"event": "slow_rate", "position_ms": "abc"},
                None,
            ],
        )
        self.assertEqual(inserted, 2)
        events = list_watch_events(self.path, sub_id="sub-1")
        self.assertEqual([item["event"] for item in events], ["seek_back", "pause"])
        self.assertEqual(events[0]["position_ms"], 12000)

    def test_replayed_batches_are_idempotent(self) -> None:
        batch = [
            {"event": "replay", "position_ms": 30000, "playback_rate": 1.0, "occurred_at": 200.0},
            {"event": "slow_rate", "position_ms": 32000, "playback_rate": 0.75, "occurred_at": 201.0},
        ]
        first = insert_watch_events(self.path, course_id="course-1", sub_id="sub-1", events=batch)
        second = insert_watch_events(self.path, course_id="course-1", sub_id="sub-1", events=batch)
        self.assertEqual(first, 2)
        self.assertEqual(second, 0, "同一批重放零新增（INSERT OR IGNORE 幂等）")
        self.assertEqual(len(list_watch_events(self.path, sub_id="sub-1")), 2)

    def test_list_is_scoped_to_lecture(self) -> None:
        insert_watch_events(self.path, course_id="c", sub_id="sub-a", events=[
            {"event": "pause", "position_ms": 1000, "playback_rate": 1.0, "occurred_at": 1.0},
        ])
        insert_watch_events(self.path, course_id="c", sub_id="sub-b", events=[
            {"event": "seek_back", "position_ms": 2000, "playback_rate": 1.0, "occurred_at": 2.0},
        ])
        self.assertEqual(len(list_watch_events(self.path, sub_id="sub-a")), 1)
        self.assertEqual(len(list_watch_events(self.path, sub_id="sub-b")), 1)

    def test_clear_scopes_and_reports_deleted_rows(self) -> None:
        insert_watch_events(self.path, course_id="c", sub_id="sub-a", events=[
            {"event": "pause", "position_ms": 1000, "playback_rate": 1.0, "occurred_at": 1.0},
        ])
        insert_watch_events(self.path, course_id="c", sub_id="sub-b", events=[
            {"event": "replay", "position_ms": 2000, "playback_rate": 1.0, "occurred_at": 2.0},
        ])
        self.assertEqual(clear_watch_events(self.path, sub_id="sub-a"), 1)
        self.assertEqual(len(list_watch_events(self.path, sub_id="sub-a")), 0)
        self.assertEqual(clear_watch_events(self.path), 1, "空 sub_id=一键抹除全部")
        self.assertEqual(clear_watch_events(self.path), 0)


if __name__ == "__main__":
    unittest.main()
