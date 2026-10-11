"""SRC-CLEANUP-1 U1: 域 snapshot 按序判态——已越过阶段=complete 而非 waiting。

串行阶段轨上，进度进入后段后前段最后一次上报可能不足 100%（断点恢复/
聚合上报）；按序判态后前端 tasks-drawer 的「waiting 且位于非 waiting 之前
→ completed」派生不再必要（U⑨ 三态语义的域层根修）。
"""

from __future__ import annotations

import unittest

from src.runtime.progress import ProgressTracker


def _segment_states(tracker: ProgressTracker) -> dict[str, str]:
    progress, _ = tracker.snapshot()
    return {segment["id"]: segment["state"] for segment in progress["segments"]}


class PhaseStateOrderTests(unittest.TestCase):
    def test_passed_phase_with_partial_percent_reports_complete(self):
        tracker = ProgressTracker("subtitle")
        tracker.update("prepare", 60.0)
        tracker.update("recognize", 10.0)
        states = _segment_states(tracker)
        self.assertEqual(states["prepare"], "complete")
        self.assertEqual(states["recognize"], "running")
        self.assertEqual(states["write"], "waiting")

    def test_resume_rebuild_with_tail_phase_marks_passed_prefix_complete(self):
        # 断点恢复重建：持久 segments 里 recognize 仅 60%，phase 已指向 write
        persisted = {
            "percent": 62.0,
            "phase": {"id": "write"},
            "segments": [
                {"id": "prepare", "percent": 100.0},
                {"id": "recognize", "percent": 60.0},
            ],
        }
        tracker = ProgressTracker("subtitle", initial_progress=persisted)
        states = _segment_states(tracker)
        self.assertEqual(states["prepare"], "complete")
        self.assertEqual(states["recognize"], "complete")
        self.assertEqual(states["write"], "running")

    def test_current_partial_and_fresh_tracker_semantics(self):
        tracker = ProgressTracker("subtitle")
        tracker.update("recognize", 40.0)
        states = _segment_states(tracker)
        self.assertEqual(states["recognize"], "running")
        self.assertEqual(states["write"], "waiting")

        fresh = ProgressTracker("subtitle")
        states = _segment_states(fresh)
        self.assertEqual(sorted(set(states.values())), ["waiting"])

    def test_complete_marks_all_segments_complete(self):
        tracker = ProgressTracker("subtitle")
        tracker.update("prepare", 50.0)
        tracker.complete()
        states = _segment_states(tracker)
        self.assertEqual(set(states.values()), {"complete"})


if __name__ == "__main__":
    unittest.main()
