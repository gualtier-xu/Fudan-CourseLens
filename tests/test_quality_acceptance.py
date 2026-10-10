from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.quality_acceptance import (
    STATE_SCHEMA,
    automatic_quality_checks,
    complete_lecture_checks,
    macro_f1_from_counts,
    public_selection,
    select_quality_samples,
    selection_digest,
    subtitle_coverage,
)
from src.runtime.learning_store import LearningStore


class FakeCatalog:
    def __init__(self) -> None:
        self._courses = [
            {"course_id": "course-a", "authorization_state": "verified"},
            {"course_id": "course-b", "authorization_state": "verified"},
            {"course_id": "course-c", "authorization_state": "verified"},
            {"course_id": "course-x", "authorization_state": "unknown"},
        ]
        self._lectures = {
            "course-a": [
                {"sub_id": "a-short", "duration_seconds": 100, "has_playback": True},
                {"sub_id": "a-long", "duration_seconds": 900, "has_playback": True},
            ],
            "course-b": [
                {"sub_id": "b", "duration_seconds": 500, "has_playback": True}
            ],
            "course-c": [
                {"sub_id": "c", "duration_seconds": 400, "has_playback": True}
            ],
            "course-x": [
                {"sub_id": "x", "duration_seconds": 2000, "has_playback": True}
            ],
        }

    def courses(self):
        return list(self._courses)

    def lectures_for_course(self, course_id):
        return list(self._lectures.get(course_id, []))


def passed_state() -> dict:
    samples = {}
    for index in range(3):
        samples[str(index)] = {
            "media": {"head_status": 200},
            "automatic_full": {
                "status": "passed",
                "observation": {"coverage": 0.96},
                "cleanup": {"cleanup_state": "complete"},
            },
            "summary_full": {
                "status": "passed",
                "observation": {
                    "input_hash": "a" * 64,
                    "chapters_monotonic": True,
                    "chapters_cover_lecture": True,
                    "ocr_page_count": 7,
                },
                "cleanup": {"cleanup_state": "complete"},
            },
            "quiz_count": 2,
        }
    return {"schema": STATE_SCHEMA, "stage": "review_ready", "samples": samples}


class QualityAcceptanceTests(unittest.TestCase):
    def test_selector_uses_longest_and_three_distinct_verified_courses(self):
        samples = select_quality_samples(FakeCatalog())
        self.assertEqual(samples[0]["sub_id"], "a-long")
        self.assertEqual(len(samples), 3)
        self.assertEqual(len({item["course_id"] for item in samples}), 3)
        rendered = json.dumps(public_selection(samples))
        self.assertNotIn("course-a", rendered)
        self.assertNotIn("a-long", rendered)
        self.assertEqual(selection_digest(samples), selection_digest(samples))

    def test_subtitle_coverage_merges_overlaps_and_clips_to_duration(self):
        coverage = subtitle_coverage(
            [
                {"start_ms": 0, "end_ms": 6000},
                {"start_ms": 5000, "end_ms": 9000},
                {"start_ms": 9000, "end_ms": 12_000},
            ],
            10,
        )
        self.assertEqual(coverage, 1.0)

    def test_automatic_checks_require_every_cleanup_and_threshold(self):
        state = passed_state()
        self.assertTrue(all(automatic_quality_checks(state).values()))
        state["samples"]["1"]["automatic_full"]["observation"]["coverage"] = 0.94
        self.assertFalse(
            automatic_quality_checks(state)["subtitle_coverage_at_least_95_percent"]
        )

    def test_complete_lecture_checks_need_one_full_sample_and_three_media_probes(self):
        state = passed_state()
        del state["samples"]["1"]["automatic_full"]
        del state["samples"]["1"]["summary_full"]
        del state["samples"]["1"]["quiz_count"]
        del state["samples"]["2"]["automatic_full"]
        del state["samples"]["2"]["summary_full"]
        del state["samples"]["2"]["quiz_count"]
        checks = complete_lecture_checks(state, "0")
        self.assertTrue(all(checks.values()))
        del state["samples"]["2"]["media"]
        self.assertFalse(
            complete_lecture_checks(state, "0")["three_sample_media_probes_passed"]
        )

    def test_macro_f1_uses_both_classes(self):
        self.assertEqual(macro_f1_from_counts(10, 0, 10, 0), 1.0)
        self.assertLess(macro_f1_from_counts(10, 10, 0, 0), 0.80)

    def test_imported_last_chapter_covers_the_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LearningStore(Path(directory) / "learning.db")
            store.replace_transcript_segments(
                "lecture",
                source_path="synthetic.vtt",
                source_mtime_ns=1,
                source_size=1,
                segments=[
                    {"start_ms": 0, "end_ms": 60_000, "text": "first"},
                    {"start_ms": 60_000, "end_ms": 120_000, "text": "second"},
                ],
            )
            store.import_remote_summary(
                course_id="course",
                sub_id="lecture",
                input_hash="a" * 64,
                model="model",
                markdown="summary",
                chapters=[
                    {"start_ms": 0, "title": "one"},
                    {"start_ms": 60_000, "title": "two"},
                ],
                ppt_pages=[],
            )
            artifact = store.find_ai_artifact("lecture", "lecture_chapters")
        chapters = artifact["content"]["chapters"]
        self.assertEqual(chapters[-1]["end_ms"], 120_000)

    def test_state_example_contains_no_plain_identifiers(self):
        samples = select_quality_samples(FakeCatalog())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(
                json.dumps({"schema": STATE_SCHEMA, "selection": public_selection(samples)}),
                encoding="utf-8",
            )
            rendered = path.read_text(encoding="utf-8")
        self.assertNotIn("course-a", rendered)
        self.assertNotIn("a-long", rendered)


if __name__ == "__main__":
    unittest.main()
