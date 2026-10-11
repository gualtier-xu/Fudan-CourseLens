"""MAC-CRED：凭据层跨平台钉。

三份合同：
- 模块在模拟 macOS 解释器下必须可导入（旧顶层 ``ctypes.wintypes`` 导入
  曾让整个客户端在非 Windows 上一 importing 就崩）；无后端时一律闭集
  fail-closed，绝不静默降级。
- Windows DPAPI 语义零回退：v2 往返、BLOB 字段布局、v1 兼容分发逐字节
  保持，且 DPAPI 路径永不触碰 Keychain 解析器。
- Keychain 令牌路由以桩后端钉死（真 Keychain 零接触），信封层整链在
  桩后端上可往返。
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import credentials
from credentials import CredentialStore

ROOT = Path(__file__).resolve().parents[1]

_SIMULATED_MAC_SNIPPET = """
import sys

sys.platform = "darwin"  # 先于导入：旧代码在此刻就会因 wintypes 导入崩掉
sys.path.insert(0, r"{root}")

import importlib
import os

module = importlib.import_module("credentials")
assert callable(module.CredentialStore)
assert module._DATA_BLOB_TYPE is None, "wintypes must stay unresolved at import scope"

# 导入后再伪装非 Windows 系统层（ctypes 已加载，此刻改 os.name 安全）
os.name = "posix"
# MAC-NIGHT-1：path_utils→数据目录接线后，src.platform.* 在导入期已被
# 真实缓存；「平台后端包不存在的构建」模拟须置空全部相关 sys.modules
# 键（只置空 "src" 不再足够，缓存命中会让桩环境误入真 security 调用）。
for _name in [n for n in sys.modules if n == "src" or n.startswith("src.platform")]:
    sys.modules[_name] = None

for call in (
    lambda: module._protect(b"payload"),
    lambda: module._unprotect("kc:v1:abc123"),
):
    try:
        call()
    except RuntimeError as exc:
        assert "macOS Keychain" in str(exc), exc
    else:
        raise SystemExit("credential calls must fail closed without the backend")

