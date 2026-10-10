"""Integration tests for the course-review increment (N7K K3/K5).

Wires the real application methods (not HTTP) against synthetic local stores:
the remote-summary import chain, the additive summary-job extras, and the
honest degradation rules.  No network, no credentials, no real course data.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import course_knowledge_contract as ck  # noqa: E402
from src.application import CourseLensApplication  # noqa: E402
from src.runtime.catalog_repository import CatalogRepository  # noqa: E402
from src.runtime.course_knowledge import (  # noqa: E402
    PACKET_TOTAL_CHARS,
    CourseKnowledgeError,
    build_course_knowledge,
    build_evidence_packet,
    course_review,
    refresh_plan,
    save_course_knowledge,
)
from src.runtime.learning_store import LearningStore  # noqa: E402
from src.runtime.student_features import ensure_student_feature_schema  # noqa: E402
from src.runtime.task_store import TaskStore  # noqa: E402

COURSE = "crs-synthetic-2026a"
SUB_A = "sub-syn-0001"
SUB_B = "sub-syn-0002"


class _SearchIndexStub:
    """Records refresh requests; the real index is covered by its own suite."""

    def __init__(self) -> None:
        self.requests: list[list[str]] = []

    def request_refresh(self, sub_ids=None, **_kwargs):
        self.requests.append([str(value) for value in (sub_ids or [])])
        return {"state": "indexing"}


class _Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_db = self.root / "state.db"
        self.db_path = self.root / "learning.db"
        self.store = LearningStore(self.db_path)
        ensure_student_feature_schema(self.db_path)
        self.catalog = CatalogRepository(self.state_db)
        self.tasks = TaskStore(self.state_db)
        self.search = _SearchIndexStub()
        self.catalog.upsert_course(COURSE, "合成课程", teacher="张老师")
        for sub_id, title in ((SUB_A, "第一讲"), (SUB_B, "第二讲")):
            self.catalog.upsert_lecture(COURSE, {
                "sub_id": sub_id, "sub_title": title, "date": "2026-09-01",
            })
        self.app = CourseLensApplication.__new__(CourseLensApplication)
        self.app.learning_store = self.store
        self.app.catalog_repository = self.catalog
        self.app.task_store = self.tasks
        self.app.search_index = self.search
        self.app.output_dir = self.root
        self._patchers = [
            mock.patch.object(CourseLensApplication, "_export_summary_markdown", lambda *a, **k: None),
            mock.patch.object(CourseLensApplication, "_run_assessment_radar", lambda *a, **k: None),
        ]
        for patcher in self._patchers:
            patcher.start()

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        self.store.close()
        self.tmp.cleanup()

    # -- helpers ------------------------------------------------------------

    def imported_spans(self, sub_id: str) -> list[str]:
        return [str(row["evidence_id"]) for row in self.store.get_transcript_segments(sub_id)]

    def build_lecture_hash(self, sub_id: str) -> str:
        from src.runtime.course_knowledge import build_lecture_knowledge
        return build_lecture_knowledge(self.store, course_id=COURSE, sub_id=sub_id)["input_hash"]

    def summary_result(self, sub_id: str, *, span_ids: list[str], markdown: str = "## 概览\n合成摘要。") -> dict:
        return {
            "input_hash": "f" * 32,
            "metrics": {"api_tokens_total": 10},
            "outputs": {
                "summary": {
                    "markdown": markdown,
                    "model": "synthetic",
                    "chapters": [{"title": "小波变换", "summary": "合成章节", "start_ms": 0}],
                    "key_takeaways": ["要点一"],
                },
                "ppt_pages": [],
                "lecture_ir": {
                    "contract": "evidence.v1",
                    "sections": [{"kind": "section", "title": "小波变换", "time": None,
                                  "spans": [{"kind": "segment", "id": span_ids[0]}],
                                  "content": None, "id": "unit:111111111111"}],
                    "knowledge_units": [{"kind": "knowledge_unit", "title": "尺度函数",
                                         "time": None,
                                         "spans": [{"kind": "segment", "id": span_ids[0]}],
                                         "content": {"text": "用尺度函数构造逼近"},
                                         "id": "unit:222222222222"}],
                    "key_moments": [],
                },
            },
        }

    def add_bookmark(self, sub_id: str, note: str) -> None:
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                """INSERT INTO bookmarks(bookmark_id,course_id,sub_id,start_ms,end_ms,note,
                       status,explanation_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("bm-synthetic-0001", COURSE, sub_id, 12_000, 30_000, note, "open", "{}",
                 1.0, 1.0),
            )

    def add_transcript(self, sub_id: str, texts: list[str]) -> list[str]:
        segments = [
            {"start_ms": index * 30_000, "end_ms": index * 30_000 + 25_000, "text": text}
            for index, text in enumerate(texts)
        ]
        self.store.replace_transcript_segments(
            sub_id, source_path=str(self.root / f"{sub_id}.srt"),
            source_mtime_ns=1, source_size=10, segments=segments,
        )
        return [str(row["evidence_id"]) for row in self.store.get_transcript_segments(sub_id)]

    def add_document_page(self, text: str, *, scope: str = "lecture") -> None:
        import hashlib
        document_id = ("d" if scope == "lecture" else "e") * 32
        sub_id = SUB_A if scope == "lecture" else ""
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                """INSERT OR IGNORE INTO learning_documents(
                       document_id,course_id,sub_id,title,original_name,extension,media_type,
                       storage_path,sha256,size_bytes,page_count,extraction_state,
                       created_at,updated_at,doc_type,scope)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (document_id, COURSE, sub_id, "讲义", "讲义.pdf", ".pdf", "application/pdf",
                 str(self.root / "doc.pdf"), "f" * 64, 10, 1, "ready",
                 1.0, 1.0, "courseware", scope),
            )
            page_num = db.execute(
                "SELECT COUNT(*) FROM learning_document_pages WHERE document_id=?", (document_id,)
            ).fetchone()[0] + 1
            db.execute(
                """INSERT INTO learning_document_pages(
                       document_id,page_num,text,text_hash,visual_hash,extraction_state,metadata_json)
                   VALUES(?,?,?,?,?,?,?)""",
                (document_id, page_num, text,
                 hashlib.sha256(text.encode("utf-8")).hexdigest(), "", "ready", "{}"),
            )


class AssessmentIrIdentityTests(_Harness):
    """N7K's question citations and N7A's Assessment IR must be one identity.

    ``src/runtime/assessment_ir.py`` (N7A's path, read-only here) documents
    that its ``item_id`` reuses the course-knowledge contract's
    ``assessment_item_id_for`` and that its evidence refs must be able to
    point back into our packet.  This test drives both sides on the same
    synthetic question and asserts the identities agree bit for bit.
    """

    DOCUMENT_ID = "d" * 32
    # What the harness's document writer stores, and what N7A's refresh passes
    # as revision_id (it reads the same learning_documents.sha256 column).
    DOCUMENT_SHA = "f" * 64

    def _seed_paper(self) -> dict:
        self.add_document_page("1、求小波变换", scope="lecture")
        question_id = "1".zfill(32)
        content_hash = "9" * 32
        with closing(sqlite3.connect(self.db_path)) as db, db:
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
                """INSERT INTO exam_questions(question_id,document_id,sub_id,course_id,question_no,
                       label,kind,start_page,end_page,anchor_text,content_hash,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (question_id, self.DOCUMENT_ID, SUB_A, COURSE, 1, "第 1 题", "big", 1, 1,
                 "1、求小波变换", content_hash, 1.0),
            )
        return {"question_id": question_id, "content_hash": content_hash}

    def _course_citation(self) -> dict:
        document, _ = build_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A])
        item = document["assessment_items"][0]
        ref = next(
            ref for lecture in document["lectures"] for ref in lecture["evidence_refs"]
            if ref["kind"] == "assessment_item"
        )
        return {"document_item": item, "ref": ref}

    def test_contract_item_id_matches_the_assessment_ir_item_id(self):
        from src.runtime.assessment_ir import assessment_evidence_ref, build_assessment_item
        seed = self._seed_paper()
        built = build_assessment_item(
            course_id=COURSE, sub_id=SUB_A, document_id=self.DOCUMENT_ID, kind="exam_paper",
            question_no=1, stem="1、求小波变换", revision_id=self.DOCUMENT_SHA,
            label="第 1 题", content_hash=seed["content_hash"], question_id=seed["question_id"],
            document_sha256=self.DOCUMENT_SHA,
        )
        mine = self._course_citation()
        # 1) 题目身份：两边同一个 item_id（N7A 直接复用合同函数）。
        self.assertEqual(built["item_id"], mine["document_item"]["item_id"])
        # 2) 引用身份：同一道题在他们的库与本包的 citation_id 逐位相同。
        their_ref = assessment_evidence_ref(built)
        self.assertEqual(their_ref["source_id"], mine["ref"]["source_id"])
        self.assertEqual(their_ref["content_hash"], mine["ref"]["content_hash"])
        self.assertEqual(their_ref["revision_id"], mine["ref"]["revision_id"])
        self.assertEqual(their_ref["locator"], mine["ref"]["locator"])
        self.assertEqual(ck.citation_id_for(their_ref), mine["ref"]["citation_id"])

    def test_a_homework_item_without_an_answer_is_still_a_valid_reference(self):
        from src.runtime.assessment_ir import assessment_evidence_ref, build_assessment_item
        seed = self._seed_paper()
        built = build_assessment_item(
            course_id=COURSE, sub_id=SUB_A, document_id=self.DOCUMENT_ID, kind="homework",
            question_no=1, stem="1、求小波变换", revision_id=self.DOCUMENT_SHA,
            content_hash=seed["content_hash"], question_id=seed["question_id"],
        )
        # No answer mark in the stem: the IR says so instead of inventing one.
        self.assertEqual(built["answer"], "")
        self.assertEqual(built["status"], "question_only")
        ref = assessment_evidence_ref(built)
        # And the reference view we publish carries no answer either.
        self.assertNotIn("answer", ref)
        self.assertEqual(ck.validate_evidence_ref(ref, COURSE)["kind"], "assessment_item")


