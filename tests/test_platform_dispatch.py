"""MAC-PLATFORM：平台抽象层 sys.platform 分发钉（分发一律调用时读平台）。"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from src.platform import (
    MACOS,
    OTHER,
    WINDOWS,
    MacOSFlockSingleInstanceLock,
    MacOSKeychainCredentialsBackend,
    MacOSLaunchdAutostartBackend,
    PlatformNotSupportedError,
    WindowsDPAPICredentialsBackend,
    WindowsMutexSingleInstanceLock,
    WindowsSchtasksAutostartBackend,
    current_platform,
    get_autostart_backend,
    get_credentials_backend,
    acquire_single_instance,
)


class CurrentPlatformPins(unittest.TestCase):
    def test_explicit_platform_names(self):
        self.assertEqual(current_platform("win32"), WINDOWS)
        self.assertEqual(current_platform("darwin"), MACOS)
        self.assertEqual(current_platform("linux"), OTHER)

    def test_runtime_reads_sys_platform_at_call_time(self):
        with patch("sys.platform", "darwin"):
            self.assertEqual(current_platform(), MACOS)
        with patch("sys.platform", "win32"):
            self.assertEqual(current_platform(), WINDOWS)

    def test_unsupported_error_is_runtime_error(self):
        self.assertTrue(issubclass(PlatformNotSupportedError, RuntimeError))


class CredentialsDispatchPins(unittest.TestCase):
    def test_windows_backend(self):
        backend = get_credentials_backend("win32")
        self.assertIsInstance(backend, WindowsDPAPICredentialsBackend)

    def test_macos_backend(self):
        backend = get_credentials_backend("darwin")
        self.assertIsInstance(backend, MacOSKeychainCredentialsBackend)

    def test_other_platform_fails_closed(self):
        with self.assertRaises(PlatformNotSupportedError):
            get_credentials_backend("linux")

    def test_dispatch_uses_call_time_platform(self):
        with patch("sys.platform", "darwin"):
            self.assertIsInstance(get_credentials_backend(), MacOSKeychainCredentialsBackend)


class SingleInstanceDispatchPins(unittest.TestCase):
    def test_windows_backend(self):
        lock = acquire_single_instance("courselens-test", "win32")
        self.assertIsInstance(lock, WindowsMutexSingleInstanceLock)

    def test_macos_backend_with_explicit_lock_path(self):
        expected = Path("/tmp/x/instance.lock")
        lock = acquire_single_instance(
            "courselens-test", "darwin", lock_path=expected
        )
        self.assertIsInstance(lock, MacOSFlockSingleInstanceLock)
        self.assertEqual(lock.lock_path, expected)

    def test_other_platform_fails_closed(self):
        with self.assertRaises(PlatformNotSupportedError):
            acquire_single_instance("courselens-test", "linux")


class AutostartDispatchPins(unittest.TestCase):
    def test_windows_backend(self):
        backend = get_autostart_backend("win32")
        self.assertIsInstance(backend, WindowsSchtasksAutostartBackend)

    def test_macos_backend(self):
        backend = get_autostart_backend("darwin")
        self.assertIsInstance(backend, MacOSLaunchdAutostartBackend)

    def test_other_platform_fails_closed(self):
        with self.assertRaises(PlatformNotSupportedError):
            get_autostart_backend("linux")

    def test_dispatch_uses_call_time_platform(self):
        with patch("sys.platform", "darwin"):
            self.assertIsInstance(get_autostart_backend(), MacOSLaunchdAutostartBackend)


if __name__ == "__main__":
    unittest.main()
