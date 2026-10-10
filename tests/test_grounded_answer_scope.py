"""Grounded answers must honor the optional lecture (sub_id) scope.

The /api/v3/search/answer route passes sub_id while the application pipeline
previously dropped it, so every scoped request failed into
grounded_answer_unavailable. These tests exercise the real answer pipeline
(search_learning -> LearningSearchIndex.search -> LearningStore.search_documents
-> evidence_answer) over synthetic stores, plus the recorded-operation retry
path and the live HTTP route.
"""

from __future__ import annotations

import inspect
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.application import CourseLensApplication
from src.runtime.http_api import make_handler
from src.runtime.learning_store import LearningStore
from src.runtime.search_index import LearningSearchIndex
from src.runtime.task_store import TaskStore
from tests.http_services import http_services


class _AnswerApplication:
    """Narrow adapter reusing the real answer pipeline over synthetic stores."""

    search_learning = CourseLensApplication.search_learning
    answer_from_evidence = CourseLensApplication.answer_from_evidence
    _answer_from_evidence_impl = CourseLensApplication._answer_from_evidence_impl
    _recorded_operation = CourseLensApplication._recorded_operation
    _retry_recorded_operation = CourseLensApplication._retry_recorded_operation
    # WIRING-FIX-1：深度链分派面（declined 判定在 key 检查之前，空证据
    # 即时降级不触任何下游依赖）。
    _deep_answer_dispatch = CourseLensApplication._deep_answer_dispatch
    # D-14-RELAX：分派面的三阶检索阶梯同为真实管线一部分，别名同步。
    _deep_search_with_relaxation = CourseLensApplication._deep_search_with_relaxation

    def __init__(self, root: Path):
        self.learning_store = LearningStore(root / "learning.db")
        self.search_index = LearningSearchIndex(
            self.learning_store,
            catalog_snapshot=lambda: {},
            subtitle_sync=lambda _sub_id: {},
        )
        self.task_store = TaskStore(root / "tasks.db")
        self._deepseek_key_value = ""

    def _deepseek_key(self) -> str:
        return self._deepseek_key_value

    def start_search_index(self) -> None:
        return None

    def authentication_snapshot(self) -> dict:
        return {"state": "ready"}

    def seed_lecture(self, sub_id: str, course_id: str, keyword: str) -> None:
        # prune=False: 每次同步只 upsert 本讲次行，不清掉其他讲次的目录行
        self.learning_store.sync_search_catalog([{
            "sub_id": sub_id,
            "course_id": course_id,
            "course_title": f"course-{course_id}",
            "lecture_title": f"lecture-{sub_id}",
            "teacher": "teacher",
            "catalog_version": "v1",
        }], prune=False)
        self.learning_store.replace_search_documents(
            sub_id,
            [{
                "doc_key": f"{sub_id}:transcript:15000",
                "source": "transcript",
                "source_ref": "",
                "document_title": f"lecture-{sub_id}",
                "start_ms": 15000,
                "display_text": f"the {keyword} rule explains the second law",
                "search_text": f"the {keyword} rule explains the second law",
                "source_version": "v1",
            }],
            indexed_version="v1",
        )


class GroundedAnswerScopeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = _AnswerApplication(Path(tmp.name))
        self.service.seed_lecture("sub-a", "c1", "gradient")
        self.service.seed_lecture("sub-b", "c1", "entropy")

    def test_service_signature_accepts_optional_sub_id(self):
        for method in (
            CourseLensApplication.answer_from_evidence,
            CourseLensApplication._answer_from_evidence_impl,
        ):
            parameters = inspect.signature(method).parameters
            self.assertIn("sub_id", parameters)
            self.assertEqual(parameters["sub_id"].default, "")
        # P3-CONTRACT-1：mode 为加性 keyword-only 参，缺省=既有本地拼装逐字不变。
        mode_parameter = inspect.signature(CourseLensApplication.answer_from_evidence).parameters["mode"]
        self.assertEqual(mode_parameter.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(mode_parameter.default, "")

    def test_scoped_answer_cites_only_requested_lecture(self):
        # "rule" 命中两个讲次：限定 sub-a 后引用必须只来自 sub-a
        answer = self.service.answer_from_evidence("rule", sub_id=" sub-a ")
        self.assertTrue(answer["grounded"])
        self.assertTrue(answer["citations"])
        self.assertEqual({item["sub_id"] for item in answer["citations"]}, {"sub-a"})

    def test_unscoped_answer_still_spans_lectures(self):
        answer = self.service.answer_from_evidence("rule")
        self.assertTrue(answer["grounded"])
        self.assertEqual(
            {item["sub_id"] for item in answer["citations"]}, {"sub-a", "sub-b"}
        )

    def test_unknown_lecture_scope_returns_refusal(self):
        answer = self.service.answer_from_evidence("gradient", sub_id="sub-missing")
        self.assertFalse(answer["grounded"])
        self.assertEqual(answer["citations"], [])

    def test_course_filter_still_applies_alongside_sub_id(self):
        self.service.seed_lecture("sub-c", "c2", "gradient")
        answer = self.service.answer_from_evidence(
            "gradient", course_ids=["c2"], sub_id="sub-c"
        )
        self.assertTrue(answer["grounded"])
        self.assertEqual(
            {item["course_id"] for item in answer["citations"]}, {"c2"}
        )

    def test_scoped_and_unscoped_are_distinct_recorded_operations(self):
        self.service.answer_from_evidence("gradient")
        self.service.answer_from_evidence("gradient", sub_id="sub-a")
        tasks = [
            task for task in self.service.task_store.list_tasks(kinds=("search_answer",))
        ]
        self.assertEqual(len(tasks), 2)
        self.assertEqual(sorted(str(task["sub_id"]) for task in tasks), ["", "sub-a"])

    def test_recorded_payload_omits_empty_sub_id(self):
        self.service.answer_from_evidence("gradient")
        task = self.service.task_store.list_tasks(kinds=("search_answer",))[0]
        self.assertNotIn("sub_id", task["payload"])

    def test_recorded_retry_payload_honors_scope(self):
        self.service.answer_from_evidence("gradient", sub_id="sub-a")
        task = self.service.task_store.list_tasks(kinds=("search_answer",))[0]
        self.assertEqual(task["payload"]["sub_id"], "sub-a")
        received = {}
        original = self.service._answer_from_evidence_impl

        def spy(query, *, course_ids=None, sub_id=""):
            received["query"] = str(query)
            received["sub_id"] = str(sub_id)
            return original(query, course_ids=course_ids, sub_id=sub_id)

        self.service._answer_from_evidence_impl = spy
        terminal = self.service._retry_recorded_operation(task)
        self.assertEqual(received, {"query": "gradient", "sub_id": "sub-a"})
        self.assertEqual(str((terminal or {}).get("state")), "completed")


class SearchAnswerModeWiringTests(unittest.TestCase):
    """WIRING-FIX-1（BROWSERWALK-3 F1-P1-2）：路由透传 mode 到服务层。

    深度链服务层（mode 闭集+declined 判定+任务链）此前经 HTTP 不可达——
    路由从不读取 body.mode，带 mode 的 POST 一律返回快速形状。钉三面：
    deep 透传（declined 端到端 202）、key 缺失专码（书签链同款闭集码）、
    无效 mode 落快速形状码。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.service = _AnswerApplication(root)
        self.service.seed_lecture("sub-a", "c1", "gradient")
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(http_services(self.service), root),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.base = f"http://127.0.0.1:{server.server_port}"

    def _post(self, body):
        request = Request(
            f"{self.base}/api/v3/search/answer",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def test_deep_mode_reaches_service_and_declines_without_evidence(self):
        # 端到端透传证明：无证据 query + mode=deep → 服务层 declined 形状
        # （零任务零计费零 LLM）原样经 202 envelope 回前端。
        status, payload = self._post({
            "query": "quantum chromodynamics nonsense", "mode": "deep",
        })
        self.assertEqual(status, 202)
        value = payload["data"]
        self.assertEqual(value["mode"], "declined")
        self.assertFalse(value["grounded"])
        self.assertEqual(value["citations"], [])
        self.assertEqual(value["error_code"], "deep_answer_evidence_unavailable")

    def test_deep_mode_without_key_maps_to_closed_code(self):
        # 证据命中但 key 缺失 → 书签链同款闭集码（前端人话文案键同笔等集）。
        status, payload = self._post({"query": "gradient", "mode": "deep"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "question_explanation_not_configured")
        self.assertTrue(payload.get("retriable"))

    def test_invalid_mode_falls_back_to_grounded_answer_unavailable(self):
        # mode 闭集 {"", "deep"}：脏值经服务层 ValueError 落快速形状码，
        # 绝不误导为 key 文案。
        status, payload = self._post({"query": "gradient", "mode": "hyper"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "grounded_answer_unavailable")

    def test_search_answer_route_accepts_sub_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = _AnswerApplication(root)
            service.seed_lecture("sub-a", "c1", "gradient")
            service.seed_lecture("sub-b", "c1", "entropy")
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), make_handler(http_services(service), root)
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                request = Request(
                    f"{base}/api/v3/search/answer",
                    data=json.dumps({"query": "gradient", "sub_id": "sub-a"}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request) as response:
                    self.assertEqual(response.status, 202)
                    value = json.loads(response.read())["data"]
                self.assertTrue(value["grounded"])
                self.assertEqual(
                    {item["sub_id"] for item in value["citations"]}, {"sub-a"}
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
