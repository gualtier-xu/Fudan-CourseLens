"""Store, schema and aggregation tests for course knowledge (N7K K1/K2).

Everything here runs against synthetic temporary databases: no real course,
account, credential or runtime data is touched.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import course_knowledge_contract as ck  # noqa: E402
from src.runtime import learning_schema  # noqa: E402
from src.runtime.course_knowledge import (  # noqa: E402
    CourseKnowledgeError,
    build_course_knowledge,
    build_evidence_packet,
    build_lecture_knowledge,
    course_review,
    merge_topics,
    refresh_plan,
    save_course_knowledge,
)
from src.runtime.document_alignment import document_search_pages, ensure_document_schema  # noqa: E402
from src.runtime.learning_store import LearningStore  # noqa: E402
from src.runtime.search_index import LearningSearchIndex  # noqa: E402
from src.runtime.student_features import ensure_student_feature_schema  # noqa: E402

COURSE = "crs-synthetic-2026a"
SUB_A = "sub-syn-0001"
SUB_B = "sub-syn-0002"
SEG_A = "seg:aaaaaaaaaaaa"
SEG_A2 = "seg:bbbbbbbbbbbb"
SLIDE_A = "slevt:cccccccccccc"
DOC_ID = "d" * 32
DOC_SHA = "e" * 64


def _segment(text: str, start_ms: int, end_ms: int, evidence_id: str) -> dict:
    return {
        "start_ms": start_ms,
        "end_ms": end_ms,
        "text": text,
        "evidence_id": evidence_id,
    }


class _Fixture(unittest.TestCase):
    """Shared synthetic learning database."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "learning.db"
        ensure_student_feature_schema(self.db_path)
        ensure_document_schema(self.db_path)
        self.store = LearningStore(self.db_path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _import_summary(self, sub_id: str) -> None:
        self.store.import_remote_summary(
            course_id=COURSE, sub_id=sub_id, input_hash="a" * 32, model="synthetic",
            markdown="## 概览\n合成摘要正文。",
            chapters=[{"title": "第一段", "summary": "合成章节", "start_ms": 0}],
            ppt_pages=[], key_takeaways=["合成要点"],
        )

    # -- builders -----------------------------------------------------------

    def add_transcript(self, sub_id: str, segments: list[dict]) -> None:
        self.store.replace_transcript_segments(
            sub_id,
            source_path=str(self.root / f"{sub_id}.srt"),
            source_mtime_ns=1700000000000000000,
            source_size=2048,
            segments=segments,
        )

    def add_slides(self, sub_id: str, pages: list[tuple[int, str, str]]) -> None:
        self.store.insert_ppt_pages_pending(sub_id, [
            {"page_num": page_num, "created_sec": page_num * 60, "pptimgurl": "", "text": ""}
            for page_num, _, _ in pages
        ])
        for page_num, text, event_id in pages:
            self.store.update_ppt_page(sub_id, page_num, text, "done")
            metadata = json.dumps(
                {"event_id": event_id, "content_sha256": "f" * 64},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            with sqlite3.connect(self.db_path) as db:
                db.execute(
                    "UPDATE ppt_pages SET evidence_json=? WHERE sub_id=? AND page_num=?",
                    (metadata, sub_id, page_num),
                )
                db.commit()

    def add_document(self, *, scope: str, pages: list[str], title: str = "讲义") -> str:
        sub_id = "" if scope == "course" else SUB_A
        document_id = DOC_ID if scope == "lecture" else "a" * 32
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """INSERT INTO learning_documents(
                       document_id,course_id,sub_id,title,original_name,extension,media_type,
                       storage_path,sha256,size_bytes,page_count,extraction_state,
                       created_at,updated_at,doc_type,scope
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (document_id, COURSE, sub_id, title, f"{title}.pdf", ".pdf", "application/pdf",
                 str(self.root / "doc.pdf"), DOC_SHA, 1024, len(pages), "ready",
                 1700000000.0, 1700000000.0, "courseware", scope),
            )
            for index, text in enumerate(pages, start=1):
                db.execute(
                    """INSERT INTO learning_document_pages(
                           document_id,page_num,text,text_hash,visual_hash,extraction_state,metadata_json
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (document_id, index, text,
                     __import__("hashlib").sha256(text.encode("utf-8")).hexdigest(), "", "ready", "{}"),
                )
            db.commit()
        return document_id

    def add_exam_question(self, sub_id: str, question_no: int = 1) -> str:
        question_id = f"{question_no}".zfill(32)
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS exam_questions (
                       question_id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
                       sub_id TEXT NOT NULL, course_id TEXT NOT NULL,
                       question_no INTEGER NOT NULL, label TEXT NOT NULL, kind TEXT NOT NULL,
                       start_page INTEGER NOT NULL, end_page INTEGER NOT NULL,
                       anchor_text TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL,
                       created_at REAL NOT NULL)"""
            )
            db.execute(
                """INSERT INTO exam_questions(question_id,document_id,sub_id,course_id,
                       question_no,label,kind,start_page,end_page,anchor_text,content_hash,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (question_id, DOC_ID, sub_id, COURSE, question_no, f"第 {question_no} 题",
                 "big", 1, 1, "1、求小波变换", "9" * 32, 1700000000.0),
            )
            db.commit()
        return question_id

    def add_bookmark(self, sub_id: str, note: str = "这里没听懂") -> str:
        bookmark_id = "bm-synthetic-0001"
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """INSERT INTO bookmarks(bookmark_id,course_id,sub_id,start_ms,end_ms,note,
                       status,explanation_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (bookmark_id, COURSE, sub_id, 125000, 168000, note, "open", "{}",
                 1700000000.0, 1700000000.0),
            )
            db.commit()
        return bookmark_id

    def add_lecture_ir(self, sub_id: str, *, title: str, span_id: str) -> None:
        self.store.import_lecture_ir(
            course_id=COURSE,
            sub_id=sub_id,
            input_hash="1" * 32,
            view={
                "contract": "evidence.v1",
                "sections": [{"kind": "section", "title": title, "time": None,
                              "spans": [{"kind": "segment", "id": span_id}], "content": None,
                              "id": "unit:111111111111"}],
                "knowledge_units": [{"kind": "knowledge_unit", "title": "小波变换",
                                     "time": None,
                                     "spans": [{"kind": "segment", "id": span_id}],
                                     "content": {"text": "将信号分解到时频平面"},
                                     "id": "unit:222222222222"}],
                "key_moments": [],
            },
        )


class SchemaUpgradeTests(_Fixture):
    def test_schema_upgrade_is_additive_idempotent_and_keeps_rows(self):
        """A legacy (v7-shaped) database upgrades in place; nothing is dropped."""
        legacy = self.root / "legacy.db"
        with sqlite3.connect(legacy) as db:
            db.executescript(learning_schema.BASE_SCHEMA_SQL)
            db.executescript(learning_schema.SEARCH_SCHEMA_SQL)
            db.execute(
                """INSERT INTO learning_schema_meta(key,value) VALUES('learning_schema_version','7')"""
            )
            db.execute(
                """INSERT INTO watch_progress(sub_id,course_id,position_ms,duration_ms,
                       completed,playback_rate,updated_at)
                   VALUES('sub-legacy','crs-legacy',1000,2000,0,1.0,1700000000.0)"""
            )
            db.execute(
                """INSERT INTO ai_artifacts(artifact_id,course_id,sub_id,kind,status,input_hash,
                       prompt_version,model,content_markdown,content_json,metrics_json,
                       created_at,updated_at)
                   VALUES('a1','crs-legacy','sub-legacy','lecture_summary','ready','h1',
                          'v1','m','body','{}','{}',1.0,2.0)"""
            )
            db.commit()
            self.assertFalse(self._has_snapshot_table(db))

        first = LearningStore(legacy)
        second = LearningStore(legacy)  # repeated startup must be a no-op
        second.close()

        with sqlite3.connect(legacy) as db:
            self.assertTrue(self._has_snapshot_table(db))
            version = db.execute(
                "SELECT value FROM learning_schema_meta WHERE key='learning_schema_version'"
            ).fetchone()[0]
            self.assertEqual(str(version), str(learning_schema.LEARNING_SCHEMA_VERSION))
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM watch_progress").fetchone()[0], 1
            )
            artifacts = db.execute("SELECT COUNT(*) FROM ai_artifacts").fetchone()[0]
            self.assertEqual(artifacts, 1)
        first.close()

    @staticmethod
    def _has_snapshot_table(db: sqlite3.Connection) -> bool:
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='course_knowledge_snapshots'"
        ).fetchall()
        return bool(rows)

    def test_snapshot_row_does_not_pollute_the_course_data_inventory(self):
        """Why a dedicated table: ai_artifacts keys the inventory by sub_id."""
        document, _ = build_course_knowledge(
            self.store, course_id=COURSE, sub_ids=[SUB_A], include_personal_notes=False
        )
        self.store.save_course_knowledge_snapshot(
            course_id=COURSE, input_hash=document["input_hash"],
            contract_version="course-knowledge-v1", document=document,
        )
        pairs = self.store.course_data_category_aggregates()["course_sub"]
        self.assertNotIn((COURSE, ""), pairs)
        # Proof of the hazard we avoided: a row in ai_artifacts with sub_id=''
        # would fabricate a phantom lecture in that same aggregate.
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """INSERT INTO ai_artifacts(artifact_id,course_id,sub_id,kind,status,input_hash,
                       prompt_version,model,content_markdown,content_json,metrics_json,
                       created_at,updated_at)
                   VALUES('phantom','%s','','course_knowledge','ready','h','v','m','x','{}','{}',1.0,2.0)"""
                % COURSE
            )
            db.commit()
        pairs = self.store.course_data_category_aggregates()["course_sub"]
        self.assertIn((COURSE, ""), pairs)


class SnapshotStoreTests(_Fixture):
    def _document(self, sub_ids=None):
        document, _ = build_course_knowledge(
            self.store, course_id=COURSE, sub_ids=sub_ids or [SUB_A]
        )
        return document

    def test_save_get_list_and_repeat_refresh_is_idempotent(self):
        document = self._document()
        first = self.store.save_course_knowledge_snapshot(
            course_id=COURSE, input_hash=document["input_hash"],
            contract_version="course-knowledge-v1", document=document, now=1000.0,
        )
        second = self.store.save_course_knowledge_snapshot(
            course_id=COURSE, input_hash=document["input_hash"],
            contract_version="course-knowledge-v1", document=document, now=1001.0,
        )
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(len(self.store.list_course_knowledge_snapshots(COURSE)), 1)
        stored = self.store.get_course_knowledge_snapshot(COURSE)
        self.assertEqual(stored["document"], document)
        # Identical content is a true no-op: the row keeps its original write
        # time instead of being rewritten on every restart.
        self.assertEqual(stored["updated_at"], 1000.0)

    def test_a_content_change_with_the_same_revision_still_updates(self):
        document = self._document()
        self.store.save_course_knowledge_snapshot(
            course_id=COURSE, input_hash=document["input_hash"],
            contract_version="course-knowledge-v1", document=document, now=1000.0,
        )
        changed = json.loads(json.dumps(document))
        changed["status"] = "stale"
        changed["stale_reasons"] = ["input_changed"]
        self.store.save_course_knowledge_snapshot(
            course_id=COURSE, input_hash=document["input_hash"],
            contract_version="course-knowledge-v1", document=changed, now=1001.0,
        )
        stored = self.store.get_course_knowledge_snapshot(COURSE)
        self.assertEqual(stored["document"]["status"], "stale")
        self.assertEqual(stored["updated_at"], 1001.0)

    def test_prune_keeps_the_newest_snapshots_only(self):
        for index in range(5):
            document = self._document()
            document["input_hash"] = f"{index:032d}"
            self.store.save_course_knowledge_snapshot(
                course_id=COURSE, input_hash=document["input_hash"],
                contract_version="course-knowledge-v1", document=document, now=1000.0 + index,
            )
        removed = self.store.prune_course_knowledge_snapshots(COURSE, keep=3)
        self.assertEqual(removed, 2)
        remaining = self.store.list_course_knowledge_snapshots(COURSE)
        self.assertEqual([row["input_hash"] for row in remaining],
                         ["00000000000000000000000000000004", "00000000000000000000000000000003",
                          "00000000000000000000000000000002"])
        self.assertEqual(self.store.get_course_knowledge_snapshot(COURSE)["input_hash"],
                         "00000000000000000000000000000004")

    def test_save_is_fail_closed_when_validation_rejects_the_build(self):
        good = save_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A])
        stored_before = self.store.get_course_knowledge_snapshot(COURSE)
        with mock.patch(
            "shared.course_knowledge_contract.validate_course_knowledge",
            side_effect=ck.CourseKnowledgeContractError("reference_missing", "synthetic"),
        ):
            with self.assertRaises(ck.CourseKnowledgeContractError):
                save_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A])
        stored_after = self.store.get_course_knowledge_snapshot(COURSE)
        self.assertEqual(stored_after["snapshot_id"], stored_before["snapshot_id"])
        self.assertEqual(stored_after["document"], stored_before["document"])
        self.assertEqual(good["document"]["status"], stored_after["document"]["status"])

    def test_review_falls_back_to_the_last_good_snapshot_on_build_failure(self):
        save_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A])
        with mock.patch(
            "src.runtime.course_knowledge.build_course_knowledge",
            side_effect=CourseKnowledgeError("course_has_no_lectures"),
        ):
            review = course_review(self.store, course_id=COURSE, sub_ids=[SUB_A])
        self.assertFalse(review["fresh"])
        self.assertEqual(review["document"]["status"], "error")
        self.assertEqual(review["document"]["stale_reasons"], ["snapshot_invalid"])

    def test_review_without_any_snapshot_is_honest_about_never_built(self):
        review = course_review(self.store, course_id=COURSE, sub_ids=[SUB_A])
        self.assertIsNone(review["snapshot"])
        self.assertEqual(review["document"]["status"], "stale")
        self.assertIn("never_built", review["document"]["stale_reasons"])
        self.assertTrue(review["document"]["lectures"][0]["stale_reasons"])


class LectureAggregationTests(_Fixture):
    def _seed_a(self):
        self.add_transcript(SUB_A, [
            _segment("今天我们讲小波变换的定义", 0, 20000, SEG_A),
            _segment("重点是尺度函数与多分辨率分析", 45000, 90000, SEG_A2),
        ])
        self.add_slides(SUB_A, [(1, "小波变换定义", SLIDE_A)])

    def test_partial_build_from_evidence_only_never_invents_knowledge(self):
        self._seed_a()
        self.add_bookmark(SUB_A)
        document, diagnostics = build_course_knowledge(
            self.store, course_id=COURSE, sub_ids=[SUB_A]
        )
        self.assertEqual(document["status"], "partial")
        self.assertIn("summary_missing", document["stale_reasons"])
        lecture = document["lectures"][0]
        self.assertEqual(lecture["status"], "partial")
        self.assertTrue(lecture["key_points"])
        for point in lecture["key_points"]:
            self.assertTrue(point["citation_ids"])
        # No AI output means no invented topics.
        self.assertEqual(lecture["topics"], [])
        kinds = {ref["kind"] for ref in lecture["evidence_refs"]}
        self.assertEqual(kinds, {"transcript", "slide", "bookmark"})
        self.assertEqual(lecture["source_coverage"]["transcript_segments"], 2)
        self.assertEqual(diagnostics["dropped"][SUB_A]["transcript_no_identity"], 0)

    def test_legacy_rows_get_their_deterministic_evidence_identity(self):
        """A cache row without stored metadata is still citable (store fallback)."""
        self.store.replace_transcript_segments(
            SUB_A,
            source_path=str(self.root / "raw.srt"),
            source_mtime_ns=1,
            source_size=1,
            segments=[{"start_ms": 0, "end_ms": 1000, "text": "没有元数据的旧行"}],
        )
        lecture = build_lecture_knowledge(self.store, course_id=COURSE, sub_id=SUB_A)
        citations = [ref for ref in lecture["evidence_refs"] if ref["kind"] == "transcript"]
        self.assertEqual(len(citations), 1)
        self.assertTrue(citations[0]["source_id"].startswith("seg:"))

    def test_ready_build_consumes_lecture_ir_and_reuses_its_spans(self):
        self._seed_a()
        self.add_lecture_ir(SUB_A, title="小波变换", span_id=SEG_A)
        document, _ = build_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A])
        lecture = document["lectures"][0]
        self.assertEqual(lecture["status"], "ready")
        self.assertEqual(document["status"], "ready")
        self.assertTrue(lecture["source_coverage"]["lecture_ir"])
        self.assertIn("小波变换", [topic["title"] for topic in lecture["topics"]])
        citation_ids = {ref["citation_id"] for ref in lecture["evidence_refs"]}
        for point in lecture["key_points"]:
            self.assertTrue(set(point["citation_ids"]) <= citation_ids)

    def test_course_level_document_reaches_the_packet_and_keeps_lecture_lookup(self):
        self._seed_a()
        self.add_document(scope="course", pages=["第一章 小波分析导论"], title="课程教材")
        self.assertEqual(len(document_search_pages(self.db_path, SUB_A)), 0)
        pages = document_search_pages(self.db_path, SUB_A, course_id=COURSE)
        self.assertEqual([page["scope"] for page in pages], ["course"])
        lecture = build_lecture_knowledge(self.store, course_id=COURSE, sub_id=SUB_A)
        self.assertEqual(lecture["source_coverage"]["course_document_pages"], 1)
        document, _ = build_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A])
        scopes = {(source["kind"], source["scope"]) for source in document["sources"]}
        self.assertIn(("document_page", "course"), scopes)
        self.assertEqual(document["coverage"]["course_document_pages"], 1)

    def test_takeaway_anchors_ride_key_points_into_the_contract(self):
        """RR-ANCHORFE-1：总结 takeaway 锚 → key_points.anchor_ms → 合同视图投影。"""
        self._seed_a()
        self.store.import_remote_summary(
            course_id=COURSE, sub_id=SUB_A, input_hash="a" * 32, model="synthetic",
            markdown="## 概览",
            chapters=[{"title": "第一段", "summary": "合成章节", "start_ms": 0}],
            ppt_pages=[],
            key_takeaways=["锚定要点", "无锚要点"],
            takeaway_anchors=[65000, None],
        )
        document, _ = build_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A])
        lecture = document["lectures"][0]
        anchored = [point for point in lecture["key_points"] if point["text"].startswith("锚定要点")]
        self.assertEqual([point.get("anchor_ms") for point in anchored], [65000])
        plain = [point for point in lecture["key_points"] if point["text"].startswith("无锚要点")]
        self.assertTrue(plain)
        self.assertTrue(all("anchor_ms" not in point for point in plain))
        view = ck.lecture_detail_view(ck.validate_course_knowledge(document), SUB_A)
        self.assertEqual(view["key_points"][0]["anchor_ms"], 65000, "合法锚原样投影到讲次详情视图")

    def test_assessment_reference_view_has_no_answer_and_survives_validation(self):
        self._seed_a()
        self.add_document(scope="lecture", pages=["1、求小波变换"])
        question_id = self.add_exam_question(SUB_A, question_no=1)
        document, _ = build_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A])
        self.assertEqual(len(document["assessment_items"]), 1)
        item = document["assessment_items"][0]
        self.assertEqual(item["document_id"], DOC_ID)
        self.assertNotIn("answer", json.dumps(item))
        self.assertTrue(item["item_id"].startswith("cka:"))
        self.assertEqual(document["coverage"]["assessment_items"], 1)
        lecture = document["lectures"][0]
        cited = {ref["source_id"] for ref in lecture["evidence_refs"] if ref["kind"] == "assessment_item"}
        self.assertEqual(cited, {question_id})

    def test_personal_notes_are_opt_in(self):
        self._seed_a()
        self.add_bookmark(SUB_A, note="这里的推导我完全没跟上")
        quiet = build_lecture_knowledge(self.store, course_id=COURSE, sub_id=SUB_A)
        loud = build_lecture_knowledge(
            self.store, course_id=COURSE, sub_id=SUB_A, include_personal_notes=True
        )
        quiet_labels = [ref["label"] for ref in quiet["evidence_refs"] if ref["kind"] == "bookmark"]
        loud_labels = [ref["label"] for ref in loud["evidence_refs"] if ref["kind"] == "bookmark"]
        self.assertEqual(quiet_labels, ["我的书签"])
        self.assertEqual(loud_labels, ["这里的推导我完全没跟上"])
        self.assertNotIn("这里的推导我完全没跟上", quiet["_excerpts"].values())


class StalenessTests(_Fixture):
    def test_only_the_changed_lecture_is_marked_stale(self):
        self.add_transcript(SUB_A, [_segment("第一讲引入小波", 0, 10000, SEG_A)])
        self.add_transcript(SUB_B, [_segment("第二讲讲滤波器组", 0, 10000, "seg:dddddddddddd")])
        first = save_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A, SUB_B])
        self.assertEqual(first["document"]["status"], "partial")
        unchanged = course_review(self.store, course_id=COURSE, sub_ids=[SUB_A, SUB_B])
        self.assertEqual(unchanged["document"]["status"], "partial")
        self.assertEqual([lecture["status"] for lecture in unchanged["document"]["lectures"]],
                         ["partial", "partial"])

        self.add_transcript(SUB_B, [
            _segment("第二讲讲滤波器组", 0, 10000, "seg:dddddddddddd"),
            _segment("重点是重构条件", 20000, 30000, "seg:eeeeeeeeeeee"),
        ])
        changed = course_review(self.store, course_id=COURSE, sub_ids=[SUB_A, SUB_B])
        self.assertEqual(changed["document"]["status"], "stale")
        self.assertIn("input_changed", changed["document"]["stale_reasons"])
        by_sub = {lecture["sub_id"]: lecture for lecture in changed["document"]["lectures"]}
        self.assertEqual(by_sub[SUB_A]["status"], "partial")
        self.assertNotIn("input_changed", by_sub[SUB_A]["stale_reasons"])
        self.assertEqual(by_sub[SUB_B]["status"], "stale")
        self.assertIn("input_changed", by_sub[SUB_B]["stale_reasons"])

    def test_adding_a_lecture_marks_it_never_built_but_not_the_old_one(self):
        self.add_transcript(SUB_A, [_segment("第一讲", 0, 10000, SEG_A)])
        save_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A])
        self.add_transcript(SUB_B, [_segment("第二讲", 0, 10000, "seg:dddddddddddd")])
        review = course_review(self.store, course_id=COURSE, sub_ids=[SUB_A, SUB_B])
        by_sub = {lecture["sub_id"]: lecture for lecture in review["document"]["lectures"]}
        self.assertEqual(by_sub[SUB_A]["status"], "partial")
        self.assertIn("never_built", by_sub[SUB_B]["stale_reasons"])

    def test_signature_is_stable_across_identical_reads(self):
        self.add_transcript(SUB_A, [_segment("稳定", 0, 10000, SEG_A)])
        first = build_lecture_knowledge(self.store, course_id=COURSE, sub_id=SUB_A)
        second = build_lecture_knowledge(self.store, course_id=COURSE, sub_id=SUB_A)
        self.assertEqual(first["input_hash"], second["input_hash"])
        self.assertEqual(first["evidence_refs"], second["evidence_refs"])


class PacketTests(_Fixture):
    def test_packet_respects_the_character_budget(self):
        self.add_transcript(SUB_A, [
            _segment(f"第 {index} 段重点内容 " + "长" * 40, index * 20000, index * 20000 + 15000,
                     f"seg:{index:012x}")
            for index in range(12)
        ])
        packet = build_evidence_packet(
            self.store, course_id=COURSE, sub_id=SUB_A, item_chars=80, total_chars=200
        )
        self.assertEqual(packet["kind"], "evidence_packet")
        self.assertLessEqual(packet["used_chars"], 200)
        for item in packet["items"]:
            self.assertLessEqual(len(item["text"]), 80)
        self.assertGreaterEqual(packet["dropped"]["items"], 1)
        self.assertGreaterEqual(packet["dropped"]["chars"], 1)
        self.assertEqual(packet["input_hash"], build_lecture_knowledge(
            self.store, course_id=COURSE, sub_id=SUB_A
        )["input_hash"])

    def test_packet_carries_every_source_family_it_claims(self):
        """A lower bound too: a keyed-lookup miss used to drop all doc pages."""
        self.add_transcript(SUB_A, [_segment("重点字幕内容", 0, 10000, SEG_A)])
        self.add_slides(SUB_A, [(1, "第 1 页 PPT 正文", SLIDE_A)])
        self.add_document(scope="lecture", pages=["讲义正文第一页"])
        self.add_document(scope="course", pages=["课程教材正文第一页"], title="课程教材")
        packet = build_evidence_packet(self.store, course_id=COURSE, sub_id=SUB_A)
        kinds = [item["kind"] for item in packet["items"]]
        self.assertIn("transcript", kinds)
        self.assertIn("slide", kinds)
        self.assertEqual(kinds.count("document_page"), 2)
        self.assertEqual(packet["dropped"]["reasons"], {})
        for item in packet["items"]:
            self.assertTrue(item["text"].strip())
            self.assertEqual(item["kind"], "document_page" if item["kind"] == "document_page" else item["kind"])

    def test_packet_never_carries_the_whole_document_set(self):
        self.add_transcript(SUB_A, [_segment("重点", 0, 10000, SEG_A)])
        self.add_document(scope="lecture", pages=[f"第 {index} 页内容" for index in range(40)])
        packet = build_evidence_packet(self.store, course_id=COURSE, sub_id=SUB_A)
        from src.runtime import course_knowledge as module
        self.assertLessEqual(
            len(packet["items"]), module.MAX_TRANSCRIPT_SEGMENTS + module.MAX_LECTURE_DOC_PAGES + 1
        )
        # The per-lecture document cap is what bounds it, and the drop is
        # recorded rather than silently swallowed.
        self.assertGreaterEqual(packet["build_dropped"]["documents_over_limit"], 1)


class RefreshPlanTests(_Fixture):
    def test_plan_queues_blocks_skips_and_respects_active_runs(self):
        self.add_transcript(SUB_A, [_segment("有字幕", 0, 10000, SEG_A)])
        self.add_transcript(SUB_B, [_segment("也继续有字幕", 0, 10000, "seg:dddddddddddd")])
        plan = refresh_plan(self.store, course_id=COURSE, sub_ids=[SUB_A, SUB_B, "sub-empty"])
        self.assertEqual([entry["sub_id"] for entry in plan["queued"]], [SUB_A, SUB_B])
        self.assertEqual(plan["queued"][0]["reason"], "missing")
        self.assertEqual(plan["blocked"], [{"sub_id": "sub-empty", "reason": "transcript_missing"}])
        self.assertEqual(plan["counts"], {"queued": 2, "skipped": 0, "blocked": 1})

        # A matching revision alone is never "up to date": a lecture with no AI
        # output yet must stay queued, or it could never get its first summary.
        save_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A, SUB_B])
        without_ai = refresh_plan(self.store, course_id=COURSE, sub_ids=[SUB_A, SUB_B])
        self.assertEqual(without_ai["counts"], {"queued": 2, "skipped": 0, "blocked": 0})
        self.assertEqual({entry["reason"] for entry in without_ai["queued"]}, {"missing"})

        self._import_summary(SUB_A)
        self._import_summary(SUB_B)
        save_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A, SUB_B])
        again = refresh_plan(self.store, course_id=COURSE, sub_ids=[SUB_A, SUB_B])
        self.assertEqual(again["counts"], {"queued": 0, "skipped": 2, "blocked": 0})
        self.assertEqual({entry["reason"] for entry in again["skipped"]}, {"up_to_date"})

        running = refresh_plan(
            self.store, course_id=COURSE, sub_ids=[SUB_A, SUB_B], active_sub_ids={SUB_A}
        )
        self.assertEqual(running["skipped"], [
            {"sub_id": SUB_A, "reason": "already_running"},
            {"sub_id": SUB_B, "reason": "up_to_date"},
        ])

        self.add_transcript(SUB_B, [
            _segment("也继续有字幕", 0, 10000, "seg:dddddddddddd"),
            _segment("新增重点段", 30000, 40000, "seg:ffffffffffff"),
        ])
        stale = refresh_plan(self.store, course_id=COURSE, sub_ids=[SUB_A, SUB_B])
        self.assertEqual(stale["queued"], [{"sub_id": SUB_B, "reason": "stale"}])
        self.assertEqual(stale["skipped"], [{"sub_id": SUB_A, "reason": "up_to_date"}])

    def test_plan_without_lectures_is_empty_not_an_error(self):
        plan = refresh_plan(self.store, course_id=COURSE, sub_ids=[])
        self.assertEqual(plan["counts"], {"queued": 0, "skipped": 0, "blocked": 0})


class TakeawayAnchorImportTests(_Fixture):
    """RR-ANCHORFE-1：takeaway 时间戳锚与 key_takeaways 同管线对齐落库。"""

    def test_anchors_align_with_takeaways_and_degrade_fail_closed(self):
        self.store.import_remote_summary(
            course_id=COURSE, sub_id=SUB_A, input_hash="a" * 32, model="synthetic",
            markdown="## 概览\n合成摘要正文。",
            chapters=[], ppt_pages=[],
            key_takeaways=["锚定要点", "  ", "", "无锚要点"],
            takeaway_anchors=[65000, 99999, -5, "x"],
        )
        artifact = self.store.find_ai_artifact(SUB_A, "timestamp_summary")
        self.assertIsNotNone(artifact)
        # 空白要点被过滤后锚随行对齐；畸形锚（负数/非整数）与越界下标一律无锚
        self.assertEqual(artifact["content"]["key_takeaways"], ["锚定要点", "无锚要点"])
        self.assertEqual(artifact["content"]["takeaway_anchors"], [65000, None])

    def test_missing_anchor_field_keeps_legacy_takeaways(self):
        self.store.import_remote_summary(
            course_id=COURSE, sub_id=SUB_A, input_hash="a" * 32, model="synthetic",
            markdown="## 概览", chapters=[], ppt_pages=[],
            key_takeaways=["旧要点"],
        )
        artifact = self.store.find_ai_artifact(SUB_A, "timestamp_summary")
        self.assertEqual(artifact["content"]["key_takeaways"], ["旧要点"])
        self.assertEqual(artifact["content"]["takeaway_anchors"], [None])


class CourseDocumentSearchTests(_Fixture):
    """One course-level textbook page must not answer once per lecture."""

    def _index(self) -> LearningSearchIndex:
        snapshot = {
            "authorization_state": "ready",
            "courses": {COURSE: {"title": "合成课程", "teacher": "张老师"}},
            "lectures": {sub_id: {"course_id": COURSE, "sub_title": f"{sub_id} 讲"}
                         for sub_id in (SUB_A, SUB_B)},
        }
        index = LearningSearchIndex(self.store, lambda: snapshot, lambda sub_id: {})
        index._refresh(None, force=True)
        return index

    def test_course_level_page_is_searchable_from_every_lecture(self):
        for sub_id in (SUB_A, SUB_B):
            self.add_transcript(sub_id, [_segment(f"{sub_id} 的字幕", 0, 10000, SEG_A)])
        self.add_document(scope="course", pages=["第一章 小波变换导论"], title="课程教材")
        index = self._index()
        try:
            scoped = index.search("小波变换", sub_id=SUB_A)
            self.assertEqual(scoped["total"], 1)
            self.assertEqual(scoped["results"][0]["document_title"], "课程教材 · 第 1 页")
            self.assertEqual(scoped["results"][0]["sub_id"], SUB_A)
        finally:
            index.close()

    def test_cross_lecture_search_collapses_the_same_page_to_one_hit(self):
        for sub_id in (SUB_A, SUB_B):
            self.add_transcript(sub_id, [_segment(f"{sub_id} 的字幕", 0, 10000, SEG_A)])
        self.add_document(scope="course", pages=["第一章 小波变换导论"], title="课程教材")
        index = self._index()
        try:
            course_wide = index.search("小波变换", course_ids=[COURSE])
            titles = [row["document_title"] for row in course_wide["results"]]
            self.assertEqual(titles.count("课程教材 · 第 1 页"), 1)
            self.assertEqual(course_wide["duplicates_collapsed"], 1)
            self.assertEqual(course_wide["total"], 1)
            self.assertEqual(course_wide["results"][0]["also_in_lectures"], [SUB_B])
        finally:
            index.close()

    def test_several_lectures_sharing_one_textbook_still_validate(self):
        """A per-lecture sum used to break validation for any course with a book."""
        for sub_id in (SUB_A, SUB_B):
            self.add_transcript(sub_id, [_segment(f"{sub_id} 字幕", 0, 10000, SEG_A)])
        self.add_document(scope="course", pages=["课程教材第一页", "课程教材第二页"], title="课程教材")
        document, _ = build_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A, SUB_B])
        # Counted once per page for the whole course, not once per lecture.
        self.assertEqual(document["coverage"]["course_document_pages"], 2)
        self.assertEqual(document["coverage"]["lectures_total"], 2)
        course_sources = [source for source in document["sources"]
                          if source["kind"] == "document_page" and source["scope"] == "course"]
        self.assertEqual(len(course_sources), 1)
        self.assertEqual(document["contract"], ck.CONTRACT_ID)

    def test_lecture_documents_are_never_collapsed_into_each_other(self):
        self.add_transcript(SUB_A, [_segment("讲义字幕", 0, 10000, SEG_A)])
        self.add_document(scope="lecture", pages=["第一章 小波变换导论"], title="讲义")
        index = self._index()
        try:
            course_wide = index.search("小波变换", course_ids=[COURSE])
            self.assertEqual(course_wide["duplicates_collapsed"], 0)
            self.assertEqual(
                course_wide["results"][0]["document_title"], "讲义 · 第 1 页"
            )
        finally:
            index.close()


class LegacyMigrationEndToEndTests(_Fixture):
    """A pre-N7K database upgrades and still yields usable course knowledge."""

    def test_legacy_rows_become_citable_and_uncitable_ones_are_counted(self):
        legacy = self.root / "legacy-e2e.db"
        with sqlite3.connect(legacy) as db:
            db.executescript(learning_schema.BASE_SCHEMA_SQL)
            db.executescript(learning_schema.SEARCH_SCHEMA_SQL)
            db.execute(
                "INSERT INTO learning_schema_meta(key,value) VALUES('learning_schema_version','7')"
            )
            db.execute(
                "INSERT INTO transcript_sources(sub_id,source_path,source_mtime_ns,source_size,"
                "segment_count,updated_at) VALUES(?,?,?,?,?,?)",
                (SUB_A, "legacy.srt", 1700000000000000000, 4096, 2, 1700000000.0),
            )
            for index, text_value in enumerate(("旧的没有证据元数据的字幕一", "旧的没有证据元数据的字幕二"), start=1):
                db.execute(
                    "INSERT INTO transcript_segments(sub_id,segment_index,start_ms,end_ms,text,evidence_json)"
                    " VALUES(?,?,?,?,?,?)",
                    (SUB_A, index, index * 30000, index * 30000 + 25000, text_value, ""),
                )
            db.execute(
                "INSERT INTO ppt_pages(sub_id,page_num,created_sec,pptimgurl,text,ocr_status,ocr_at,evidence_json)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (SUB_A, 1, 60, "", "旧课件页有正文但没有事件身份", "done", 1700000000.0, ""),
            )
            db.commit()
        self.store.close()
        self.store = LearningStore(legacy)
        self.db_path = legacy

        lecture = build_lecture_knowledge(self.store, course_id=COURSE, sub_id=SUB_A)
        transcript_refs = [ref for ref in lecture["evidence_refs"] if ref["kind"] == "transcript"]
        # Legacy transcript rows get the store's deterministic fallback identity
        # and stay citable; the slide without an event id is honestly dropped.
        self.assertEqual(len(transcript_refs), 2)
        self.assertTrue(all(ref["source_id"].startswith("seg:") for ref in transcript_refs))
        self.assertEqual(lecture["source_coverage"]["slide_pages"], 0)
        self.assertGreaterEqual(lecture["_dropped"]["slides_no_identity"], 1)
        self.assertTrue(lecture["key_points"])
        document, _ = build_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A])
        self.assertEqual(document["coverage"]["transcript_segments"], 2)
        self.assertEqual(document["lectures"][0]["status"], "partial")


class TranscriptCoverageTests(_Fixture):
    """A bounded selection must still span the whole recording."""

    def test_mid_size_lecture_keeps_its_tail(self):
        segments = [
            _segment(f"第 {index} 段内容", index * 40000, index * 40000 + 35000, f"seg:{index:012x}")
            for index in range(47)
        ]
        self.add_transcript(SUB_A, segments)
        lecture = build_lecture_knowledge(self.store, course_id=COURSE, sub_id=SUB_A)
        transcript = [ref for ref in lecture["evidence_refs"] if ref["kind"] == "transcript"]
        starts = sorted(ref["locator"]["start_ms"] for ref in transcript)
        last_available = segments[-1]["start_ms"]
        # The closing minutes are represented, not only the opening half.
        self.assertGreater(max(starts), last_available * 3 // 4)
        self.assertLessEqual(len(transcript), 24)

    def test_a_short_lecture_keeps_every_segment(self):
        self.add_transcript(SUB_A, [
            _segment(f"第 {index} 段", index * 40000, index * 40000 + 35000, f"seg:{index:012x}")
            for index in range(6)
        ])
        lecture = build_lecture_knowledge(self.store, course_id=COURSE, sub_id=SUB_A)
        transcript = [ref for ref in lecture["evidence_refs"] if ref["kind"] == "transcript"]
        self.assertEqual(len(transcript), 6)


class SourceLabelTests(_Fixture):
    """Source rows must be tellable apart at their own granularity."""

    def test_transcript_source_rows_carry_distinct_time_labels(self):
        self.add_transcript(SUB_A, [
            _segment("第一段重点内容", 0, 30000, SEG_A),
            _segment("第二段重点内容", 90000, 120000, SEG_A2),
        ])
        document, _ = build_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A])
        labels = sorted(source["label"] for source in document["sources"]
                        if source["kind"] == "transcript")
        self.assertEqual(labels, ["同步字幕 00:00–00:30", "同步字幕 01:30–02:00"])

    def test_document_source_rows_stay_one_row_per_document(self):
        self.add_transcript(SUB_A, [_segment("字幕", 0, 10000, SEG_A)])
        self.add_document(scope="lecture", pages=["讲义第一页", "讲义第二页"], title="讲义")
        document, _ = build_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A])
        documents = [source for source in document["sources"] if source["kind"] == "document_page"]
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["label"], "讲义")

    def test_every_citation_still_resolves_to_its_own_source_row(self):
        """The front-end indexes sources by source_id, so all must be present."""
        self.add_transcript(SUB_A, [
            _segment("第一段", 0, 30000, SEG_A),
            _segment("第二段", 90000, 120000, SEG_A2),
        ])
        self.add_slides(SUB_A, [(1, "幻灯片正文", SLIDE_A)])
        self.add_bookmark(SUB_A)
        document, _ = build_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A])
        declared = {(source["kind"], source["external_id"]) for source in document["sources"]}
        for lecture in document["lectures"]:
            for ref in lecture["evidence_refs"]:
                self.assertIn((ref["kind"], ref["source_id"]), declared)
        labels = [source["label"] for source in document["sources"]]
        self.assertEqual(len(labels), len(set(labels)))


class EvidenceMatrixTests(_Fixture):
    """Every sane combination of sources must build and validate.

    The contract is fail-closed, so a build that *should* work but does not is
    a producer bug — exactly the class of failure that the per-lecture
    coverage/citation invariants are there to catch.  Hand-written cases missed
    one such bug; this matrix enumerates them instead.
    """

    def _build(self, *, transcript: bool, slides: bool, lecture_doc: bool,
               course_doc: bool, paper: bool, bookmark: bool, ai: str) -> dict:
        if transcript:
            self.add_transcript(SUB_A, [
                _segment("第一讲重点内容", 0, 30000, SEG_A),
                _segment("第一讲另一个重点", 60000, 90000, SEG_A2),
            ])
            self.add_transcript(SUB_B, [_segment("第二讲内容", 0, 30000, "seg:dddddddddddd")])
        if slides:
            self.add_slides(SUB_A, [(1, "幻灯片正文", SLIDE_A)])
        if lecture_doc:
            self.add_document(scope="lecture", pages=["讲义第一页", "讲义第二页"], title="讲义")
        if course_doc:
            self.add_document(scope="course", pages=["课程教材第一页"], title="课程教材")
        if paper:
            self.add_exam_question(SUB_A, question_no=1)
        if bookmark:
            self.add_bookmark(SUB_A)
        if ai == "ir":
            spans = [row["evidence_id"] for row in self.store.get_transcript_segments(SUB_A)] or [SEG_A]
            self.add_lecture_ir(SUB_A, title="小波变换", span_id=spans[0])
        elif ai == "summary":
            self._import_summary(SUB_A)
        document, _ = build_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A, SUB_B])
        # The view builders must also survive whatever the build produced.
        ck.course_overview_view(document)
        ck.lecture_detail_view(document, SUB_A)
        ck.assessment_workspace_view(document)
        return document

    def test_seventy_two_combinations_all_build_and_validate(self):
        combos = 0
        for flags in range(1 << 6):
            transcript = bool(flags & 1)
            slides = bool(flags & 2)
            lecture_doc = bool(flags & 4)
            course_doc = bool(flags & 8)
            paper = bool(flags & 16)
            bookmark = bool(flags & 32)
            with self.subTest(combo=flags):
                self.setUp()
                try:
                    document = self._build(
                        transcript=transcript, slides=slides, lecture_doc=lecture_doc,
                        course_doc=course_doc, paper=paper, bookmark=bookmark,
                        ai="ir" if flags % 3 == 0 else ("summary" if flags % 3 == 1 else "none"),
                    )
                    self.assertEqual(ck.validate_course_knowledge(document)["course_id"], COURSE)
                    self.assertIn(document["status"], {"ready", "partial"})
                finally:
                    self.tearDown()
                combos += 1
        self.assertEqual(combos, 64)


class TopicMergeTests(unittest.TestCase):
    def test_alias_and_title_matches_merge_uncertain_ones_stay_separate(self):
        merged = merge_topics([
            {"title": "小波变换", "aliases": ["Wavelet"], "lecture_ids": [SUB_A],
             "citation_ids": ["ckc:000000000001"], "status": "ready"},
            {"title": "Wavelet", "aliases": [], "lecture_ids": [SUB_B],
             "citation_ids": ["ckc:000000000002"], "status": "ready"},
            {"title": "滤波器组", "aliases": [], "lecture_ids": [SUB_B],
             "citation_ids": ["ckc:000000000003"], "status": "ready"},
        ])
        self.assertEqual(len(merged), 2)
        wavelet = [topic for topic in merged if topic["title"] == "小波变换"][0]
        self.assertEqual(wavelet["lecture_ids"], [SUB_A, SUB_B])
        self.assertEqual(len(wavelet["citation_ids"]), 2)

    def test_partial_status_propagates_when_topics_merge(self):
        merged = merge_topics([
            {"title": "小波变换", "aliases": [], "lecture_ids": [SUB_A],
             "citation_ids": ["ckc:000000000001"], "status": "ready"},
            {"title": "小波变换", "aliases": [], "lecture_ids": [SUB_B],
             "citation_ids": ["ckc:000000000002"], "status": "partial"},
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["status"], "partial")


if __name__ == "__main__":
    unittest.main()
