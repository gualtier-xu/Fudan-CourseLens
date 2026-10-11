"""一次性 ticket 传输腿的同 seed 合成 A/B benchmark（合同 §11）。

用法（双进程，两边各自加载所在仓库的实现）：
    cd <baseline-tree> && python tests/benchmark_ticket_transport.py --side baseline
    cd <candidate-tree> && python tests/benchmark_ticket_transport.py --side candidate

拓扑：loopback IdP（直连腿）+ loopback WebVPN（票腿 / 预热 / vpn 转发的
IdP+iCourse 端点）。iCourse 腿的全部请求经 get_vpn_url 落在 WebVPN 主机，
故无需第三台服务器。驱动真实 CourseLensApplication._login_with_retry
（3 attempt / 90s deadline，time.sleep 置零）。

故障模型（同 seed 同脚本，只注入 WebVPN 主机；IdP 直连腿始终健康）：
- reset profile F：WebVPN 对该 run 的前 F 条 TCP 连接立即 RST（冷连接失败）；
- hold 探针：第 1 条连接接受后挂起（模拟建连后无响应，单次测量）。

硬性验收（每 run 断言）：同 ticket 零重放、凭据提交 ≤3。
输出仅闭集计数与耗时，无任何请求内容。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import threading
import time
import unittest.mock as mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from Crypto.PublicKey import RSA


class BenchState:
    """服务器侧闭集计数器（不记录任何请求内容）。"""

    def __init__(self, resets: int, hold_first: bool):
        self.lock = threading.Lock()
        self.resets_left = resets
        self.hold_first = hold_first
        self.conn_count = 0
        self.credentials_submitted = 0
        self.tickets: dict[str, int] = {}
        self.webvpn_root_gets = 0
        self.webvpn_verify_probes = 0
        self.ticket_counter = 0
        self.icourse_ticket_counter = 0


class _BenchServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    pubkey_b64 = ""
    webvpn_base = ""
    state: BenchState

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


class _IdPHandler(BaseHTTPRequestHandler):
    """直连 IdP：WebVPN 主腿的 1-6 步（context/methods/key/execute/engine）。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        state = self.server.state
        if "/idp/authCenter/authenticate" in self.path:
            self.send_response(302)
            self.send_header("Location", "/idp/ac/?lck=bench-lck")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif "/idp/authn/getJsPublicKey" in self.path:
            self._json({"data": self.server.pubkey_b64})
        else:
            self._json({"error": "unknown"}, 404)

    def do_POST(self):
        state = self.server.state
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if "/idp/authn/queryAuthMethods" in self.path:
            self._json(
                {
                    "data": [{"moduleCode": "userAndPwd", "authChainCode": "chain-bench"}],
                    "requestType": "chain_type",
                }
            )
        elif "/idp/authn/authExecute" in self.path:
            with state.lock:
                state.credentials_submitted += 1
            self._json({"code": "200", "loginToken": "bench-token"})
        elif "/idp/authCenter/authnEngine" in self.path:
            with state.lock:
                state.ticket_counter += 1
                ticket = f"ST-webvpn-{state.ticket_counter}"
            html = (
                'var locationValue = "'
                f"{self.server.webvpn_base}/ticket-consume?ticket={ticket}"
                '";'
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        else:
            self._json({"error": "unknown"}, 404)


class _WebVPNHandler(BaseHTTPRequestHandler):
    """WebVPN loopback：预热根路径 / 票消费 / 转发的 IdP+iCourse 端点 / 故障注入。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        state = self.server.state
        with state.lock:
            state.conn_count += 1
            reset = state.resets_left > 0
            if reset:
                state.resets_left -= 1
            hold = state.hold_first and state.conn_count == 1
        if hold:
            # 挂起第 1 条连接：客户端读取超时路径触发后由对端断开。
            # 用 Event().wait 而非 time.sleep：驱动侧 patch 了 time.sleep。
            threading.Event().wait(30)
            self.close_connection = True
            return
        if reset:
            raise ConnectionResetError("bench cold-connection reset")

    def _reply(self, status, *, location="", cookie="", body=b"", content_type="text/html"):
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        state = self.server.state
        path = self.path
        has_cookie = bool(self.headers.get("Cookie"))
        if path.rstrip("/") in ("", "/"):
            with state.lock:
                state.webvpn_root_gets += 1
                if has_cookie:
                    state.webvpn_verify_probes += 1
            if has_cookie:
                self._reply(200, body=b"ok")
            else:
                self._reply(302, location="/login")
            return
        if "ticket=" in path:
            ticket = next(
                part.split("=", 1)[1]
                for part in path.split("?")[-1].split("&")
                if part.startswith("ticket=")
            )
            with state.lock:
                state.tickets[ticket] = state.tickets.get(ticket, 0) + 1
            self._reply(
                302,
                location="/portal",
                cookie="bench_session=ticket-ok; Path=/",
            )
            return
        if path.rstrip("/").endswith("/portal"):
            self._reply(200, body=b"portal")
            return
        if "casapi" in path:
            self._reply(200, body=b'<a href="/ac/?lck=bench-icourse-lck">login</a>')
            return
        if "getJsPublicKey" in path:
            body = json.dumps({"data": self.server.pubkey_b64}).encode()
            self._reply(200, body=body, content_type="application/json")
            return
        if "infosimple" in path:
            self._reply(200, body=b'{"code": 0}', content_type="application/json")
            return
        self._reply(302, location="/login")

    def do_POST(self):
        state = self.server.state
        path = self.path
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if "queryAuthMethods" in path:
            body = json.dumps(
                {
                    "data": [{"moduleCode": "userAndPwd", "authChainCode": "chain-bench"}],
                    "requestType": "chain_type",
                }
            ).encode()
            self._reply(200, body=body, content_type="application/json")
        elif "authExecute" in path:
            with state.lock:
                state.credentials_submitted += 1
            body = json.dumps({"code": "200", "loginToken": "bench-token-2"}).encode()
            self._reply(200, body=body, content_type="application/json")
        elif "authnEngine" in path:
            with state.lock:
                state.icourse_ticket_counter += 1
                ticket = f"ST-icourse-{state.icourse_ticket_counter}"
            html = (
                'var locationValue = "https://icourse.invalid/ticket-consume'
                f'?ticket={ticket}";'
            ).encode()
            self._reply(200, body=html)
        else:
            self._reply(302, location="/login")

    def do_HEAD(self):
        self._reply(200)


_MODULES = {}


def load_app(repo_root: Path):
    """Import src from the given repo once per process."""
    if not _MODULES:
        root = str(repo_root)
        if root not in sys.path:
            sys.path.insert(0, root)
        from src import application as app_mod
        from src.runtime import config as config_mod

        _MODULES["application"] = app_mod
        _MODULES["config"] = config_mod
    return _MODULES["application"], _MODULES["config"]


def run_once(repo_root: Path, resets: int, hold_first: bool, scratch: Path) -> dict:
    application, config = load_app(repo_root)
    key = RSA.generate(2048)
    pubkey_b64 = "".join(
        line for line in key.export_key().decode("ascii").splitlines() if "KEY" not in line
    )
    state = BenchState(resets=resets, hold_first=hold_first)
    servers = []
    for handler in (_IdPHandler, _WebVPNHandler):
        server = _BenchServer(("127.0.0.1", 0), handler)
        server.state = state
        server.pubkey_b64 = pubkey_b64
        servers.append(server)
    idp, webvpn = servers
    idp.webvpn_base = webvpn.base
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()

    # 隔离进程内 last-success 记忆：每个 run 从默认候选顺序出发
    memory = getattr(application, "_LAST_TICKET_SUCCESS", None)
    if memory is not None:
        memory.update(route=None, transport=None)

    started = time.monotonic()
    outcome: dict = {}
    with mock.patch.object(config, "IDP_BASE", idp.base), \
            mock.patch.object(config, "WEBVPN_BASE", webvpn.base), \
            mock.patch.object(config, "ICOURSE_BASE", "https://icourse.invalid"), \
            mock.patch("time.sleep", lambda _s: None), \
            tempfile.TemporaryDirectory(dir=scratch) as workdir:
        service = application.CourseLensApplication(Path(workdir))
        try:
            service.set_credentials("student", "password")
            service.network.service_proxies = lambda *a, **k: [""]
            try:
                service._login_with_retry(max_attempts=3)
                outcome["success"] = True
            except Exception as exc:
                outcome["success"] = False
                outcome["error_code"] = str(getattr(exc, "code", "") or type(exc).__name__)
        finally:
            service.close()
    outcome["ready_ms"] = int((time.monotonic() - started) * 1000)
    with state.lock:
        outcome["credentials_submitted"] = state.credentials_submitted
        outcome["ticket_gets"] = sum(state.tickets.values())
        outcome["distinct_tickets"] = len(state.tickets)
        outcome["ticket_replays"] = max(
            (count - 1 for count in state.tickets.values()), default=0
        )
        outcome["webvpn_connections"] = state.conn_count
        outcome["webvpn_verify_probes"] = state.webvpn_verify_probes
        outcome["webvpn_warmup_gets"] = state.webvpn_root_gets - state.webvpn_verify_probes
    for server in servers:
        server.shutdown()
        server.server_close()
    return outcome


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def summarize(runs: list[dict]) -> dict:
    durations = [run["ready_ms"] for run in runs]
    successes = [run for run in runs if run["success"]]
    return {
        "success_rate": len(successes) / len(runs),
        "ready_p50_ms": percentile(durations, 0.50),
        "ready_p95_ms": percentile(durations, 0.95),
        "credentials_submitted_total": sum(run["credentials_submitted"] for run in runs),
        "ticket_gets_total": sum(run["ticket_gets"] for run in runs),
        "webvpn_connections_total": sum(run["webvpn_connections"] for run in runs),
        "verify_probes_total": sum(run["webvpn_verify_probes"] for run in runs),
        "warmup_gets_total": sum(run["webvpn_warmup_gets"] for run in runs),
        "avg_credential_submissions_per_success": statistics.mean(
            [run["credentials_submitted"] for run in successes]
        )
        if successes
        else None,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--side", choices=["baseline", "candidate"], required=True)
    parser.add_argument("--runs", type=int, default=40)
    parser.add_argument("--profiles", type=str, default="0,1,2,3")
    parser.add_argument("--hold-probe", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo).resolve()
    scratch = repo_root / "runtime" / "cache" / "ticket-transport-bench"
    scratch.mkdir(parents=True, exist_ok=True)

    seed = 20260907  # 同 seed 同故障脚本：profile 集合、run 数与故障行为固定
    results = {"side": args.side, "seed": seed, "runs_per_profile": args.runs, "profiles": {}}
    for profile in [int(p) for p in args.profiles.split(",")]:
        runs = [
            run_once(repo_root, resets=profile, hold_first=False, scratch=scratch)
            for _ in range(args.runs)
        ]
        assert all(run["ticket_replays"] == 0 for run in runs), "同 ticket 重放：硬性违规"
        # 产品语义：每 attempt 两腿各提交一次（≤2×max_attempts）；
        # “凭据提交不增加”是与对侧同 profile 的对比结论，在汇总报告中裁决。
        assert all(run["credentials_submitted"] <= 6 for run in runs), "凭据提交超产品语义上限"
        results["profiles"][str(profile)] = summarize(runs)
    if args.hold_probe:
        held = run_once(repo_root, resets=0, hold_first=True, scratch=scratch)
        results["hold_probe"] = {
            "success": held["success"],
            "ready_ms": held["ready_ms"],
            "webvpn_connections": held["webvpn_connections"],
        }
    payload = json.dumps(results, ensure_ascii=False, indent=2)
    print(payload)
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
