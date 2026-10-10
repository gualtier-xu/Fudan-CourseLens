"""Stable managed-install helper.

This file is installed in the non-versioned launcher directory and is never
loaded from a downloaded package.  It accepts only an installation root and a
closed command; target versions are read from already-verified state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
import unicodedata
import uuid
from pathlib import Path, PurePosixPath


HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{value}" for value in range(1, 10)),
    *(f"lpt{value}" for value in range(1, 10)),
}
WINDOWS_FORBIDDEN_RE = re.compile(r'[<>:"\\|?*\x00-\x1f]')


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(
            path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        )
    except AttributeError:
        return path.is_symlink()


def _slot_name(value: str) -> PurePosixPath:
    if (
        not value or value.startswith("/") or value.endswith("/")
        or "\\" in value or "//" in value
        or value != unicodedata.normalize("NFC", value)
    ):
        raise RuntimeError("staged_update_inventory_invalid")
    parts = value.split("/")
    if any(
        part in {"", ".", ".."}
        or part.endswith((".", " "))
        or WINDOWS_FORBIDDEN_RE.search(part)
        or unicodedata.normalize("NFKC", part).casefold().split(".", 1)[0]
        in WINDOWS_RESERVED_NAMES
        for part in parts
    ):
        raise RuntimeError("staged_update_inventory_invalid")
    return PurePosixPath(*parts)


def _inside(root: Path, child: Path) -> Path:
    root = root.resolve(strict=True)
    child = child.resolve(strict=False)
    if root not in child.parents:
        raise RuntimeError("target_outside_install_root")
    return child


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        import ctypes

        flags = 0x1 | 0x8  # MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
        if not ctypes.windll.kernel32.MoveFileExW(str(source), str(destination), flags):
            raise ctypes.WinError()
        return
    os.replace(source, destination)
    _sync_directory(destination.parent)


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _sync_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    _durable_replace(temporary, path)


def _remove_durable(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _sync_directory(path.parent)


def apply(root: Path) -> str:
    state = _inside(root, root / "state")
    # 布局/状态文件损坏必须落闭集码（N6CP 边界族），绝不向学生裸抛堆栈。
    try:
        layout = json.loads((state / "install-layout.json").read_text(encoding="utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise RuntimeError("managed_install_required") from exc
    pending_path = state / "pending.json"
    current_path = state / "current.json"
    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        try:
            current = json.loads(current_path.read_text(encoding="utf-8-sig"))
        except (UnicodeError, ValueError) as exc:
            raise RuntimeError("pending_update_missing") from exc
        if current.get("awaiting_health"):
            return str(_inside(root, root / "versions" / str(current["version"])))
        raise RuntimeError("pending_update_missing")
    except (UnicodeError, ValueError) as exc:
        raise RuntimeError("pending_update_invalid") from exc
    if layout.get("schema") != "courselens.managed-install.v1":
        raise RuntimeError("managed_install_required")
    if pending.get("schema") != "courselens.update-pending.v1":
        raise RuntimeError("pending_update_invalid")
    target_version = str(pending.get("target_version") or "")
    previous_version = str(pending.get("previous_version") or "")
    target = _inside(root, root / "versions" / target_version)
    if target.parent != (root / "versions").resolve() or not target.is_dir():
        raise RuntimeError("staged_update_missing")
    package = json.loads((target / "courselens-package.json").read_text(encoding="utf-8-sig"))
    if not isinstance(package, dict):
        raise RuntimeError("staged_update_identity_invalid")
    files = package.get("files")
    if package.get("version") != target_version or not isinstance(files, dict) or not files:
        raise RuntimeError("staged_update_identity_invalid")
    collision_keys: set[str] = set()
    for name, expected in files.items():
        normalized = _slot_name(str(name))
        collision_key = "/".join(
            unicodedata.normalize("NFKC", part).casefold() for part in normalized.parts
        )
        if collision_key in collision_keys or not HEX64_RE.fullmatch(str(expected)):
            raise RuntimeError("staged_update_inventory_invalid")
        collision_keys.add(collision_key)
        path = _inside(target, target.joinpath(*normalized.parts))
        if not path.is_file() or _is_link(path) or path.stat().st_nlink != 1:
            raise RuntimeError("staged_update_file_invalid")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise RuntimeError("staged_update_file_invalid")
    items = list(target.rglob("*"))
    if any(_is_link(item) for item in items):
        raise RuntimeError("staged_update_inventory_invalid")
    actual = {
        item.relative_to(target).as_posix() for item in items if item.is_file()
    }
    if actual != {"courselens-package.json", *files}:
        raise RuntimeError("staged_update_inventory_invalid")
    current = {}
    try:
        current = json.loads(current_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        pass
    if current.get("version") == target_version and current.get("awaiting_health"):
        _remove_durable(pending_path)
        return str(target)
    # UPDATE-UX-1（真实用户模拟实锤，2026-10-09）： switched_at 必须随 apply 落盘
    # —— 否则新槽位进程 UpdateService.recover() 读不到 switched_at（=0），
    # 把刚完成的换装判成 >120s 健康超窗而立即自动回滚，launcher 的 confirm
    # 随后必然 health_confirmation_invalid：每一次受管更新都会装上又滚回。
    # 与 src/update/service.py apply_pending 的字段语义对齐。
    _atomic(current_path, {
        "schema": "courselens.current-version.v1", "version": target_version,
        "previous_version": previous_version, "awaiting_health": True,
        "switched_at": time.time(),
    })
    _remove_durable(pending_path)
    return str(target)


def confirm(root: Path, version: str) -> str:
    state = _inside(root, root / "state")
    current_path = state / "current.json"
    current = json.loads(current_path.read_text(encoding="utf-8-sig"))
    if current.get("version") == version and not current.get("awaiting_health"):
        return version
    if current.get("version") != version or not current.get("awaiting_health"):
        raise RuntimeError("health_confirmation_invalid")
    current["awaiting_health"] = False
    _atomic(current_path, current)
    return version


def rollback(root: Path) -> str:
    state = _inside(root, root / "state")
    current_path = state / "current.json"
    current = json.loads(current_path.read_text(encoding="utf-8-sig"))
    previous = str(current.get("previous_version") or "")
    target = _inside(root, root / "versions" / previous)
    if not current.get("awaiting_health") and current.get("rollback_reason"):
        return str(_inside(root, root / "versions" / str(current["version"])))
    if not current.get("awaiting_health") or target.parent != (root / "versions").resolve() or not target.is_dir():
        raise RuntimeError("rollback_unavailable")
    _atomic(current_path, {
        "schema": "courselens.current-version.v1", "version": previous,
        "previous_version": str(current.get("version") or ""), "awaiting_health": False,
        "rollback_reason": "startup_health_failed",
    })
    return str(target)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("apply", "confirm", "rollback", "current"))
    parser.add_argument("--install-root", required=True)
    parser.add_argument("--version", default="")
    args = parser.parse_args(argv)
    root = Path(args.install_root).resolve(strict=True)
    try:
        if args.command == "apply":
            value = apply(root)
        elif args.command == "confirm":
            value = confirm(root, args.version)
        elif args.command == "rollback":
            value = rollback(root)
        else:
            state = json.loads((root / "state" / "current.json").read_text(encoding="utf-8-sig"))
            value = str(_inside(root, root / "versions" / str(state["version"])))
    except (KeyError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"client_update_helper_failed:{exc}", file=sys.stderr)
        return 2
    print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
