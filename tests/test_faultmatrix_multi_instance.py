"""FAULT-MATRIX-1 维度四（单元钉）：UI 服务器绑定语义的平台收口。

F-INST-1 回归钉：``ThreadingHTTPServer`` 默认 ``allow_reuse_address=1`` 在
Windows 上是可劫持语义（第二个实例能绑住在位实例的端口并同样「服务就绪」）。
产品必须使用 ``_ExclusiveBindHTTPServer``（Windows=SO_EXCLUSIVEADDRUSE，
第二绑定者确定性失败→既有 ERROR_PORT_BUSY 人话通道；POSIX 保持既有语义）。

进程级全旅程（双开拦截/证据隔离/锁死亡接管）见
``tools/faultmatrix_multi_instance.py``（真实多进程 harness，不在单元套件内）。
"""

from __future__ import annotations

import os
import socket
import threading
import unittest
from http.server import ThreadingHTTPServer

from src.app import _ExclusiveBindHTTPServer


class ExclusiveBindSemanticsTests(unittest.TestCase):
    """Windows=第二绑定者必须失败；POSIX=保持既有 TIME_WAIT 复用语义。"""

    def test_platform_allow_reuse_address_contract(self):
        if os.name == "nt":
            self.assertFalse(
                _ExclusiveBindHTTPServer.allow_reuse_address,
                "Windows 上必须禁用 SO_REUSEADDR（可劫持语义，F-INST-1）",
            )
        else:
            self.assertTrue(
                _ExclusiveBindHTTPServer.allow_reuse_address,
                "POSIX 保持既有 allow_reuse_address 语义",
            )
        self.assertTrue(
            issubclass(_ExclusiveBindHTTPServer, ThreadingHTTPServer),
            "必须是 ThreadingHTTPServer 的收口子类（线程模型不变）",
        )

    def test_second_bind_on_live_listener_fails_deterministically_on_windows(self):
        if os.name != "nt":
            self.skipTest("Windows 特有 SO_REUSEADDR 劫持语义")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        first = _ExclusiveBindHTTPServer(
            ("127.0.0.1", port), lambda *args, **kwargs: None
        )
        thread = threading.Thread(target=first.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(OSError):
                _ExclusiveBindHTTPServer(
                    ("127.0.0.1", port), lambda *args, **kwargs: None
                )
        finally:
            first.shutdown()
            first.server_close()
            thread.join(timeout=5)

    def test_serve_uses_exclusive_bind_server(self):
        """serve() 的两阶段绑定①必须走收口类（防回退钉）。"""
        import inspect

        from src import app as app_module

        source = inspect.getsource(app_module.serve)
        self.assertIn(
            "_ExclusiveBindHTTPServer(",
            source,
            "serve() 必须用 _ExclusiveBindHTTPServer 建立两阶段绑定①",
        )
        self.assertNotIn(
            "ThreadingHTTPServer(\n",
            source,
            "serve() 不得再直接实例化裸 ThreadingHTTPServer",
        )


class RequestQueueSizeTests(unittest.TestCase):
    """F-GATE-2（发布门 2026-10-10）回归钉：listen backlog 收口。

    缺陷形状：socketserver 默认 ``request_queue_size=5``，首载并发突发
    （约 49 静态模块+约 10 API+SSE）叠加 accept 线程短暂挤占时，Windows
    backlog 溢出=新连接直接拒绝（ERR_CONNECTION_REFUSED），浏览器侧整页
    静态 import 图死亡（白屏形，发布门实测 stage-preload.js 拒载 ×1）。
    收口=128（同域并发 ≤6 的数量级余量；内核队列深度，零显著成本）。
    """

    def test_request_queue_size_contract(self):
        self.assertEqual(
            _ExclusiveBindHTTPServer.request_queue_size, 128,
            "listen backlog 必须=128（F-GATE-2：默认 5 会被首载突发打穿成拒载）",
        )
        self.assertGreater(
            _ExclusiveBindHTTPServer.request_queue_size,
            ThreadingHTTPServer.request_queue_size,
            "收口类必须显式放大默认 backlog（默认值=5）",
        )

    def test_burst_connections_all_served(self):
        """行为腿：32 并发连接全部受理（无拒绝、全部拿到 200 应答）。

        慢 handler（每请求 ~20ms）逼真首载突发的积压窗。灵敏度实测（2026-10-10，
        车道 scratch）：同形状 12 轮×32 连发在 backlog=5 下亦全受理——纯 accept
        压力在本机打不满默认 backlog，发布门拒载形需要整机高载才触达；故本腿
        是受理合同的守卫（放大后不得反而拒载），真正钉死收口值的是上一条
        配置腿。"""
        from http.server import BaseHTTPRequestHandler

        class _SlowHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                threading.Event().wait(0.02)
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args, **kwargs):
                pass

        server = _ExclusiveBindHTTPServer(("127.0.0.1", 0), _SlowHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        errors: list[Exception] = []

        def _one_request() -> None:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=10) as conn:
                    conn.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                    data = b""
                    while b"\r\n" not in data:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                self.assertTrue(
                    data.startswith(b"HTTP/1.") and b" 200 " in data.split(b"\r\n", 1)[0],
                    f"应答非 200：{data[:40]!r}",
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        try:
            threads = [
                threading.Thread(target=_one_request)
                for _ in range(32)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)
            self.assertEqual(
                errors, [],
                f"32 并发连接必须全部受理（收到 {32 - len(errors)} 个），"
                f"拒绝/超时={[str(e) for e in errors[:3]]}",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
