"""One-time maintainer registration for the public CourseLens GitHub App.

The manifest exchange returns several server credentials. This desktop client
does not need them, so they are never printed or persisted. Only the public
client ID and app slug are written to runtime-assets.json.
"""

from __future__ import annotations

import argparse
import html
import json
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.distribution import DISTRIBUTION_REPOSITORY

ROOT = PROJECT_ROOT
RUNTIME_ASSETS = ROOT / "runtime-assets.json"
GITHUB_NEW_APP = "https://github.com/settings/apps/new"
GITHUB_API = "https://api.github.com"


def _manifest(callback_url: str) -> dict:
    return {
        "name": "Fudan CourseLens Student 2026",
        "url": f"https://github.com/{DISTRIBUTION_REPOSITORY}",
        "description": "Per-student encrypted GitHub Actions worker for Fudan CourseLens.",
        "redirect_url": callback_url,
        "callback_urls": [callback_url],
        "hook_attributes": {
            "url": f"https://github.com/{DISTRIBUTION_REPOSITORY}",
            "active": False,
        },
        "public": True,
        "request_oauth_on_install": False,
        "default_permissions": {
            "actions": "write",
            "actions_variables": "write",
            "administration": "write",
            # The client verifies and repairs only the managed per-student
            # Worker.  Workflows is separate from Contents in GitHub Apps, so
            # both write permissions are required for a one-click repair.
            "contents": "write",
            "environments": "write",
            "issues": "write",
            "workflows": "write",
        },
        "default_events": [],
    }


class RegistrationServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, state: str):
        super().__init__(address, RegistrationHandler)
        self.state = state
        self.result: dict[str, str] = {}
        self.done = threading.Event()


class RegistrationHandler(BaseHTTPRequestHandler):
    server: RegistrationServer

    def log_message(self, _format, *args):
        return

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/":
            callback = f"http://127.0.0.1:{self.server.server_port}/callback"
            manifest = json.dumps(_manifest(callback), ensure_ascii=False)
            page = f"""<!doctype html><meta charset=\"utf-8\"><title>注册 CourseLens GitHub App</title>
<p>正在打开 GitHub App 注册页…</p>
<form id=\"register\" action=\"{GITHUB_NEW_APP}?state={html.escape(self.server.state)}\" method=\"post\">
<input type=\"hidden\" name=\"manifest\" value=\"{html.escape(manifest, quote=True)}\"></form>
<script>document.getElementById('register').submit()</script>"""
            self._send(200, page)
            return
        if parsed.path == "/callback":
            query = urllib.parse.parse_qs(parsed.query)
            state = (query.get("state") or [""])[0]
            code = (query.get("code") or [""])[0]
            if not secrets.compare_digest(state, self.server.state) or not code:
                self._send(400, "注册校验失败，请关闭此页并重新运行工具。")
                return
            self.server.result = {"code": code}
            self.server.done.set()
            self._send(200, "App 已创建。请返回终端完成最后配置，然后可以关闭此页。")
            return
        self._send(404, "Not found")

    def _send(self, status: int, message: str):
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _exchange(code: str, proxy: str) -> dict:
    session = requests.Session()
    session.trust_env = False
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    response = session.post(
        f"{GITHUB_API}/app-manifests/{code}/conversions",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "Fudan-CourseLens-Maintainer/2",
        },
        timeout=30,
    )
    if response.status_code != 201:
        request_id = response.headers.get("X-GitHub-Request-Id", "unknown")
        raise RuntimeError(f"GitHub App 注册交换失败：HTTP {response.status_code}（请求 {request_id}）")
    return dict(response.json())


def _write_public_config(payload: dict) -> tuple[str, str]:
    client_id = str(payload.get("client_id") or "").strip()
    slug = str(payload.get("slug") or "").strip()
    if not client_id or not slug:
        raise RuntimeError("GitHub 未返回 client_id 或 slug")
    assets = json.loads(RUNTIME_ASSETS.read_text(encoding="utf-8"))
    assets["github_app"] = {"client_id": client_id, "slug": slug}
    temporary = RUNTIME_ASSETS.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(assets, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(RUNTIME_ASSETS)
    return client_id, slug


def main() -> int:
    parser = argparse.ArgumentParser(description="注册 CourseLens 公共 GitHub App（维护者只需运行一次）")
    parser.add_argument("--proxy", default="http://127.0.0.1:6268")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--port", type=int, default=0, help="本地回调端口；0 表示自动分配")
    parser.add_argument("--no-open", action="store_true", help="不自动打开系统默认浏览器")
    args = parser.parse_args()

    state = secrets.token_urlsafe(32)
    server = RegistrationServer(("127.0.0.1", int(args.port)), state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        registration_url = f"http://127.0.0.1:{server.server_port}/"
        if not args.no_open:
            webbrowser.open(registration_url)
            print("已打开 GitHub App 注册页。请核对权限后点击 Create GitHub App。")
        else:
            print(f"REGISTRATION_URL={registration_url}", flush=True)
        if not server.done.wait(max(60, int(args.timeout))):
            raise RuntimeError("等待 GitHub 注册超时；没有写入任何配置")
        payload = _exchange(server.result["code"], str(args.proxy or "").strip())
        _client_id, slug = _write_public_config(payload)
        settings_url = str(payload.get("html_url") or f"https://github.com/settings/apps/{slug}")
        if not args.no_open:
            webbrowser.open(settings_url)
        print("APP_CONFIG_OK：公开 client_id 和 slug 已写入 runtime-assets.json。")
        print("请在刚打开的 App 设置页启用 Device Flow，并确认 App visibility 为 Any account。")
        print("GitHub 返回的 private key、client secret 和 webhook secret 已丢弃，未写入磁盘。")
        return 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
