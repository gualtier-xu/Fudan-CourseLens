"""API-layer tests for the course-review routes (N7K K3).

Covers the frozen surface only: the local-first three-state gate, the
authorized-course closed set, the frozen view models served by
``GET /api/v3/course-review``, and the ``refresh`` action reusing the existing
summary queue.  All fixtures are synthetic.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from shared import course_knowledge_contract as ck
from src.runtime.catalog_repository import CatalogRepository
from src.runtime.course_knowledge import build_course_knowledge, save_course_knowledge
from src.application import CourseLensApplication
from src.runtime.http_api import make_handler
from src.runtime.flashcards import (
    RATING_GOOD,
    flashcard_deck,
    review_flashcard,
)
from src.runtime.learning_store import LearningStore
from src.runtime.student_features import ensure_student_feature_schema
from src.runtime.task_store import TaskStore

COURSE = "crs-synthetic-2026a"
OTHER_COURSE = "crs-synthetic-2026b"
SUB_A = "sub-syn-0001"
SUB_B = "sub-syn-0002"


class _SearchIndexStub:
    """Records refresh requests; the real index has its own suite."""

    def __init__(self) -> None:
        self.requests: list[list[str]] = []

    def request_refresh(self, sub_ids=None, **_kwargs):
        self.requests.append([str(value) for value in (sub_ids or [])])
        return {"state": "indexing"}


class _CourseReviewService:
    """Narrow double exposing exactly the container paths the routes touch."""

    def __init__(self, root: Path):
        self.root = root
        self.catalog_repository = CatalogRepository(root / "state.db")
        self.task_store = TaskStore(root / "state.db")
        self.learning_store = LearningStore(root / "learning.db")
        ensure_student_feature_schema(root / "learning.db")
        self._auth_state = "ready"
        self._summary_enqueues: list[tuple[str, str]] = []
        self._queue_failure: str = ""
        # N8A：题目 AI 解答动作的替身记录（真实链路在 test_ai_assessment_answer.py）。
        self._answer_requests: list[tuple[str, str, bool]] = []
        self._answer_items: dict[str, dict] = {}
        self._answer_failure: str = ""
        self.learning = SimpleNamespace(
            repository=self.learning_store,
            course_review=self.course_review,
            course_review_refresh=self.course_review_refresh,
            explain_assessment_item=self.explain_assessment_item,
            course_flashcards=self.course_flashcards,
            course_flashcard_review=self.course_flashcard_review,
            course_term_candidate_action=self.course_term_candidate_action,
        )
        self.auth_catalog = SimpleNamespace(
            catalog=self.catalog_repository,
            authentication_snapshot=lambda: {"state": self._auth_state},
        )
        self.tasks = SimpleNamespace(repository=self.task_store)
        # 三态门在 restoring-trusted 分支要读「已保存账号且启用自动登录」，
        # 判据来自设置页的隐私快照（生产为 auto_connect.fudan 嵌套形状）。
        self._auto_login = False
        self.settings = SimpleNamespace(
            privacy_snapshot=lambda: {
                "auto_connect": {"fudan": {"enabled": bool(self._auto_login), "status": "ready"}}
            }
        )

    # -- application-shaped operations -------------------------------------

    def course_review(self, course_id: str = "", *, sub_id: str = "",
                      include_personal_notes: bool = False) -> dict:
        from src.services.domains import CourseReviewActionError
        from src.runtime.course_knowledge import course_review as review_document

        if course_id not in self._catalog_courses():
            raise CourseReviewActionError("course_review_course_unknown")
        sub_ids = self._catalog_sub_ids(course_id)
        if sub_id and sub_id not in sub_ids:
            raise CourseReviewActionError("course_review_lecture_unknown")
        if not sub_ids:
            raise CourseReviewActionError("course_review_course_unknown")
        review = review_document(
            self.learning_store, course_id=course_id, sub_ids=sub_ids,
            include_personal_notes=include_personal_notes,
        )
        document = review["document"]
        if sub_id:
            view = ck.lecture_detail_view(document, sub_id)
            assessment = None
        else:
            view = ck.course_overview_view(document)
            assessment = ck.assessment_workspace_view(document)
        return {
            "view": view,
            "assessment_workspace": assessment,
            "snapshot": review["snapshot"],
            "state": str(document.get("status") or ""),
            "diagnostics": review.get("diagnostics") or {},
            "term_candidates": self._term_candidates_view(course_id),
            "observed_at": time.time(),
        }

    def _term_candidates_view(self, course_id: str) -> dict:
        """P10 读面键的窄替身：真实 feedback 函数直连（本地文件域）。"""
        from src.runtime.course_memory_feedback import confirmed_term_list, term_candidates

        try:
            rows = term_candidates(self.root, course_id)
            confirmed_count = len(confirmed_term_list(self.root, course_id))
        except Exception:
            rows, confirmed_count = [], 0
        return {"rows": rows, "confirmed_count": confirmed_count}

    def course_review_refresh(self, course_id: str = "", *, include_personal_notes: bool = False) -> dict:
        from src.services.domains import CourseReviewActionError
        from src.runtime.course_knowledge import refresh_plan, build_lecture_knowledge

        if course_id not in self._catalog_courses():
            raise CourseReviewActionError("course_review_course_unknown")
        sub_ids = self._catalog_sub_ids(course_id)
        plan = refresh_plan(self.learning_store, course_id=course_id, sub_ids=sub_ids)
        queued = []
        blocked = list(plan["blocked"])
        for entry in plan["queued"]:
            if self._queue_failure:
                blocked.append({"sub_id": entry["sub_id"], "reason": self._queue_failure})
                continue
            self._summary_enqueues.append((course_id, entry["sub_id"]))
            queued.append({"sub_id": entry["sub_id"], "reason": entry["reason"], "task_id": "task-1"})
        skipped = list(plan["skipped"])
        if queued:
            status_value = "queued"
        elif blocked:
            status_value = "blocked"
        else:
            status_value = "noop"
        sentences: list[str] = []
        if queued:
            sentences.append(f"{len(queued)} 个讲次已排进 AI 整理队列，跑完会自动更新。")
        for entry in blocked:
            sentences.append(f"{entry['sub_id']} 暂时排不进去（{entry['reason']}）。")
        if not sentences:
            sentences.append("这一课的知识已经是最新的，不用重新整理。")
        return {
            "course_id": course_id,
            "action": "refresh",
            "status": status_value,
            "queued": len(queued),
            "skipped": len(skipped),
            "blocked": len(blocked),
            "reasons": sentences,
            "queued_lectures": queued,
            "skipped_lectures": skipped,
            "blocked_lectures": blocked,
            "counts": {"queued": len(queued), "skipped": len(skipped), "blocked": len(blocked)},
            "observed_at": time.time(),
        }

    # -- helpers ------------------------------------------------------------

    def explain_assessment_item(self, course_id: str, item_id: str, *,
                                content_hash: str = "", retry: bool = False) -> dict:
        """N8A 动作的窄替身：只覆盖路由关心的三类结果。"""
        from src.services.domains import CourseReviewActionError

        if course_id not in self._catalog_courses():
            raise CourseReviewActionError("course_review_course_unknown")
        item = self._answer_items.get(str(item_id))
        if item is None:
            raise CourseReviewActionError("assessment_item_unknown")
        if self._answer_failure:
            raise CourseReviewActionError(self._answer_failure)
        self._answer_requests.append((str(item_id), str(content_hash), bool(retry)))
        return {
            "course_id": course_id,
            "item_id": str(item_id),
            "action": "explain_assessment",
            "status": "queued",
            "created": True,
            "error_code": "",
            "task_id": "task-answer-1",
            "ai": {"ai_state": "queued", "ai_explanation": "", "ai_citations": [],
                   "ai_error_code": ""},
            "observed_at": time.time(),
        }

    # -- 闪卡（RR-P4FSRS-1）：真实实现走真库，替身只做闭集门 ----------------

    def course_flashcards(self, course_id: str) -> dict:
        from src.services.domains import CourseReviewActionError
        from src.runtime.flashcards import derive_flashcards, last_derived_input_hash, mark_derived

        if course_id not in self._catalog_courses():
            raise CourseReviewActionError("course_review_course_unknown")
        sub_ids = self._catalog_sub_ids(course_id)
        labels = {}
        for sub_id in sub_ids:
            labels[sub_id] = "这一讲"
        stored = self.learning_store.get_course_knowledge_snapshot(course_id)
        document = (stored or {}).get("document")
        input_hash = str((stored or {}).get("input_hash") or "")
        if isinstance(document, dict) and input_hash                 and input_hash != last_derived_input_hash(self.learning_store, course_id=course_id):
            derive_flashcards(self.learning_store, course_id=course_id, document=document)
            mark_derived(self.learning_store, course_id=course_id, input_hash=input_hash)
        return flashcard_deck(self.learning_store, course_id=course_id, lecture_labels=labels)

    def course_flashcard_review(self, course_id: str, card_id: str, rating: int) -> dict:
        from src.services.domains import CourseReviewActionError

        if course_id not in self._catalog_courses():
            raise CourseReviewActionError("course_review_course_unknown")
        if not str(card_id or "").strip() or rating not in (1, 2, 3, 4):
            raise CourseReviewActionError("course_review_action_invalid")
        try:
            return review_flashcard(
                self.learning_store, card_id=str(card_id), rating=int(rating),
                course_id=str(course_id),
            )
        except KeyError as error:
            raise CourseReviewActionError("course_review_flashcard_unknown") from error
        except ValueError as error:
            raise CourseReviewActionError("course_review_action_invalid") from error

    def course_term_candidate_action(self, course_id: str, action: str, *,
                                     wrong: str = "", right: str = "") -> dict:
        """P10 动作的窄替身：真实 feedback 函数直连（本地文件域，无外部依赖）。"""
        from src.services.domains import CourseReviewActionError
        from src.runtime.course_memory_feedback import (
            TermCandidateUnknown,
            confirm_term_mapping,
            dismiss_term_mapping,
        )

        if course_id not in self._catalog_courses():
            raise CourseReviewActionError("course_review_course_unknown")
        kind = str(action or "").strip().lower()
        wrong = str(wrong or "").strip()
        right = str(right or "").strip()
        if (
            kind not in {"confirm_term_candidate", "dismiss_term_candidate"}
            or not wrong or not right
        ):
            raise CourseReviewActionError("course_review_action_invalid")
        try:
            if kind == "confirm_term_candidate":
                return confirm_term_mapping(self.root, course_id, wrong=wrong, right=right)
            return dismiss_term_mapping(self.root, course_id, wrong=wrong, right=right)
        except TermCandidateUnknown as error:
            raise CourseReviewActionError("term_candidate_unknown") from error

    def seed_answer_item(self, item_id: str) -> None:
        self._answer_items[str(item_id)] = {"item_id": str(item_id)}

    def set_answer_failure(self, code: str) -> None:
        self._answer_failure = str(code)

    def _catalog_courses(self) -> set[str]:
        return {str(lecture.get("course_id") or "") for lecture in self.catalog_repository.lecture_course_pairs().values()}

    def _catalog_sub_ids(self, course_id: str) -> list[str]:
        pairs = self.catalog_repository.lecture_course_pairs()
        return sorted(str(sub) for sub, lecture in pairs.items()
                      if str(lecture.get("course_id") or "") == str(course_id))

    def set_auth_state(self, state: str) -> None:
        self._auth_state = state

    def set_auto_login(self, enabled: bool) -> None:
        self._auto_login = bool(enabled)

    def set_queue_failure(self, code: str) -> None:
        self._queue_failure = code

    def seed_course(self) -> None:
        self.catalog_repository.upsert_course(COURSE, "合成课程", teacher="张老师")
        for sub_id, title in ((SUB_A, "第一讲"), (SUB_B, "第二讲")):
            self.catalog_repository.upsert_lecture(COURSE, {
                "sub_id": sub_id, "sub_title": title, "date": "2026-09-01",
            })
        self.catalog_repository.upsert_course(OTHER_COURSE, "另一门课", teacher="李老师")
        self.catalog_repository.upsert_lecture(OTHER_COURSE, {
            "sub_id": "sub-other-0001", "sub_title": "别讲", "date": "2026-09-01",
        })

    def seed_transcript(self, sub_id: str, text: str = "重点是尺度函数") -> None:
        with closing(sqlite3.connect(self.learning_store.path)) as db, db:
            db.execute(
                "INSERT INTO transcript_sources(sub_id,source_path,source_mtime_ns,source_size,"
                "segment_count,updated_at) VALUES(?,?,?,?,?,?)",
                (sub_id, "synthetic.srt", 1, 10, 1, time.time()),
            )
            db.execute(
                "INSERT INTO transcript_segments(sub_id,segment_index,start_ms,end_ms,text,evidence_json)"
                " VALUES(?,?,?,?,?,?)",
                (sub_id, 1, 0, 30_000, text, ""),
            )

    def seed_bookmark(self, sub_id: str, note: str) -> None:
        with closing(sqlite3.connect(self.learning_store.path)) as db, db:
            db.execute(
                """INSERT INTO bookmarks(bookmark_id,course_id,sub_id,start_ms,end_ms,note,
                       status,explanation_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("bm-synthetic-0001", COURSE, sub_id, 12_000, 30_000, note, "open", "{}", 1.0, 1.0),
            )

    def seed_snapshot(self) -> None:
        save_course_knowledge(self.learning_store, course_id=COURSE, sub_ids=[SUB_A, SUB_B])

    def import_summary(self, sub_id: str) -> None:
        """Simulate one landed AI summary, exactly as the import path writes it."""
        self.learning_store.import_remote_summary(
            course_id=COURSE,
            sub_id=sub_id,
            input_hash="a" * 32,
            model="synthetic",
            markdown="## 概览\n合成摘要正文。",
            chapters=[{"title": "第一段", "summary": "合成章节", "start_ms": 0}],
            ppt_pages=[],
            key_takeaways=["合成要点"],
        )


