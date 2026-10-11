"""MAC-PLATFORM：自启/每日任务后端钉。

Windows schtasks 后端钉「委托既有 scheduler_windows、零复制」；macOS
launchd 后端钉 plist 内容（纯函数）与 launchctl argv（注入 runner 桩）。
"""

from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from src.platform import (
    MacOSLaunchdAutostartBackend,
    PlatformNotSupportedError,
    WindowsSchtasksAutostartBackend,
    get_autostart_backend,
)
from src.platform.autostart import (
    LAUNCHD_LABEL,
    LAUNCHD_PLIST_NAME,
    render_launchd_plist,
    shlex_split_for_plist,
)
from src.runtime import scheduler_windows


def _completed(returncode: int = 0):
    return types.SimpleNamespace(returncode=returncode, stdout="", stderr="")


class WindowsSchtasksBackendTests(unittest.TestCase):
    def test_register_delegates_to_existing_scheduler(self):
        captured = {}

        def fake_register(**kwargs):
            captured.update(kwargs)
            return {"task_name": scheduler_windows.TASK_NAME, "enabled": True}

        backend = WindowsSchtasksAutostartBackend()
        with patch("sys.platform", "win32"), patch.object(
            scheduler_windows, "register_daily_task", fake_register
        ):
            result = backend.register(
                command="python -m src", at="07:30",
                arguments="--daily", working_directory="C:/courselens",
            )
        self.assertEqual(
            captured,
            {
                "command": "python -m src", "at": "07:30", "enabled": True,
                "arguments": "--daily", "working_directory": "C:/courselens",
            },
        )
        self.assertEqual(result["task_name"], scheduler_windows.TASK_NAME)

    def test_unregister_delegates_with_enabled_false(self):
        captured = {}

        def fake_register(**kwargs):
            captured.update(kwargs)
            return {"task_name": scheduler_windows.TASK_NAME, "enabled": False}

        backend = WindowsSchtasksAutostartBackend()
        with patch("sys.platform", "win32"), patch.object(
            scheduler_windows, "register_daily_task", fake_register
        ):
            result = backend.unregister()
        self.assertIs(captured["enabled"], False)
        self.assertFalse(result["registered"])

    def test_guard_rejects_other_platform(self):
        backend = WindowsSchtasksAutostartBackend()
        with patch("sys.platform", "darwin"):
            with self.assertRaises(PlatformNotSupportedError):
                backend.register(command="x")


class LaunchdPlistRenderingTests(unittest.TestCase):
    def test_plist_content_pins(self):
        plist = render_launchd_plist(command="/usr/bin/python3 -m src", at="07:30")
        self.assertIn(f"<string>{LAUNCHD_LABEL}</string>", plist)
        self.assertIn("<key>StartCalendarInterval</key>", plist)
        self.assertIn("<integer>7</integer>", plist)
        self.assertIn("<integer>30</integer>", plist)
        self.assertIn("<key>RunAtLoad</key>", plist)
        self.assertIn("<false/>", plist)
        self.assertIn("/usr/bin/python3 -m src", plist)
        self.assertIn("<!DOCTYPE plist", plist)

    def test_arguments_are_shlex_split_into_array_items(self):
        parts = shlex_split_for_plist("--daily-sync --label '早 课 同 步'")
        self.assertEqual(parts, ["--daily-sync", "--label", "早 课 同 步"])
        plist = render_launchd_plist(
            command="python3", arguments="--flag 'quoted value'"
        )
        self.assertIn("<string>--flag</string>", plist)
        self.assertIn("<string>quoted value</string>", plist)

    def test_xml_escaping(self):
        plist = render_launchd_plist(command="cmd && echo <ok>")
        self.assertIn("cmd &amp;&amp; echo &lt;ok&gt;", plist)

    def test_working_directory_included_when_given(self):
        with_dir = render_launchd_plist(command="x", working_directory="/Users/m/app")
        self.assertIn("<key>WorkingDirectory</key>", with_dir)
        without_dir = render_launchd_plist(command="x")
        self.assertNotIn("WorkingDirectory", without_dir)


class MacOSLaunchdBackendStubTests(unittest.TestCase):
    """无真机 CI：平台桩（darwin）+ runner/base_dir 桩，只钉逻辑。"""

    def setUp(self):
        platform_stub = patch("sys.platform", "darwin")
        platform_stub.start()
        self.addCleanup(platform_stub.stop)

    def _backend(self, base: Path, calls: list):
        def runner(argv):
            calls.append(argv)
            return _completed(0)

        return MacOSLaunchdAutostartBackend(base_dir=base, subprocess_runner=runner)

    def test_register_writes_plist_and_loads(self):
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            backend = self._backend(base, calls)
            result = backend.register(command="/usr/bin/python3 -m src", at="06:45")
            plist_path = base / "Library" / "LaunchAgents" / LAUNCHD_PLIST_NAME
            self.assertTrue(plist_path.is_file())
            self.assertIn("<integer>6</integer>", plist_path.read_text(encoding="utf-8"))
            self.assertEqual(calls, [["launchctl", "load", str(plist_path)]])
            self.assertTrue(result["registered"])

    def test_register_launchctl_failure_raises_not_fake_success(self):
        calls: list = []

        def failing_runner(argv):
            calls.append(argv)
            return _completed(1)

        with tempfile.TemporaryDirectory() as tmp:
            backend = MacOSLaunchdAutostartBackend(
                base_dir=Path(tmp), subprocess_runner=failing_runner
            )
            with self.assertRaises(RuntimeError):
                backend.register(command="x")
        self.assertEqual(len(calls), 1)

    def test_register_empty_command_rejected_before_any_write(self):
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(Path(tmp), calls)
            with self.assertRaises(ValueError):
                backend.register(command="  ")
        self.assertEqual(calls, [])
        self.assertFalse((Path(tempfile.gettempdir()) / "must-not-exist").exists())

    def test_unregister_unloads_and_removes_plist(self):
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            backend = self._backend(base, calls)
            backend.register(command="x")
            result = backend.unregister()
            plist_path = base / "Library" / "LaunchAgents" / LAUNCHD_PLIST_NAME
            self.assertFalse(plist_path.exists())
            self.assertEqual(calls[1], ["launchctl", "unload", str(plist_path)])
            self.assertFalse(result["registered"])

    def test_unregister_without_plist_is_safe(self):
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(Path(tmp), calls)
            result = backend.unregister()
        self.assertEqual(calls, [])
        self.assertFalse(result["registered"])

    def test_status_reflects_plist_presence(self):
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(Path(tmp), calls)
            self.assertFalse(backend.status()["plist_exists"])
            backend.register(command="x")
            self.assertTrue(backend.status()["plist_exists"])

    def test_guard_blocks_subprocess_on_other_host(self):
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            backend = MacOSLaunchdAutostartBackend(
                base_dir=Path(tmp),
                subprocess_runner=lambda argv: (calls.append(argv), _completed(0))[1],
            )
            with patch("sys.platform", "win32"):
                with self.assertRaises(PlatformNotSupportedError):
                    backend.register(command="x")
                with self.assertRaises(PlatformNotSupportedError):
                    backend.unregister()
        self.assertEqual(calls, [])

    def test_factory_returns_launchd_on_macos(self):
        with patch("sys.platform", "darwin"):
            self.assertIsInstance(get_autostart_backend(), MacOSLaunchdAutostartBackend)


if __name__ == "__main__":
    unittest.main()
