"""DEFECT-2 根修钉：deep-QA 答案落库 + GET search/answer 读出契约。

修前形态（product-backenddeath1-result-20261010 + D13-REPLAY 检查点 2-R 定谳）：
``student_features.save_search_answer`` 全树不存在 → application.py getattr 恒
None → 每次 deep-QA 答案（学生真实付费 LLM 调用）静默不落库；GET
search/answer 路由不存在 → 404 无码 → 前端 search-palette.js:652 只能走
「读出面未就绪」降级。

修后判据（三族钉）：
1. 存储面：save/load 全字段往返（引用含 start_seconds 原样往返，前端
   citationSeekTarget 可跳回原位）；task_id 主键 upsert 幂等单行；state 闭集
   ready|insufficient|failed（闭集外 ValueError）；重启（重开库重入）后行仍在。
2. 端到端（BACKEND-DEATH-1 建议的验收钉）：任务 completed 而 search_answers
   无行=红——真实 save（零 mock）走完问题队列，completed 任务必有一行 ready
   记录；终态失败必有一行 failed+error_code（学生可复看失败原因）。
3. GET 契约（形状照 search-palette.js:601-662 消费面）：200 envelope
   {task:{state,error_code,label…}, answer:{state,answer,citations…}}；
   未知任务=404+task_task_unknown（前端按码出「任务不在了」人话，路由就绪后
   裸 404 降级窗口关闭）；缺 task_id=400 闭集码；任务在而记录未落=200+
   answer:null（前端如实呈「结果记录还没就绪」，绝不伪造答案）。

全部合成夹具：零真实账号、零外呼、零付费调用。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import urlopen

from src.application import CourseLensApplication
from src.runtime.http_api import make_handler
from src.runtime.learning_store import LearningStore
from src.runtime.search_index import LearningSearchIndex
from src.runtime.student_features import (
    SEARCH_ANSWER_STATES,
    ensure_student_feature_schema,
    load_search_answer,
    save_search_answer,
)
from src.runtime.task_store import TaskStore
from tests.http_services import http_services

COURSE = "crs-sa-store"
SUB_A = "sub-sa-store-1"

_CITATION = {
    "citation_id": "c1",
    "course_id": COURSE,
    "sub_id": SUB_A,
    "start_seconds": 15.0,
    "end_seconds": 21.0,
    "snippet": "链式聚合的收敛性由压缩映射原理保证。",
    "source_hash": "a" * 64,
    "source": "transcript",
    "label": "第一讲 · 字幕",
}


class _FakeCoordinator:
    """替掉远端协调器：按次回放合成结果或抛合成错误，零网络零付费。"""

    def __init__(self, outputs=None, *, metrics=None, failures=()):
        self.outputs = dict(outputs or {})
        self.metrics = dict(metrics or {})
        self.failures = list(failures)

    def execute(self, *, task_id, build_job, import_result, cancel_requested=None, progress=None):
        build_job("synthetic-public-key")
        if self.failures:
            raise self.failures.pop(0)
        result = {"outputs": dict(self.outputs), "metrics": dict(self.metrics)}
        import_result(result)
        return result


class _RealStoreHarness(unittest.TestCase):
    """合成应用壳：真实 save_search_answer（零 mock），远端协调器用替身。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = LearningStore(self.root / "learning.db")
        self.tasks = TaskStore(self.root / "tasks.db")
        self.app = CourseLensApplication.__new__(CourseLensApplication)
        self.app.learning_store = self.store
        self.app.task_store = self.tasks
        self.app.output_dir = self.root
        self.app.search_index = LearningSearchIndex(
            self.store, catalog_snapshot=lambda: {}, subtitle_sync=lambda _sub_id: {},
        )
        self.app.start_search_index = lambda: None
        self.app._lock = threading.RLock()
        self.app._paused = False
        self.app._question_queue = []
        self.app._question_current = None
        self.app._question_cancel = threading.Event()
        self.app._generation_workers_stop = threading.Event()
        self.app._active_question_task_id = ""
        self.app._cloud_run_condition = threading.Condition()
        self.app._cloud_runs_active = 0
        self.app._deepseek_api_key = "sk-synthetic"
        self.coordinator = _FakeCoordinator()

        patchers = [
            mock.patch.object(CourseLensApplication, "_ensure_question_worker", lambda self: None),
            mock.patch.object(
                CourseLensApplication, "_leased_remote_coordinator", self._fake_coordinator_lease,
            ),
            mock.patch.object(CourseLensApplication, "_cloud_run_slot", self._no_cloud_slot),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.store.close)

    @contextlib.contextmanager
    def _fake_coordinator_lease(self, _task_id: str, *, workflow: str | None = None):
        yield self.coordinator

    @contextlib.contextmanager
    def _no_cloud_slot(self, _task_id: str, *, cancel_requested=None, on_wait=None):
        yield

    def enqueue_palette_task(self) -> dict:
        text = "链式聚合的收敛性由压缩映射原理保证。"
        payload = {
            "deep_answer": True,
            "query": "链式聚合为什么收敛？",
            "evidence": [{
                "citation_id": "c1",
                "course_id": COURSE,
                "sub_id": SUB_A,
                "start_ms": 15000,
                "end_ms": 21000,
                "source_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "text": text,
                "source": "transcript",
                "label": "第一讲 · 字幕",
            }],
            "input_hash": "a" * 64,
            "prompt_version": "bookmark-answer-v1",
            "cancel_requested": False,
        }
        task, _created = self.tasks.add_task(
            "question", COURSE, SUB_A, payload, config_key="palette:" + payload["input_hash"],
        )
        # 生产分派（_deep_answer_dispatch）在入队时写 v3 metadata；夹具对齐
        # （与 test_p3_deep_mode 同款，否则终态分支 upsert_v3_metadata 缺键）。
        self.tasks.upsert_v3_metadata(
            task["task_id"], dedupe_key=f"question:palette:{payload['input_hash']}",
            stage="queued", privacy_state="sealed", requested_outputs=["answer"],
            input_hash=payload["input_hash"],
        )
        with self.app._lock:
            self.app._queue_persisted_task(task)
        return task

    def drain_question_queue(self) -> None:
        self.app._run_question_queue()


