from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from path_utils import PROJECT_ROOT
from src.runtime.http_api import make_handler
from src.runtime.task_store import TaskStore
from tests.http_services import http_services


class IdleLoopSseSnapshotTests(unittest.TestCase):
    """PF1 稳态空闲循环降载（后端半）：SSE tasks 快照按投影变化条件化。

    量尺（真实 handler + 真实 TaskStore）：
    - 投影稳定（全部任务终态）的空闲连接 12s 内只发首拍快照（5s/10s 拍抑制）；
    - queued 任务投影含真实演进的 elapsed_queued_seconds（学生可见「已等待」），
      逐拍照发——去重不得吞掉真实演进；
    - 任务真变化（新增 + 状态跃迁）下一拍（≤5s）必发新快照；
    - 心跳不受影响。
    """

    class Service:
        def __init__(self, root):
            self.task_store = TaskStore(root / "state.db")

    def test_idle_connection_suppresses_unchanged_task_snapshots(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "runtime" / "cache") as tmp:
            service = self.Service(Path(tmp))
            task, created = service.task_store.add_task(
                "subtitle", "c1", "s1", {"note": "sse-bench"}, start_paused=False,
            )
            self.assertTrue(created)
            service.task_store.mark_terminal(task["task_id"], "completed")
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                make_handler(http_services(service), PROJECT_ROOT / "frontend"),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                stream = _SseStream(server.server_port)
                idle = stream.read(12.0)
                # 投影稳定：12s 覆盖 0/5/10 三个快照拍，只允许首拍一次。
                idle_events = idle.count(b"event: tasks")
                self.assertEqual(idle_events, 1, f"空闲 12s 应仅首拍快照，实际 {idle_events}")
                self.assertGreaterEqual(idle.count(b": heartbeat"), 1, "心跳保持")

                # queued 任务投影真实演进（elapsed_queued_seconds 走动）：照发不抑制。
                service.task_store.add_task("summary", "c1", "s2", {"note": "queued"}, start_paused=False)
                walking = stream.read(12.0)
                walking_events = walking.count(b"event: tasks")
                self.assertGreaterEqual(walking_events, 2, "queued 投影演进必须逐拍照发")
                self.assertLessEqual(walking_events, 3, "演进拍不得超过节拍数")

                # 终态化 s2 并冲刷一拍，使投影回到全稳定基线——变化窗才有
                # 「恰好一拍」的可断言性（活动任务排队秒数每拍真实演进，
                # 多拍属演进照发，上一段已钉）。
                service.task_store.mark_terminal(service.task_store.list_tasks(
                    kinds=["summary"], limit=1)[0]["task_id"], "completed")
                stream.read(7.0)

                # 负例钉：真变化（新增终态任务=历史新增一条记录）下一拍必发、
                # 恰好一拍、载荷含新任务。
                task2, _ = service.task_store.add_task(
                    "quiz", "c1", "s3", {"note": "change"}, start_paused=False,
                )
                service.task_store.mark_terminal(task2["task_id"], "failed")
                changed = stream.read(8.0)
                self.assertEqual(changed.count(b"event: tasks"), 1, "真变化恰好一拍新快照")
                self.assertIn(task2["task_id"].encode(), changed, "新快照含新任务")
                self.assertIn(b"failed", changed, "新快照反映新任务状态")
                stream.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class _SseStream:
    """裸 socket SSE 读流：按秒窗口收字节（真 handler 逐拍推送）。"""

    def __init__(self, port: int):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=20)
        self.sock.sendall(
            b"GET /api/v3/events?topics=tasks&after=0 HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\nAccept: text/event-stream\r\n\r\n"
        )
        first = self._recv_chunk()
        assert b"200 OK" in first, f"SSE status line missing: {first[:80]!r}"
        self.buffer = bytearray(first)

    def _recv_chunk(self) -> bytes:
        self.sock.settimeout(1.0)
        while True:
            try:
                return self.sock.recv(65536)
            except TimeoutError:
                continue

    def read(self, seconds: float) -> bytes:
        deadline = time.monotonic() + seconds
        chunks = [bytes(self.buffer)]
        self.buffer.clear()
        while time.monotonic() < deadline:
            try:
                self.sock.settimeout(max(0.1, deadline - time.monotonic()))
                chunk = self.sock.recv(65536)
            except TimeoutError:
                continue
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        self.sock.close()


if __name__ == "__main__":
    unittest.main()