class AssessmentAnswerWiringTests(_Harness):
    """K4 接线：题目工作台消费 N7A 的 Assessment IR 答案面（仅本地）。"""

    DOCUMENT_ID = "d" * 32
    DOCUMENT_SHA = "f" * 64

    def _seed_ir(self, *, answer: str, answer_source: str) -> None:
        from src.runtime.assessment_ir import build_assessment_item, save_assessment_items

        self.add_document_page("1、求小波变换（3 分）", scope="lecture")
        with closing(sqlite3.connect(self.db_path)) as db, db:
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
                """INSERT INTO exam_questions(question_id,document_id,sub_id,course_id,question_no,
                       label,kind,start_page,end_page,anchor_text,content_hash,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("1".zfill(32), self.DOCUMENT_ID, SUB_A, COURSE, 1, "第 1 题", "big", 1, 1,
                 "1、求小波变换（3 分）", "9" * 32, 1.0),
            )
        item = build_assessment_item(
            course_id=COURSE, sub_id=SUB_A, document_id=self.DOCUMENT_ID, kind="exam_paper",
            question_no=1, stem=f"1、求小波变换（3 分）{answer}", revision_id=self.DOCUMENT_SHA,
            label="第 1 题", content_hash="9" * 32, question_id="1".zfill(32),
            answer_source=answer_source, document_sha256=self.DOCUMENT_SHA,
        )
        save_assessment_items(self.store.path, [item])

    def test_workspace_carries_the_answer_source_label(self):
        self.add_transcript(SUB_A, ["有字幕"])
        self._seed_ir(answer="参考答案：积分变换", answer_source="user_material")
        workspace = self.app.course_review(COURSE)["assessment_workspace"]
        self.assertEqual(len(workspace["items"]), 1)
        item = workspace["items"][0]
        # 合同 8 键仍在，答案面是额外投影。
        self.assertTrue(item["item_id"].startswith("cka:"))
        self.assertEqual(item["answer_source"], "user_material")
        self.assertTrue(item["has_answer"])
        self.assertIn("积分变换", item["answer"])
        self.assertEqual(workspace["answers"]["annotated"], 1)

    def test_no_answer_never_claims_one(self):
        self.add_transcript(SUB_A, ["有字幕"])
        self._seed_ir(answer="", answer_source="none")
        workspace = self.app.course_review(COURSE)["assessment_workspace"]
        item = workspace["items"][0]
        self.assertEqual(item["answer_source"], "none")
        self.assertFalse(item["has_answer"])
        self.assertEqual(item["answer"], "")
        self.assertEqual(workspace["answers"]["annotated"], 0)

    def test_answers_stay_local_and_never_reach_the_cloud_packet(self):
        self.add_transcript(SUB_A, ["有字幕"])
        self._seed_ir(answer="参考答案：积分变换", answer_source="user_material")
        packet = build_evidence_packet(self.store, course_id=COURSE, sub_id=SUB_A)
        self.assertNotIn("积分变换", json.dumps(packet, ensure_ascii=False))
        # 合同文档同样不带答案（AssessmentItem 无答案字段，这一点没被接线动摇）。
        document = self.store.get_course_knowledge_snapshot(COURSE)
        if document is not None:
            self.assertNotIn("积分变换", json.dumps(document["document"], ensure_ascii=False))
        for item in self.app.course_review(COURSE)["view"].get("lectures", []):
            self.assertNotIn("answer", item)

    def test_workspace_degrades_to_the_contract_view_without_the_ir(self):
        self.add_transcript(SUB_A, ["有字幕"])
        self.add_document_page("1、求小波变换（3 分）", scope="lecture")
        with closing(sqlite3.connect(self.db_path)) as db, db:
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
                """INSERT INTO exam_questions(question_id,document_id,sub_id,course_id,question_no,
                       label,kind,start_page,end_page,anchor_text,content_hash,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("1".zfill(32), self.DOCUMENT_ID, SUB_A, COURSE, 1, "第 1 题", "big", 1, 1,
                 "1、求小波变换（3 分）", "9" * 32, 1.0),
            )
        workspace = self.app.course_review(COURSE)["assessment_workspace"]
        item = workspace["items"][0]
        self.assertNotIn("has_answer", item)
        self.assertNotIn("answers", workspace)


