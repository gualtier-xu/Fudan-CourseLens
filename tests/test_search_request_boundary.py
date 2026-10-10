"""F1（FUZZ-INPUT-1 P2）：/api/v3/search 控制字符 query 边界闭集钉。

修前形态：控制字符（\\x01-\\x1f/NUL/DEL）query 穿透校验打穿检索深链 →
500 ``runtime_failed``（闭集破口：垃圾输入不该是 runtime 故障）。修后：
边界消毒映射 400 ``search_request_invalid``（与 emoji 超长同一正确形态），
检索深链零触达；正常查询与含 \\t\\n\\r 空白查询零变化。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen
from urllib.parse import quote

from src.application import CourseLensApplication
from src.runtime.http_api import make_handler
from tests.http_services import http_services


class _CrashySearchIndex:
    """检索深链替身：控制字符到达即抛（复刻修前 500 形），其余诚实空结果。

    边界消毒生效时控制字符永不抵达本层——本替身既当修前红的复现器，也当
    「深链零触达」的断言器。"""

    def __init__(self) -> None:
        self.deep_calls = 0

    @staticmethod
    def _has_control_characters(query: object) -> bool:
        text = str(query or "")
        return any((ord(ch) < 32 and ch not in "\t\n\r") or ord(ch) == 127 for ch in text)

    def search(self, query, **kwargs):
        self.deep_calls += 1
        if self._has_control_characters(query):
            raise RuntimeError("synthetic deep crash on control characters")
        return {"schema": "courselens.search-result.v1", "hits": [], "total": 0}

    def status(self):
        return {}


class _SearchApplication:
    """最小装配：search_learning 走真实应用层薄封装，search_index 用替身。"""

    search_learning = CourseLensApplication.search_learning

    def __init__(self, root: Path):
        self.search_index = _CrashySearchIndex()

    def start_search_index(self) -> None:
        return None

    def authentication_snapshot(self) -> dict:
        return {"state": "ready"}


class SearchRequestBoundaryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.service = _SearchApplication(root)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(http_services(self.service), root),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.base = f"http://127.0.0.1:{server.server_port}"

    def _get(self, raw_query: str):
        url = f"{self.base}/api/v3/search?q={quote(raw_query, safe='')}"
        try:
            with urlopen(url) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def test_control_character_query_maps_to_closed_400_not_runtime_500(self):
        """F1 复现四拍核心：ctrl-pure/ctrl-mixed/nul-only 三形一律 400 闭集
        （修前 500 runtime_failed），检索深链零触达。"""
        for name, query in (
            ("ctrl-pure", "\x01\x02\x03"),
            ("ctrl-mixed", "概率论\x1f线性代数"),
            ("nul-only", "\x00"),
            ("del", "热力学\x7f"),
        ):
            with self.subTest(case=name):
                status, payload = self._get(query)
                self.assertEqual(status, 400, f"{name} 修前 500 runtime_failed 回潮")
                self.assertEqual(payload["error_code"], "search_request_invalid")
        self.assertEqual(self.service.search_index.deep_calls, 0, "垃圾输入零触达检索深链")

    def test_meaningful_whitespace_and_normal_queries_keep_working(self):
        """\\t\\n\\r 为有意义空白放行；普通查询 200 envelope（既有路径零变化）。"""
        status, payload = self._get("梯度\n下降\t规律\r\n")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["schema"], "courselens.search-result.v1")
        status, payload = self._get("概率论")
        self.assertEqual(status, 200)
        self.assertEqual(self.service.search_index.deep_calls, 2, "正常查询照常进检索深链")


if __name__ == "__main__":
    unittest.main()
