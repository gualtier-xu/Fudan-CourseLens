"""LIVE-BE-1 U3 · 原生文稿增量层钉（F4 预建件 · S3 后端半边）。

钉六件事：
1. 全量读契约不变：ms 换算+start 升序+空白段过滤+闭集参数（零新外联面）。
2. since_ms 增量读=含尾水位（start_ms >= since_ms）：生长中的尾段随新
   文本重发，调用方按 start_ms 身份替换；TTL 过期后恰好重联一次以发现
   新段/新文本。
3. 会话内缓存：新鲜窗（TRANSCRIPT_CACHE_TTL_SECONDS，含边界）内全量/
   增量读都不再打平台。
4. 失败隔离：端点失败返回 None，不污染既有缓存，下次读重试网络。
5. 每键隔离+新鲜拷贝：调用方改动返回行不伤缓存。
6. 兼容：get_transcript 平文路径语义不变。
"""
from __future__ import annotations

import unittest
from unittest import mock

from src.api.icourse import ICourseClient


def _payload(*rows, code=0):
    return {"code": code, "list": [{"all_content": list(rows)}]}


def _row(begin, end, text):
    return {"BeginSec": begin, "EndSec": end, "Text": text}


FULL_ROWS = (_row(10, 12, "alpha"), _row(5, 7, "beta"), _row(20, 22, "  "))


class Resp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class VPN:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.last_params = None

    def get(self, url, params=None, **kwargs):
        self.calls += 1
        self.last_params = dict(params or {})
        return Resp(self.payload)


class TranscriptIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.clock = [0.0]
        patcher = mock.patch("time.monotonic", new=lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.vpn = VPN(_payload(*FULL_ROWS))
        self.client = ICourseClient(self.vpn)

    def test_full_read_contract_unchanged_with_closed_params(self):
        segments = self.client.get_transcript_segments("s1")
        self.assertEqual([s["start_ms"] for s in segments], [5000, 10000])
        self.assertEqual(segments[0], {"start_ms": 5000, "end_ms": 7000, "text": "beta"})
        self.assertEqual(self.vpn.calls, 1)
        self.assertEqual(self.vpn.last_params, {"sub_id": "s1", "format": "json"})

    def test_incremental_watermark_is_inclusive(self):
        self.client.get_transcript_segments("s1")
        tail = self.client.get_transcript_segments("s1", since_ms=10000)
        self.assertEqual(tail, [{"start_ms": 10000, "end_ms": 12000, "text": "alpha"}])
        self.assertEqual(self.vpn.calls, 1)  # 新鲜缓存上的增量读不再外联

    def test_growing_tail_rediscovered_after_ttl(self):
        self.client.get_transcript_segments("s1")
        self.clock[0] = 3.0  # > TTL 2.0
        self.vpn.payload = _payload(_row(10, 14, "alpha grew"), _row(5, 7, "beta"))
        tail = self.client.get_transcript_segments("s1", since_ms=10000)
        self.assertEqual(tail, [{"start_ms": 10000, "end_ms": 14000, "text": "alpha grew"}])
        self.assertEqual(self.vpn.calls, 2)

    def test_cache_hit_within_ttl_boundary_and_single_stale_refetch(self):
        self.client.get_transcript_segments("s1")
        self.client.get_transcript_segments("s1")
        self.assertEqual(self.vpn.calls, 1)
        walked = self.client.get_transcript_segments("s1", since_ms=5000)
        self.assertEqual([s["start_ms"] for s in walked], [5000, 10000])
        self.assertEqual(self.vpn.calls, 1)
        self.clock[0] = 2.0  # 恰在边界：仍算新鲜（<=）
        self.client.get_transcript_segments("s1")
        self.assertEqual(self.vpn.calls, 1)
        self.clock[0] = 2.5
        self.client.get_transcript_segments("s1")
        self.assertEqual(self.vpn.calls, 2)  # 过期恰好重联一次

    def test_error_returns_none_and_preserves_cache(self):
        self.client.get_transcript_segments("s1")
        self.vpn.payload = _payload(code=9)
        self.clock[0] = 3.0
        self.assertIsNone(self.client.get_transcript_segments("s1"))
        self.assertEqual(self.client._transcript_cache["s1"][1][0]["start_ms"], 5000)
        self.vpn.payload = _payload(*FULL_ROWS)
        self.assertEqual(self.client.get_transcript_segments("s1")[0]["start_ms"], 5000)
        self.assertEqual(self.vpn.calls, 3)

    def test_per_key_isolation_and_fresh_copies(self):
        self.client.get_transcript_segments("s1")
        self.client.get_transcript_segments("s2")
        self.assertEqual(self.vpn.calls, 2)
        first = self.client.get_transcript_segments("s1")
        first[0]["text"] = "mutated"
        first.append({"junk": True})
        again = self.client.get_transcript_segments("s1")
        self.assertEqual(again[0]["text"], "beta")
        self.assertEqual(len(again), 2)
        self.assertEqual(self.vpn.calls, 2)

    def test_empty_and_error_semantics(self):
        self.vpn.payload = {"code": 0, "list": []}
        self.assertEqual(self.client.get_transcript_segments("s1"), [])
        self.vpn.payload = {"code": 5}
        self.assertIsNone(self.client.get_transcript_segments("s2"))

    def test_get_transcript_compat(self):
        self.assertEqual(self.client.get_transcript("s1"), "beta alpha")
        self.vpn.payload = {"code": 9}
        self.assertIsNone(self.client.get_transcript("zz"))


if __name__ == "__main__":
    unittest.main()