class RealRefreshWiringTests(_Harness):
    """Exercise ``CourseLensApplication.course_review_refresh`` itself.

    The API suite uses a narrow double; these tests drive the real method so
    the plan → enqueue wiring (including a second click while a run is queued)
    is covered where it actually lives.
    """

    def _patch_enqueue(self, recorded: list[tuple[str, str]]):
        def _enqueue(_app, course_id, sub_id, **_kwargs):
            recorded.append((str(course_id), str(sub_id)))
            task, _created = self.tasks.add_task(
                "summary", str(course_id), str(sub_id),
                {"course_id": str(course_id), "sub_id": str(sub_id)},
                config_key="lecture-summary:ppt",
            )
            return task

        return mock.patch.object(CourseLensApplication, "enqueue_summary", _enqueue)

    def test_refresh_enqueues_only_missing_lectures_and_skips_a_second_click(self):
        self.add_transcript(SUB_A, ["有字幕的一讲"])
        recorded: list[tuple[str, str]] = []
        with self._patch_enqueue(recorded):
            first = self.app.course_review_refresh(COURSE)
            second = self.app.course_review_refresh(COURSE)
        self.assertEqual(recorded, [(COURSE, SUB_A)])
        self.assertEqual(first["status"], "queued")
        self.assertEqual((first["queued"], first["skipped"], first["blocked"]), (1, 0, 1))
        self.assertTrue(first["reasons"])
        self.assertTrue(all(isinstance(item, str) for item in first["reasons"]))
        # Second click: the queued run is visible, so nothing is queued again.
        self.assertEqual(second["queued"], 0)
        self.assertEqual(second["status"], "blocked")
        self.assertIn(
            {"sub_id": SUB_A, "reason": "already_running"}, second["skipped_lectures"]
        )
        self.assertIn(
            {"sub_id": SUB_B, "reason": "transcript_missing"}, second["blocked_lectures"]
        )

    def test_refresh_reports_an_unknown_course_with_a_closed_code(self):
        from src.services.domains import CourseReviewActionError
        with self.assertRaises(CourseReviewActionError) as caught:
            self.app.course_review_refresh("crs-not-mine")
        self.assertEqual(caught.exception.code, "course_review_course_unknown")

    def test_read_surface_rejects_foreign_courses_and_lectures(self):
        from src.services.domains import CourseReviewActionError
        with self.assertRaises(CourseReviewActionError):
            self.app.course_review("crs-not-mine")
        with self.assertRaises(CourseReviewActionError):
            self.app.course_review(COURSE, sub_id="sub-not-mine")

    def test_a_course_without_lectures_returns_an_honest_empty_view(self):
        self.catalog.upsert_course("crs-empty-0001", "空课", teacher="无")
        payload = self.app.course_review("crs-empty-0001")
        # No lectures in the local catalog: the empty state is not a contract
        # document and says so, but keeps the frozen view shape.
        self.assertEqual(payload["state"], "unavailable")
        self.assertEqual(payload["reason_code"], "course_has_no_lectures")
        self.assertEqual(payload["view"]["view"], "course_overview")
        self.assertEqual(payload["view"]["lectures"], [])
        self.assertEqual(payload["view"]["coverage"]["lectures_total"], 0)


