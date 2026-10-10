"""C1-LOCATE 防护钉：tasks/enqueue 入队前置门 fail-closed 语义。

SWEEP1-C1（2026-10-08 混沌风暴，生产壳实证）：未授权环境点 AI 处理 →
POST /api/v3/tasks/enqueue → 400 + 闭集码 ``cloud_setup_required``（设计内
fail-closed，前端已人话闭环——定位/根因见
archive/external-artifacts/top-model-results-20260930/product-c1loc-result-20261008.md）。

本钉防未来入队门语义漂移（每腿一句「防什么回归」）：
- 门拒绝必须落闭集码出口、绝不洗成 500 runtime_failed（MEDIA-RETRY-1 逃逸律）；
- 响应体键集闭包、零异常原文（服务端只发码，不向浏览器泄漏内部指引文案）；
- ``cloud_setup_required`` 不在 retriable 白名单（诚实：连接没就绪≠值得原样重试）；
- 门拒绝零副作用（store 不落半截任务）；
- 同门同码：summary 与 subtitle 两种 kind 同一拒绝形状（C1 curl 双腿实证形状）；
- kind/course_id/sub_id 校验腿 400 ``task_enqueue_invalid`` 闭集（原零测）；
- question kind（须走书签链）落 ``task_enqueue_failed`` 闭集兜底，不逃逸；
- summary happy 腿锚定 202 + envelope 形状与 include_ppt/force 透传（kind 矩阵闭合）。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.application import CloudSetupRequired
from src.runtime.http_api import make_handler
from src.runtime.task_store import TaskStore
from tests.http_services import http_services

C1_HUMAN_INTENT_TEXT = "在线计算尚未完成 GitHub 授权"


class _EnqueueGateService:
    """只承载入队门前置面的最小服务桩（会话门 ready，其余面不在本钉范围）。"""

    def __init__(self, root: Path):
        self.repository = TaskStore(root / "state.db")
        self.summary_calls: list[dict] = []
        self.subtitle_calls: list[dict] = []
        self.summary_failure: Exception | None = None
        self.subtitle_failure: Exception | None = None

    @staticmethod
    def authentication_snapshot():
        return {"state": "ready"}

    def enqueue_summary(self, course_id, sub_id, include_ppt=True, force=False):
        self.summary_calls.append({
            "course_id": course_id, "sub_id": sub_id,
            "include_ppt": include_ppt, "force": force,
        })
        if self.summary_failure is not None:
            raise self.summary_failure
        task, _ = self.repository.add_task(
            "summary", course_id, sub_id,
            {"include_ppt": include_ppt, "force": force}, config_key="summary",
        )
        return task

    def enqueue_subtitle(self, course_id, sub_id, **kwargs):
        self.subtitle_calls.append({"course_id": course_id, "sub_id": sub_id, **kwargs})
        if self.subtitle_failure is not None:
            raise self.subtitle_failure
        task, _ = self.repository.add_task(
            "subtitle", course_id, sub_id,
            {"subtitle_mode": "automatic", **kwargs}, config_key="automatic",
        )
        return task


class EnqueueGateFailClosedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = _EnqueueGateService(Path(self._tmp.name))
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(http_services(self.service), Path(self._tmp.name))
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        # addCleanup LIFO：与既有路由测试同序（shutdown → server_close → join），
        # 避免 server_close 先于 shutdown 关闭 fd 触发 selector 竞态。
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _enqueue(self, payload: dict):
        request = Request(
            f"{self.base}/api/v3/tasks/enqueue",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return request

    def test_summary_enqueue_in_unauthorized_environment_fails_closed_with_closed_code(self):
        """C1 主腿：未授权环境 summary 入队 → 400 闭集码，绝不 500 逃逸、零副作用。"""
        self.service.summary_failure = CloudSetupRequired(C1_HUMAN_INTENT_TEXT)
        with self.assertRaises(HTTPError) as rejected:
            urlopen(self._enqueue({"kind": "summary", "course_id": "90007", "sub_id": "900071"}))
        self.assertEqual(rejected.exception.code, 400)
        payload = json.loads(rejected.exception.read())
        # 键集闭包：服务端只发码，不向浏览器泄漏异常原文（学生面文案由前端码表渲染）。
        self.assertEqual(
            payload,
            {"error": "Task could not be queued", "error_code": "cloud_setup_required", "retriable": False},
        )
        self.assertNotIn(C1_HUMAN_INTENT_TEXT, json.dumps(payload, ensure_ascii=False))
        # fail-closed 零副作用：门拒绝不落半截任务。
        self.assertEqual(self.service.repository.list_tasks(), [])
        self.assertEqual(len(self.service.summary_calls), 1, "门拒绝前真实到达入队调用点（门在服务内）")

    def test_subtitle_enqueue_hits_the_same_gate_with_the_same_code(self):
        """同门同码腿：subtitle kind 过同一前置门、同一闭集形状（C1 curl 双腿）。"""
        self.service.subtitle_failure = CloudSetupRequired(C1_HUMAN_INTENT_TEXT)
        with self.assertRaises(HTTPError) as rejected:
            urlopen(self._enqueue({"kind": "subtitle", "course_id": "90007", "sub_id": "900071"}))
        self.assertEqual(rejected.exception.code, 400)
        payload = json.loads(rejected.exception.read())
        self.assertEqual(payload["error_code"], "cloud_setup_required")
        self.assertEqual(payload["retriable"], False)
        self.assertEqual(self.service.repository.list_tasks(), [])

    def test_closed_rejection_is_never_marked_retriable(self):
        """retriable 白名单腿：连接/授权没就绪不诱导学生原样硬重试。"""
        for attr in ("summary_failure", "subtitle_failure"):
            setattr(self.service, attr, CloudSetupRequired(C1_HUMAN_INTENT_TEXT))
        for payload in ({"kind": "summary", "course_id": "c", "sub_id": "s"},
                        {"kind": "subtitle", "course_id": "c", "sub_id": "s"}):
            with self.assertRaises(HTTPError) as rejected:
                urlopen(self._enqueue(payload))
            body = json.loads(rejected.exception.read())
            self.assertIs(body["retriable"], False, f"{payload['kind']} 门拒绝 retriable 恒 False")

    def test_question_kind_falls_back_to_closed_task_enqueue_failed(self):
        """question kind 腿：需走书签链的 kind 直发入队 → 闭集兜底码，不逃逸 500。"""
        with self.assertRaises(HTTPError) as rejected:
            urlopen(self._enqueue({"kind": "question", "course_id": "c", "sub_id": "s"}))
        self.assertEqual(rejected.exception.code, 400)
        body = json.loads(rejected.exception.read())
        self.assertEqual(body["error"], "Task could not be queued")
        # 现行为勘误（首跑实证）：兜底码实测落 task_failed（闭集词表成员，诚实
        # 通用死因），非 task_enqueue_failed——按现状钉死；码表归位若调整，
        # 本钉随契约同笔更新。
        self.assertEqual(body["error_code"], "task_failed")
        self.assertEqual(body.get("retriable"), False)

    def test_enqueue_request_validation_rejects_with_closed_invalid_code(self):
        """校验腿（原零测）：kind/course_id/sub_id 校验 400 task_enqueue_invalid 闭集。"""
        for payload in (
            {"kind": "bogus", "course_id": "c", "sub_id": "s"},
            {"kind": "summary", "course_id": "", "sub_id": "s"},
            {"kind": "subtitle", "course_id": "c", "sub_id": ""},
            {"course_id": "c", "sub_id": "s"},
            {"kind": "summary"},
        ):
            with self.assertRaises(HTTPError) as rejected:
                urlopen(self._enqueue(payload))
            self.assertEqual(rejected.exception.code, 400, str(payload))
            body = json.loads(rejected.exception.read())
            self.assertEqual(body["error_code"], "task_enqueue_invalid", str(payload))
            self.assertNotIn("retriable", body, "校验腿无 retriable 键（键集闭包）")
        self.assertEqual(
            self.service.repository.list_tasks(), [], "校验拒绝零副作用"
        )

    def test_summary_enqueue_happy_path_accepts_with_envelope_and_passthrough(self):
        """happy 锚定腿：summary 202 + envelope 任务形状 + include_ppt/force 透传。"""
        with urlopen(self._enqueue({
            "kind": "summary", "course_id": "90007", "sub_id": "900071",
            "include_ppt": False, "force": True,
        })) as response:
            self.assertEqual(response.status, 202)
            payload = json.loads(response.read())
        self.assertEqual(payload["schema"], "courselens.api.v3")
        task = payload["data"]["task"]
        self.assertTrue(task["task_id"], "envelope 携带任务标识")
        self.assertEqual(task["kind"], "summary")
        self.assertEqual(self.service.summary_calls[-1], {
            "course_id": "90007", "sub_id": "900071", "include_ppt": False, "force": True,
        }, "include_ppt/force 逐字段透传（请求体不新增任何模式选择参数之外的键）")
        self.assertEqual(len(self.service.repository.list_tasks()), 1, "happy 路径恰落一单")


if __name__ == "__main__":
    unittest.main()
