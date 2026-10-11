"""单实例互斥后端：Windows 命名互斥体 vs macOS flock 文件锁。

与既有 ``src.runtime.lifecycle.InstanceLock`` 的语义一致（只锁、不杀进程、
成功与否诚实返回），但把「拿锁原语」本身平台化：

- Windows：``CreateMutexW`` 命名互斥（与根 credentials 信封锁同口径），
  在 Windows CI 上可真跑。
- macOS：对锁文件 ``flock(LOCK_EX | LOCK_NB)``；``fcntl`` 不存在于
  Windows，故 flock 实现以注入函数承载，CI 无真机也能桩测逻辑。

证据文件（server-instance.json / PID 重用检测）留在既有 InstanceLock，
本层只负责互斥原语；接线由后续车道完成。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, Protocol

from src.platform._core import MACOS, WINDOWS, PlatformNotSupportedError, current_platform

_MUTEX_NAME_PREFIX = "Local\\CourseLensSingleInstance-"
_FLOCK_LOCK_OP = 6  # fcntl.LOCK_EX | fcntl.LOCK_NB；避免在 Windows 导入 fcntl
_FLOCK_UNLOCK_OP = 8  # fcntl.LOCK_UN


class SingleInstanceLock(Protocol):
    """单实例互斥最小面：acquire 诚实返回，绝不终止他人进程。"""

    name: str

    def acquire(self) -> bool:
        """拿到互斥 → True；已有实例在持 → False。"""
        ...

    def release(self) -> None:
        """释放本进程持有的互斥；未持有时调用是安全no-op。"""
        ...


class WindowsMutexSingleInstanceLock:
    """命名互斥体实现；Windows 上可真跑，其他平台守卫拒绝。"""

    def __init__(self, name: str):
        if not str(name or "").strip():
            raise ValueError("single instance name is required")
        self.name = str(name)
        self._handle = None

    def _mutex_name(self) -> str:
        digest = hashlib.sha256(self.name.encode("utf-8")).hexdigest()
        return _MUTEX_NAME_PREFIX + digest

    def _kernel32(self):
        if current_platform() != WINDOWS:
            raise PlatformNotSupportedError(
                "windows-mutex single instance backend requires Windows"
            )
        import ctypes

        return ctypes.windll.kernel32

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        kernel32 = self._kernel32()
        handle = kernel32.CreateMutexW(None, False, self._mutex_name())
        if not handle:
            return False
        wait_result = kernel32.WaitForSingleObject(handle, 0)
        if wait_result != 0:  # WAIT_ABANDONED/WAIT_TIMEOUT：已有实例在持
            kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        kernel32 = self._kernel32()
        kernel32.ReleaseMutex(self._handle)
        kernel32.CloseHandle(self._handle)
        self._handle = None


class MacOSFlockSingleInstanceLock:
    """flock 文件锁实现；flock 原语可注入（CI 桩测逻辑，无需真机）。"""

    def __init__(
        self,
        name: str,
        lock_path: str | Path,
        *,
        flock_fn: Callable[[int, int], None] | None = None,
    ):
        if not str(name or "").strip():
            raise ValueError("single instance name is required")
        self.name = str(name)
        self.lock_path = Path(lock_path)
        self._flock = flock_fn
        self._stream = None

    def _flock_impl(self):
        if self._flock is not None:
            return self._flock
        if current_platform() != MACOS:
            raise PlatformNotSupportedError(
                "macos-flock single instance backend requires macOS"
            )
        import fcntl

        return lambda fd, operation: fcntl.flock(fd, operation)

    def acquire(self) -> bool:
        if self._stream is not None:
            return True
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(self.lock_path, "a+b")
        try:
            self._flock_impl()(stream.fileno(), _FLOCK_LOCK_OP)
        except OSError:
            stream.close()
            return False
        self._stream = stream
        return True

    def release(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            self._flock_impl()(stream.fileno(), _FLOCK_UNLOCK_OP)
        except (PlatformNotSupportedError, OSError):
            pass
        finally:
            stream.close()


def acquire_single_instance(
    name: str,
    platform: str | None = None,
    *,
    lock_path: str | Path | None = None,
) -> SingleInstanceLock:
    """按平台构造互斥后端（不自动 acquire；调用方决定持锁时机）。"""
    resolved = current_platform(platform)
    if resolved == WINDOWS:
        return WindowsMutexSingleInstanceLock(name)
    if resolved == MACOS:
        path = Path(lock_path) if lock_path else Path.home() / ".courselens-instance.lock"
        return MacOSFlockSingleInstanceLock(name, path)
    raise PlatformNotSupportedError(
        f"no single-instance backend for platform: {resolved}"
    )


__all__ = [
    "MacOSFlockSingleInstanceLock",
    "SingleInstanceLock",
    "WindowsMutexSingleInstanceLock",
    "acquire_single_instance",
]