class WorkerConsumerContractTests(_Harness):
    """Our packet must satisfy the Worker consumer's documented rules.

    The consumer lives in ``worker/courselens_worker/course_knowledge.py``
    (N7A's exclusive path, read-only here).  These assertions re-encode the
    rules it applies to a received packet, so a producer-side drift fails in
    our lane instead of silently degrading to ``evidence_packet_rejected``.
    """

    def _packet(self) -> dict:
        self.add_transcript(SUB_A, [
            f"第 {index} 段重点内容" for index in range(3)
        ])
        self.add_document_page("第一章 小波分析导论正文")
        self.add_document_page("第二章 滤波器组与重构正文")
        return build_evidence_packet(self.store, course_id=COURSE, sub_id=SUB_A)

    def test_items_container_and_entry_form_match_the_consumer(self):
        packet = self._packet()
        self.assertEqual(packet["contract"], "courselens.course-knowledge.v1")
        self.assertEqual(packet["course_id"], COURSE)
        self.assertIn("items", packet)
        self.assertIsInstance(packet["items"], list)
        self.assertTrue(packet["items"])
        allowed = {"citation_id", "kind", "source_id", "revision_id",
                   "content_hash", "locator", "label", "course_id"}
        for item in packet["items"]:
            # The consumer filters these keys out of the entry to rebuild the
            # reference, and reads ``text`` separately from the entry itself.
            self.assertTrue(set(item) <= allowed | {"text"}, set(item) - allowed - {"text"})
            self.assertTrue({"citation_id", "kind", "source_id"} <= set(item))
            self.assertIsInstance(item["text"], str)
            self.assertTrue(item["text"].strip())

    def test_every_item_text_matches_its_own_content_hash(self):
        """The consumer rejects any item whose hash does not match its text."""
        import hashlib
        for item in self._packet()["items"]:
            digest = hashlib.sha256(item["text"].encode("utf-8")).hexdigest()
            self.assertEqual(
                digest[: len(item["content_hash"])], item["content_hash"],
                f"hash mismatch for {item['citation_id']}",
            )

    def test_dropped_block_carries_the_three_keys_the_consumer_reads(self):
        packet = self._packet()
        dropped = packet["dropped"]
        self.assertEqual(set(dropped), {"items", "chars", "reasons"})
        self.assertIsInstance(dropped["items"], int)
        self.assertIsInstance(dropped["chars"], int)
        self.assertIsInstance(dropped["reasons"], dict)
        for key, value in dropped["reasons"].items():
            self.assertLessEqual(len(str(key)), 40)
            self.assertIsInstance(value, int)

    def test_course_context_is_flat_scalars_within_the_consumer_limits(self):
        """The Worker keeps only scalars: nested structures would be dropped."""
        self.add_transcript(SUB_A, ["第一讲字幕"])
        self.add_transcript(SUB_B, ["第二讲字幕"])
        extras = self.app._course_knowledge_job_extras(course_id=COURSE, sub_id=SUB_A)
        context = extras["course_context"]
        self.assertLessEqual(len(context), 16)          # MAX_COURSE_CONTEXT_KEYS
        for key, value in context.items():
            self.assertLessEqual(len(str(key)), 40)
            self.assertIsInstance(value, (str, int, float))
            self.assertNotIsInstance(value, bool)
            self.assertLessEqual(len(str(value)), 200)  # MAX_COURSE_CONTEXT_CHARS
        # The useful parts survive as readable sentences, not as objects.
        self.assertEqual(context["lecture_count"], 2)
        self.assertIn("sub-syn-0001", context["lecture_list"])
        self.assertEqual(context["current_lecture_index"], 1)
        self.assertIn("sub-syn-0002", context["lecture_list"])

    def test_no_item_text_is_shortened_behind_its_hash(self):
        """A truncated excerpt would look tampered to the consumer."""
        packet = self._packet()
        for item in packet["items"]:
            self.assertLessEqual(len(item["text"]), packet["limits"]["item_chars"])
        self.assertLessEqual(packet["used_chars"], packet["limits"]["total_chars"])


