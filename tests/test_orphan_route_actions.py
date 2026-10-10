"""B34（夜15-R4 D-1 路由映射表）：四条孤儿路由的 HTTP 路径级行为钉。

夜15 D-1 复查：documents/preview、search-index/actions、bookmarks/explain、
client-update/actions 在 py 测试树零字面引用（automation/imports 属 W1
活跃域按禁碰面跳过）。本件按 N15-R4 B34 设计卡补齐：每路由 HTTP 真往返
（ThreadingHTTPServer + make_handler + http_services 合成壳），错误闭集
码与信封/裸体形态按现状钉死。前三路由的 happy/负分支用录制型合成应用；
client-update 经 http_services 适配后覆写录制型 client_update 命名空间
（适配层自带的静态桩无法注入 complete_restart 录制）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.runtime.http_api import make_handler
from src.update import UpdateError
from tests.http_services import http_services


class _RecordingLearningApp:
    """四路由只触 learning 域合成件；零会话门外的服务依赖。"""

    def __init__(self, root: Path):
        self.preview_root = root / "preview"
        self.preview_root.mkdir(parents=True, exist_ok=True)
        self.preview_document = self.preview_root / "deck.txt"
        self.preview_document.write_bytes(b"%PDF-synthetic-preview-bytes")
        self.preview_error: Exception | None = None
        self.search_index_status = {"state": "idle", "documents": 0}
        self.refresh_calls = 0
        self.explain_calls: list[str] = []
        self.explain_result = {
            "bookmark": {"bookmark_id": "b1", "question": "什么是递归？"},
            "task": {"task_id": "task-1", "state": "queued", "payload": {"secret": "x"}},
            "created": True,
        }
        self.explain_error: Exception | None = None

    def start_search_index(self) -> None:
        return None

    def authentication_snapshot(self) -> dict:
        return {"state": "ready"}

    def learning_document_preview_path(self, document_id: str) -> Path:
        if self.preview_error is not None:
            raise self.preview_error
        return self.preview_document

    def refresh_search_index(self) -> None:
        self.refresh_calls += 1

    @property
    def search_index(self):
        return SimpleNamespace(status=lambda: dict(self.search_index_status))

    def explain_question_bookmark(self, bookmark_id: str) -> dict:
        self.explain_calls.append(bookmark_id)
        if self.explain_error is not None:
            raise self.explain_error
        return self.explain_result


class OrphanRouteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = _RecordingLearningApp(Path(tmp.name))
        services = http_services(self.app)
        self.update_calls: list[tuple[str, bool]] = []
        self.restart_calls = 0

        def update_action(action, confirmed=False):
            self.update_calls.append((str(action), bool(confirmed)))
            if self.update_error is not None:
                raise self.update_error
            return {"state": "downloading", "action": action}

        def complete_restart():
            self.restart_calls += 1

        self.update_error: UpdateError | None = None
        services.client_update = SimpleNamespace(
            action=update_action, complete_restart=complete_restart,
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(services, Path(tmp.name)),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _get(self, path):
        try:
            with urlopen(f"{self.base}{path}") as response:
                return response.status, response.read(), response.headers
        except HTTPError as exc:
            with exc:
                return exc.code, exc.read(), exc.headers

    def _post(self, path, body):
        request = Request(
            f"{self.base}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            with exc:
                raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return exc.code, raw

    # --- documents/preview（GET，此前零 py 测试引用） ----------------------

    def test_document_preview_streams_with_no_store_and_no_range(self):
        status, raw, headers = self._get("/api/v3/documents/preview?document_id=doc-1")
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"%PDF-synthetic-preview-bytes")
        self.assertTrue(str(headers.get("Content-Type", "")).startswith("text/plain"))
        cache_controls = headers.get_all("Cache-Control") or []
        self.assertIn(
            "private, no-store", cache_controls,
            "no_store=True 追加专条 Cache-Control（与公共文件头 max-age=0 并存）",
        )
        self.assertNotIn("Accept-Ranges", headers, "预览面禁 Range（allow_range=False）")

    def test_document_preview_requires_document_id(self):
        status, raw, _headers = self._get("/api/v3/documents/preview")
        self.assertEqual(status, 400)
        payload = json.loads(raw)
        self.assertEqual(payload["error_code"], "document_id_required")

    def test_document_preview_missing_file_is_404(self):
        self.app.preview_error = FileNotFoundError("no such document")
        status, _raw, _headers = self._get("/api/v3/documents/preview?document_id=doc-1")
        self.assertEqual(status, 404)

    # --- search-index/actions（POST，此前零 py 测试引用） ------------------

    def test_search_index_retry_returns_202_and_status_envelope(self):
        status, payload = self._post("/api/v3/search-index/actions", {"action": "retry"})
        self.assertEqual(status, 202)
        self.assertEqual(payload["data"], {"state": "idle", "documents": 0})
        self.assertEqual(self.app.refresh_calls, 1)

    def test_search_index_unknown_action_is_closed_rejected(self):
        status, payload = self._post("/api/v3/search-index/actions", {"action": "rebuild"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "search_index_action_invalid")
        self.assertEqual(self.app.refresh_calls, 0)

    # --- bookmarks/explain（POST，此前仅前端 mjs 字符串提及） ---------------

    def test_bookmark_explain_missing_id_is_bare_400(self):
        status, payload = self._post("/api/v3/bookmarks/explain", {})
        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": "bookmark_id is required"})
        self.assertNotIn("error_code", payload, "现行为钉：该路由负分支为裸错误体")
        self.assertEqual(self.app.explain_calls, [])

    def test_bookmark_explain_unknown_bookmark_is_bare_404(self):
        self.app.explain_error = KeyError("bookmark was not found")
        status, payload = self._post("/api/v3/bookmarks/explain", {"bookmark_id": "b1"})
        self.assertEqual(status, 404)
        # 现行为钉：str(KeyError) 带 repr 引号，路由原样透传（裸错误体）。
        self.assertEqual(payload, {"error": "'bookmark was not found'"})

    def test_bookmark_explain_unconfigured_is_retriable_400(self):
        self.app.explain_error = RuntimeError("explanation backend missing")
        status, payload = self._post("/api/v3/bookmarks/explain", {"bookmark_id": "b1"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "question_explanation_not_configured")
        self.assertTrue(payload["retriable"])

    def test_bookmark_explain_happy_projects_task_without_payload_leak(self):
        status, payload = self._post("/api/v3/bookmarks/explain", {"bookmark_id": "b1"})
        self.assertEqual(status, 200)
        data = payload["data"]
        self.assertEqual(data["bookmark"]["bookmark_id"], "b1")
        self.assertEqual(data["task"]["task_id"], "task-1")
        self.assertTrue(data["created"])
        self.assertNotIn("payload", data["task"], "任务载荷永不进浏览器投影")

    # --- client-update/actions（POST，此前仅前端 mjs 字符串提及） -----------

    def test_client_update_action_returns_202_then_restarts_exactly_once(self):
        status, payload = self._post(
            "/api/v3/client-update/actions", {"action": "download", "confirmed": True},
        )
        self.assertEqual(status, 202)
        self.assertEqual(payload["data"]["action"], "download")
        self.assertEqual(self.update_calls, [("download", True)])
        # 合同=「202 响应写尽后」才编排重启：客户端先于 handler 线程下一行
        # 观察到响应是固有窗口，短窗轮询替代立即断言（2s 内必达）。
        deadline = time.monotonic() + 2.0
        while self.restart_calls < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.restart_calls, 1, "202 写尽后恰一次编排重启")

    def test_client_update_busy_is_409_retriable_without_restart(self):
        self.update_error = UpdateError("update_busy")
        status, payload = self._post("/api/v3/client-update/actions", {"action": "apply"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error_code"], "update_busy")
        self.assertTrue(payload["retriable"])
        self.assertEqual(self.restart_calls, 0, "动作失败永不触发重启")

    def test_client_update_restart_blocked_is_409_retriable(self):
        self.update_error = UpdateError("update_restart_blocked")
        status, payload = self._post("/api/v3/client-update/actions", {"action": "apply"})
        self.assertEqual(status, 409)
        self.assertTrue(payload["retriable"])
        self.assertEqual(self.restart_calls, 0)

    def test_client_update_confirmation_required_is_400_non_retriable(self):
        self.update_error = UpdateError("confirmation_required")
        status, payload = self._post("/api/v3/client-update/actions", {"action": "apply"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "confirmation_required")
        self.assertFalse(payload["retriable"])

    def test_client_update_unknown_code_falls_back_to_generic_code(self):
        self.update_error = UpdateError("totally_unknown_code")
        status, payload = self._post("/api/v3/client-update/actions", {"action": "apply"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "update_action_failed")
        self.assertFalse(payload["retriable"])
        self.assertEqual(self.restart_calls, 0)


if __name__ == "__main__":
    unittest.main()
