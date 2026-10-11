"""Lightweight coordination for online streams and the local text index."""

from __future__ import annotations

import os
import platform
import sys
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator


CPU_PRESSURE_PERCENT = 85.0
MIN_AVAILABLE_MEMORY_BYTES = 1 * 1024**3


def system_resource_probe() -> dict[str, object]:
    value: dict[str, object] = {
        "on_ac_power": True,
        "cpu_percent": 0.0,
        "available_memory_bytes": 0,
    }
    try:
        import psutil

        value["cpu_percent"] = float(psutil.cpu_percent(interval=None))
        value["available_memory_bytes"] = int(psutil.virtual_memory().available)
        battery = psutil.sensors_battery()
        if battery is not None:
            value["on_ac_power"] = bool(battery.power_plugged)
    except Exception:
        pass
    return value


class ResourceCoordinator:
    """Bound local text indexing while giving browser streams priority."""

    def __init__(
        self,
        probe: Callable[[], dict[str, object]] = system_resource_probe,
        *,
        clock: Callable[[], float] = time.monotonic,
        refresh_seconds: float = 2.0,
    ):
        self._probe = probe
        self._clock = clock
        self._refresh_seconds = max(0.1, float(refresh_seconds))
        self._condition = threading.Condition(threading.RLock())
        self._system: dict[str, object] = {}
        self._last_probe = -1.0
        self._active: dict[str, int] = {}
        self._player_streams = 0

    def _refresh_locked(self, *, force: bool = False) -> None:
        now = self._clock()
        if force or not self._system or now - self._last_probe >= self._refresh_seconds:
            self._system = dict(self._probe() or {})
            self._last_probe = now

    def _limits_locked(self) -> dict[str, int]:
        on_ac = bool(self._system.get("on_ac_power", True))
        return {
            "remote_streams": 4,
            "search_batch_size": 50 if self._player_streams else (250 if on_ac else 100),
        }

    def _pressure_reasons_locked(self) -> list[str]:
        reasons: list[str] = []
        if float(self._system.get("cpu_percent") or 0.0) >= CPU_PRESSURE_PERCENT:
            reasons.append("cpu_pressure")
        memory = int(self._system.get("available_memory_bytes") or 0)
        if memory and memory < MIN_AVAILABLE_MEMORY_BYTES:
            reasons.append("memory_pressure")
        if self._player_streams:
            reasons.append("interactive_playback")
        return reasons

    def _can_claim_locked(self, kind: str) -> bool:
        if kind not in {"search", "interactive_stream"}:
            raise ValueError(f"unsupported local resource claim: {kind}")
        reasons = set(self._pressure_reasons_locked())
        if kind == "search" and reasons.intersection({"cpu_pressure", "memory_pressure"}):
            return False
        if kind == "interactive_stream":
            return self._active.get(kind, 0) < self._limits_locked()["remote_streams"]
        return True

    @contextmanager
    def claim(
        self,
        kind: str,
        *,
        cancel_event: threading.Event | None = None,
        timeout: float | None = None,
    ) -> Iterator[None]:
        started = self._clock()
        with self._condition:
            while True:
                self._refresh_locked()
                if self._can_claim_locked(kind):
                    self._active[kind] = self._active.get(kind, 0) + 1
                    break
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError(f"resource claim canceled: {kind}")
                if timeout is not None and self._clock() - started >= timeout:
                    raise TimeoutError(f"resource claim timed out: {kind}")
                self._condition.wait(0.25)
        try:
            yield
        finally:
            with self._condition:
                remaining = max(0, self._active.get(kind, 1) - 1)
                if remaining:
                    self._active[kind] = remaining
                else:
                    self._active.pop(kind, None)
                self._condition.notify_all()

    def begin_player_stream(self) -> None:
        with self._condition:
            self._player_streams += 1
            self._condition.notify_all()

    def end_player_stream(self) -> None:
        with self._condition:
            self._player_streams = max(0, self._player_streams - 1)
            self._condition.notify_all()

    def limits(self) -> dict[str, int]:
        with self._condition:
            self._refresh_locked()
            return dict(self._limits_locked())

    def snapshot(self) -> dict[str, object]:
        with self._condition:
            self._refresh_locked()
            return {
                "mode": "online-client",
                "on_ac_power": bool(self._system.get("on_ac_power", True)),
                "cpu_percent": round(float(self._system.get("cpu_percent") or 0.0), 1),
                "available_memory_mb": int(self._system.get("available_memory_bytes") or 0) // 1024**2,
                "player_streams": self._player_streams,
                "limits": self._limits_locked(),
                "active_claims": dict(self._active),
                "throttle_reasons": self._pressure_reasons_locked(),
            }


def device_fingerprint() -> dict[str, object]:
    """Coarse local identity for ETA profiles; contains no account data."""
    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "logical_cores": int(os.cpu_count() or 0),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
    }


__all__ = ["ResourceCoordinator", "device_fingerprint", "system_resource_probe"]
