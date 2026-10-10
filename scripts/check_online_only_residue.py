"""Static guard that rejects retired local-media client paths."""

from __future__ import annotations

import argparse
from pathlib import Path


DEFAULT_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = (
    "COURSELENS_ONLINE_ONLY",
    "COURSELENS_WORKER_MIRROR_MODE",
    "worker_template_legacy",
    "legacy-template",
    "/api/download",
    "/api/file-action",
    "/api/subtitle-merge",
    "/api/open-folder",
    "requirements-main-py310",
    "requirements-learning-py310",
    ".venv-learning-py310",
    "tools/ffmpeg",
    "PotPlayer",
    "SenseVoice",
    "RapidOCR",
    "DEFAULT_DOWNLOAD_DIR",
    "download_video",
    "raw_sensevoice_path",
    "public_manifest_snapshot",
    "_merge_manifest_status",
    "online_only =",
)
EXCLUDED = {"worker", "shared/protocol", "tests", "docs", "scripts/check_online_only_residue.py"}


def scan(root: Path = DEFAULT_ROOT) -> list[str]:
    findings: list[str] = []
    paths = sorted(root.glob("src/**/*.py"))
    paths += sorted(root.glob("frontend/**/*.js"))
    paths += sorted(root.glob("frontend/**/*.html"))
    paths += sorted(root.glob("frontend/**/*.css"))
    paths += [
        root / "credentials.py",
        root / "path_utils.py",
        root / "start_fudan_courselens.ps1",
        root / "scripts" / "setup_fudan_courselens_runtime.ps1",
        root / "runtime-assets.json",
    ]
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if any(relative == item or relative.startswith(item + "/") for item in EXCLUDED):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            findings.append(f"{relative}: unreadable")
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            folded = line.casefold()
            if any(marker.casefold() in folded for marker in FORBIDDEN):
                findings.append(f"{relative}:{line_no}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    findings = scan(args.root)
    for item in findings:
        print(item)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