class SummaryImportIntegrationTests(_Harness):
    def test_import_rebuilds_a_ready_snapshot_and_requests_search_refresh(self):
        spans = self.add_transcript(SUB_A, ["重点是尺度函数与多分辨率分析"])
        self.add_transcript(SUB_B, ["第二讲引入滤波器组"])
        receipt = self.app._import_remote_summary_result(
            COURSE, SUB_A, self.summary_result(SUB_A, span_ids=spans)
        )
        self.assertEqual(receipt["state"], "saved")
        self.assertEqual(receipt["reason"], "summary_import")
        self.assertEqual(self.search.requests, [[SUB_A]])

        stored = self.store.get_course_knowledge_snapshot(COURSE)
        self.assertIsNotNone(stored)
        document = stored["document"]
        self.assertEqual(document["contract"], ck.CONTRACT_ID)
        self.assertEqual(document["coverage"]["lectures_total"], 2)
        by_sub = {lecture["sub_id"]: lecture for lecture in document["lectures"]}
        self.assertEqual(by_sub[SUB_A]["status"], "ready")
        self.assertEqual(by_sub[SUB_B]["status"], "partial")
        # The imported IR's own spans became the citations of its key points.
        citation_sources = {ref["source_id"] for ref in by_sub[SUB_A]["evidence_refs"]}
        self.assertIn(spans[0], citation_sources)
        for point in by_sub[SUB_A]["key_points"]:
            self.assertTrue(point["citation_ids"])
        # A published snapshot is a valid contract document.
        self.assertEqual(ck.validate_course_knowledge(document)["course_id"], COURSE)

    def test_unresolvable_ai_citations_are_dropped_not_invented(self):
        self.add_transcript(SUB_A, ["有字幕但没有对应证据的要点"])
        receipt = self.app._import_remote_summary_result(
            COURSE, SUB_A,
            self.summary_result(SUB_A, span_ids=["seg:ffffffffffff"]),
        )
        self.assertEqual(receipt["state"], "saved")
        document = self.store.get_course_knowledge_snapshot(COURSE)["document"]
        lecture = document["lectures"][0]
        cited = {ref["source_id"] for ref in lecture["evidence_refs"]}
        self.assertNotIn("seg:ffffffffffff", cited)
        for point in lecture["key_points"]:
            self.assertTrue(set(point["citation_ids"]) <= {
                ref["citation_id"] for ref in lecture["evidence_refs"]
            })
        # Uncited knowledge units do not become published key points.
        texts = " ".join(point["text"] for point in lecture["key_points"])
        self.assertNotIn("用尺度函数构造逼近", texts)

    def test_repeated_import_is_idempotent_for_the_same_revision(self):
        spans = self.add_transcript(SUB_A, ["重复导入"])
        result = self.summary_result(SUB_A, span_ids=spans)
        for _ in range(3):
            self.app._import_remote_summary_result(COURSE, SUB_A, result)
        snapshots = self.store.list_course_knowledge_snapshots(COURSE, limit=20)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(self.search.requests, [[SUB_A], [SUB_A], [SUB_A]])

    def test_import_survives_a_snapshot_rebuild_failure(self):
        spans = self.add_transcript(SUB_A, ["导入必须落地"])
        with mock.patch(
            "src.runtime.course_knowledge.build_course_knowledge",
            side_effect=RuntimeError("synthetic build failure"),
        ):
            receipt = self.app._import_remote_summary_result(
                COURSE, SUB_A, self.summary_result(SUB_A, span_ids=spans)
            )
        self.assertEqual(receipt["state"], "error")
        self.assertEqual(receipt["code"], "course_knowledge_failed")
        # The accepted summary itself still landed.
        artifact = self.store.find_ai_artifact(SUB_A, "timestamp_summary")
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact["status"], "ready")
        # No snapshot was written, so readers keep whatever they had (nothing).
        self.assertIsNone(self.store.get_course_knowledge_snapshot(COURSE))
        self.assertEqual(self.search.requests, [])

    def test_refresh_plan_reuses_the_existing_queue_without_a_new_task_kind(self):
        spans = self.add_transcript(SUB_A, ["有字幕"])
        self.app._import_remote_summary_result(COURSE, SUB_A, self.summary_result(SUB_A, span_ids=spans))
        plan = refresh_plan(self.store, course_id=COURSE, sub_ids=[SUB_A, SUB_B])
        self.assertEqual(plan["skipped"], [{"sub_id": SUB_A, "reason": "up_to_date"}])
        # SUB_B has no subtitle at all, so it is blocked rather than queued.
        self.assertEqual(plan["queued"], [])
        self.assertEqual(plan["blocked"], [{"sub_id": SUB_B, "reason": "transcript_missing"}])
        kinds = {str(row.get("kind") or "") for row in self.tasks.list_tasks(limit=50) or []}
        self.assertLessEqual(kinds, {"summary"})

    def test_transcript_change_makes_only_that_lecture_stale_after_import(self):
        spans_a = self.add_transcript(SUB_A, ["第一讲内容"])
        spans_b = self.add_transcript(SUB_B, ["第二讲内容"])
        self.app._import_remote_summary_result(COURSE, SUB_A, self.summary_result(SUB_A, span_ids=spans_a))
        self.app._import_remote_summary_result(COURSE, SUB_B, self.summary_result(SUB_B, span_ids=spans_b))
        current = course_review(self.store, course_id=COURSE, sub_ids=[SUB_A, SUB_B])
        self.assertEqual(current["document"]["status"], "ready")

        self.add_transcript(SUB_B, ["第二讲内容", "第二讲新增重点段"])
        changed = course_review(self.store, course_id=COURSE, sub_ids=[SUB_A, SUB_B])
        self.assertEqual(changed["document"]["status"], "stale")
        by_sub = {lecture["sub_id"]: lecture for lecture in changed["document"]["lectures"]}
        self.assertEqual(by_sub[SUB_A]["status"], "ready")
        self.assertEqual(by_sub[SUB_B]["status"], "stale")
        self.assertIn("input_changed", by_sub[SUB_B]["stale_reasons"])