class _Server(ThreadingHTTPServer):
    # Handler threads must be joined before the temp tree is removed, exactly
    # as in the course-data API suite (daemon threads would race cleanup).
    daemon_threads = False


class CourseReviewApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.service = _CourseReviewService(root)
        self.service.seed_course()
        server = _Server(("127.0.0.1", 0), make_handler(self.service, root))
        self._server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{server.server_port}"

    def tearDown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._tmp.cleanup()

    def get(self, path: str):
        with urlopen(f"{self.base}{path}") as response:
            return response.status, json.loads(response.read())["data"]

    def post(self, path: str, body: dict):
        request = Request(
            f"{self.base}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urlopen(request) as response:
            return response.status, json.loads(response.read())["data"]

    def expect_error(self, path: str, *, status: int, error_code: str,
                     body: dict | None = None, method: str = "POST"):
        data = json.dumps(body or {}).encode() if method == "POST" else None
        request = Request(
            f"{self.base}{path}", data=data,
            headers={"Content-Type": "application/json"}, method=method,
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request)
        self.assertEqual(caught.exception.code, status)
        self.assertEqual(json.loads(caught.exception.read())["error_code"], error_code)

    # -- gate ---------------------------------------------------------------

    def test_course_review_joins_the_local_first_gate(self):
        for state in ("idle", "checking"):
            self.service.set_auth_state(state)
            self.expect_error(
                f"/api/v3/course-review?course_id={COURSE}", method="GET",
                status=401, error_code="fudan_login_required",
            )
            self.expect_error(
                "/api/v3/course-review/actions", body={"course_id": COURSE, "action": "refresh"},
                status=401, error_code="fudan_login_required",
            )

    def test_reads_stay_available_while_the_session_is_being_restored(self):
        """Local-first: a pure local read is allowed mid-restore, actions are not."""
        self.service.seed_transcript(SUB_A)
        self.service.set_auth_state("checking")
        self.service.set_auto_login(True)
        status, data = self.get(f"/api/v3/course-review?course_id={COURSE}")
        self.assertEqual(status, 200)
        self.assertEqual(data["view"]["view"], "course_overview")
        # 恢复期动作走瞬态码（前端映射「正在登录」提示），不是硬性要求重新登录。
        self.expect_error(
            "/api/v3/course-review/actions", body={"course_id": COURSE, "action": "refresh"},
            status=401, error_code="fudan_session_restoring",
        )

    # -- read surface -------------------------------------------------------

    def test_course_overview_matches_the_frozen_view_shape(self):
        self.service.seed_transcript(SUB_A)
        status, data = self.get(f"/api/v3/course-review?course_id={COURSE}")
        self.assertEqual(status, 200)
        view = data["view"]
        self.assertEqual(view["view"], "course_overview")
        self.assertEqual(view["contract"], ck.CONTRACT_ID)
        self.assertEqual(view["course_id"], COURSE)
        self.assertEqual([lecture["sub_id"] for lecture in view["lectures"]], [SUB_A, SUB_B])
        self.assertEqual(view["coverage"]["lectures_total"], 2)
        self.assertEqual(data["state"], "stale")  # never built yet
        self.assertIn("never_built", view["stale_reasons"])
        self.assertIsNone(data["snapshot"])
        # The assessment workspace travels with the course-level response.
        self.assertEqual(data["assessment_workspace"]["view"], "assessment_workspace")
        # A frozen view model round-trips through the contract unchanged.
        self.assertEqual(view["contract"], ck.CONTRACT_ID)
        self.assertTrue(view["lectures"][0]["stale_reasons"])

    def test_lecture_detail_is_served_from_the_same_endpoint(self):
        self.service.seed_transcript(SUB_A, "重点是尺度函数与多分辨率分析")
        status, data = self.get(f"/api/v3/course-review?course_id={COURSE}&sub_id={SUB_A}")
        self.assertEqual(status, 200)
        view = data["view"]
        self.assertEqual(view["view"], "lecture_detail")
        self.assertEqual(view["sub_id"], SUB_A)
        self.assertIsNone(data["assessment_workspace"])
        self.assertTrue(view["key_points"])
        for point in view["key_points"]:
            self.assertTrue(point["citation_ids"])
        self.assertTrue(view["evidence_refs"])

    def test_lecture_route_serves_the_detail_view_only(self):
        self.service.seed_transcript(SUB_A, "重点是尺度函数")
        status, data = self.get(f"/api/v3/course-review/lecture?course_id={COURSE}&sub_id={SUB_A}")
        self.assertEqual(status, 200)
        # The sub-route returns the frozen view itself, not the wrapper.
        self.assertEqual(data["view"], "lecture_detail")
        self.assertEqual(data["sub_id"], SUB_A)
        self.assertNotIn("assessment_workspace", data)
        self.expect_error(
            f"/api/v3/course-review/lecture?course_id={COURSE}", method="GET",
            status=400, error_code="course_review_request_invalid",
        )

    def test_assessment_route_serves_the_workspace_view(self):
        self.service.seed_transcript(SUB_A)
        status, data = self.get(f"/api/v3/course-review/assessment?course_id={COURSE}")
        self.assertEqual(status, 200)
        self.assertEqual(data["view"], "assessment_workspace")
        self.assertEqual(data["course_id"], COURSE)
        self.assertIn("counts", data)
        self.assertEqual(data["items"], [])
        # A stray sub_id does not turn the course-level workspace into a lecture view.
        _status, still = self.get(
            f"/api/v3/course-review/assessment?course_id={COURSE}&sub_id={SUB_A}"
        )
        self.assertEqual(still["view"], "assessment_workspace")
        self.assertEqual(still["course_id"], COURSE)

    def test_notes_opt_in_is_a_query_flag_and_defaults_off(self):
        """The only way note text reaches a view is an explicit request flag."""
        self.service.seed_transcript(SUB_A)
        self.service.seed_bookmark(SUB_A, note="私人批注不该默认出现")
        _status, quiet = self.get(
            f"/api/v3/course-review/lecture?course_id={COURSE}&sub_id={SUB_A}"
        )
        _status, loud = self.get(
            f"/api/v3/course-review/lecture?course_id={COURSE}&sub_id={SUB_A}&include_notes=1"
        )
        quiet_labels = [ref["label"] for ref in quiet["evidence_refs"] if ref["kind"] == "bookmark"]
        loud_labels = [ref["label"] for ref in loud["evidence_refs"] if ref["kind"] == "bookmark"]
        self.assertTrue(quiet_labels)
        self.assertNotIn("私人批注不该默认出现", ";".join(quiet_labels))
        self.assertIn("私人批注不该默认出现", ";".join(loud_labels))

    def test_read_yields_a_valid_contract_document(self):
        self.service.seed_transcript(SUB_A)
        self.service.seed_snapshot()
        status, data = self.get(f"/api/v3/course-review?course_id={COURSE}")
        self.assertEqual(status, 200)
        self.assertIsNotNone(data["snapshot"])
        self.assertEqual(data["snapshot"]["contract_version"], "course-knowledge-v1")

    # -- closed sets --------------------------------------------------------

    def test_unknown_and_foreign_courses_are_rejected(self):
        self.expect_error(
            "/api/v3/course-review?course_id=crs-not-mine", method="GET",
            status=404, error_code="course_review_course_unknown",
        )
        self.expect_error(
            f"/api/v3/course-review?course_id={COURSE}&sub_id=sub-other-0001", method="GET",
            status=404, error_code="lecture_not_found",
        )
        self.expect_error(
            f"/api/v3/course-review/lecture?course_id={COURSE}&sub_id=sub-other-0001", method="GET",
            status=404, error_code="lecture_not_found",
        )
        self.expect_error(
            "/api/v3/course-review/actions", body={"course_id": "crs-not-mine", "action": "refresh"},
            status=404, error_code="course_review_course_unknown",
        )

    def test_missing_course_id_and_unknown_action_are_bad_requests(self):
        self.expect_error(
            "/api/v3/course-review", method="GET",
            status=400, error_code="course_review_request_invalid",
        )
        self.expect_error(
            "/api/v3/course-review/actions", body={"course_id": COURSE, "action": "rebuild"},
            status=400, error_code="course_review_action_invalid",
        )
        self.expect_error(
            "/api/v3/course-review/actions", body={"course_id": COURSE},
            status=400, error_code="course_review_action_invalid",
        )

    def test_oversized_identifiers_are_rejected_before_any_lookup(self):
        long_id = "c" * 300
        self.expect_error(
            f"/api/v3/course-review?course_id={long_id}", method="GET",
            status=400, error_code="course_review_request_invalid",
        )
        self.expect_error(
            f"/api/v3/course-review/lecture?course_id={COURSE}&sub_id={long_id}", method="GET",
            status=400, error_code="course_review_request_invalid",
        )
        self.expect_error(
            "/api/v3/course-review/actions", body={"course_id": long_id, "action": "refresh"},
            status=400, error_code="course_review_action_invalid",
        )

    # -- refresh action -----------------------------------------------------

    def test_refresh_queues_only_missing_lectures_and_reuses_the_summary_queue(self):
        self.service.seed_transcript(SUB_A)
        status, data = self.post(
            "/api/v3/course-review/actions", {"course_id": COURSE, "action": "refresh"}
        )
        self.assertEqual(status, 202)
        self.assertEqual(data["counts"], {"queued": 1, "skipped": 0, "blocked": 1})
        # 上层是前端直接读的计数（整数），明细在 *_lectures 里。
        self.assertEqual(data["status"], "queued")
        self.assertEqual((data["queued"], data["skipped"], data["blocked"]), (1, 0, 1))
        self.assertIsInstance(data["reasons"], list)
        self.assertTrue(all(isinstance(item, str) and item for item in data["reasons"]))
        self.assertEqual(
            data["queued_lectures"],
            [{"sub_id": SUB_A, "reason": "missing", "task_id": "task-1"}],
        )
        self.assertEqual(data["blocked_lectures"], [{"sub_id": SUB_B, "reason": "transcript_missing"}])
        self.assertEqual(self.service._summary_enqueues, [(COURSE, SUB_A)])

    def test_refresh_shape_matches_the_frontend_consumer(self):
        """The refresh receipt carries the exact fields the review page reads."""
        self.service.seed_transcript(SUB_A)
        self.service.seed_transcript(SUB_B)
        self.service.import_summary(SUB_A)
        self.service.seed_snapshot()
        status, data = self.post(
            "/api/v3/course-review/actions", {"course_id": COURSE, "action": "refresh"}
        )
        self.assertEqual(status, 202)
        for key in ("status", "queued", "skipped", "blocked", "reasons"):
            self.assertIn(key, data)
        self.assertIsInstance(data["queued"], int)
        self.assertIsInstance(data["skipped"], int)
        self.assertIsInstance(data["blocked"], int)
        self.assertIn(data["status"], {"queued", "blocked", "noop"})

    def test_refresh_never_skips_a_lecture_that_never_had_a_summary(self):
        """A matching revision is not "up to date" when no AI output exists yet."""
        self.service.seed_transcript(SUB_A)
        self.service.seed_transcript(SUB_B)
        self.service.seed_snapshot()
        status, data = self.post(
            "/api/v3/course-review/actions", {"course_id": COURSE, "action": "refresh"}
        )
        self.assertEqual(status, 202)
        self.assertEqual(data["counts"], {"queued": 2, "skipped": 0, "blocked": 0})
        self.assertEqual({entry["reason"] for entry in data["queued_lectures"]}, {"missing"})
        self.assertEqual(
            sorted(sub_id for _, sub_id in self.service._summary_enqueues), [SUB_A, SUB_B]
        )

    def test_refresh_skips_a_lecture_whose_ai_output_already_matches(self):
        self.service.seed_transcript(SUB_A)
        self.service.seed_transcript(SUB_B)
        self.service.import_summary(SUB_A)
        self.service.seed_snapshot()
        status, data = self.post(
            "/api/v3/course-review/actions", {"course_id": COURSE, "action": "refresh"}
        )
        self.assertEqual(status, 202)
        self.assertEqual(data["counts"], {"queued": 1, "skipped": 1, "blocked": 0})
        self.assertEqual(data["skipped_lectures"], [{"sub_id": SUB_A, "reason": "up_to_date"}])
        self.assertEqual(self.service._summary_enqueues, [(COURSE, SUB_B)])

    def test_queue_failure_lands_in_blocked_with_a_closed_reason(self):
        self.service.seed_transcript(SUB_A)
        self.service.set_queue_failure("ai_key_missing")
        status, data = self.post(
            "/api/v3/course-review/actions", {"course_id": COURSE, "action": "refresh"}
        )
        self.assertEqual(status, 202)
        self.assertEqual(data["counts"], {"queued": 0, "skipped": 0, "blocked": 2})
        reasons = {entry["reason"] for entry in data["blocked_lectures"]}
        self.assertEqual(reasons, {"ai_key_missing", "transcript_missing"})
        self.assertEqual(data["status"], "blocked")

    # -- explain_assessment action（N8A）--------------------------------------

    ITEM = "cka:" + "a" * 32

    def test_explain_action_needs_an_item_and_stays_in_the_closed_action_set(self):
        self.expect_error(
            "/api/v3/course-review/actions", body={"course_id": COURSE, "action": "explain_assessment"},
            status=400, error_code="course_review_action_invalid",
        )
        self.expect_error(
            "/api/v3/course-review/actions",
            body={"course_id": COURSE, "action": "explain_everything", "item_id": self.ITEM},
            status=400, error_code="course_review_action_invalid",
        )
        self.assertEqual(self.service._answer_requests, [], "非法请求不得走到动作层")

    def test_explain_action_receipt_matches_the_frontend_consumer(self):
        self.service.seed_answer_item(self.ITEM)
        status, data = self.post("/api/v3/course-review/actions", {
            "course_id": COURSE, "action": "explain_assessment",
            "item_id": self.ITEM, "content_hash": "c" * 64,
        })
        self.assertEqual(status, 202)
        self.assertEqual(data["action"], "explain_assessment")
        self.assertEqual(data["status"], "queued")
        self.assertTrue(data["created"])
        self.assertEqual(data["item_id"], self.ITEM)
        for key in ("ai", "task_id", "error_code", "observed_at"):
            self.assertIn(key, data)
        for key in ("ai_state", "ai_explanation", "ai_citations", "ai_error_code"):
            self.assertIn(key, data["ai"])
        self.assertEqual(self.service._answer_requests, [(self.ITEM, "c" * 64, False)])

    def test_explain_action_maps_closed_failure_codes_to_status(self):
        self.expect_error(
            "/api/v3/course-review/actions",
            body={"course_id": "crs-not-here", "action": "explain_assessment", "item_id": self.ITEM},
            status=404, error_code="course_review_course_unknown",
        )
        self.expect_error(
            "/api/v3/course-review/actions",
            body={"course_id": COURSE, "action": "explain_assessment", "item_id": self.ITEM},
            status=404, error_code="assessment_item_unknown",
        )
        for code, status in (
            ("assessment_item_not_explainable", 400),
            ("assessment_item_content_stale", 400),
            ("assessment_item_source_lost", 400),
            ("ai_key_missing", 400),
        ):
            self.service.seed_answer_item(self.ITEM)
            self.service.set_answer_failure(code)
            self.expect_error(
                "/api/v3/course-review/actions",
                body={"course_id": COURSE, "action": "explain_assessment", "item_id": self.ITEM},
                status=status, error_code=code,
            )

    def test_explain_action_joins_the_local_first_gate(self):
        self.service.seed_answer_item(self.ITEM)
        self.service.set_auth_state("checking")
        self.service.set_auto_login(True)
        self.expect_error(
            "/api/v3/course-review/actions",
            body={"course_id": COURSE, "action": "explain_assessment", "item_id": self.ITEM},
            status=401, error_code="fudan_session_restoring",
        )
        self.assertEqual(self.service._answer_requests, [], "恢复期不得派发云端解答")

    def test_only_the_explicit_action_asks_for_an_answer(self):
        """读面（GET）与刷新动作都不会替学生问云端——只有 explain_assessment 会。"""
        self.service.seed_answer_item(self.ITEM)
        self.get(f"/api/v3/course-review?course_id={COURSE}")
        self.get(f"/api/v3/course-review/assessment?course_id={COURSE}")
        self.post("/api/v3/course-review/actions", {"course_id": COURSE, "action": "refresh"})
        self.assertEqual(self.service._answer_requests, [])

    # -- 闪卡（RR-P4FSRS-1）-------------------------------------------------

    def test_flashcards_route_serves_deck_derived_from_snapshot(self):
        """有快照：GET 闪卡读面 → 卡片从快照派生，队形含来源标签与计数。"""
        self.service.seed_transcript(SUB_A)
        self.service.import_summary(SUB_A)
        self.service.seed_snapshot()
        status, deck = self.get(f"/api/v3/course-review/flashcards?course_id={COURSE}")
        self.assertEqual(status, 200)
        self.assertEqual(deck["view"], "course_flashcards")
        self.assertGreater(deck["counts"]["total"], 0)
        self.assertEqual(deck["counts"]["due"], 0)  # 全是新卡，不算到期
        for card in deck["cards"]:
            self.assertIn(card["card_type"], ("cloze", "topic_cue", "anchor_recall"))
            self.assertTrue(card["front"])
            self.assertTrue(card["back"])
            self.assertIsInstance(card["evidence"], list)

    def test_flashcards_without_artifacts_is_an_honest_empty_state(self):
        """无产物：诚实空态 + 指向「更新课程知识」，绝不编卡。"""
        status, deck = self.get(f"/api/v3/course-review/flashcards?course_id={COURSE}")
        self.assertEqual(status, 200)
        self.assertEqual(deck["counts"]["total"], 0)
        self.assertEqual(deck["empty_action"]["action"], "refresh_course_knowledge")

    def test_flashcards_route_rejects_unknown_course(self):
        self.expect_error(
            "/api/v3/course-review/flashcards?course_id=no-such", method="GET",
            status=404, error_code="course_review_course_unknown",
        )

    def test_flashcards_joins_the_local_first_gate(self):
        self.service.set_auth_state("idle")
        self.expect_error(
            f"/api/v3/course-review/flashcards?course_id={COURSE}", method="GET",
            status=401, error_code="fudan_login_required",
        )

    def test_review_flashcard_action_returns_next_interval(self):
        self.service.seed_transcript(SUB_A)
        self.service.import_summary(SUB_A)
        self.service.seed_snapshot()
        _status, deck = self.get(f"/api/v3/course-review/flashcards?course_id={COURSE}")
        card_id = deck["cards"][0]["card_id"]
        status, result = self.post("/api/v3/course-review/actions", {
            "course_id": COURSE, "action": "review_flashcard",
            "card_id": card_id, "rating": RATING_GOOD,
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["card_id"], card_id)
        self.assertEqual(result["state"], "learning")
        self.assertTrue(result["interval_text"])
        # 评分后再读：learning 计数入账，reviewed_today=1。
        _status, deck2 = self.get(f"/api/v3/course-review/flashcards?course_id={COURSE}")
        self.assertEqual(deck2["counts"]["learning"], 1)
        self.assertEqual(deck2["counts"]["reviewed_today"], 1)

    def test_review_flashcard_action_rejects_bad_rating_and_unknown_card(self):
        self.expect_error(
            "/api/v3/course-review/actions",
            body={"course_id": COURSE, "action": "review_flashcard", "card_id": "x", "rating": 9},
            status=400, error_code="course_review_action_invalid",
        )
        self.expect_error(
            "/api/v3/course-review/actions",
            body={"course_id": COURSE, "action": "review_flashcard",
                  "card_id": "f" * 32, "rating": RATING_GOOD},
            status=404, error_code="course_review_flashcard_unknown",
        )

    def test_review_flashcard_rejects_non_integer_ratings(self):
        # QA-SWEEP-1 P2-11：rating 只收真整数——"3"/2.9/true/缺席这类会被
        # int() 强转洗进闭集的形状，一律 400，不代为取整。
        for bad_rating in ("3", 2.9, True, None):
            self.expect_error(
                "/api/v3/course-review/actions",
                body={"course_id": COURSE, "action": "review_flashcard",
                      "card_id": "x", "rating": bad_rating},
                status=400, error_code="course_review_action_invalid",
            )

    def test_review_flashcard_rejects_card_from_another_course(self):
        # QA-SWEEP-1 P1-3：course A 的授权面改不动 course B 的卡——卡真实
        # 存在但归属他课时，按「卡不存在」同一闭集 404，不泄露他课卡片存在性。
        self.service.seed_transcript("sub-other-0001", "另一门课的重点是采样定理")
        self.service.learning_store.import_remote_summary(
            course_id=OTHER_COURSE,
            sub_id="sub-other-0001",
            input_hash="b" * 32,
            model="synthetic",
            markdown="## 概览\n另一门课的合成摘要正文。",
            chapters=[{"title": "第一段", "summary": "合成章节", "start_ms": 0}],
            ppt_pages=[],
            key_takeaways=["合成要点"],
        )
        save_course_knowledge(
            self.service.learning_store, course_id=OTHER_COURSE,
            sub_ids=["sub-other-0001"],
        )
        _status, other_deck = self.get(
            f"/api/v3/course-review/flashcards?course_id={OTHER_COURSE}"
        )
        self.assertTrue(other_deck["cards"])
        other_card_id = other_deck["cards"][0]["card_id"]
        self.expect_error(
            "/api/v3/course-review/actions",
            body={"course_id": COURSE, "action": "review_flashcard",
                  "card_id": other_card_id, "rating": RATING_GOOD},
            status=404, error_code="course_review_flashcard_unknown",
        )

    # -- P10 术语候选确认/忽略（P10-CONTRACT-1 §②/§③ 后端半边）------------

    def _seed_term_pair(self, before="这个费米能及很重要", after="这个费米能级很重要，"):
        from src.runtime.course_memory import sink_course_examples

        added = sink_course_examples(
            self.service.root, COURSE,
            [{"start_ms": 0, "end_ms": 1000, "before": before, "after": after}],
            sub_id=SUB_A,
        )
        self.assertEqual(added, 1)

    def test_term_confirm_action_books_ledger_and_hides_row(self):
        self._seed_term_pair()
        _status, review = self.get(f"/api/v3/course-review?course_id={COURSE}")
        self.assertEqual(len(review["term_candidates"]["rows"]), 1)
        status, result = self.post("/api/v3/course-review/actions", {
            "course_id": COURSE, "action": "confirm_term_candidate",
            "wrong": "费米能及", "right": "费米能级",
        })
        self.assertEqual(status, 200)
        self.assertEqual(result, {"confirmed": True, "total_confirmed": 1})
        _status, review = self.get(f"/api/v3/course-review?course_id={COURSE}")
        self.assertEqual(review["term_candidates"]["rows"], [])
        self.assertEqual(review["term_candidates"]["confirmed_count"], 1)

    def test_term_dismiss_action_removes_candidate(self):
        self._seed_term_pair()
        status, result = self.post("/api/v3/course-review/actions", {
            "course_id": COURSE, "action": "dismiss_term_candidate",
            "wrong": "费米能及", "right": "费米能级",
        })
        self.assertEqual(status, 200)
        self.assertEqual(result, {"dismissed": True, "total_dismissed": 1})
        _status, review = self.get(f"/api/v3/course-review?course_id={COURSE}")
        self.assertEqual(review["term_candidates"]["rows"], [])
        self.assertEqual(review["term_candidates"]["confirmed_count"], 0)

    def test_term_action_unknown_pair_maps_to_closed_404(self):
        self._seed_term_pair()
        self.expect_error(
            "/api/v3/course-review/actions",
            body={"course_id": COURSE, "action": "confirm_term_candidate",
                  "wrong": "不存在的词", "right": "没有的词"},
            status=404, error_code="term_candidate_unknown",
        )

    def test_term_action_shape_gates_stay_bad_requests(self):
        self._seed_term_pair()
        # wrong/right 缺席或空=请求不合法（400），与「词对不在候选集」（404）分开报
        self.expect_error(
            "/api/v3/course-review/actions",
            body={"course_id": COURSE, "action": "confirm_term_candidate"},
            status=400, error_code="course_review_action_invalid",
        )
        self.expect_error(
            "/api/v3/course-review/actions",
            body={"course_id": COURSE, "action": "confirm_term_candidate",
                  "wrong": "", "right": "费米能级"},
            status=400, error_code="course_review_action_invalid",
        )
        self.expect_error(
            "/api/v3/course-review/actions",
            body={"course_id": COURSE, "action": "dismiss_term_candidate",
                  "wrong": "词" * 65, "right": "费米能级"},
            status=400, error_code="course_review_action_invalid",
        )

    def test_term_action_unknown_course_maps_to_closed_404(self):
        self.expect_error(
            "/api/v3/course-review/actions",
            body={"course_id": "crs-not-mine", "action": "confirm_term_candidate",
                  "wrong": "费米能及", "right": "费米能级"},
            status=404, error_code="course_review_course_unknown",
        )


class StudentJourneyApiTests(unittest.TestCase):
    """The whole student-visible flow through real HTTP and the real app.

    A narrow auth stub is the only fake here: routing, the course-review
    application methods, the contract validation, the snapshot store and the
    summary import are all the production code paths.
    """

    def setUp(self) -> None:
        from tests.http_services import http_services

        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.db_path = root / "learning.db"
        self.store = LearningStore(self.db_path)
        ensure_student_feature_schema(self.db_path)
        self.catalog = CatalogRepository(root / "state.db")
        self.tasks = TaskStore(root / "state.db")
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
        self.app.output_dir = root
        self._patchers = [
            mock.patch.object(CourseLensApplication, "_export_summary_markdown", lambda *a, **k: None),
            mock.patch.object(CourseLensApplication, "_run_assessment_radar", lambda *a, **k: None),
        ]
        # 摘要入队本身不在本测试主题内（它需要远端协调器与凭据）：用记录式替身
        # 顶替这一步，其余全部走生产代码。真写法仍写一条真实任务行，好让
        # 「再点一次不会重复排队」的判定基于真实任务表。
        self.enqueued: list[tuple[str, str]] = []

        def _enqueue(_app, course_id, sub_id, **_kwargs):
            self.enqueued.append((str(course_id), str(sub_id)))
            task, _created = self.tasks.add_task(
                "summary", str(course_id), str(sub_id),
                {"course_id": str(course_id), "sub_id": str(sub_id)},
                config_key="lecture-summary:ppt",
            )
            return task

        self._enqueue_patch = mock.patch.object(CourseLensApplication, "enqueue_summary", _enqueue)
        self._enqueue_patch.start()
        for patcher in self._patchers:
            patcher.start()
        services = http_services(self.app)
        services.learning.course_review = self.app.course_review
        services.learning.course_review_refresh = self.app.course_review_refresh
        services.auth_catalog.authentication_snapshot = lambda: {"state": "ready"}
        server = _Server(("127.0.0.1", 0), make_handler(services, root))
        self._server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{server.server_port}"

    def tearDown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._enqueue_patch.stop()
        for patcher in self._patchers:
            patcher.stop()
        self.store.close()
        self._tmp.cleanup()

    def get(self, path: str):
        with urlopen(f"{self.base}{path}") as response:
            return response.status, json.loads(response.read())["data"]

    def post(self, path: str, body: dict):
        request = Request(
            f"{self.base}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urlopen(request) as response:
            return response.status, json.loads(response.read())["data"]

    def _review_over_http(self) -> dict:
        _status, data = self.get(f"/api/v3/course-review?course_id={COURSE}")
        return data

    def test_student_journey_open_refresh_land_and_read_again(self):
        self._seed_transcript(SUB_A, ["重点是尺度函数与多分辨率分析"])
        self._seed_transcript(SUB_B, ["第二讲引入滤波器组"])

        # 1) 打开课程复习：诚实可见的初始态（还没整理过）。
        opening = self._review_over_http()
        self.assertEqual(opening["view"]["view"], "course_overview")
        self.assertEqual(opening["state"], "stale")
        self.assertEqual(opening["view"]["coverage"]["lectures_total"], 2)
        self.assertIsNone(opening["snapshot"])
        self.assertEqual(opening["view"]["coverage"]["transcript_segments"], 2)

        # 2) 点「更新课程知识」：两讲都有字幕，都排进既有摘要队列。
        _status, refreshed = self.post(
            "/api/v3/course-review/actions", {"course_id": COURSE, "action": "refresh"}
        )
        self.assertEqual(refreshed["status"], "queued")
        self.assertEqual(refreshed["queued"], 2)
        self.assertTrue(refreshed["reasons"])
        self.assertEqual(sorted(sub_id for _, sub_id in self.enqueued), [SUB_A, SUB_B])

        # 3) AI 结果按既定单入口落地。
        spans = [row["evidence_id"] for row in self.store.get_transcript_segments(SUB_A)]
        self.app._import_remote_summary_result(COURSE, SUB_A, {
            "input_hash": "f" * 32,
            "metrics": {},
            "outputs": {
                "summary": {
                    "markdown": "## 概览\n小波变换与尺度函数。",
                    "model": "synthetic",
                    "chapters": [{"title": "小波变换", "summary": "合成章节", "start_ms": 0}],
                    "key_takeaways": ["要点一"],
                },
                "ppt_pages": [],
                "lecture_ir": {
                    "contract": "evidence.v1",
                    "sections": [{"kind": "section", "title": "小波变换", "time": None,
                                  "spans": [{"kind": "segment", "id": spans[0]}],
                                  "content": None, "id": "unit:111111111111"}],
                    "knowledge_units": [{"kind": "knowledge_unit", "title": "尺度函数", "time": None,
                                         "spans": [{"kind": "segment", "id": spans[0]}],
                                         "content": {"text": "用尺度函数构造逼近"},
                                         "id": "unit:222222222222"}],
                    "key_moments": [],
                },
            },
        })

        # 4) 再看：这一讲已就绪，另一讲仍如实地是未整理。
        after = self._review_over_http()
        by_sub = {lecture["sub_id"]: lecture for lecture in after["view"]["lectures"]}
        self.assertEqual(by_sub[SUB_A]["status"], "ready")
        self.assertNotEqual(by_sub[SUB_B]["status"], "ready")
        self.assertIsNotNone(after["snapshot"])
        self.assertEqual(after["assessment_workspace"]["view"], "assessment_workspace")

        # 5) 点开这一讲：要点都带着可回跳的引用。
        _status, detail = self.get(
            f"/api/v3/course-review/lecture?course_id={COURSE}&sub_id={SUB_A}"
        )
        self.assertEqual(detail["view"], "lecture_detail")
        self.assertTrue(detail["key_points"])
        citation_ids = {ref["citation_id"] for ref in detail["evidence_refs"]}
        for point in detail["key_points"]:
            self.assertTrue(set(point["citation_ids"]) <= citation_ids)
        transcript = [ref for ref in detail["evidence_refs"] if ref["kind"] == "transcript"]
        self.assertTrue(transcript[0]["locator"]["start_ms"] >= 0)

    def _seed_transcript(self, sub_id: str, texts: list[str]) -> None:
        self.store.replace_transcript_segments(
            sub_id, source_path="synthetic.srt", source_mtime_ns=1, source_size=10,
            segments=[
                {"start_ms": index * 30_000, "end_ms": index * 30_000 + 25_000, "text": text}
                for index, text in enumerate(texts)
            ],
        )


if __name__ == "__main__":
    unittest.main()
