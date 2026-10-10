"""Validate the repository's UTF-8 and Windows launcher encoding policy."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


TEXT_SUFFIXES = {
    ".bat", ".cmd", ".css", ".html", ".ini", ".js", ".json", ".md",
    ".ps1", ".py", ".toml", ".txt", ".yaml", ".yml",
}
ASCII_LAUNCHERS = {".bat", ".cmd"}


def tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    names = result.stdout.decode("utf-8").split("\0")
    return [root / name for name in names if name]


def check(root: Path) -> list[str]:
    errors: list[str] = []
    for path in tracked_files(root):
        if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
            continue
        raw = path.read_bytes()
        label = str(path.relative_to(root))
        has_bom = raw.startswith(b"\xef\xbb\xbf")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            errors.append(f"{label}: invalid UTF-8 ({exc})")
            continue
        if "\ufffd" in text:
            errors.append(f"{label}: contains U+FFFD replacement character")
        if "\x00" in text:
            errors.append(f"{label}: contains NUL byte")
        if path.suffix.lower() in ASCII_LAUNCHERS:
            if has_bom:
                errors.append(f"{label}: batch files must not contain a UTF-8 BOM")
            if any(ord(char) > 127 for char in text):
                errors.append(f"{label}: batch files must contain ASCII only")
        if path.suffix.lower() == ".ps1" and any(ord(char) > 127 for char in text) and not has_bom:
            errors.append(f"{label}: non-ASCII PowerShell must have a UTF-8 BOM for Windows PowerShell 5.1")
    return errors


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors = check(root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Text encoding policy passed: tracked text is UTF-8; batch launchers are ASCII-safe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