print("MAC-IMPORT-OK")
""".replace("{root}", str(ROOT))


class _StubKeychainBackend:
    """``src.platform.credentials`` Keychain 后端的闭集桩。"""

    name = "stub-keychain"

    def __init__(self):
        self.vault: dict[str, bytes] = {}
        self.protect_calls: list[bytes] = []
        self.unprotect_calls: list[str] = []

    def protect(self, data: bytes) -> str:
        self.protect_calls.append(bytes(data))
        handle = f"stub-handle-{len(self.vault) + 1:04d}"
        self.vault[handle] = bytes(data)
        return "kc:v1:" + handle

    def unprotect(self, token: str) -> bytes:
        self.unprotect_calls.append(token)
        handle = token[len("kc:v1:"):]
        if handle not in self.vault:
            raise KeyError("Saved keychain credential not found or unreadable")
        return self.vault[handle]


class SimulatedMacImportTests(unittest.TestCase):
    """模拟 darwin 解释器：导入必须成功，凭据调用必须闭集失败。"""

    def test_credentials_imports_with_simulated_darwin_interpreter(self):
        completed = subprocess.run(
            [sys.executable, "-c", _SIMULATED_MAC_SNIPPET],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"stdout={completed.stdout}\nstderr={completed.stderr}",
        )
        self.assertIn("MAC-IMPORT-OK", completed.stdout)


class WindowsZeroDriftTests(unittest.TestCase):
    """Windows DPAPI 行为零变化钉（真 DPAPI，仅 Windows 跑）。"""

    def setUp(self):
        if sys.platform != "win32":
            self.skipTest("Windows DPAPI 真跑仅限 Windows")

    def test_v2_roundtrip_unchanged(self):
        payload = " courselen-zero-drift-钉 ".encode("utf-8")
        token = credentials._protect(payload)
        self.assertTrue(token.startswith(credentials.CIPHERTEXT_V2_PREFIX))
        self.assertEqual(credentials._unprotect(token), payload)

    def test_legacy_v1_roundtrip_unchanged_after_keychain_dispatch(self):
        legacy = credentials._protect_legacy_v1(b"v1-payload")
        self.assertFalse(legacy.startswith(credentials.CIPHERTEXT_V2_PREFIX))
        self.assertEqual(credentials._unprotect(legacy), b"v1-payload")

    def test_dpapi_blob_layout_is_identical_to_legacy_definition(self):
        from ctypes import wintypes

        blob_type = credentials._data_blob_type()
        self.assertIs(credentials._data_blob_type(), blob_type)  # 缓存生效
        fields = blob_type._fields_
        self.assertEqual([name for name, _ in fields], ["cbData", "pbData"])
        self.assertIs(fields[0][1], wintypes.DWORD)
        self.assertEqual(fields[1][1], ctypes.POINTER(ctypes.c_char))

    def test_windows_v2_tokens_never_consult_keychain_backend(self):
        token = credentials._protect(b"windows-only-payload")

        def _bomb():
            raise AssertionError("keychain resolver must not run on the DPAPI path")

        with patch.object(credentials, "_macos_keychain_backend", _bomb):
            self.assertEqual(credentials._unprotect(token), b"windows-only-payload")

    def test_dpapi_guard_message_unchanged_off_windows(self):
        with patch("os.name", "posix"):
            with self.assertRaisesRegex(
                RuntimeError, "Saved credentials require Windows DPAPI"
            ):
                credentials._dpapi_protect(b"payload", None)
            with self.assertRaisesRegex(
                RuntimeError, "Saved credentials require Windows DPAPI"
            ):
                credentials._dpapi_unprotect("cp:v2:aa", None)


class KeychainRoutingTests(unittest.TestCase):
    """Keychain 令牌路由与闭集降级（桩后端，全平台可跑）。"""

    def test_resolver_gate_is_platform_conditional(self):
        with patch("sys.platform", "linux"):
            self.assertIsNone(credentials._macos_keychain_backend())

    def test_darwin_without_platform_package_fails_closed(self):
        saved = {
            name: sys.modules.get(name)
            for name in ("src", "src.platform", "src.platform.credentials")
        }
        try:
            for name in saved:
                sys.modules[name] = None  # 阻断惰性导入
            with patch("sys.platform", "darwin"), patch("os.name", "posix"):
                self.assertIsNone(credentials._macos_keychain_backend())
                with self.assertRaisesRegex(RuntimeError, "macOS Keychain"):
                    credentials._protect(b"payload")
                with self.assertRaisesRegex(RuntimeError, "macOS Keychain"):
                    credentials._unprotect("kc:v1:abc123")
        finally:
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

    def test_keychain_token_without_backend_fails_closed(self):
        with patch.object(credentials, "_macos_keychain_backend", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "macOS Keychain"):
                credentials._unprotect("kc:v1:abc123")

    def test_unprotect_routes_keychain_tokens_to_backend(self):
        stub = _StubKeychainBackend()
        handle = stub.protect(b"routed-payload")
        with patch.object(credentials, "_macos_keychain_backend", return_value=stub):
            self.assertEqual(credentials._unprotect(handle), b"routed-payload")
        self.assertEqual(stub.unprotect_calls, [handle])

    def test_protect_routes_to_backend_when_not_windows(self):
        stub = _StubKeychainBackend()
        with patch.object(credentials, "_is_windows", return_value=False), patch.object(
            credentials, "_macos_keychain_backend", return_value=stub
        ):
            token = credentials._protect(b"mac-payload")
        self.assertTrue(token.startswith("kc:v1:"))
        self.assertEqual(stub.protect_calls, [b"mac-payload"])

    def test_keychain_tokens_are_never_migrated(self):
        holders_called: list[int] = []

        def holder(data):
            holders_called.append(1)
            return {"value": "kc:v1:abc"}

        scratch = ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            store = CredentialStore(Path(directory) / "credentials.json")
            store._migrate_legacy_v1_token(
                holder, "value", "kc:v1:abc", b"payload"
            )
        self.assertEqual(holders_called, [])

    def test_envelope_roundtrip_over_keychain_stub(self):
        stub = _StubKeychainBackend()
        scratch = ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            store = CredentialStore(Path(directory) / "credentials.json")
            with patch.object(
                credentials, "_is_windows", return_value=False
            ), patch.object(
                credentials, "_macos_keychain_backend", return_value=stub
            ):
                store.save("30301", "stub-password-xyz")
                store.save_deepseek_key("sk-stub-123")
                store.save_secret("integration.token", "stub-secret-value")
                raw = store.path.read_text(encoding="utf-8")
                self.assertEqual(
                    store.load("30301"), ("30301", "stub-password-xyz")
                )
                self.assertEqual(store.load_deepseek_key(), "sk-stub-123")
                self.assertEqual(
                    store.load_secret("integration.token"), "stub-secret-value"
                )
        self.assertIn("kc:v1:", raw)
        self.assertNotIn("stub-password-xyz", raw)
        self.assertNotIn("sk-stub-123", raw)
        self.assertNotIn("stub-secret-value", raw)


if __name__ == "__main__":
    unittest.main()
