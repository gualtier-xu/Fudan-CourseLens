"""Deterministic, evidence-grounded Lecture IR view (worker side)."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from courselens_worker.lecture_ir import build_lecture_ir
from courselens_worker.runner import _process_materialized_job
from shared.evidence_contract import (
    CONTRACT_ID,
    NAMESPACE_SEGMENT,
    NAMESPACE_SLIDE_ENTITY,
    NAMESPACE_SLIDE_EVENT,
    NAMESPACE_SOURCE,
    NAMESPACE_UNIT,
    compute_id,
    validate_document,
)

_PROVENANCE = {"producer": "test-producer", "model": "test-model", "config_hash": "0123456789abcdef"}
_DECK_ID = "deck-1"
_SLIDE_SHA_PREFIX = "c" * 64
_SOURCE_IDENTITY = {
    "kind": "recording",
    "origin": "external_import",
    "title": None,
    "duration_ms": 600_000,
    "source_sha256": "d" * 64,
}
_DOC_SOURCE_ID = compute_id(NAMESPACE_SOURCE, _SOURCE_IDENTITY)


def stamp_segment_id(start_ms, end_ms, text):
    return compute_id(NAMESPACE_SEGMENT, {
        "source_id": _DOC_SOURCE_ID,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "text": text,
        "lang": None,
        "no_speech": False,
        "producer": _PROVENANCE["producer"],
        "model": _PROVENANCE["model"],
        "config_hash": _PROVENANCE["config_hash"],
    })


def identified_segment(start_ms, end_ms, text):
    return {
        "start_ms": start_ms,
        "end_ms": end_ms,
        "text": text,
        "segment_id": stamp_segment_id(start_ms, end_ms, text),
    }


def slide_ids(created_sec, content_sha):
    entity = compute_id(NAMESPACE_SLIDE_ENTITY, {
        "source_id": _DOC_SOURCE_ID,
        "deck_id": _DECK_ID,
        "page": 1,
        "content_sha256": content_sha,
    })
    event = compute_id(NAMESPACE_SLIDE_EVENT, {
        "entity": entity,
        "start_ms": created_sec * 1000,
        "end_ms": None,
    })
    return entity, event


def identified_page(created_sec, text="幻灯片", content_sha=None, **extra):
    page = {
        "page_num": created_sec + 1,
        "created_sec": created_sec,
        "text": text,
        "source_sha256": content_sha or _SLIDE_SHA_PREFIX,
    }
    entity, event = slide_ids(page["created_sec"], page["source_sha256"])
    page.update({"entity_id": entity, "event_id": event, "deck_id": _DECK_ID})
    page.update(extra)
    return page


def chapter(title, start_ms, summary="章节概述"):
    return {"title": title, "start_ms": start_ms, "summary": summary}


def empty_view():
    return {"contract": CONTRACT_ID, "sections": [], "knowledge_units": [], "key_moments": []}


class BuilderUnitTests(unittest.TestCase):
    def test_empty_and_legacy_inputs_degrade_to_empty_view(self):
        self.assertEqual(build_lecture_ir(), empty_view())
        self.assertEqual(build_lecture_ir([], [], []), empty_view())
        # Legacy shapes: no contract IDs anywhere, yet anchors exist.
        legacy = build_lecture_ir(
            transcript=[{"start_ms": 0, "end_ms": 2000, "text": "旧文本"}],
            chapters=[chapter("第一章", 0)],
            ppt_pages=[{"page_num": 1, "created_sec": 0, "text": "旧幻灯片"}],
        )
        self.assertEqual(legacy, empty_view())

    def test_section_end_comes_from_next_anchor_then_evidence_end(self):
        transcript = [
            identified_segment(0, 30_000, "第一段"),
            identified_segment(60_000, 120_000, "第二段"),
        ]
        view = build_lecture_ir(
            transcript=transcript,
            chapters=[chapter("第一章", 0), chapter("第二章", 60_000)],
            ppt_pages=[identified_page(30)],
        )
        self.assertEqual(
            [item["time"] for item in view["sections"]],
            [{"start_ms": 0, "end_ms": 60_000}, {"start_ms": 60_000, "end_ms": 120_000}],
        )

    def test_unfounded_or_invalid_chapter_anchors_fail_closed(self):
        transcript = [identified_segment(0, 60_000, "只有一段")]
        view = build_lecture_ir(
            transcript=transcript,
            chapters=[
                chapter("无锚章节", 30_000),
                chapter("负锚", -1),
                chapter("假锚", True),
                chapter("坏锚", "0"),
                chapter("开头", 0),
            ],
            ppt_pages=[],
        )
        self.assertEqual(len(view["sections"]), 1)
        self.assertEqual(view["sections"][0]["title"], "开头")
        self.assertEqual(view["sections"][0]["time"], {"start_ms": 0, "end_ms": 60_000})

    def test_repeated_slide_events_are_preserved_and_duplicates_collapse(self):
        # Same slide content shown twice: two events, one entity.
        pages = [
            identified_page(1, content_sha=_SLIDE_SHA_PREFIX),
            identified_page(40, content_sha=_SLIDE_SHA_PREFIX),
            # A literal duplicate occurrence (identical identity) collapses.
            identified_page(1, content_sha=_SLIDE_SHA_PREFIX),
        ]
        view = build_lecture_ir(ppt_pages=pages)
        self.assertEqual(len(view["key_moments"]), 2)
        entities = {item["content"]["entity_id"] for item in view["key_moments"]}
        self.assertEqual(len(entities), 1)
        self.assertEqual(
            [item["time"]["start_ms"] for item in view["key_moments"]],
            [1_000, 40_000],
        )

    def test_key_moment_intervals_partition_the_evidence_range(self):
        pages = [identified_page(1), identified_page(10), identified_page(20)]
        view = build_lecture_ir(ppt_pages=pages)
        self.assertEqual(
            [item["time"] for item in view["key_moments"]],
            [
                {"start_ms": 1_000, "end_ms": 10_000},
                {"start_ms": 10_000, "end_ms": 20_000},
                {"start_ms": 20_000, "end_ms": 20_000},
            ],
        )

    def test_knowledge_units_split_when_the_active_slide_changes(self):
        transcript = [
            identified_segment(0, 8_000, "第一题"),
            identified_segment(9_000, 15_000, "还是第一题"),
            identified_segment(20_000, 30_000, "第二题"),
        ]
        pages = [identified_page(0, content_sha="a" * 64), identified_page(18, content_sha="b" * 64)]
        view = build_lecture_ir(
            transcript=transcript,
            chapters=[chapter("整课", 0)],
            ppt_pages=pages,
        )
        self.assertEqual(len(view["knowledge_units"]), 2)
        self.assertEqual(view["knowledge_units"][0]["time"], {"start_ms": 0, "end_ms": 15_000})
        self.assertEqual(view["knowledge_units"][1]["time"], {"start_ms": 20_000, "end_ms": 30_000})
        self.assertEqual(
            [len(unit["spans"]) for unit in view["knowledge_units"]], [2, 1]
        )

    def test_segments_without_identifiers_are_never_cited(self):
        transcript = [
            identified_segment(0, 10_000, "有身份"),
            {"start_ms": 10_000, "end_ms": 20_000, "text": "被改写丢失身份"},
        ]
        view = build_lecture_ir(
            transcript=transcript,
            chapters=[chapter("开头", 0)],
            ppt_pages=[],
        )
        section = view["sections"][0]
        self.assertEqual(section["spans"], [{"kind": "segment", "id": transcript[0]["segment_id"]}])
        self.assertEqual(len(view["knowledge_units"]), 1)
        self.assertEqual(len(view["knowledge_units"][0]["spans"]), 1)

    def test_malformed_evidence_identifiers_are_ignored(self):
        transcript = [{
            "start_ms": 0, "end_ms": 1000, "text": "坏身份",
            "segment_id": "seg:ZZZZ",
        }]
        pages = [{
            "page_num": 1, "created_sec": 0, "text": "页",
            "event_id": "slevt:ABCDEF012345", "entity_id": "slent:short",
        }]
        view = build_lecture_ir(transcript=transcript, chapters=[chapter("开头", 0)], ppt_pages=pages)
        self.assertEqual(view, empty_view())

    def test_zero_width_section_at_evidence_end_keeps_anchor_evidence(self):
        pages = [identified_page(60)]
        view = build_lecture_ir(ppt_pages=pages, chapters=[chapter("结尾", 60_000)])
        self.assertEqual(len(view["sections"]), 1)
        self.assertEqual(view["sections"][0]["time"], {"start_ms": 60_000, "end_ms": 60_000})
        self.assertEqual(
            view["sections"][0]["spans"],
            [{"kind": "slide_event", "id": pages[0]["event_id"]}],
        )

    def test_ids_are_stable_under_input_reordering(self):
        transcript = [
            identified_segment(0, 10_000, "甲"),
            identified_segment(10_000, 20_000, "乙"),
            identified_segment(20_000, 30_000, "丙"),
        ]
        pages = [identified_page(0, content_sha="a" * 64), identified_page(5, content_sha="b" * 64)]
        chapters = [chapter("第一章", 0), chapter("第二章", 10_000)]
        ordered = build_lecture_ir(transcript=transcript, chapters=chapters, ppt_pages=pages)
        reordered = build_lecture_ir(
            transcript=list(reversed(transcript)),
            chapters=list(reversed(chapters)),
            ppt_pages=list(reversed(pages)),
        )
        self.assertEqual(ordered, reordered)

    def test_generated_chapter_prose_never_becomes_evidence(self):
        summary = "模型生成的独有句子标记"
        transcript = [identified_segment(0, 60_000, "讲解")]
        view = build_lecture_ir(
            transcript=transcript,
            chapters=[chapter("章节", 0, summary=summary)],
            ppt_pages=[],
        )
        self.assertNotIn(summary, json.dumps(view, ensure_ascii=False))
        evidence_ids = {transcript[0]["segment_id"]}
        for unit in view["sections"] + view["knowledge_units"] + view["key_moments"]:
            self.assertTrue(unit["spans"])
            for span in unit["spans"]:
                self.assertIn(span["id"], evidence_ids)


class ContractConformanceTests(unittest.TestCase):
    def test_emitted_units_validate_inside_an_evidence_v1_document(self):
        transcript = [
            identified_segment(0, 10_000, "甲"),
            identified_segment(10_000, 20_000, "乙"),
        ]
        pages = [identified_page(0, content_sha="a" * 64), identified_page(15, content_sha="b" * 64)]
        view = build_lecture_ir(
            transcript=transcript,
            chapters=[chapter("第一章", 0), chapter("第二章", 15_000)],
            ppt_pages=pages,
        )
        document = {
            "contract": CONTRACT_ID,
            "source": {"id": _DOC_SOURCE_ID, **_SOURCE_IDENTITY},
            "fingerprints": dict(_PROVENANCE),
            "speech": {"segments": [
                {
                    "id": item["segment_id"],
                    "start_ms": item["start_ms"],
                    "end_ms": item["end_ms"],
                    "text": item["text"],
                }
                for item in transcript
            ]},
            "slides": {
                "entities": [
                    {
                        "id": page["entity_id"],
                        "deck_id": _DECK_ID,
                        "page": 1,
                        "content_sha256": page["source_sha256"],
                    }
                    for page in pages
                ],
                "events": [
                    {
                        "id": page["event_id"],
                        "entity": page["entity_id"],
                        "start_ms": page["created_sec"] * 1000,
                        "end_ms": None,
                    }
                    for page in pages
                ],
            },
            "units": (
                view["sections"] + view["knowledge_units"] + view["key_moments"]
            ),
        }
        normalized = validate_document(document)
        self.assertEqual(
            {unit["id"] for unit in normalized["units"]},
            {unit["id"] for unit in document["units"]},
        )
        for unit in normalized["units"]:
            self.assertIn(unit["kind"], {"section", "knowledge_unit", "key_moment"})
            self.assertTrue(unit["spans"])


def summary_job(kind="summary", requested=None, transcript=None, slides=None, prior=None):
    payload = {
        "title": "测试课程",
        "transcript": list(transcript or []),
        "slides": list(slides or []),
    }
    if prior is not None:
        payload["checkpoint"] = dict(prior)
    job = {
        "job_kind": kind,
        "task_id": "task-ir",
        "input_hash": "hash-ir",
        "pipeline": {"version": "test-v2"},
        "payload": payload,
        "secrets": {"deepseek_api_key": "secret"},
    }
    if requested is not None:
        job["requested_outputs"] = list(requested)
    return job


class RunnerSeamTests(unittest.TestCase):
    def run_summary_family(self, job, chapters):
        pages = [identified_page(0, content_sha="a" * 64)]

        def fake_slides(slides_arg, *, progress, prior_checkpoint, checkpoint, **kwargs):
            return pages, {}

        def fake_summary(api_key, *, title, transcript, ppt_pages, prior_checkpoint, checkpoint):
            return {"model": "deepseek-chat", "markdown": "笔记", "chapters": chapters}

        with patch("courselens_worker.ocr.process_slides", side_effect=fake_slides), \
                patch("courselens_worker.llm.create_summary", side_effect=fake_summary):
            return _process_materialized_job(job), pages

    def test_summary_job_attaches_the_additive_view(self):
        transcript = [identified_segment(0, 60_000, "讲解")]
        job = summary_job(kind="summary", transcript=transcript, slides=[{"source": {}}])
        result, pages = self.run_summary_family(job, [chapter("第一章", 0)])
        outputs = result["outputs"]
        self.assertEqual(set(outputs), {"ppt_pages", "summary", "lecture_ir"})
        view = outputs["lecture_ir"]
        self.assertEqual(view["contract"], CONTRACT_ID)
        self.assertEqual(len(view["sections"]), 1)
        self.assertEqual(view["sections"][0]["title"], "第一章")
        self.assertEqual(len(view["key_moments"]), 1)
        # Pre-existing outputs stay byte-identical to the produced values.
        self.assertEqual(outputs["ppt_pages"], pages)
        self.assertEqual(outputs["summary"]["markdown"], "笔记")

    def test_chapters_job_attaches_the_additive_view(self):
        transcript = [identified_segment(0, 60_000, "讲解")]
        job = summary_job(kind="chapters", transcript=transcript, slides=[{"source": {}}])
        result, _ = self.run_summary_family(job, [chapter("第一章", 0)])
        self.assertEqual(set(result["outputs"]), {"ppt_pages", "chapters", "lecture_ir"})
        self.assertEqual(len(result["outputs"]["lecture_ir"]["sections"]), 1)

    def test_learning_pack_attaches_the_view_for_summary_outputs(self):
        transcript = [identified_segment(0, 60_000, "讲解")]
        prior = {"ppt_pages": [identified_page(0, content_sha="a" * 64)], "ppt_skipped": {}}
        job = summary_job(
            kind="learning_pack",
            requested=["summary"],
            transcript=transcript,
            prior=prior,
        )
        result, _ = self.run_summary_family(job, [chapter("第一章", 0)])
        self.assertIn("lecture_ir", result["outputs"])
        self.assertEqual(len(result["outputs"]["lecture_ir"]["key_moments"]), 1)

    def test_learning_pack_without_summary_family_has_no_lecture_ir(self):
        job = summary_job(
            kind="learning_pack",
            requested=["ocr"],
            transcript=[identified_segment(0, 60_000, "讲解")],
            slides=[{"source": {}}],
        )
        result, _ = self.run_summary_family(job, [])
        self.assertNotIn("lecture_ir", result["outputs"])

    def test_subtitle_only_job_has_no_lecture_ir(self):
        job = {
            "job_kind": "subtitle",
            "task_id": "task-ir",
            "input_hash": "hash-ir",
            "pipeline": {"version": "test-v2"},
            "payload": {
                "mode": "automatic",
                "media": {"start_seconds": 0, "duration_seconds": 60},
                "transcript": [],
            },
            "secrets": {"deepseek_api_key": "secret"},
        }
        with patch.dict("os.environ", {"SENSEVOICE_MODEL_DIR": "s", "FIRERED_MODEL_DIR": "f"}), \
                patch("courselens_worker.asr.transcribe") as transcribe_mock:
            transcribe_mock.return_value = {
                "mode": "automatic",
                "segments": [],
                "raw_sensevoice": [],
                "raw_firered": [],
                "metrics": {},
            }
            result = _process_materialized_job(job)
        self.assertNotIn("lecture_ir", result["outputs"])


if __name__ == "__main__":
    unittest.main()
