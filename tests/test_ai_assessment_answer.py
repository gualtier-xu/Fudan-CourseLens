"""N8A: 用户触发的题目 AI 解答/解析 + 讲次级练习接入课程级工作台。

覆盖的判据（全部合成夹具，零真实账号、零外呼）：

1. 唯一触发点是用户点击：导入 / 刷新快照 / 自动材料规则都不会替学生问云端；
2. 未知课程、未知题目、孤儿题、题目版本对不上、缺可用证据一律 fail-closed；
3. 证据包只取本机材料的有界窗口，**永不携带答案字段**；
4. 引用校验与 ``student_features.validate_grounded_answer`` 同判据（同一份证据下
   两者判定一致），差异只有一处并在此钉住；
5. 没有材料答案时 AI 结果由视图层升格成答案并标 ``ai_generated``；有材料答案时
   原答案与来源一个字节都不动，AI 只进 ``ai_explanation``；
6. 题目内容变了、文档删了、题目成孤儿之后，旧 AI 结果不再当当前答案显示；
7. 重复点击复用在途任务或同身份结果，不重复烧云；失败/资料不足不自动重试；
8. 本地练习分组：有题/空题/作答统计三种形态，且答案提交前不下发；
9. 本地出题（字幕 → 回忆题）不需要 DeepSeek Key。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from src.application import CourseLensApplication
from src.runtime import assessment_ir as air
from src.runtime.catalog_repository import CatalogRepository
from src.runtime.learning_store import LearningStore
from src.runtime.student_features import (
    LOCAL_PRACTICE_SOURCE_LABEL,
    ensure_student_feature_schema,
    list_quiz_items,
    local_practice_view,
    save_quiz_items,
    validate_grounded_answer,
)
from src.runtime.task_store import TaskStore
from src.services.domains import CourseReviewActionError

COURSE = "crs-synthetic-2026a"
SUB_A = "sub-syn-0001"
# 课程级考核文档（scope=course）按设计没有讲次：真题的主要形态。
DOCUMENT_ID = "e" * 32
DOCUMENT_SHA = "f" * 64
# 题面必须命中拆题器的题号规则（`^\d{1,2}[．.、]\s*\D`），否则一页切不出题单元。
QUESTION = "1、求尺度函数的傅里叶变换，写出关键步骤。"
# 拆题器会把「答案：」标记本身切掉，留下的才是答案正文。
ANSWER_TEXT = "先把定义式写出来再积分。"
ANSWER_LINE = "答案：" + ANSWER_TEXT


class _FakeCoordinator:
    """替掉远端协调器：只回放一份合成 result，不碰网络。"""

    def __init__(self, outputs: dict):
        self.outputs = outputs
        self.jobs: list[dict] = []

    def execute(self, *, task_id, build_job, import_result, cancel_requested=None, progress=None):
        job = build_job("synthetic-public-key")
        self.jobs.append(job)
        result = {"outputs": dict(self.outputs), "metrics": {}}
        import_result(result)
        return result


class _Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "learning.db"
        self.store = LearningStore(self.db_path)
        ensure_student_feature_schema(self.db_path)
        self.catalog = CatalogRepository(self.root / "state.db")
        self.tasks = TaskStore(self.root / "state.db")
        self.catalog.upsert_course(COURSE, "合成课程", teacher="张老师")
        self.catalog.upsert_lecture(COURSE, {
            "sub_id": SUB_A, "sub_title": "第一讲", "date": "2026-09-01",
        })

        self.app = CourseLensApplication.__new__(CourseLensApplication)
        self.app.learning_store = self.store
        self.app.catalog_repository = self.catalog
        self.app.task_store = self.tasks
        self.app.output_dir = self.root
        self.app.search_index = type("_S", (), {
            "request_refresh": lambda self, sub_ids=None, **_k: {"state": "indexing"},
        })()
        # 只填这条链路真正会碰到的运行时状态（不启动后台线程、不建真实远端客户端）。
        self.app._lock = threading.RLock()
        self.app._paused = False
        self.app._question_queue = []
        self.app._question_current = None
        self.app._question_cancel = threading.Event()
        # WP-3（close 链收口面）：循环顶读 _generation_workers_stop，合成壳同须补齐。
        self.app._generation_workers_stop = threading.Event()
        self.app._active_question_task_id = ""
        self.app._cloud_run_condition = threading.Condition()
        self.app._cloud_runs_active = 0
        self.app._deepseek_api_key = ""
        self.coordinator = _FakeCoordinator({
            "answer": {
                "answer": "先写定义式，再逐项积分得到结论。",
                "grounded": True,
                "citations": [],
            },
        })
        self.captured_lease_workflows: list[str | None] = []
        self._patchers = [
            mock.patch.object(CourseLensApplication, "_export_summary_markdown", lambda *a, **k: None),
            mock.patch.object(CourseLensApplication, "_run_assessment_radar", lambda *a, **k: None),
            mock.patch.object(CourseLensApplication, "_request_search_refresh", lambda *a, **k: None),
            mock.patch.object(CourseLensApplication, "_ensure_question_worker", lambda self: None),
            mock.patch.object(CourseLensApplication, "_deepseek_key", lambda self: "sk-synthetic"),
            mock.patch.object(
                CourseLensApplication, "_leased_remote_coordinator",
                self._fake_coordinator_lease,
            ),
            mock.patch.object(CourseLensApplication, "_cloud_run_slot", self._no_cloud_slot),
        ]
        for patcher in self._patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in self._patchers:
            patcher.stop()
        self.store.close()
        self.tmp.cleanup()

    # -- 远端替身 -----------------------------------------------------------

    @contextlib.contextmanager
    def _fake_coordinator_lease(self, _task_id: str, *, workflow: str | None = None):
        self.captured_lease_workflows.append(workflow)
        yield self.coordinator

    @contextlib.contextmanager
    def _no_cloud_slot(self, _task_id: str, *, cancel_requested=None, on_wait=None):
        yield

    # -- 夹具 ---------------------------------------------------------------

    def seed_paper(self, *, pages: tuple[str, ...] = (QUESTION,), scope: str = "course") -> str:
        """落一份考核文档 + 页文本，再按生产路径投影成题目。"""
        sub_id = SUB_A if scope == "lecture" else ""
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                """INSERT INTO learning_documents(
                       document_id,course_id,sub_id,title,original_name,extension,media_type,
                       storage_path,sha256,size_bytes,page_count,extraction_state,
                       created_at,updated_at,doc_type,scope)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (DOCUMENT_ID, COURSE, sub_id, "期末真题", "真题.pdf", ".pdf", "application/pdf",
                 str(self.root / "paper.pdf"), DOCUMENT_SHA, 10, len(pages), "ready",
                 1.0, 1.0, "exam_paper", scope),
            )
            for index, text in enumerate(pages, start=1):
                db.execute(
                    """INSERT INTO learning_document_pages(
                           document_id,page_num,text,text_hash,visual_hash,extraction_state,metadata_json)
                       VALUES(?,?,?,?,?,?,?)""",
                    (DOCUMENT_ID, index, text,
                     hashlib.sha256(text.encode("utf-8")).hexdigest() if text else "",
                     "", "ready", "{}"),
                )
        air.refresh_assessment_items(self.db_path, DOCUMENT_ID)
        return DOCUMENT_ID

    def seed_exam_question_rows(self) -> None:
        """把拆题表补上（生产由 worker 侧拆题写入），让课程快照能收进这道题。

        身份逐字抄自 projection 结果：课程的引用视图与本地题目库必须是同一道题。
        """
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
            for item in self.items():
                db.execute(
                    """INSERT OR REPLACE INTO exam_questions(question_id,document_id,sub_id,
                           course_id,question_no,label,kind,start_page,end_page,anchor_text,
                           content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item["question_id"], item["document_id"], item["sub_id"], item["course_id"],
                     item["question_no"], item["label"], item["kind"],
                     item["start_page"], item["end_page"], item["label"],
                     item["content_hash"], 1.0),
                )

    def rewrite_paper(self, text: str) -> None:
        """把正文换掉再重投影：生产路径里 item_id 与 content_hash 都会跟着变。"""
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                "UPDATE learning_document_pages SET text=? WHERE document_id=? AND page_num=1",
                (text, DOCUMENT_ID),
            )
            db.execute(
                "UPDATE learning_documents SET sha256=? WHERE document_id=?",
                ("a" * 64, DOCUMENT_ID),
            )
        air.refresh_assessment_items(self.db_path, DOCUMENT_ID)

    def items(self) -> list[dict]:
        return air.list_assessment_items(self.db_path, course_id=COURSE)

    def first_item(self) -> dict:
        rows = self.items()
        self.assertTrue(rows, "夹具应当已经投影出至少一道题")
        return rows[0]

    def question_tasks(self) -> list[dict]:
        return [task for task in self.tasks.list_tasks(limit=200) if task.get("kind") == "question"]

    def drain_question_queue(self) -> None:
        """同步跑完队列（生产是后台线程，测试里就地跑）。"""
        self.app._run_question_queue()
        # N1-ROUTING + D-20261009-13：learning_pack 在 worker N20 profile 合同
        # 闭集内（必须 process-v1）→ 白名单恒 process.yml。
        self.assertTrue(all(w == "process.yml" for w in self.captured_lease_workflows))


class AutoTriggerTests(_Harness):
    """导入与刷新不得替学生问云端。"""

    def test_refresh_projection_never_requests_an_answer(self):
        self.seed_paper()
        self.assertEqual(self.question_tasks(), [], "投影题目本身不是一次云端请求")
        self.assertEqual(air.list_ai_answers(self.db_path, course_id=COURSE), {})

    def test_evidence_builder_is_read_only(self):
        self.seed_paper()
        item = self.first_item()
        before = json.dumps(self.items(), ensure_ascii=False, sort_keys=True)
        air.build_assessment_evidence(self.db_path, item)
        self.assertEqual(
            json.dumps(self.items(), ensure_ascii=False, sort_keys=True), before,
            "组装证据不得改动题目行",
        )
        self.assertEqual(self.question_tasks(), [])

    def test_local_quiz_generation_needs_no_cloud_key(self):
        """quiz_after_import 是纯本地出题：没有 DeepSeek Key 也必须照常跑完。

        之前这条链按 Key 分流成 action_required + quiz_local_key_required，是假阻塞。
        """
        self.tasks.replace_automation_rules([{"course_id": COURSE, "quiz_after_import": True}])
        with mock.patch.object(CourseLensApplication, "_deepseek_key", lambda self: ""), \
                mock.patch("threading.Thread", _SyncThread):
            self.app._import_automation_result({"outputs": {"cloud_catalog": {
                "course_id": COURSE,
                "lecture": {"sub_id": SUB_A, "sub_title": "第一讲"},
            }}})
        pending = self.tasks.get_app_state("automation_quiz_pending", {}) or {}
        self.assertEqual(pending[SUB_A]["state"], "completed")
        self.assertEqual(pending[SUB_A]["error_code"], "")
        self.assertNotIn("quiz_local_key_required", json.dumps(pending))
        self.assertEqual(self.question_tasks(), [], "本地出题不发任何云任务")


class _SyncThread:
    """把后台生成线程就地跑完，好断言终态。"""

    def __init__(self, target=None, name="", daemon=False):
        self._target = target

    def start(self) -> None:
        if self._target is not None:
            self._target()

    def is_alive(self) -> bool:
        return False


class ExplainActionFailClosedTests(_Harness):
    """未知课程/题目、孤儿题、版本不符、缺证据一律 fail-closed。"""

    def test_unknown_course_is_rejected(self):
        self.seed_paper()
        with self.assertRaises(CourseReviewActionError) as caught:
            self.app.explain_assessment_item("crs-not-here", self.first_item()["item_id"])
        self.assertEqual(caught.exception.code, "course_review_course_unknown")

    def test_unknown_item_is_rejected(self):
        self.seed_paper()
        with self.assertRaises(CourseReviewActionError) as caught:
            self.app.explain_assessment_item(COURSE, "cka:" + "0" * 32)
        self.assertEqual(caught.exception.code, "assessment_item_unknown")

    def test_projected_local_quiz_is_not_explainable(self):
        """本地练习的「答案」就是课程字幕，不是作业/真题：不给云端解答。"""
        self.seed_paper()
        with self.assertRaises(CourseReviewActionError) as caught:
            self.app.explain_assessment_item(COURSE, "quiz:abcdef012345")
        self.assertEqual(caught.exception.code, "assessment_item_not_explainable")

    def test_content_hash_mismatch_is_rejected_before_any_work(self):
        self.seed_paper()
        item = self.first_item()
        with self.assertRaises(CourseReviewActionError) as caught:
            self.app.explain_assessment_item(COURSE, item["item_id"], content_hash="0" * 64)
        self.assertEqual(caught.exception.code, "assessment_item_content_stale")
        self.assertEqual(self.question_tasks(), [])

    def test_orphaned_item_reports_source_lost(self):
        self.seed_paper()
        item = self.first_item()
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                "UPDATE assessment_items SET status='orphaned' WHERE item_id=?", (item["item_id"],))
        with self.assertRaises(CourseReviewActionError) as caught:
            self.app.explain_assessment_item(COURSE, item["item_id"])
        self.assertEqual(caught.exception.code, "assessment_item_source_lost")

    def test_deleted_document_reports_source_lost(self):
        self.seed_paper()
        item = self.first_item()
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("DELETE FROM learning_documents WHERE document_id=?", (DOCUMENT_ID,))
        with self.assertRaises(CourseReviewActionError) as caught:
            self.app.explain_assessment_item(COURSE, item["item_id"])
        self.assertEqual(caught.exception.code, "assessment_item_source_lost")

    def test_missing_material_declines_honestly_and_costs_nothing(self):
        """题还在、原始材料没了：诚实说资料不足，不派发、不烧云。"""
        self.seed_paper()
        item = self.first_item()
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("DELETE FROM learning_document_pages WHERE document_id=?", (DOCUMENT_ID,))
        receipt = self.app.explain_assessment_item(COURSE, item["item_id"])
        self.assertEqual(receipt["status"], "insufficient")
        self.assertFalse(receipt["created"])
        self.assertEqual(receipt["error_code"], "assessment_evidence_unavailable")
        self.assertEqual(self.question_tasks(), [], "证据不足不派发云任务")
        self.assertEqual(receipt["ai"]["ai_state"], "insufficient")


class ExplainActionChainTests(_Harness):
    """用户点击 → 排队 → 云端 → 本地读面：整条链的行为。"""

    def _request(self, **kwargs) -> dict:
        item = self.first_item()
        return self.app.explain_assessment_item(
            COURSE, item["item_id"], content_hash=item["content_hash"], **kwargs)

    def test_grounded_answer_lands_locally_and_exposes_citations(self):
        self.seed_paper()
        item = self.first_item()
        evidence, _ = air.build_assessment_evidence(self.db_path, item)
        self.coordinator.outputs["answer"]["citations"] = [evidence[1]["citation_id"]]

        receipt = self._request()
        self.assertEqual(receipt["status"], "queued")
        self.assertTrue(receipt["created"])
        self.drain_question_queue()

        view = air.assessment_view(self.first_item(), self._ai_row())
        self.assertEqual(view["ai_state"], "ready")
        self.assertEqual(view["answer_source"], "ai_generated")
        self.assertEqual(view["answer"], "先写定义式，再逐项积分得到结论。")
        self.assertTrue(view["has_answer"])
        self.assertEqual(view["ai_citations"][0]["kind"], "document_page")
        self.assertEqual(view["ai_citations"][0]["locator"]["page"], 1)

    def test_material_answer_is_never_overwritten(self):
        """材料自带答案时 AI 只出解析：answer 与 answer_source 一个字节都不动。"""
        self.seed_paper(pages=(QUESTION + "\n" + ANSWER_LINE,))
        item = self.first_item()
        self.assertEqual(item["answer_source"], "user_material")
        evidence, _ = air.build_assessment_evidence(self.db_path, item)
        self.coordinator.outputs["answer"]["citations"] = [evidence[1]["citation_id"]]

        self.app.explain_assessment_item(COURSE, item["item_id"])
        self.drain_question_queue()

        stored = self.first_item()
        self.assertEqual(stored["answer"], ANSWER_TEXT, "材料答案不得被 AI 覆盖")
        self.assertEqual(stored["answer_source"], "user_material")
        view = air.assessment_view(stored, self._ai_row())
        self.assertEqual(view["answer_source"], "user_material")
        self.assertEqual(view["answer"], ANSWER_TEXT)
        self.assertEqual(view["ai_explanation"], "先写定义式，再逐项积分得到结论。")

    def test_evidence_packet_never_carries_answer_fields(self):
        """题目自带的答案只能留在本地存储行里，绝不作为字段进云端证据包。"""
        self.seed_paper(pages=(QUESTION + "\n" + ANSWER_LINE,))
        item = self.first_item()
        self.assertEqual(item["answer"], ANSWER_TEXT, "夹具应当切出了材料自带答案")
        self.app.explain_assessment_item(COURSE, item["item_id"])
        self.drain_question_queue()
        job = self.coordinator.jobs[0]
        payload = dict(job.get("payload") or {})
        self.assertEqual(job["job_kind"], "learning_pack")
        self.assertEqual(job["requested_outputs"], ["answer"])
        self.assertNotIn("sk-synthetic", json.dumps(payload), "凭据只走 secrets 通道")
        for entry in payload["evidence"]:
            for field in ("answer", "answer_source", "has_answer", "ai_explanation"):
                self.assertNotIn(field, entry)
        # 题干必须送达（否则答不了题），但那是材料正文，不是答案字段。
        self.assertIn("傅里叶变换", json.dumps(payload, ensure_ascii=False))

    def test_ai_answer_text_never_reaches_the_contract_document_or_packet(self):
        """AI 生成的文本只出现在本地题目读面：合同文档与云端证据包里一个字都没有。"""
        self.seed_paper(pages=(QUESTION,), scope="lecture")
        item = self.first_item()
        evidence, _ = air.build_assessment_evidence(self.db_path, item)
        self.coordinator.outputs["answer"]["citations"] = [evidence[1]["citation_id"]]
        self.app.explain_assessment_item(COURSE, item["item_id"])
        self.drain_question_queue()

        ai_text = "先写定义式，再逐项积分得到结论。"
        from src.runtime.course_knowledge import (
            build_course_knowledge, build_evidence_packet,
        )
        document, _ = build_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A])
        packet = build_evidence_packet(self.store, course_id=COURSE, sub_id=SUB_A)
        self.assertNotIn(ai_text, json.dumps(document, ensure_ascii=False), "AI 文本不得进合同文档")
        self.assertNotIn(ai_text, json.dumps(packet, ensure_ascii=False), "AI 文本不得进云端证据包")
        # 本地读面照常给出来（这才是它该在的地方）。
        view = air.assessment_view(self.first_item(), self._ai_row())
        self.assertEqual(view["ai_explanation"], ai_text)

    def test_repeat_click_reuses_the_in_flight_task(self):
        self.seed_paper()
        first = self._request()
        second = self._request()
        self.assertTrue(first["created"])
        self.assertFalse(second["created"], "在途任务必须被复用")
        self.assertEqual(len(self.question_tasks()), 1, "不得重复派发")

    def test_repeat_click_reuses_a_ready_result_and_never_retries(self):
        self.seed_paper()
        item = self.first_item()
        evidence, _ = air.build_assessment_evidence(self.db_path, item)
        self.coordinator.outputs["answer"]["citations"] = [evidence[1]["citation_id"]]
        self._request()
        self.drain_question_queue()

        again = self._request()
        self.assertEqual(again["status"], "ready")
        self.assertFalse(again["created"])
        self.assertEqual(len(self.question_tasks()), 1, "已完成的结果不得再烧一次云")

    def test_insufficient_result_is_not_retried_without_the_user(self):
        """模型没给出可用引用 → 记 insufficient；不动手重试，只有 retry=True 才重跑。"""
        self.seed_paper()
        self._request()
        self.drain_question_queue()
        self.assertEqual(self._ai_row()["stage"], "insufficient")

        passive = self._request()
        self.assertFalse(passive["created"], "失败不自动重试")
        self.assertEqual(len(self.question_tasks()), 1)

        forced = self._request(retry=True)
        self.assertEqual(forced["status"], "queued")
        self.assertFalse(forced["created"], "重试复用同一行任务，不新开一行")
        self.assertEqual(len(self.question_tasks()), 1, "重试不得堆出第二个同身份任务")
        self.assertEqual(self.question_tasks()[0]["state"], "queued", "同一行任务重新排队")
        self.assertEqual(self._ai_row()["stage"], "queued")

    def test_queued_task_for_a_rewritten_item_fails_instead_of_answering(self):
        """排队期间题目被重写：新旧题不再是同一道，如实失败而不是将就回答。"""
        self.seed_paper()
        item = self.first_item()
        self.app.explain_assessment_item(COURSE, item["item_id"])
        self.rewrite_paper("1、求小波基的支撑区间。")
        self.drain_question_queue()
        task = self.question_tasks()[0]
        self.assertEqual(task["state"], "failed")
        # 重投影会连带删掉旧题行（item_id 由 content_hash 派生），所以是「没了」而不是「变了」。
        self.assertEqual(task["error"], "assessment_item_unknown")

    def test_worker_rejects_a_payload_whose_content_hash_moved(self):
        """防御判据：题目行还在、但内容哈希已经对不上 → 不将就回答。"""
        self.seed_paper()
        item = self.first_item()
        row, code = self.app._assessment_answer_target(
            {"course_id": COURSE},
            {"assessment_item_id": item["item_id"], "content_hash": "0" * 64},
        )
        self.assertIsNone(row)
        self.assertEqual(code, "assessment_item_content_stale")

    def test_ai_result_stops_being_current_after_the_item_changes(self):
        self.seed_paper()
        item = self.first_item()
        evidence, _ = air.build_assessment_evidence(self.db_path, item)
        self.coordinator.outputs["answer"]["citations"] = [evidence[1]["citation_id"]]
        self.app.explain_assessment_item(COURSE, item["item_id"])
        self.drain_question_queue()
        self.assertTrue(air.list_ai_answers(self.db_path, course_id=COURSE))
        stored = self.first_item()
        self.assertEqual(air.assessment_view(stored, self._ai_row())["ai_state"], "ready")

        self.rewrite_paper("1、求小波基的支撑区间。")
        rewritten = self.first_item()
        self.assertNotEqual(rewritten["item_id"], stored["item_id"])
        self.assertEqual(
            air.list_ai_answers(self.db_path, course_id=COURSE), {},
            "题目行消失时它的 AI 结果必须一并消失",
        )
        self.assertEqual(air.assessment_view(rewritten)["ai_state"], "")

    def test_ai_rows_are_cleared_when_the_document_is_deleted(self):
        self.seed_paper()
        item = self.first_item()
        evidence, _ = air.build_assessment_evidence(self.db_path, item)
        self.coordinator.outputs["answer"]["citations"] = [evidence[1]["citation_id"]]
        self.app.explain_assessment_item(COURSE, item["item_id"])
        self.drain_question_queue()
        air.delete_assessment_items(self.db_path, document_id=DOCUMENT_ID)
        self.assertEqual(air.list_ai_answers(self.db_path, course_id=COURSE), {})

    def test_stale_identity_is_reported_not_shown(self):
        """直接把一行对不上的 AI 结果塞进去：读面必须说它过期，而不是当答案。"""
        self.seed_paper()
        item = self.first_item()
        row = {
            "item_id": item["item_id"], "course_id": COURSE, "document_id": DOCUMENT_ID,
            "content_hash": "0" * 64, "revision_id": "", "stage": "ready",
            "answer": "过期的解答", "explanation": "过期的解答", "citations": [],
            "model": "deepseek-flash", "prompt_version": air.AI_ANSWER_PROMPT_VERSION,
            "error_code": "", "task_id": "", "input_hash": "",
        }
        view = air.ai_answer_view(row, item)
        self.assertEqual(view["ai_state"], "stale")
        self.assertEqual(view["ai_explanation"], "")
        self.assertFalse(air.assessment_view(item, row)["has_answer"])

    def test_missing_key_fails_closed(self):
        self.seed_paper()
        with mock.patch.object(CourseLensApplication, "_deepseek_key", lambda self: ""):
            with self.assertRaises(CourseReviewActionError) as caught:
                self._request()
        self.assertEqual(caught.exception.code, "ai_key_missing")
        self.assertEqual(self.question_tasks(), [])

    def _ai_row(self) -> dict:
        return air.list_ai_answers(
            self.db_path, item_ids=[self.first_item()["item_id"]]
        ).get(self.first_item()["item_id"], {})


class EvidenceWindowTests(_Harness):
    """证据窗口的边界与引用校验的等价性。"""

    def test_window_is_bounded_and_anchored(self):
        """一道跨页大题：页窗再长，证据条数与页数都有明确上界。"""
        pages = ("一、请阅读下列材料并回答以下问题。",) + tuple(
            f"材料第 {index} 段正文。" for index in range(2, 12))
        self.seed_paper(pages=pages)
        item = self.first_item()
        self.assertEqual((item["start_page"], item["end_page"]), (1, 11),
                         "题单元应当跨页，才能测出页窗上界")
        evidence, reason = air.build_assessment_evidence(self.db_path, item)
        self.assertEqual(reason, "")
        self.assertLessEqual(len(evidence), air.MAX_EVIDENCE_ENTRIES)
        self.assertLessEqual(sum(len(entry["text"]) for entry in evidence), air.MAX_EVIDENCE_TOTAL_CHARS)
        pages_used = [entry["locator"]["page"] for entry in evidence if entry["kind"] == "document_page"]
        self.assertEqual(pages_used, [1, 2, 3, 4], "页窗最多取 MAX_EVIDENCE_PAGES 页")
        for entry in evidence:
            self.assertEqual(
                entry["source_hash"], hashlib.sha256(entry["text"].encode("utf-8")).hexdigest())
            self.assertTrue(air._entry_has_jumpable_anchor(entry))

    def test_question_only_evidence_is_not_enough(self):
        """只剩「题目本身」等于没有材料可依据：不许拿题干自证。"""
        self.seed_paper()
        item = self.first_item()
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("DELETE FROM learning_document_pages WHERE document_id=?", (DOCUMENT_ID,))
        evidence, reason = air.build_assessment_evidence(self.db_path, item)
        self.assertEqual(evidence, [])
        self.assertEqual(reason, "assessment_evidence_unavailable")

    def test_transcript_window_becomes_citable_evidence(self):
        self.seed_paper(pages=(QUESTION,), scope="lecture")
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                "INSERT INTO transcript_sources(sub_id,source_path,source_mtime_ns,source_size,"
                "segment_count,updated_at) VALUES(?,?,?,?,?,?)",
                (SUB_A, "synthetic.srt", 1, 10, 1, time.time()),
            )
            db.execute(
                "INSERT INTO transcript_segments(sub_id,segment_index,start_ms,end_ms,text,evidence_json)"
                " VALUES(?,?,?,?,?,?)",
                (SUB_A, 1, 60_000, 90_000, "老师在这里讲了尺度函数的构造方法。", ""),
            )
        item = self.first_item()
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                "UPDATE assessment_items SET evidence_refs_json=? WHERE item_id=?",
                (json.dumps([{"kind": "transcript", "start_ms": 60_000, "end_ms": 90_000,
                              "source_id": SUB_A}]), item["item_id"]),
            )
        item = self.first_item()
        evidence, _ = air.build_assessment_evidence(self.db_path, item)
        transcript = [entry for entry in evidence if entry["kind"] == "transcript"]
        self.assertEqual(len(transcript), 1)
        self.assertEqual(transcript[0]["locator"], {"start_ms": 60_000, "end_ms": 90_000})
        self.assertIn("尺度函数", transcript[0]["text"])

    def test_validator_agrees_with_the_shared_bookmark_validator(self):
        """同一份字幕形状的证据下，两套引用校验判定必须逐条一致。

        唯一允许的差异是归属校验：共享版要求每条证据都有讲次，课程级真题按设计
        没有讲次（document_alignment.py 把 scope=course 的 sub_id 置空），照抄会恒拒答。
        """
        self.seed_paper(pages=(QUESTION,), scope="lecture")
        item = self.first_item()
        evidence, _ = air.build_assessment_evidence(self.db_path, item)
        lecture_evidence = [
            {"citation_id": "e-1", "kind": "transcript", "course_id": COURSE, "sub_id": SUB_A,
             "start_ms": 0, "end_ms": 30_000, "text": "重点是尺度函数",
             "locator": {"start_ms": 0, "end_ms": 30_000},
             "source_hash": hashlib.sha256("重点是尺度函数".encode()).hexdigest()},
        ]
        cases = [
            ({"answer": "先写定义式。", "grounded": True, "citations": ["e-1"]}, True),
            ({"answer": "先写定义式。", "grounded": True, "citations": ["e-9"]}, False),
            ({"answer": "先写定义式。", "grounded": False, "citations": ["e-1"]}, False),
            ({"answer": "   ", "grounded": True, "citations": ["e-1"]}, False),
            ({"answer": "先写定义式。", "grounded": True, "citations": ["e-1", "e-1"]}, True),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                shared = validate_grounded_answer(payload, lecture_evidence)
                local = air.validate_assessment_answer(payload, lecture_evidence)
                self.assertEqual(bool(shared["grounded"]), expected)
                self.assertEqual(
                    bool(local["grounded"]), bool(shared["grounded"]),
                    "字幕形状证据下两套校验判定必须一致",
                )
        # 归属差异只在「有文档页锚、没有讲次」的课程级证据上生效。
        course_level = [{"citation_id": "c-1", "kind": "document_page", "course_id": COURSE,
                         "sub_id": "", "text": "第1题 求尺度函数", "locator": {"page": 3},
                         "source_hash": hashlib.sha256("第1题 求尺度函数".encode()).hexdigest()}]
        grounded = {"answer": "先写定义式。", "grounded": True, "citations": ["c-1"]}
        self.assertFalse(validate_grounded_answer(grounded, course_level)["grounded"])
        self.assertTrue(air.validate_assessment_answer(grounded, course_level)["grounded"])
        self.assertTrue(evidence)


class LocalPracticeViewTests(_Harness):
    """课程级「本课程练习」：有题/空题/作答统计，且答案提交前不下发。"""

    def _quiz(self, quiz_id: str, sub_id: str = SUB_A, start_ms: int = 60_000) -> dict:
        text = f"第 {quiz_id} 段被引原文"
        return {
            "quiz_id": quiz_id, "course_id": COURSE, "sub_id": sub_id,
            "question_type": "short_answer", "question": f"用自己的话复述 {quiz_id} 的要点。",
            "answer": text, "explanation": "提交后显示。", "difficulty": "medium",
            "evidence": {"start_ms": start_ms, "end_ms": start_ms + 20_000,
                         "text": text, "text_hash": hashlib.sha256(text.encode()).hexdigest(),
                         "prompt_version": "quiz-v2"},
        }

    def test_empty_practice_offers_one_action_and_never_pretends_to_be_a_paper(self):
        view = local_practice_view(self.db_path, course_id=COURSE, empty_sub_id=SUB_A)
        self.assertEqual(view["view"], "generated_quiz")
        self.assertEqual(view["counts"]["total"], 0)
        self.assertEqual(view["items"], [])
        self.assertFalse(view["answer_visible_before_submit"])
        self.assertEqual(view["empty_action"]["action"], "open_quiz_entry")
        self.assertEqual(view["empty_action"]["sub_id"], SUB_A)
        self.assertTrue(view["empty_action"]["label"])
        self.assertNotIn("answer", view, "空题库里不该有任何答案面字段")
        self.assertNotIn("answer", view["items"])

    def test_items_are_counted_grouped_and_never_carry_answers(self):
        save_quiz_items(self.db_path, [self._quiz("q1"), self._quiz("q2", start_ms=90_000)])
        view = local_practice_view(
            self.db_path, course_id=COURSE, lecture_labels={SUB_A: "第一讲"}, empty_sub_id=SUB_A)
        self.assertEqual(view["counts"]["total"], 2)
        self.assertEqual(view["counts"]["unanswered"], 2)
        self.assertEqual(view["counts"]["lectures"], 1)
        self.assertEqual(view["lectures"][0]["label"], "第一讲")
        self.assertEqual(view["lectures"][0]["count"], 2)
        self.assertEqual(view["source_label"], LOCAL_PRACTICE_SOURCE_LABEL)
        self.assertNotIn("empty_action", view)
        for item in view["items"]:
            self.assertNotIn("answer", item)
            self.assertNotIn("explanation", item)
            self.assertNotIn("answer_source", item)
            self.assertTrue(item["question"])

    def test_wrong_answers_sort_first_using_existing_attempts_only(self):
        save_quiz_items(self.db_path, [self._quiz("q1"), self._quiz("q2", start_ms=90_000)])
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                "INSERT INTO quiz_attempts(attempt_id,quiz_id,answer,correct,confidence,created_at)"
                " VALUES('a1','q2','我的复述',0,3,1.0)")
            db.execute(
                "INSERT INTO quiz_attempts(attempt_id,quiz_id,answer,correct,confidence,created_at)"
                " VALUES('a2','q1','我的复述',1,3,2.0)")
        view = local_practice_view(self.db_path, course_id=COURSE)
        self.assertEqual([item["quiz_id"] for item in view["items"]], ["q2", "q1"])
        self.assertEqual(view["counts"]["answered"], 2)
        self.assertEqual(view["counts"]["wrong"], 1)
        self.assertTrue(view["items"][0]["wrong"])
        self.assertFalse(view["items"][1]["wrong"])
        # 统计只用于排序：没有掌握百分比、没有间隔重复字段。
        rendered = json.dumps(view, ensure_ascii=False)
        for forbidden in ("mastery", "mastered", "interval", "next_review", "retention"):
            self.assertNotIn(forbidden, rendered)

    def test_practice_group_rides_in_the_course_level_workspace(self):
        """课程级工作台必须带上这个分组，且它不进 AssessmentItem 的 items。"""
        self.seed_paper(scope="lecture")
        save_quiz_items(self.db_path, [self._quiz("q1")])
        self.seed_exam_question_rows()
        from src.runtime.course_knowledge import save_course_knowledge
        save_course_knowledge(self.store, course_id=COURSE, sub_ids=[SUB_A])

        payload = self.app.course_review(COURSE)
        workspace = payload["assessment_workspace"]
        self.assertIsNotNone(workspace)
        practice = workspace["local_practice"]
        self.assertEqual(practice["view"], "generated_quiz")
        self.assertEqual(practice["counts"]["total"], 1)
        self.assertEqual(practice["counts"]["lectures"], 1)
        self.assertEqual(
            [item["item_id"] for item in workspace["items"]],
            [self.first_item()["item_id"]],
            "本地练习不得混进作业/真题的引用视图",
        )
        self.assertEqual(
            [item.get("quiz_id") for item in workspace["local_practice"]["items"]],
            ["q1"],
            "本课程练习只在自己那一组里",
        )


class QuestionUsageLedgerTests(_Harness):
    """RR-PARK-1 P2：解释/解答链的 worker 消耗必须落任务行账（AS6）。

    月账 deepseek_tokens 恒 0 的客户端断点：question 队列的 import_result
    从不调 _record_task_usage，worker 上报的 metrics 直接丢弃。
    """

    def test_assessment_answer_import_records_usage(self) -> None:
        self.seed_paper()
        item = self.first_item()
        evidence, _ = air.build_assessment_evidence(self.db_path, item)
        citations = [evidence[1]["citation_id"]]

        class _PayingCoordinator:
            def execute(self, *, task_id, build_job, import_result,
                        cancel_requested=None, progress=None):
                build_job("synthetic-public-key")
                result = {
                    "outputs": {"answer": {
                        "answer": "先写定义式，再逐项积分得到结论。",
                        "grounded": True,
                        "citations": list(citations),
                    }},
                    "metrics": {"elapsed_seconds": 12.0, "deepseek_tokens": 4321},
                }
                import_result(result)
                return result

        self.coordinator = _PayingCoordinator()
        receipt = self.app.explain_assessment_item(
            COURSE, item["item_id"], content_hash=item["content_hash"])
        self.assertEqual(receipt["status"], "queued")
        self.assertTrue(receipt["created"])
        self.drain_question_queue()

        task = self.tasks.get_task(str(receipt["task_id"]))
        self.assertIsNotNone(task)
        self.assertEqual(task["state"], "completed")
        self.assertEqual(task["deepseek_tokens"], 4321)
        self.assertEqual(task["runner_seconds"], 12.0)
        totals = self.tasks.usage_totals_since(0.0)
        self.assertEqual(totals["deepseek_tokens"], 4321)


if __name__ == "__main__":
    unittest.main()
