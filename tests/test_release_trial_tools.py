from __future__ import annotations

import json
import subprocess
import sys
import threading
import unittest
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BROWSER_RESET = ROOT / "scripts" / "reset_courselens_browser_state.py"


class ReleaseTrialToolTests(unittest.TestCase):
    def test_sandbox_bundle_is_git_only_and_privacy_hardened(self) -> None:
        generator = (ROOT / "scripts" / "windows-sandbox" / "New-CourseLensSandbox.ps1").read_text(encoding="utf-8")
        bootstrap = (ROOT / "scripts" / "windows-sandbox" / "SandboxBootstrap.ps1").read_text(encoding="utf-8")
        self.assertIn("git -C $ProjectRoot archive", generator)
        self.assertIn("<ReadOnly>true</ReadOnly>", generator)
        self.assertIn("<ClipboardRedirection>Disable</ClipboardRedirection>", generator)
        self.assertIn('source_has_runtime', bootstrap)
        self.assertIn('source_has_downloads', bootstrap)
        self.assertIn('OpenFudanCourseLens.cmd', bootstrap)

    def test_browser_reset_requires_page_confirmation_and_uses_scoped_keys(self) -> None:
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        process = subprocess.Popen(
            [
                sys.executable,
                # MAC-NIGHT-1 round4：-u=服务就绪输出即时成像（诊断面）。
                "-u",
                str(BROWSER_RESET),
                "--no-open",
                "--port",
                str(port),
                "--timeout",
                "10",
            ],
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            page = b""
            # MAC-NIGHT-1：deadline 制就绪轮询——旧版固定 50 次快轮询在
            # connection-refused 即时失败下总窗只有 ~1s，macOS 冷启动
            # （解释器+导入+可能的服务器 bind 反查延迟）稳定超窗。45s 窗口
            # 对齐生命周期子进程 45s 口径；子进程提前退出立即放行给下方
            # 断言（带 stderr）。
            import time as _time

            ready_deadline = _time.monotonic() + 45.0
            while _time.monotonic() < ready_deadline:
                if process.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.2) as response:
                        page = response.read()
                    break
                except OSError:
                    threading.Event().wait(0.05)
            self.assertIn(b"fudan_courselens_", page)
            self.assertNotIn(b"localStorage.clear", page)
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/__courselens_reset_complete__",
                data=b"",
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=1) as response:
                self.assertEqual(response.read(), b"ok")
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertTrue(json.loads(stdout)["ok"])
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