class SearchAnswerStoreTests(unittest.TestCase):
    """存储面钉：往返、upsert 幂等、闭集、重启持久。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / "learning.db")

    def test_save_load_roundtrip_keeps_citation_jump_fields(self):
        record = save_search_answer(
            self.path, task_id="t1", state="ready", input_hash="h" * 64,
            query="链式聚合为什么收敛？", course_ids=[COURSE], sub_id=SUB_A,
            answer="收敛性由压缩映射原理保证。", citations=[dict(_CITATION)],
            grounded=True, model="deepseek-flash", prompt_version="bookmark-answer-v1",
        )
        self.assertEqual(record["state"], "ready")
        loaded = load_search_answer(self.path, task_id="t1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["answer"], "收敛性由压缩映射原理保证。")
        self.assertEqual(loaded["course_ids"], [COURSE])
        self.assertEqual(loaded["sub_id"], SUB_A)
        self.assertTrue(loaded["grounded"])
        self.assertEqual(loaded["model"], "deepseek-flash")
        self.assertEqual(loaded["prompt_version"], "bookmark-answer-v1")
        self.assertEqual(loaded["error_code"], "")
        citation = loaded["citations"][0]
        # 前端 citationSeekTarget 契约：course_id/sub_id/start_seconds + 标签字段
        # 原样往返，引用 chip 才能跳回原位（绝不猜测课程/讲次/时间）。
        self.assertEqual(citation["course_id"], COURSE)
        self.assertEqual(citation["sub_id"], SUB_A)
        self.assertEqual(citation["start_seconds"], 15.0)
        self.assertEqual(citation["source"], "transcript")
        self.assertEqual(citation["label"], "第一讲 · 字幕")
        self.assertEqual(citation["source_hash"], "a" * 64)

    def test_upsert_keeps_single_row_per_task(self):
        save_search_answer(self.path, task_id="t1", state="ready", answer="v1")
        record = save_search_answer(self.path, task_id="t1", state="failed", error_code="deepseek_rate_limited")
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["error_code"], "deepseek_rate_limited")
        import sqlite3

        with sqlite3.connect(self.path) as db:
            count = db.execute("SELECT COUNT(*) FROM search_answers WHERE task_id='t1'").fetchone()[0]
        self.assertEqual(count, 1, "task_id 主键：一任务一行，重写=更新而非追加")

    def test_state_closed_set_and_task_id_required(self):
        for state in ("", "running", "READY", "canceled", "done"):
            with self.assertRaises(ValueError, msg=f"闭集外 state={state!r} 必须拒绝"):
                save_search_answer(self.path, task_id="t2", state=state)
        with self.assertRaises(ValueError):
            save_search_answer(self.path, task_id="", state="ready")
        self.assertEqual(SEARCH_ANSWER_STATES, ("ready", "insufficient", "failed"))

    def test_row_survives_reopen_restart(self):
        save_search_answer(
            self.path, task_id="t3", state="ready", answer="重启后仍可复看。",
            citations=[dict(_CITATION)], grounded=True,
        )
        # 模拟客户端重启：全部新连接重入（schema 幂等 + 读出）。
        ensure_student_feature_schema(self.path)
        loaded = load_search_answer(self.path, task_id="t3")
        self.assertEqual(loaded["answer"], "重启后仍可复看。")
        self.assertEqual(loaded["citations"][0]["start_seconds"], 15.0)

    def test_load_unknown_or_empty_returns_none(self):
        self.assertIsNone(load_search_answer(self.path, task_id="missing"))
        self.assertIsNone(load_search_answer(self.path, task_id=""))


class DeepAnswerLandsSearchAnswersTests(_RealStoreHarness):
    """端到端验收钉（BACKEND-DEATH-1 建议）：任务终态与 search_answers 行一致。"""

    def test_completed_task_must_land_ready_row(self):
        task = self.enqueue_palette_task()
        self.coordinator.outputs = {
            "answer": {"answer": "收敛性由压缩映射原理保证。", "grounded": True, "citations": ["c1"]},
        }
        self.coordinator.metrics = {"deepseek_tokens": 120, "elapsed_seconds": 3.0}
        self.drain_question_queue()
        final = self.tasks.get_task(task["task_id"]) or {}
        self.assertEqual(final.get("state"), "completed")
        record = load_search_answer(self.store.path, task_id=task["task_id"])
        # 修前红钉：任务 completed 而 search_answers 无行=红（付费答案静默丢失）。
        self.assertIsNotNone(record, "任务 completed 而 search_answers 无行=红")
        self.assertEqual(record["state"], "ready")
        self.assertTrue(record["grounded"])
        self.assertEqual(record["model"], "deepseek-flash")
        self.assertEqual(record["citations"][0]["start_seconds"], 15.0, "引用可跳回原位")

    def test_terminal_failure_lands_failed_row_for_review(self):
        task = self.enqueue_palette_task()
        self.coordinator.failures = [RuntimeError("DeepSeek 429 rate limited")]
        self.drain_question_queue()
        final = self.tasks.get_task(task["task_id"]) or {}
        self.assertEqual(final.get("state"), "failed")
        record = load_search_answer(self.store.path, task_id=task["task_id"])
        self.assertIsNotNone(record, "终态失败也落行：学生可复看失败原因")
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["error_code"], "deepseek_rate_limited")
        self.assertEqual(record["answer"], "")

    def test_insufficient_answer_lands_insufficient_row(self):
        task = self.enqueue_palette_task()
        self.coordinator.outputs = {
            "answer": {"answer": "资料不足，无法根据当前课程资料回答。", "grounded": False, "citations": []},
        }
        self.drain_question_queue()
        final = self.tasks.get_task(task["task_id"]) or {}
        self.assertEqual(final.get("state"), "completed", "降级答案也是成功导入（宁缺勿假）")
        record = load_search_answer(self.store.path, task_id=task["task_id"])
        self.assertIsNotNone(record)
        self.assertEqual(record["state"], "insufficient")
        self.assertFalse(record["grounded"])


class _StubSearchIndex:
    def search(self, query, **kwargs):
        return {"schema": "courselens.search-result.v1", "hits": [], "total": 0}

    def status(self):
        return {}


class _ReadoutApp:
    """最小装配：真实 TaskStore + 真实 learning.db，供 GET 读出契约钉。"""

    def __init__(self, root: Path):
        self.learning_store = LearningStore(root / "learning.db")
        self.task_store = TaskStore(root / "tasks.db")
        self.search_index = _StubSearchIndex()

    def start_search_index(self) -> None:
        return None

    def authentication_snapshot(self) -> dict:
        return {"state": "ready"}


class SearchAnswerReadoutRouteTests(unittest.TestCase):
    """GET /api/v3/search/answer 契约钉（形状=前端 search-palette.js 消费面）。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.app = _ReadoutApp(root)
        task, _created = self.app.task_store.add_task(
            "question", COURSE, SUB_A, {"deep_answer": True, "query": "q"}, config_key="palette:x",
        )
        self.task_id = task["task_id"]
        self.pending_task_id = self.app.task_store.add_task(
            "question", COURSE, SUB_A, {"deep_answer": True, "query": "q2"}, config_key="palette:y",
        )[0]["task_id"]
        save_search_answer(
            self.app.learning_store.path, task_id=self.task_id, state="ready",
            input_hash="h" * 64, query="q", course_ids=[COURSE], sub_id=SUB_A,
            answer="收敛性由压缩映射原理保证。", citations=[dict(_CITATION)],
            grounded=True, model="deepseek-flash", prompt_version="bookmark-answer-v1",
        )
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(http_services(self.app), root),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.app.learning_store.close)
        self.base = f"http://127.0.0.1:{server.server_port}"

    def _get(self, query: str):
        url = f"{self.base}/api/v3/search/answer{query}"
        try:
            with urlopen(url) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def test_get_returns_task_and_answer_with_jumpable_citation(self):
        status, payload = self._get(f"?task_id={self.task_id}")
        self.assertEqual(status, 200)
        data = payload["data"]
        self.assertEqual(data["task"]["task_id"], self.task_id)
        self.assertEqual(data["task"]["state"], "queued")
        answer = data["answer"]
        self.assertIsNotNone(answer)
        self.assertEqual(answer["state"], "ready")
        self.assertEqual(answer["answer"], "收敛性由压缩映射原理保证。")
        citation = answer["citations"][0]
        self.assertEqual(citation["start_seconds"], 15.0)
        self.assertEqual(citation["sub_id"], SUB_A)
        self.assertEqual(citation["label"], "第一讲 · 字幕")

    def test_unknown_task_maps_to_404_with_closed_code(self):
        status, payload = self._get("?task_id=does-not-exist")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error_code"], "task_task_unknown")
        # 路由就绪后 404 必带码：前端 644 行按码出「任务不在了」人话，
        # 652 行「读出路由未就绪」裸 404 降级窗口就此关闭。

    def test_missing_task_id_maps_to_400(self):
        status, payload = self._get("")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "search_answer_task_required")

    def test_task_without_record_returns_null_answer(self):
        status, payload = self._get(f"?task_id={self.pending_task_id}")
        self.assertEqual(status, 200)
        self.assertIsNotNone(payload["data"]["task"])
        self.assertIsNone(payload["data"]["answer"], "任务在而记录未落=answer:null，绝不伪造答案")


if __name__ == "__main__":
    unittest.main()
