"""MAC-PLATFORM：单实例互斥后端钉。

Windows 命名互斥在 Windows CI 真跑（真进程内争用）；macOS flock 以注入
flock 原语桩钉争用逻辑（fd → (dev, ino) 识别同一文件，无需真机）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from src.platform import (
    MacOSFlockSingleInstanceLock,
    PlatformNotSupportedError,
    WindowsMutexSingleInstanceLock,
    acquire_single_instance,
)


class _FakeFlock:
    """按 (dev, ino) 识别同一文件的 flock 语义桩：独占、非阻塞、可解锁。"""

    def __init__(self):
        self.locked: set[tuple[int, int]] = set()
        self.operations: list[tuple[int, int]] = []

    def __call__(self, fd: int, operation: int) -> None:
        stat = os.fstat(fd)
        key = (stat.st_dev, stat.st_ino)
        self.operations.append((fd, operation))
        if operation == 8:  # LOCK_UN
            self.locked.discard(key)
            return
        if key in self.locked:
            raise OSError(11, "Resource temporarily unavailable")
        self.locked.add(key)


@unittest.skipIf(sys.platform != "win32", "Windows 命名互斥真跑仅限 Windows")
class WindowsMutexRealTests(unittest.TestCase):
    def test_second_holder_in_other_thread_is_rejected_until_release(self):
        # Windows 互斥体是线程属主语义：同线程可重入，跨线程才见争用。
        name = f"courselens-platform-test-{uuid.uuid4().hex}"
        first = WindowsMutexSingleInstanceLock(name)
        second = WindowsMutexSingleInstanceLock(name)
        self.assertTrue(first.acquire())
        results: list[bool] = []
        holder = threading.Thread(target=lambda: results.append(second.acquire()))
        holder.start()
        holder.join()
        try:
            self.assertEqual(results, [False])  # 别的线程拿不到
        finally:
            first.release()
        results.clear()
        after = threading.Thread(target=lambda: results.append(second.acquire()))
        after.start()
        after.join()
        try:
            self.assertEqual(results, [True])  # 释放后可接手
        finally:
            second.release()

    def test_reacquire_after_release(self):
        name = f"courselens-platform-test-{uuid.uuid4().hex}"
        lock = WindowsMutexSingleInstanceLock(name)
        self.assertTrue(lock.acquire())
        lock.release()
        self.assertTrue(lock.acquire())
        lock.release()

    def test_release_without_acquire_is_safe(self):
        lock = WindowsMutexSingleInstanceLock(f"courselens-platform-test-{uuid.uuid4().hex}")
        lock.release()  # 绝不抛

    def test_empty_name_rejected(self):
        with self.assertRaises(ValueError):
            WindowsMutexSingleInstanceLock("  ")

    def test_guard_rejects_other_platform(self):
        lock = WindowsMutexSingleInstanceLock("courselens-guard-test")
        with patch("sys.platform", "darwin"):
            with self.assertRaises(PlatformNotSupportedError):
                lock.acquire()


class MacOSFlockStubTests(unittest.TestCase):
    def _lock(self, path: Path, fake: _FakeFlock, name: str = "courselens-test") -> MacOSFlockSingleInstanceLock:
        return MacOSFlockSingleInstanceLock(name, path, flock_fn=fake)

    def test_second_instance_rejected_until_release(self):
        fake = _FakeFlock()
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "instance.lock"
            first = self._lock(lock_path, fake)
            second = self._lock(lock_path, fake)
            self.assertTrue(first.acquire())
            self.assertTrue(first.acquire())  # 同一持有者幂等
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_lock_and_unlock_operations_pinned(self):
        fake = _FakeFlock()
        with tempfile.TemporaryDirectory() as tmp:
            lock = self._lock(Path(tmp) / "instance.lock", fake, name="op-pin")
            lock.acquire()
            lock.release()
        self.assertEqual(
            [operation for _fd, operation in fake.operations], [6, 8]
        )  # LOCK_EX|LOCK_NB=6 → LOCK_UN=8

    def test_release_without_acquire_is_safe(self):
        fake = _FakeFlock()
        with tempfile.TemporaryDirectory() as tmp:
            lock = self._lock(Path(tmp) / "instance.lock", fake)
            lock.release()
        self.assertEqual(fake.operations, [])

    def test_default_impl_guard_rejects_other_platform(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = MacOSFlockSingleInstanceLock(
                "courselens-test", Path(tmp) / "instance.lock"
            )
            with patch("sys.platform", "win32"):
                with self.assertRaises(PlatformNotSupportedError):
                    lock.acquire()

    def test_dispatch_macos_uses_given_lock_path(self):
        with patch("sys.platform", "darwin"):
            lock = acquire_single_instance(
                "courselens-test", lock_path=os.sep + "tmp" + os.sep + "x.lock"
            )
        self.assertIsInstance(lock, MacOSFlockSingleInstanceLock)


if __name__ == "__main__":
    unittest.main()
