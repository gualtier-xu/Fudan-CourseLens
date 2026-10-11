"""Privacy-safe, read-only CourseLens release audit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_MEDIA_SUFFIXES = {
    ".aac", ".m3u8", ".m4a", ".mkv", ".mp3", ".mp4", ".pcm", ".ts", ".wav",
}
PACKAGE_PATHS = (
    "frontend", "src", "shared", "scripts", "docs/repository-readmes",
    "tools/python312",
    "credentials.py", "path_utils.py",
    "runtime-assets.json", "requirements-client-py310.lock.txt",
    "requirements-client-py312.lock.txt",
    "OpenFudanCourseLens.cmd",
    "SetupFudanCourseLensRuntime.cmd", "start_fudan_courselens.ps1",
)
REQUIRED_INSTALLED_PATHS = ("tools/python312",)
EVIDENCE_SCHEMA = "courselens.acceptance-evidence.v1"
REQUIRED_EXTERNAL_GATES = (
    "signed_worker_repair",
    "encrypted_echo_cleanup",
    "browser_matrix",
)


def _data_root() -> Path:
    configured = os.environ.get("COURSELENS_DATA_DIR", "").strip()
    return Path(configured).expanduser().resolve() if configured else ROOT / "runtime" / "data"


def _iter_files(path: Path, *, include_generated: bool = False) -> Iterable[Path]:
    if path.is_file():
        yield path
    elif path.is_dir():
        yield from (
            item
            for item in path.rglob("*")
            if item.is_file()
            and (
                include_generated
                or (
                    "__pycache__" not in item.parts
                    and item.suffix.lower() not in {".pyc", ".pyo"}
                )
            )
        )


def _size(paths: Iterable[Path]) -> int:
    total = 0
    for path in paths:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _media_summary(root: Path) -> dict[str, Any]:
    rows = []
    if root.is_dir():
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in ACTIVE_MEDIA_SUFFIXES:
                continue
            try:
                size = path.stat().st_size
                relative = str(path.relative_to(root)).replace("\\", "/")
            except OSError:
                continue
            rows.append({
                "extension": path.suffix.lower(),
                "size_bytes": size,
                "path_sha256": hashlib.sha256(relative.encode("utf-8")).hexdigest(),
            })
    by_extension: dict[str, dict[str, int]] = {}
    for item in rows:
        bucket = by_extension.setdefault(item["extension"], {"count": 0, "bytes": 0})
        bucket["count"] += 1
        bucket["bytes"] += int(item["size_bytes"])
    return {
        "count": len(rows),
        "bytes": sum(item["size_bytes"] for item in rows),
        "by_extension": by_extension,
        "sample_path_sha256": [item["path_sha256"] for item in rows[:10]],
    }


def _database_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"present": False, "integrity": "missing"}
    try:
        with closing(sqlite3.connect(path)) as db:
            integrity = str(db.execute("pragma integrity_check").fetchone()[0])
            tables = {
                str(row[0])
                for row in db.execute("select name from sqlite_master where type='table'")
            }
            summary: dict[str, Any] = {
                "present": True,
                "integrity": integrity,
                "table_count": len(tables),
                "size_bytes": path.stat().st_size,
            }
            if path.name == "state.db" and "remote_runs" in tables:
                summary["remote_run_states"] = {
                    str(state): int(count)
                    for state, count in db.execute(
                        "select remote_state,count(*) from remote_runs group by remote_state"
                    )
                }
            if path.name == "state.db" and {
                "catalog_courses", "catalog_lectures"
            }.issubset(tables):
                summary["catalog_counts"] = {
                    "courses": int(
                        db.execute("select count(*) from catalog_courses").fetchone()[0]
                    ),
                    "lectures": int(
                        db.execute("select count(*) from catalog_lectures").fetchone()[0]
                    ),
                }
            return summary
    except (OSError, sqlite3.Error):
        return {"present": True, "integrity": "unreadable"}


def _cleanup_gate(state_path: Path, *, external_ready: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "mode": "accelerated-real-acceptance",
        "required_days": 0,
        "required_runs": 0,
        "observed_days": 0.0,
        "observed_runs": 0,
        "eligible": bool(external_ready),
    }
    if not state_path.exists():
        return result
    try:
        with closing(sqlite3.connect(state_path)) as db:
            tables = {
                str(row[0])
                for row in db.execute("select name from sqlite_master where type='table'")
            }
            if "remote_runs" not in tables:
                return result
            first_at, run_count = db.execute(
                "select min(coalesce(dispatched_at,updated_at)),count(*) "
                "from remote_runs where remote_state in ('completed','imported')"
            ).fetchone()
        elapsed_days = (time.time() - float(first_at)) / 86400 if first_at else 0.0
        result.update({
            "observed_days": round(max(0.0, elapsed_days), 2),
            "observed_runs": int(run_count or 0),
        })
    except (OSError, sqlite3.Error, TypeError, ValueError):
        result["error"] = "state_database_unreadable"
    return result


def _external_evidence(path: Path | None) -> tuple[dict[str, Any], bool]:
    empty = {
        gate: {"status": "not_recorded", "evidence_sha256": ""}
        for gate in REQUIRED_EXTERNAL_GATES
    }
    if path is None or not path.is_file():
        return empty, False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return empty, False
    available = set(payload.get("gates") or {})
    if (
        payload.get("schema") != EVIDENCE_SCHEMA
        or not set(REQUIRED_EXTERNAL_GATES).issubset(available)
    ):
        return empty, False
    normalized: dict[str, Any] = {}
    ready = True
    for gate in REQUIRED_EXTERNAL_GATES:
        value = dict(payload["gates"].get(gate) or {})
        digest = str(value.get("evidence_sha256") or "").lower()
        status = str(value.get("status") or "not_recorded")
        valid_digest = len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)
        passed = status == "passed" and valid_digest
        normalized[gate] = {
            "status": "passed" if passed else status,
            "evidence_sha256": digest if valid_digest else "",
        }
        ready = ready and passed
    return normalized, ready


def _process_summary() -> tuple[dict[str, int], str]:
    if os.name != "nt":
        return {}, "not_applicable"
    script = (
        "$rows=Get-CimInstance Win32_Process | "
        "Where-Object {$_.Name -match '^(ffmpeg|ffprobe|python)(\\.exe)?$'} | "
        "ForEach-Object { $c=[string]$_.CommandLine; "
        "if ($_.Name -match '^ffmpeg') {'ffmpeg'} "
        "elseif ($c -match 'src[\\\\/]subtitle|firered|sensevoice') {'local_asr'} "
        "elseif ($c -match 'rapidocr|src[\\\\/]learning') {'local_ocr_or_learning'} }; "
        "$rows | Group-Object | ForEach-Object { "
        "[pscustomobject]@{name=$_.Name;count=$_.Count} } | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        if completed.returncode:
            return {}, "unavailable"
        if not completed.stdout.strip():
            return {}, "ok"
        payload = json.loads(completed.stdout)
        rows = payload if isinstance(payload, list) else [payload]
        return (
            {str(row["name"]): int(row["count"]) for row in rows if row.get("name")},
            "ok",
        )
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, TypeError, ValueError):
        return {}, "unavailable"


def build_report(
    *,
    evidence_path: Path | None = None,
    package_root: Path = ROOT,
    cache_root: Path = ROOT / "runtime" / "cache",
    require_installed_package: bool = False,
) -> dict[str, Any]:
    data_root = _data_root()
    package_root = package_root.resolve()
    active_roots = {
        "data": data_root,
        "cache": cache_root,
        "logs": ROOT / "runtime" / "logs",
    }
    active_media = {name: _media_summary(path) for name, path in active_roots.items()}
    active_media_count = sum(item["count"] for item in active_media.values())
    active_media_bytes = sum(item["bytes"] for item in active_media.values())
    package_files = []
    for name in PACKAGE_PATHS:
        package_files.extend(_iter_files(package_root / name, include_generated=True))
    package_bytes = _size(package_files)
    package_complete = all((package_root / name).is_dir() for name in REQUIRED_INSTALLED_PATHS)
    databases = {
        name: _database_summary(data_root / name)
        for name in ("state.db", "learning.db")
    }
    processes, process_probe = _process_summary()
    database_ok = all(
        item.get("present") is True and item.get("integrity") == "ok"
        for item in databases.values()
    )
    local_gates = {
        "active_media_clear": active_media_count == 0,
        "database_integrity": database_ok,
        "catalog_database_ready": isinstance(
            databases["state.db"].get("catalog_counts"), dict
        ),
        "package_under_800_mib": package_bytes <= 800 * 1024 * 1024,
        "installed_package_complete": package_complete or not require_installed_package,
        "no_local_compute_process": (
            process_probe in {"ok", "not_applicable"}
            and sum(processes.values()) == 0
        ),
    }
    external_gates, external_ready = _external_evidence(evidence_path)
    return {
        "schema": "courselens.final-acceptance-audit.v2",
        "observed_at": time.time(),
        "active_data_root": "configured" if os.environ.get("COURSELENS_DATA_DIR") else "runtime/data",
        "active_media": {
            "count": active_media_count,
            "bytes": active_media_bytes,
            "roots": active_media,
        },
        "legacy_rollback_media": _media_summary(ROOT / "downloads"),
        "package": {
            "bytes": package_bytes,
            "limit_bytes": 800 * 1024 * 1024,
            "complete": package_complete,
            "required": bool(require_installed_package),
        },
        "databases": databases,
        "local_compute_probe": process_probe,
        "local_compute_processes": processes,
        "cleanup_gate": _cleanup_gate(
            data_root / "state.db", external_ready=external_ready
        ),
        "external_gates": external_gates,
        "local_gates": local_gates,
        "local_audit_passed": all(local_gates.values()),
        "release_ready": all(local_gates.values()) and external_ready,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the privacy-safe, read-only CourseLens release audit."
    )
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--package-root", type=Path, default=ROOT)
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=ROOT / "runtime" / "cache",
        help="media-scan cache root (default: ROOT/runtime/cache for backward compatibility; "
             "acceptance/CI runs should point this at a clean temporary root)",
    )
    parser.add_argument("--require-installed-package", action="store_true")
    args = parser.parse_args(argv)
    report = build_report(
        evidence_path=args.evidence,
        package_root=args.package_root,
        cache_root=args.cache_root,
        require_installed_package=args.require_installed_package,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
        temporary.write_text(rendered, encoding="utf-8")
        os.replace(temporary, args.output)
    print(rendered, end="")
    return 0 if report["local_audit_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
