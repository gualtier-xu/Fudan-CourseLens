"""MAC-PLATFORM：数据目录后端钉（LOCALAPPDATA vs ~/Library/Application Support）。"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from src.platform import (
    MACOS,
    WINDOWS,
    PlatformNotSupportedError,
    data_dir,
    platform_paths,
)


class DataDirPins(unittest.TestCase):
    def test_env_override_wins_over_every_platform_default(self):
        override = str(Path(os.sep + "tmp") / "courselens-test-data")
        for platform in (WINDOWS, MACOS):
            with self.subTest(platform=platform):
                paths = platform_paths(platform, environ={"COURSELENS_DATA_DIR": override})
                self.assertEqual(paths.data_dir, Path(override).resolve())

    def test_windows_uses_localappdata(self):
        paths = platform_paths(
            WINDOWS,
            environ={"LOCALAPPDATA": str(Path(os.sep + "tmp") / "local-roam")},
        )
        self.assertEqual(
            paths.data_dir, Path(os.sep + "tmp") / "local-roam" / "CourseLens"
        )

    def test_windows_falls_back_without_localappdata(self):
        paths = platform_paths(WINDOWS, environ={})
        self.assertEqual(
            paths.data_dir,
            Path.home() / "AppData" / "Local" / "CourseLens",
        )

    def test_macos_uses_application_support(self):
        paths = platform_paths(MACOS, environ={"HOME": str(Path(os.sep + "tmp") / "mac-home")})
        self.assertEqual(
            paths.data_dir,
            Path(os.sep + "tmp") / "mac-home" / "Library" / "Application Support" / "CourseLens",
        )

    def test_macos_falls_back_without_home(self):
        paths = platform_paths(MACOS, environ={})
        self.assertEqual(
            paths.data_dir,
            Path.home() / "Library" / "Application Support" / "CourseLens",
        )

    def test_managed_file_names_match_existing_layout(self):
        paths = platform_paths(WINDOWS, environ={"LOCALAPPDATA": os.sep + "x"})
        self.assertEqual(paths.credentials_file.name, "credentials.json")
        self.assertEqual(paths.instance_lock_file.name, "instance.lock")
        self.assertEqual(paths.instance_evidence_file.name, "server-instance.json")
        self.assertEqual(paths.subdir("logs").name, "logs")

    def test_unknown_platform_fails_closed(self):
        with self.assertRaises(PlatformNotSupportedError):
            data_dir("linux", environ={})

    def test_real_call_on_this_host_never_raises_or_touches_disk(self):
        # 本机真跑（Windows CI=windows 路径）：只描述路径，不做任何 IO。
        resolved = data_dir(environ={})
        self.assertIsInstance(resolved, Path)


if __name__ == "__main__":
    unittest.main()
