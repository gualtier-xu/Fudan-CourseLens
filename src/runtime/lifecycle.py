"""Fail-closed local process lifecycle, instance ownership, and shutdown budgets."""

from __future__ import annotations

import json
import os
import signal
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


PUBLIC_STATES = (
    "starting",
    "migrating",
    "ready",
    "draining",
    "stopping",
    "stopped",
    "recovery_required",
    "failed",
)
PUBLIC_STAGES = (
    "bootstrap",
    "instance_lock",
    "local_data",
    "service_start",
    "serving",
    "request_drain",
    "live_sessions",
    "task_checkpoint",
    "service_stop",
    "resource_release",
    "complete",
)

ERROR_INSTANCE_ACTIVE = "LIFECYCLE_E_INSTANCE_ACTIVE"
ERROR_INSTANCE_LOCK = "LIFECYCLE_E_INSTANCE_LOCK"
ERROR_PORT_BUSY = "LIFECYCLE_E_PORT_BUSY"
ERROR_STARTUP_FAILED = "LIFECYCLE_E_STARTUP_FAILED"
ERROR_RECOVERY_REQUIRED = "LIFECYCLE_E_RECOVERY_REQUIRED"
ERROR_DRAIN_TIMEOUT = "LIFECYCLE_E_DRAIN_TIMEOUT"
ERROR_SERVICE_STOP_TIMEOUT = "LIFECYCLE_E_SERVICE_STOP_TIMEOUT"
ERROR_REQUEST_REJECTED = "LIFECYCLE_E_DRAINING"


class LifecycleError(RuntimeError):
    """Closed-code lifecycle failure with no upstream or user data."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _process_started_at(pid: int) -> int:
    try:
        import psutil

        return int(psutil.Process(pid).create_time() * 1_000_000)
    except Exception:
        return 0


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class InstanceLock:
    """An OS-held lock plus redacted identity evidence; never kills a process."""

    def __init__(self, data_root: str | Path, instance_id: str):
        self.root = Path(data_root)
        self.instance_id = str(instance_id)
        self.lock_path = self.root / "instance.lock"
        self.instance_path = self.root / "server-instance.json"
        self.owner_token = uuid.uuid4().hex
        self.process_started_at = _process_started_at(os.getpid())
        self._stream = None

    def acquire(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        stream = self.lock_path.open("a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                if stream.read(1) == "":
                    stream.seek(0)
                    stream.write("\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise LifecycleError(ERROR_INSTANCE_ACTIVE) from exc
        self._stream = stream
        self._remove_stale_instance_evidence()

    def _remove_stale_instance_evidence(self) -> None:
        try:
            payload = json.loads(self.instance_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError):
            self.instance_path.unlink(missing_ok=True)
            return
        pid = int(payload.get("pid") or 0)
        recorded_start = int(payload.get("process_started_at") or 0)
        live_start = _process_started_at(pid) if _process_alive(pid) else 0
        # Holding the OS lock proves there is no active CourseLens owner. A
        # live PID with different creation time is PID reuse, not our process.
        if not live_start or not recorded_start or live_start != recorded_start:
            self.instance_path.unlink(missing_ok=True)
            return
        self.instance_path.unlink(missing_ok=True)

    def publish(self, port: int) -> None:
        payload = {
            "schema": "courselens.instance.v2",
            "instance_id": self.instance_id,
            "owner_token": self.owner_token,
            "pid": os.getpid(),
            "process_started_at": self.process_started_at,
            "port": int(port),
        }
        temporary = self.instance_path.with_name(
            f".{self.instance_path.name}.{self.owner_token}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=True, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temporary, self.instance_path)
        finally:
            temporary.unlink(missing_ok=True)

    def release(self) -> None:
        try:
            payload = json.loads(self.instance_path.read_text(encoding="utf-8"))
            if payload.get("owner_token") == self.owner_token:
                self.instance_path.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError):
            pass
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


@dataclass(frozen=True)
class ShutdownBudgets:
    request_drain: float = 1.0
    live_sessions: float = 1.0
    task_checkpoint: float = 3.0
    service_stop: float = 6.0

    @property
    def total(self) -> float:
        return sum(
            (self.request_drain, self.live_sessions, self.task_checkpoint, self.service_stop)
        )


class LifecycleController:
    """Thread-safe state machine shared by startup, HTTP, and every exit cause."""

    def __init__(self, budgets: ShutdownBudgets | None = None):
        self.budgets = budgets or ShutdownBudgets()
        self._lock = threading.RLock()
        self._state = "starting"
        self._stage = "bootstrap"
        self._error_code = ""
        self._shutdown_reason = ""
        self._shutdown_started = threading.Event()
        self._shutdown_complete = threading.Event()
        self._shutdown_callback: Callable[[], None] | None = None

    def transition(self, state: str, stage: str, error_code: str = "") -> None:
        if state not in PUBLIC_STATES or stage not in PUBLIC_STAGES:
            raise ValueError("invalid lifecycle state or stage")
        with self._lock:
            self._state = state
            self._stage = stage
            self._error_code = str(error_code or "")
        if state == "stopped":
            self._shutdown_complete.set()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "state": self._state,
                "stage": self._stage,
                "error_code": self._error_code,
                "accepting_requests": self._state == "ready",
                # BACKEND-DEATH-1④：尸检 [exit] 行的原因来源（additive）。
                "shutdown_reason": self._shutdown_reason,
            }

    def accepting_requests(self) -> bool:
        with self._lock:
            return self._state == "ready"

    def set_shutdown_callback(self, callback: Callable[[], None]) -> None:
        self._shutdown_callback = callback

    def request_shutdown(self, reason: str) -> bool:
        with self._lock:
            if self._shutdown_started.is_set():
                return False
            self._shutdown_reason = str(reason or "requested")
            self._shutdown_started.set()
            self._state = "draining"
            self._stage = "request_drain"
            callback = self._shutdown_callback
        if callback is not None:
            threading.Thread(
                target=callback,
                name="lifecycle-http-stop",
                daemon=True,
            ).start()
        return True

    def wait_for_shutdown(self, timeout: float | None = None) -> bool:
        return self._shutdown_complete.wait(timeout)


def install_signal_handlers(controller: LifecycleController) -> dict[int, object]:
    previous: dict[int, object] = {}

    def handle(signum, _frame) -> None:
        controller.request_shutdown(f"signal_{int(signum)}")

    candidates = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        candidates.append(signal.SIGBREAK)
    for candidate in candidates:
        previous[int(candidate)] = signal.getsignal(candidate)
        signal.signal(candidate, handle)
    return previous


def restore_signal_handlers(previous: dict[int, object]) -> None:
    for number, handler in previous.items():
        signal.signal(number, handler)


__all__ = [
    "ERROR_DRAIN_TIMEOUT",
    "ERROR_INSTANCE_ACTIVE",
    "ERROR_INSTANCE_LOCK",
    "ERROR_PORT_BUSY",
    "ERROR_RECOVERY_REQUIRED",
    "ERROR_REQUEST_REJECTED",
    "ERROR_SERVICE_STOP_TIMEOUT",
    "ERROR_STARTUP_FAILED",
    "InstanceLock",
    "LifecycleController",
    "LifecycleError",
    "PUBLIC_STAGES",
    "PUBLIC_STATES",
    "ShutdownBudgets",
    "install_signal_handlers",
    "restore_signal_handlers",
]
