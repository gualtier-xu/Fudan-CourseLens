"""凭据加密后端：Windows DPAPI（现逻辑适配）vs macOS Keychain（security CLI）。

契约与根目录 ``credentials.py`` 的信封存储对齐：后端只负责「字节 → 令牌
字符串」的封印/开启，JSON 信封（accounts/secrets/session_checkpoints）的
形状、锁与迁移逻辑全部留在既有 ``CredentialStore``，接线时无需改形状。

- Windows 后端：懒加载复用根 ``credentials`` 模块的现役 DPAPI 实现（v2
  熵与 ``cp:v2:`` 令牌字节不变，零复制零漂移）。必须先做平台守卫再导入——
  该模块顶部的 ``ctypes.wintypes`` 在 macOS 上导入即失败。
- macOS 后端：系统 ``security`` 命令行读写 Keychain 通用密码；数据本体
  存在 Keychain 内，令牌 ``kc:v1:<handle>`` 只携带非敏感的账号句柄。
"""

from __future__ import annotations

import secrets as _secrets
import subprocess
import sys
from typing import Protocol, runtime_checkable

from src.platform._core import MACOS, WINDOWS, PlatformNotSupportedError, current_platform

KEYCHAIN_TOKEN_PREFIX = "kc:v1:"
KEYCHAIN_SERVICE = "FudanCourseLens"
_KEYCHAIN_LABEL = "Fudan CourseLens saved credential"


@runtime_checkable
class CredentialsBackend(Protocol):
    """信封存储依赖的最小封印/开启面。"""

    name: str

    def protect(self, data: bytes) -> str:
        """封印字节 → 可持久化令牌字符串（令牌本身不含明文）。"""
        ...

    def unprotect(self, token: str) -> bytes:
        """开启令牌 → 原始字节；任何疑点抛异常（fail-closed）。"""
        ...


class WindowsDPAPICredentialsBackend:
    """现役 DPAPI 逻辑的适配壳：令牌格式与根 credentials 模块字节一致。"""

    name = "windows-dpapi"

    def _root(self):
        if sys.platform != "win32":
            raise PlatformNotSupportedError(
                "windows-dpapi credentials backend requires Windows"
            )
        import credentials as root_credentials

        return root_credentials

    def protect(self, data: bytes) -> str:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("protect expects bytes")
        return self._root()._protect(bytes(data))

    def unprotect(self, token: str) -> bytes:
        if not isinstance(token, str) or not token:
            raise ValueError("credential token is required")
        return self._root()._unprotect(token)


class MacOSKeychainCredentialsBackend:
    """macOS Keychain 通用密码后端（security 命令行，零第三方依赖）。

    ``subprocess_runner`` 注入仅供测试打桩；生产恒用 subprocess.run。
    Keychain 条目：service=FudanCourseLens，account=随机句柄；删除信封
    条目时应调用 :meth:`delete` 清理 Keychain 侧孤儿数据。
    """

    name = "macos-keychain"

    def __init__(self, *, subprocess_runner=None):
        self._run = subprocess_runner or self._subprocess_run

    @staticmethod
    def _subprocess_run(argv: list[str]):
        return subprocess.run(
            argv, capture_output=True, text=True, check=False,
        )

    def _require_macos(self) -> None:
        if current_platform() != MACOS:
            raise PlatformNotSupportedError(
                "macos-keychain credentials backend requires macOS"
            )

    def protect(self, data: bytes) -> str:
        self._require_macos()
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ValueError("protect expects non-empty bytes")
        handle = _secrets.token_hex(16)
        completed = self._run([
            "security", "add-generic-password",
            "-s", KEYCHAIN_SERVICE,
            "-a", handle,
            "-l", _KEYCHAIN_LABEL,
            "-w", bytes(data).decode("utf-8", "surrogateescape"),
            "-U",
        ])
        if completed.returncode != 0:
            raise RuntimeError(
                f"keychain add-generic-password failed: {completed.returncode}"
            )
        return KEYCHAIN_TOKEN_PREFIX + handle

    def unprotect(self, token: str) -> bytes:
        self._require_macos()
        handle = self._parse_token(token)
        completed = self._run([
            "security", "find-generic-password",
            "-s", KEYCHAIN_SERVICE,
            "-a", handle,
            "-w",
        ])
        if completed.returncode != 0:
            raise KeyError("Saved keychain credential not found or unreadable")
        return completed.stdout.rstrip("\n").encode("utf-8", "surrogateescape")

    def delete(self, token: str) -> bool:
        self._require_macos()
        handle = self._parse_token(token)
        completed = self._run([
            "security", "delete-generic-password",
            "-s", KEYCHAIN_SERVICE,
            "-a", handle,
        ])
        return completed.returncode == 0

    @staticmethod
    def _parse_token(token: str) -> str:
        if not isinstance(token, str) or not token.startswith(KEYCHAIN_TOKEN_PREFIX):
            raise ValueError("credential token is not a keychain token")
        handle = token[len(KEYCHAIN_TOKEN_PREFIX):]
        if not handle or not all(char.isalnum() for char in handle):
            raise ValueError("credential token handle is malformed")
        return handle


def get_credentials_backend(platform: str | None = None) -> CredentialsBackend:
    """按平台返回凭据后端；未知平台 fail-closed。"""
    resolved = current_platform(platform)
    if resolved == WINDOWS:
        return WindowsDPAPICredentialsBackend()
    if resolved == MACOS:
        return MacOSKeychainCredentialsBackend()
    raise PlatformNotSupportedError(
        f"no credentials backend for platform: {resolved}"
    )


__all__ = [
    "CredentialsBackend",
    "KEYCHAIN_SERVICE",
    "KEYCHAIN_TOKEN_PREFIX",
    "MacOSKeychainCredentialsBackend",
    "WindowsDPAPICredentialsBackend",
    "get_credentials_backend",
]
