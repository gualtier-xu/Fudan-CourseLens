"""MAC-PLATFORM：凭据后端钉。

Windows DPAPI 后端在 Windows CI 真跑（与根 credentials 模块字节兼容）；
macOS Keychain 后端以注入 runner 桩钉 argv 形状、令牌格式与错误映射。
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

import credentials as root_credentials
from src.platform import (
    MacOSKeychainCredentialsBackend,
    PlatformNotSupportedError,
    WindowsDPAPICredentialsBackend,
    get_credentials_backend,
)
from src.platform.credentials import (
    KEYCHAIN_SERVICE,
    KEYCHAIN_TOKEN_PREFIX,
    CredentialsBackend,
)


def _completed(returncode: int = 0, stdout: str = ""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


class RecordingRunner:
    def __init__(self, results=None):
        self.calls: list[list[str]] = []
        self._results = list(results or [])

    def __call__(self, argv):
        self.calls.append(argv)
        return self._results.pop(0) if self._results else _completed()


@unittest.skipIf(sys.platform != "win32", "Windows DPAPI 真跑仅限 Windows")
class WindowsDPAPIBackendRealTests(unittest.TestCase):
    def test_roundtrip_uses_current_v2_format(self):
        backend = WindowsDPAPICredentialsBackend()
        token = backend.protect(" courselen-secret-钉 ".encode("utf-8"))
        self.assertTrue(token.startswith(root_credentials.CIPHERTEXT_V2_PREFIX))
        self.assertEqual(backend.unprotect(token), " courselen-secret-钉 ".encode("utf-8"))

    def test_byte_compatible_with_root_credentials_module(self):
        backend = WindowsDPAPICredentialsBackend()
        root_token = root_credentials._protect(b"cross-compat")
        self.assertEqual(backend.unprotect(root_token), b"cross-compat")
        backend_token = backend.protect(b"cross-compat-2")
        self.assertEqual(root_credentials._unprotect(backend_token), b"cross-compat-2")

    def test_rejects_non_bytes(self):
        backend = WindowsDPAPICredentialsBackend()
        with self.assertRaises(TypeError):
            backend.protect("not-bytes")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            backend.unprotect("")


class WindowsDPAPIBackendGuardTests(unittest.TestCase):
    def test_guard_raises_before_importing_root_module_on_other_platform(self):
        backend = WindowsDPAPICredentialsBackend()
        with patch("sys.platform", "darwin"):
            with self.assertRaises(PlatformNotSupportedError):
                backend.protect(b"x")
            with self.assertRaises(PlatformNotSupportedError):
                backend.unprotect("cp:v2:whatever")


class MacOSKeychainBackendStubTests(unittest.TestCase):
    """无真机 CI：平台桩（darwin）+ runner 桩双管齐下，只钉逻辑。"""

    def setUp(self):
        platform_stub = patch("sys.platform", "darwin")
        platform_stub.start()
        self.addCleanup(platform_stub.stop)

    def _backend(self, runner):
        return MacOSKeychainCredentialsBackend(subprocess_runner=runner)

    def test_protocol_conformance(self):
        self.assertIsInstance(WindowsDPAPICredentialsBackend(), CredentialsBackend)
        self.assertIsInstance(MacOSKeychainCredentialsBackend(), CredentialsBackend)

    def test_protect_uses_add_generic_password_and_returns_kc_token(self):
        runner = RecordingRunner()
        token = self._backend(runner).protect(b"keychain-secret")
        self.assertTrue(token.startswith(KEYCHAIN_TOKEN_PREFIX))
        handle = token[len(KEYCHAIN_TOKEN_PREFIX):]
        self.assertEqual(len(handle), 32)
        self.assertTrue(all(char.isalnum() for char in handle))
        self.assertEqual(
            runner.calls[0],
            [
                "security", "add-generic-password",
                "-s", KEYCHAIN_SERVICE,
                "-a", handle,
                "-l", "Fudan CourseLens saved credential",
                "-w", "keychain-secret",
                "-U",
            ],
        )

    def test_unprotect_uses_find_generic_password(self):
        runner = RecordingRunner([_completed(0, "stored-value\n")])
        plaintext = self._backend(runner).unprotect(KEYCHAIN_TOKEN_PREFIX + "abc123")
        self.assertEqual(plaintext, b"stored-value")
        self.assertEqual(
            runner.calls[0],
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", "abc123", "-w"],
        )

    def test_unprotect_missing_item_raises_keyerror(self):
        runner = RecordingRunner([_completed(44, "")])
        with self.assertRaises(KeyError):
            self._backend(runner).unprotect(KEYCHAIN_TOKEN_PREFIX + "gone")

    def test_delete_reports_honest_result(self):
        ok_runner = RecordingRunner([_completed(0, "")])
        self.assertTrue(self._backend(ok_runner).delete(KEYCHAIN_TOKEN_PREFIX + "abc"))
        miss_runner = RecordingRunner([_completed(44, "")])
        self.assertFalse(self._backend(miss_runner).delete(KEYCHAIN_TOKEN_PREFIX + "abc"))

    def test_malformed_tokens_rejected(self):
        backend = self._backend(RecordingRunner())
        with self.assertRaises(ValueError):
            backend.unprotect("cp:v2:not-a-keychain-token")
        with self.assertRaises(ValueError):
            backend.unprotect(KEYCHAIN_TOKEN_PREFIX + "bad handle!")
        with self.assertRaises(ValueError):
            backend.unprotect("")

    def test_protect_rejects_empty_payload(self):
        with self.assertRaises(ValueError):
            self._backend(RecordingRunner()).protect(b"")

    def test_platform_guard_blocks_subprocess_on_other_host(self):
        runner = RecordingRunner()
        with patch("sys.platform", "win32"):
            backend = MacOSKeychainCredentialsBackend(subprocess_runner=runner)
            with self.assertRaises(PlatformNotSupportedError):
                backend.protect(b"x")
            with self.assertRaises(PlatformNotSupportedError):
                backend.unprotect(KEYCHAIN_TOKEN_PREFIX + "abc")
        self.assertEqual(runner.calls, [])

    def test_keychain_failure_raises_instead_of_fake_success(self):
        runner = RecordingRunner([_completed(1, "")])
        with self.assertRaises(RuntimeError):
            self._backend(runner).protect(b"x")

    def test_factory_macos_backend_matches(self):
        with patch("sys.platform", "darwin"):
            self.assertIsInstance(
                get_credentials_backend(), MacOSKeychainCredentialsBackend
            )


if __name__ == "__main__":
    unittest.main()