class JobPayloadIntegrationTests(_Harness):
    def test_job_extras_are_additive_bounded_and_never_raise(self):
        self.add_transcript(SUB_A, [f"第 {index} 段重点 " + "内容" * 30 for index in range(20)])
        for index in range(40):
            self.add_document_page(f"第 {index} 页讲义正文")
        extras = self.app._course_knowledge_job_extras(course_id=COURSE, sub_id=SUB_A)
        self.assertIn("evidence_packet", extras)
        self.assertIn("course_context", extras)
        packet = extras["evidence_packet"]
        self.assertEqual(packet["kind"], "evidence_packet")
        self.assertLessEqual(len(packet["items"]), 40)
        self.assertLessEqual(sum(len(item["text"]) for item in packet["items"]), PACKET_TOTAL_CHARS)
        self.assertEqual(packet["input_hash"], self.build_lecture_hash(SUB_A))
        # Additive keys must not collide with the legacy job payload keys.
        self.assertFalse({"title", "transcript", "slides", "checkpoint"} & set(extras))
        json.dumps(extras)  # must stay JSON-serializable for the job envelope

    def test_job_extras_degrade_cleanly_on_an_empty_course(self):
        extras = self.app._course_knowledge_job_extras(course_id=COURSE, sub_id="sub-missing")
        self.assertIsInstance(extras, dict)
        # An unknown lecture degrades to an empty packet, never an exception.
        self.assertEqual(extras["evidence_packet"]["items"], [])
        self.assertEqual(extras["evidence_packet"]["used_chars"], 0)
        self.assertEqual(extras["course_context"]["lecture_count"], 2)

    def test_packet_is_a_pure_local_read(self):
        self.add_transcript(SUB_A, ["本地读取"])
        packet = build_evidence_packet(self.store, course_id=COURSE, sub_id=SUB_A)
        self.assertEqual(packet["course_id"], COURSE)
        self.assertEqual(packet["sub_id"], SUB_A)
        self.assertTrue(all(item["citation_id"].startswith("ckc:") for item in packet["items"]))

class SnapshotLifecycleIntegrationTests(_Harness):
    def test_partial_is_publishable_and_never_blocks_a_later_ready(self):
        self.add_transcript(SUB_A, ["第一讲"])
        first = save_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A, SUB_B])
        self.assertEqual(first["document"]["status"], "partial")
        self.assertEqual(self.store.get_course_knowledge_snapshot(COURSE)["status"], "partial")

        spans = self.imported_spans(SUB_A)
        self.app._import_remote_summary_result(COURSE, SUB_A, self.summary_result(SUB_A, span_ids=spans))
        stored = self.store.get_course_knowledge_snapshot(COURSE)
        by_sub = {lecture["sub_id"]: lecture for lecture in stored["document"]["lectures"]}
        self.assertEqual(by_sub[SUB_A]["status"], "ready")
        self.assertEqual(by_sub[SUB_B]["status"], "partial")
        self.assertEqual(stored["status"], "partial")

    def test_restart_style_repeated_builds_are_byte_identical(self):
        self.add_transcript(SUB_A, ["稳定输入"])
        first, _ = build_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A], now=1234.5)
        second, _ = build_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A], now=1234.5)
        # With a fixed clock the whole document is byte-identical.
        self.assertEqual(ck.canonical_json(first), ck.canonical_json(second))
        self.assertEqual([lecture["updated_at"] for lecture in first["lectures"]], [1234.5])


