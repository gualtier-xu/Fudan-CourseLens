"""Crash-consistent primitives used by the update engine and stable helper."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable


FaultHook = Callable[[str], None]


def sync_directory(path: Path) -> str:
    """Persist directory metadata where the host exposes a usable primitive.

    POSIX permits opening a directory and calling fsync. Windows does not expose
    that operation through Python; durable_replace uses MoveFileExW with
    WRITE_THROUGH there instead. The return value is recorded in the audit log.
    """
    if os.name == "nt":
        return "windows_movefile_write_through"
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return "directory_fsync"


def durable_replace(source: Path, destination: Path) -> str:
    if os.name == "nt":
        import ctypes

        flags = 0x1 | 0x8  # MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
        if not ctypes.windll.kernel32.MoveFileExW(str(source), str(destination), flags):
            raise ctypes.WinError()
        return "windows_movefile_write_through"
    os.replace(source, destination)
    return sync_directory(destination.parent)


def atomic_json(
    path: Path,
    value: dict[str, Any],
    *,
    boundary: str,
    fault: FaultHook | None = None,
) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    sync_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    if fault:
        fault(f"{boundary}.before_write")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    if fault:
        fault(f"{boundary}.after_file_fsync")
    durability = durable_replace(temporary, path)
    if fault:
        fault(f"{boundary}.after_replace")
    return durability


def append_audit(path: Path, event: str, **fields: Any) -> None:
    """Append a non-authoritative transition record and make it durable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"event": event, **fields}
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def remove_durable(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    sync_directory(path.parent)


def cleanup_atomic_temps(root: Path) -> None:
    if not root.exists():
        return
    for path in root.rglob(".*.tmp"):
        if path.is_file():
            remove_durable(path)
