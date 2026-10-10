"""Clear CourseLens-only browser state for the default loopback origin."""

from __future__ import annotations

import argparse
import html
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


DEFAULT_PORT = 8765


def _page() -> bytes:
    return b"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CourseLens local site reset</title>
</head>
<body>
  <main>
    <h1 id="status">Resetting CourseLens local site state...</h1>
    <p id="detail">Only CourseLens UI preferences on this loopback origin are removed.</p>
  </main>
  <script>
    (async () => {
      const exactKeys = new Set([
        "courselens.theme.v2",
        "courselens.course-order.v1",
        "courselens.catalog-term.v1",
        "courselens_ai_skipped",
        "fudan_courselens_ui_v1",
        "fudan_icource_local_ui_v1",
        "video_archiver_ui_v3",
        "video_archiver_ui_v2",
        "video_archiver_course_ids",
        "fudan_courselens_player_settings_v1",
        "fudan_courselens_smart_playback_v1"
      ]);
      const matches = (key) => exactKeys.has(key)
        || key.startsWith("courselens_")
        || key.startsWith("fudan_courselens_")
        || key.startsWith("fudan_icource_")
        || key.startsWith("video_archiver_");
      for (const storage of [localStorage, sessionStorage]) {
        for (let index = storage.length - 1; index >= 0; index -= 1) {
          const key = storage.key(index);
          if (key && matches(key)) storage.removeItem(key);
        }
      }
      document.getElementById("status").textContent = "CourseLens local site state was cleared";
      document.getElementById("detail").textContent = "No values were read or displayed, and unrelated site data was not changed. You may close this page.";
      document.documentElement.dataset.resetComplete = "true";
      try { await fetch("/__courselens_reset_complete__", {method: "POST", cache: "no-store"}); } catch (_) {}
    })();
  </script>
</body>
</html>
"""


def serve_reset(*, port: int, timeout: float, open_browser: bool) -> dict[str, object]:
    complete = threading.Event()
    page = _page()

    class Handler(BaseHTTPRequestHandler):
        def _headers(self, status: int, content_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; connect-src 'self'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
            if self.path not in {"/", "/index.html"}:
                body = b"not found"
                self._headers(404, "text/plain; charset=utf-8", len(body))
                self.wfile.write(body)
                return
            self._headers(200, "text/html; charset=utf-8", len(page))
            self.wfile.write(page)

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            if self.path != "/__courselens_reset_complete__":
                body = b"not found"
                self._headers(404, "text/plain; charset=utf-8", len(body))
                self.wfile.write(body)
                return
            body = b"ok"
            self._headers(200, "text/plain; charset=utf-8", len(body))
            self.wfile.write(body)
            complete.set()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    try:
        server = ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
    except OSError as exc:
        raise RuntimeError("The CourseLens browser-reset port is unavailable") from exc
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{int(port)}/"
    try:
        if open_browser and not webbrowser.open(url, new=1):
            raise RuntimeError("Could not open the browser reset page")
        if not complete.wait(max(1.0, float(timeout))):
            raise RuntimeError("Browser state reset was not confirmed before timeout")
        return {"ok": True, "origin": f"http://127.0.0.1:{int(port)}"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    try:
        result = serve_reset(
            port=args.port,
            timeout=args.timeout,
            open_browser=not args.no_open,
        )
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": html.escape(str(exc))}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
