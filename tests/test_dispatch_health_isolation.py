"""DISPATCH-HEALTH-1（第四十案）行为钉：派发进行中 /api/health 恒应答 + 探测留痕。

现场实证：派发字幕任务前后页面亮「本地服务已断开，请重启客户端」，而本地
进程健在、服务端零关停痕迹、零 mid-request 断连。本钉固定两条不变量：

① 健康路由与业务路由在服务端互不阻塞——每连接一线程，健康分支只读一次
   生命周期快照（零业务锁、零磁盘 I/O、零网络），因此派发请求阻塞在慢的
   远端连接预检里时，健康探测仍必须按时应答；
② 探测到达留痕（HealthProbeTrace）是闭集、节流、零载荷的，且留痕失败绝不
   影响健康响应——这是下一次「页面报断开」能否归因的前提。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

from src.runtime.http_api import HealthProbeTrace, make_handler
from src.runtime.task_store import TaskStore
from tests.http_services import http_services

ROOT = Path(__file__).resolve().parents[1]
CACHE_ROOT = ROOT / "runtime" / "cache"
TRACE_LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} http: "
    r"(?:health probe resumed after \d+s of silence"
    r"|health probe served while lifecycle=[a-z_]{1,24})$"
)


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class HealthProbeTraceTests(unittest.TestCase):
    """留痕本体：闭集、节流、零载荷、写失败不影响调用方。"""

    def test_silence_gap_emits_exactly_one_closed_line(self):
        clock = _Clock()
        lines: list[str] = []
        trace = HealthProbeTrace(clock=clock, writer=lines.append)

        trace.note("ready")
        self.assertEqual(lines, [], "首次探测没有可比对的前一次，不记行")

        clock.value = 120.0
        trace.note("ready")
        self.assertEqual(len(lines), 1, "静默 ≥60s 后的首次探测记一行")
        self.assertRegex(lines[0], TRACE_LINE)
        self.assertIn("resumed after 120s", lines[0])

        clock.value = 130.0
        trace.note("ready")
        self.assertEqual(len(lines), 1, "恢复后的常规探测不再记行")

    def test_steady_probes_emit_nothing(self):
        clock = _Clock()
        lines: list[str] = []
        trace = HealthProbeTrace(clock=clock, writer=lines.append)
        for step in range(10):
            clock.value = step * 10.0
            trace.note("ready")
        self.assertEqual(lines, [], "10s 一拍的常规探测不得刷屏")

    def test_not_ready_state_is_named_and_throttled(self):
        clock = _Clock()
        lines: list[str] = []
        trace = HealthProbeTrace(clock=clock, writer=lines.append)

        trace.note("draining")
        trace.note("draining")
        self.assertEqual(len(lines), 1, "60s 窗口内同一状态至多一行")
        self.assertIn("lifecycle=draining", lines[0])

        clock.value = 300.0
        trace.note("draining")
        self.assertEqual(len(lines), 2, "节流窗过后仍非就绪再记一行")

    def test_unexpected_state_collapses_to_closed_label(self):
        lines: list[str] = []
        trace = HealthProbeTrace(writer=lines.append)
        trace.note("draining ../../etc/passwd http://127.0.0.1:8765/api/v3/tasks")
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], TRACE_LINE)
        self.assertIn("lifecycle=unknown", lines[0])
        for leak in ("passwd", "8765", "/api/"):
            self.assertNotIn(leak, lines[0], "留痕不得携带请求细节")

    def test_writer_failure_never_reaches_the_caller(self):
        def explode(_line: str) -> None:
            raise OSError("synthetic log sink failure")

        trace = HealthProbeTrace(writer=explode)
        trace.note("failed")
        trace.note("unknown-state!")
        # 没有断言可失败：note() 必须静默吞掉写入异常（健康响应优先级最高）


class _DispatchService:
    """派发处理器内联等待远端连接预检的最小复刻（现场形状：GitHub 走本地代理）。"""

    def __init__(self, root: Path) -> None:
        self.task_store = TaskStore(root / "state.db")
        self.dispatch_started = threading.Event()
        self.release_dispatch = threading.Event()

    def app_shell_snapshot(self) -> dict:
        return {"schema": "courselens.app-shell.v1", "observed_at": 1, "expires_at": 2}

    def authentication_snapshot(self) -> dict:
        return {"state": "ready"}

    def enqueue_subtitle(self, course_id: str, sub_id: str, **kwargs) -> dict:
        self.dispatch_started.set()
        self.release_dispatch.wait(15)
        return {
            "task_id": "task-dispatch-health-1", "kind": "subtitle",
            "course_id": course_id, "sub_id": sub_id, "state": "queued",
            "progress": {}, "estimate": {},
        }


class DispatchHealthIsolationTests(unittest.TestCase):
    """派发在飞 → 健康探测照常应答（连续三次都要即时应答）。"""

    def _start(self, service, *, health_trace: HealthProbeTrace | None = None):
        """service 传入前已装配完毕（健康路由只需要 lifecycle 边界）。"""
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(service, ROOT / "frontend", health_trace=health_trace),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        return server, thread, base

    def _dispatch(self, base: str, sink: list) -> None:
        body = json.dumps({
            "kind": "subtitle", "course_id": "synthetic-course", "sub_id": "synthetic-lecture",
        }).encode("utf-8")
        request = Request(
            f"{base}/api/v3/tasks/enqueue", data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=20) as response:
                sink.append(response.status)
        except Exception as exc:  # pragma: no cover - 失败由断言在测试侧暴露
            sink.append(exc)

    def test_health_answers_three_times_while_a_dispatch_request_is_blocked(self):
        with tempfile.TemporaryDirectory(dir=CACHE_ROOT) as tmp:
            service = _DispatchService(Path(tmp))
            server, thread, base = self._start(http_services(service))
            dispatched: list = []
            dispatch_thread = threading.Thread(
                target=self._dispatch, args=(base, dispatched), daemon=True,
            )
            try:
                dispatch_thread.start()
                self.assertTrue(service.dispatch_started.wait(5.0), "派发请求应已进入处理器")
                for attempt in range(3):
                    started = time.monotonic()
                    with urlopen(f"{base}/api/health", timeout=3) as response:
                        health = json.loads(response.read())
                    elapsed = time.monotonic() - started
                    self.assertTrue(health["ok"], f"第 {attempt + 1} 次探测应为就绪")
                    self.assertEqual(health["service"], "fudan-courselens")
                    self.assertEqual(health["schema_version"], 1)
                    self.assertEqual(health["pid"], os.getpid())
                    self.assertLess(
                        elapsed, 2.0,
                        f"第 {attempt + 1} 次探测排队在派发请求之后（{elapsed:.2f}s）",
                    )
                self.assertFalse(dispatched, "派发请求此时仍应被阻塞在处理器内")
            finally:
                service.release_dispatch.set()
                dispatch_thread.join(timeout=5.0)
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_not_ready_lifecycle_is_traced_and_reported(self):
        with tempfile.TemporaryDirectory(dir=CACHE_ROOT) as tmp:
            service = SimpleNamespace(lifecycle=SimpleNamespace(snapshot=lambda: {
                "state": "draining", "stage": "request_drain",
                "error_code": "", "accepting_requests": False,
            }))
            lines: list[str] = []
            server, thread, base = self._start(
                service, health_trace=HealthProbeTrace(writer=lines.append),
            )
            try:
                with urlopen(f"{base}/api/health", timeout=3) as response:
                    health = json.loads(response.read())
                self.assertFalse(health["ok"], "非就绪生命周期一律 ok=false")
                self.assertEqual(health["lifecycle"]["state"], "draining")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
            self.assertEqual(len(lines), 1, "非就绪探测恰好留一行")
            self.assertRegex(lines[0], TRACE_LINE)
            self.assertIn("lifecycle=draining", lines[0])


if __name__ == "__main__":
    unittest.main()
