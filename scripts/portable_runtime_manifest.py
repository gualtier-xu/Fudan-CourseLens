"""Create a non-destructive manifest for the future online-only bundle."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
KEEP = [
    "frontend",
    "src",
    "shared",
    "scripts",
    "docs/repository-readmes",
    "credentials.py",
    "path_utils.py",
    "runtime-assets.json",
    "requirements-client-py310.lock.txt",
    "OpenFudanCourseLens.cmd",
    "SetupFudanCourseLensRuntime.cmd",
    "start_fudan_courselens.ps1",
]


def _package_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return [
        item
        for item in path.rglob("*")
        if item.is_file()
        and "__pycache__" not in item.parts
        and item.suffix.lower() not in {".pyc", ".pyo"}
    ]


def main() -> int:
    files = []
    for item in KEEP:
        path = ROOT / item
        candidates = _package_files(path)
        for candidate in candidates:
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            files.append({"path": str(candidate.relative_to(ROOT)), "bytes": candidate.stat().st_size, "sha256": digest})
    output = ROOT / "runtime" / "portable-runtime-manifest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"version": 1, "destructive_cleanup": False, "files": files}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