class HardeningTests(_Harness):
    """Counter-example sweep: tamper, ordering, secret-shaped text, restart."""

    def test_a_tampered_stored_snapshot_degrades_to_an_error_envelope(self):
        self.add_transcript(SUB_A, ["正常一讲"])
        save_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A])
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                "UPDATE course_knowledge_snapshots SET document_json=? WHERE course_id=?",
                ('{"contract": "courselens.course-knowledge.v1"}', COURSE),
            )
        with mock.patch(
            "src.runtime.course_knowledge.build_course_knowledge",
            side_effect=CourseKnowledgeError("course_has_no_lectures"),
        ):
            review = course_review(self.store, course_id=COURSE, sub_ids=[SUB_A])
        # The fallback is an honest error envelope, never a crash and never a
        # silently-served half-document presented as good.
        self.assertEqual(review["document"]["status"], "error")
        self.assertEqual(review["document"]["stale_reasons"], ["snapshot_invalid"])
        self.assertFalse(review["fresh"])
        self.assertEqual(review["diagnostics"]["error_code"], "course_has_no_lectures")

    def test_a_corrupt_snapshot_degrades_without_a_server_error(self):
        """A damaged stored row must still answer with an honest error state."""
        self.add_transcript(SUB_A, ["正常一讲"])
        save_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A])
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                "UPDATE course_knowledge_snapshots SET document_json=? WHERE course_id=?",
                ('{"contract": "courselens.course-knowledge.v1"}', COURSE),
            )
        with mock.patch(
            "src.runtime.course_knowledge.build_course_knowledge",
            side_effect=CourseKnowledgeError("course_has_no_lectures"),
        ):
            payload = self.app.course_review(COURSE)
        self.assertEqual(payload["state"], "error")
        self.assertEqual(payload["reason_code"], "snapshot_invalid")
        # The rendered view keeps its frozen shape, so the page can still say
        # "这次整理没有成功" instead of failing to paint.
        self.assertEqual(payload["view"]["view"], "course_overview")
        self.assertEqual(payload["view"]["status"], "error")
        self.assertEqual(payload["view"]["stale_reasons"], ["snapshot_invalid"])
        self.assertEqual(payload["view"]["lectures"], [])
        self.assertIsNone(payload["assessment_workspace"])

    def test_a_lecture_missing_from_the_snapshot_is_a_closed_404(self):
        from src.services.domains import CourseReviewActionError
        self.add_transcript(SUB_A, ["第一讲"])
        save_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A])
        self.catalog.upsert_lecture(COURSE, {
            "sub_id": "sub-syn-0009", "sub_title": "新加的讲", "date": "2026-09-15",
        })
        with mock.patch(
            "src.runtime.course_knowledge.build_course_knowledge",
            side_effect=CourseKnowledgeError("course_has_no_lectures"),
        ):
            # The stored snapshot predates the new lecture.
            with self.assertRaises(CourseReviewActionError) as caught:
                self.app.course_review(COURSE, sub_id="sub-syn-0009")
        self.assertEqual(caught.exception.code, "course_review_lecture_unknown")

    def test_unexpected_build_errors_are_not_masked(self):
        """Only known course-knowledge failures degrade; unknown bugs surface."""
        self.add_transcript(SUB_A, ["正常一讲"])
        save_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A])
        with mock.patch(
            "src.runtime.course_knowledge.build_course_knowledge",
            side_effect=RuntimeError("synthetic unexpected failure"),
        ):
            with self.assertRaises(RuntimeError):
                course_review(self.store, course_id=COURSE, sub_ids=[SUB_A])

    def test_out_of_order_lecture_imports_converge(self):
        spans_a = self.add_transcript(SUB_A, ["第一讲"])
        spans_b = self.add_transcript(SUB_B, ["第二讲"])
        # B lands first; A lands afterwards with an older producer input hash.
        self.app._import_remote_summary_result(COURSE, SUB_B, self.summary_result(SUB_B, span_ids=spans_b))
        self.app._import_remote_summary_result(COURSE, SUB_A, self.summary_result(SUB_A, span_ids=spans_a))
        stored = self.store.get_course_knowledge_snapshot(COURSE)["document"]
        self.assertEqual([lecture["sub_id"] for lecture in stored["lectures"]], [SUB_A, SUB_B])
        self.assertTrue(all(lecture["key_points"] for lecture in stored["lectures"]))

    def test_secret_shaped_evidence_text_fails_closed(self):
        """A key-looking string in real content must never reach a snapshot."""
        self.add_transcript(SUB_A, ["上课提到的示例 token sk-abcdefghijklmnopqrstuvwx"])
        with self.assertRaises(ck.CourseKnowledgeContractError) as caught:
            build_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A])
        self.assertEqual(caught.exception.code, "secret_like")
        with self.assertRaises(ck.CourseKnowledgeContractError):
            save_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A])
        # The write path re-raises before touching the database.
        self.assertIsNone(self.store.get_course_knowledge_snapshot(COURSE))

    def test_restart_rebuild_is_a_no_op_for_an_unchanged_course(self):
        spans = self.add_transcript(SUB_A, ["稳定"])
        self.app._import_remote_summary_result(COURSE, SUB_A, self.summary_result(SUB_A, span_ids=spans))
        first = self.store.get_course_knowledge_snapshot(COURSE)
        for _ in range(3):
            self.app._rebuild_course_knowledge(COURSE, reason="restart")
        snapshots = self.store.list_course_knowledge_snapshots(COURSE, limit=20)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["snapshot_id"], first["snapshot_id"])
        self.assertEqual(snapshots[0]["document"], first["document"])

    def test_snapshot_pruning_bounds_growth_without_losing_the_current(self):
        self.add_transcript(SUB_A, ["不断变化的讲次"])
        for index in range(5):
            self.add_transcript(SUB_A, [f"不断变化的讲次 {index}"])
            save_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A], keep=3)
        snapshots = self.store.list_course_knowledge_snapshots(COURSE, limit=20)
        self.assertEqual(len(snapshots), 3)
        current = self.store.get_course_knowledge_snapshot(COURSE)
        self.assertEqual(current["snapshot_id"], snapshots[0]["snapshot_id"])

    def test_a_late_delivered_result_lands_and_stays_traceable(self):
        """Known, accepted semantics of the single import funnel.

        A recovered run that arrives later still lands through
        ``_import_remote_summary_result`` (the platform routes recovery and
        back-fill through exactly this entry point), so the published snapshot
        reflects the most recently *imported* result.  The snapshot keeps the
        producer's ``input_hash`` per lecture, so which revision the knowledge
        came from stays visible instead of silently drifting.
        """
        spans = self.add_transcript(SUB_A, ["迟到结果也要落地"])
        first = self.summary_result(SUB_A, span_ids=spans, markdown="## 概览\n第一版。")
        first["input_hash"] = "1" * 32
        self.app._import_remote_summary_result(COURSE, SUB_A, first)
        after_first = self.store.get_course_knowledge_snapshot(COURSE)["document"]

        late = self.summary_result(SUB_A, span_ids=spans, markdown="## 概览\n第二版。")
        late["input_hash"] = "2" * 32
        self.app._import_remote_summary_result(COURSE, SUB_A, late)
        after_late = self.store.get_course_knowledge_snapshot(COURSE)["document"]

        self.assertNotEqual(
            after_first["lectures"][0]["input_hash"],
            after_late["lectures"][0]["input_hash"],
        )
        self.assertEqual(ck.validate_course_knowledge(after_late)["course_id"], COURSE)
        artifact = self.store.find_ai_artifact(SUB_A, "timestamp_summary")
        self.assertEqual(artifact["input_hash"], "2" * 32)
        # Both producer revisions stay in the table: the import is additive,
        # nothing was destroyed to accept the late result.
        self.assertEqual(len(self.store.list_course_knowledge_snapshots(COURSE, limit=20)), 2)

    def test_personal_note_text_never_reaches_the_cloud_packet(self):
        """Privacy default: notes stay local unless the reader opts in."""
        self.add_transcript(SUB_A, ["有字幕的一讲"])
        self.add_bookmark(SUB_A, note="我自己的私人批注不该上云")
        packet = build_evidence_packet(self.store, course_id=COURSE, sub_id=SUB_A)
        blob = json.dumps(packet, ensure_ascii=False)
        self.assertNotIn("我自己的私人批注不该上云", blob)
        # The bookmark is still represented as a citable reference, and its
        # missing text is counted rather than silently dropped.
        self.assertGreaterEqual(packet["build_dropped"]["bookmarks_over_limit"], 0)
        self.assertGreaterEqual(packet["dropped"]["reasons"].get("empty_text", 0), 1)
        # The job extras build with the safe default too.
        extras = self.app._course_knowledge_job_extras(course_id=COURSE, sub_id=SUB_A)
        self.assertNotIn("我自己的私人批注不该上云", json.dumps(extras, ensure_ascii=False))

    def test_personal_note_label_appears_only_on_an_explicit_local_read(self):
        self.add_transcript(SUB_A, ["有字幕的一讲"])
        self.add_bookmark(SUB_A, note="我自己的私人批注")
        quiet = self.app.course_review(COURSE, sub_id=SUB_A)["view"]
        loud = self.app.course_review(COURSE, sub_id=SUB_A, include_personal_notes=True)["view"]
        quiet_labels = [ref["label"] for ref in quiet["evidence_refs"] if ref["kind"] == "bookmark"]
        loud_labels = [ref["label"] for ref in loud["evidence_refs"] if ref["kind"] == "bookmark"]
        self.assertTrue(quiet_labels)
        self.assertEqual(quiet_labels, ["我的书签"])
        self.assertEqual(loud_labels, ["我自己的私人批注"])

    def test_two_courses_never_see_each_others_knowledge(self):
        """Snapshots and views are keyed by course; nothing crosses over."""
        other_course = "crs-synthetic-2026b"
        other_sub = "sub-other-0001"
        self.catalog.upsert_course(other_course, "另一门课", teacher="李老师")
        self.catalog.upsert_lecture(other_course, {
            "sub_id": other_sub, "sub_title": "别讲", "date": "2026-09-05",
        })
        spans_a = self.add_transcript(SUB_A, ["甲课的重点内容"])
        self.add_transcript(other_sub, ["乙课完全不同内容"])
        self.app._import_remote_summary_result(
            COURSE, SUB_A, self.summary_result(SUB_A, span_ids=spans_a, markdown="## 概览\n甲课。")
        )
        other_spans = [row["evidence_id"] for row in self.store.get_transcript_segments(other_sub)]
        self.app._import_remote_summary_result(
            other_course, other_sub,
            self.summary_result(other_sub, span_ids=other_spans, markdown="## 概览\n乙课。"),
        )

        view_a = self.app.course_review(COURSE)["view"]
        view_b = self.app.course_review(other_course)["view"]
        sub_ids_a = {lecture["sub_id"] for lecture in view_a["lectures"]}
        sub_ids_b = {lecture["sub_id"] for lecture in view_b["lectures"]}
        self.assertEqual(sub_ids_a, {SUB_A, SUB_B})
        self.assertEqual(sub_ids_b, {other_sub})
        self.assertNotIn(other_sub, sub_ids_a)
        self.assertNotIn(SUB_A, sub_ids_b)
        for source in view_a["sources"]:
            self.assertNotEqual(source["sub_id"], other_sub)
        for source in view_b["sources"]:
            self.assertNotEqual(source["sub_id"], SUB_A)
        # Each course keeps its own snapshot, and neither is the other's.
        snapshot_a = self.store.get_course_knowledge_snapshot(COURSE)
        snapshot_b = self.store.get_course_knowledge_snapshot(other_course)
        self.assertNotEqual(snapshot_a["snapshot_id"], snapshot_b["snapshot_id"])
        self.assertEqual(snapshot_a["document"]["course_id"], COURSE)
        self.assertEqual(snapshot_b["document"]["course_id"], other_course)
        self.assertNotIn("乙课", json.dumps(snapshot_a["document"], ensure_ascii=False))

    def test_only_one_summary_import_entry_point_exists(self):
        """The startup truth-reconcile keeps using the same single funnel."""
        import inspect
        source = inspect.getsource(CourseLensApplication)
        self.assertEqual(source.count("def _import_remote_summary_result"), 1)
        self.assertEqual(source.count("self._import_remote_summary_result("), 2)


if __name__ == "__main__":
    unittest.main()
