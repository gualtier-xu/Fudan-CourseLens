"""Fail-closed cleanup for retired local CourseLens assets.

This command is intentionally conservative.  It records an auditable report,
refuses unknown paths, checks SQLite integrity and refuses to remove anything
when a CourseLens process appears to be active.  Deletion requires the exact
``--confirm-online-only`` acknowledgement and is never implicit.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = (
    "downloads",
    "models",
    "tools/ffmpeg",
    ".venv",
    ".venv-learning-py310",
    "archive.py",
    "manifest.py",
    "src/subtitle",
    "src/ai",
    "src/learning",
    "src/pipeline",
)
EXTERNAL_TARGETS = (
    "CourseLens-Rollback-Media-20260720-151657",
    "CourseLens-Reset-Audit",
)
WORKSPACE_TARGETS = (
    "archive/rollback-media-20260720",
    "archive/reset-audit",
    "archive/redundant-python310-pre-consolidation",
    "archive/verification",
    "private/ui-redesign/downloads",
    "private/ui-redesign/models",
    "private/ui-redesign/tools/ffmpeg",
    "private/ui-redesign/.venv",
    "private/ui-redesign/.venv-learning-py310",
)
PROTECTED_PREFIXES = (
    "runtime/data",
    "runtime/cache",
    "runtime/logs",
    "worker",
    "shared/protocol",
    "src/remote",
)
REPORT_SCHEMA = "courselens.legacy-cleanup.v1"
BACKUP_SCHEMA = "courselens.pre-cleanup-backup.v1"
BACKUP_DIRECTORY = "online-only-pre-cleanup-backup"
ACCEPTANCE_REPORT_SCHEMA = "courselens.final-acceptance-audit.v2"
ACCEPTANCE_GATES = (
    "signed_worker_repair",
    "encrypted_echo_cleanup",
    "browser_matrix",
)
LIVE_DEPENDENCY_MARKERS = (
    "from archive import",
    "import archive",
    "from manifest import",
    "import manifest",
    "src.subtitle",
    "src.ai",
    "src.learning",
    "src.pipeline",
    ".venv-learning-py310",
    "tools/ffmpeg",
)


def _relative(path: Path, root: Path = ROOT) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _files(path: Path, root: Path = ROOT) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    return sorted((item for item in path.rglob("*") if item.is_file()), key=lambda item: _relative(item, root))


def _processes(root: Path) -> list[dict[str, str]]:
    """Return CourseLens processes without requiring psutil."""
    if os.name == "nt":
        script = (
            "$root=[string]$env:COURSELENS_SCAN_ROOT; "
            "Get-CimInstance Win32_Process | ForEach-Object { "
            "$cmd=[string]$_.CommandLine; $name=[string]$_.Name; "
            "if($cmd -match 'cleanup_legacy.py'){ return }; "
            "$kind=$null; "
            "if($name -match '^(ffmpeg|ffprobe)(\\.exe)?$'){ $kind='ffmpeg' } "
            "elseif($cmd.IndexOf($root,[StringComparison]::OrdinalIgnoreCase) -ge 0){ $kind='courselens' } "
            "elseif($cmd -match 'sensevoice|rapidocr|src[\\/](subtitle|learning)'){ $kind='local_compute' }; "
            "if($kind){ Write-Output ($_.ProcessId.ToString() + '|' + $kind) } }"
        )
        command = ["powershell", "-NoProfile", "-Command", script]
    else:
        command = ["ps", "-eo", "pid=,args="]
    try:
        environment = dict(os.environ)
        environment["COURSELENS_SCAN_ROOT"] = str(root.resolve())
        raw = subprocess.check_output(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            stderr=subprocess.DEVNULL,
            timeout=10,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        return [{"pid": "", "kind": "process_probe_failed"}]
    found: list[dict[str, str]] = []
    marker = str(root.resolve()).casefold()
    if os.name == "nt":
        for line in raw.splitlines():
            pid, separator, kind = line.strip().partition("|")
            if separator and pid.isdigit() and kind in {"courselens", "ffmpeg", "local_compute"}:
                found.append({"pid": pid, "kind": kind})
    else:
        for line in raw.splitlines():
            pid, _, cmd = line.strip().partition(" ")
            if marker in cmd.casefold() and "cleanup_legacy.py" not in cmd:
                found.append({"pid": pid, "kind": "courselens"})
    return found


def _db_check(path: Path) -> str:
    if not path.is_file():
        return "missing"
    try:
        with closing(sqlite3.connect(path, timeout=5)) as db:
            row = db.execute("PRAGMA quick_check").fetchone()
        return str(row[0]) if row else "unknown"
    except sqlite3.Error as exc:
        return f"error:{type(exc).__name__}"


def _is_unsafe_path(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return path.is_symlink() or bool(attributes & 0x400)


def _restrict_backup_access(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o700)
        for item in path.iterdir():
            if item.is_file():
                item.chmod(0o600)
        return
    identity = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=10,
        check=True,
    )
    row = next(csv.reader([identity.stdout.strip()]), [])
    sid = str(row[1] if len(row) > 1 else "").strip()
    if not sid.startswith("S-1-"):
        raise RuntimeError("could not determine the current Windows user SID")
    subprocess.run(
        [
            "icacls", str(path), "/inheritance:r", "/grant:r",
            f"*{sid}:(OI)(CI)F", "/C",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=True,
    )
    for item in path.iterdir():
        if not item.is_file():
            raise RuntimeError("pre-cleanup backup contains a non-file entry")
        subprocess.run(
            [
                "icacls", str(item), "/inheritance:r", "/grant:r",
                f"*{sid}:F", "/C",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=True,
        )


def _create_safety_backup(root: Path) -> dict[str, object]:
    data = root / "runtime" / "data"
    state_source = data / "state.db"
    credential_source = data / "credentials.json"
    for source in (state_source, credential_source):
        if not source.is_file() or _is_unsafe_path(source):
            raise RuntimeError("required state or credential source is missing or unsafe")

    backup = data / BACKUP_DIRECTORY
    if backup.exists() and (not backup.is_dir() or _is_unsafe_path(backup)):
        raise RuntimeError("pre-cleanup backup directory is unsafe")
    backup.mkdir(parents=True, exist_ok=True)
    allowed = {"state.db", "credentials.json"}
    if any(item.name not in allowed for item in backup.iterdir()):
        raise RuntimeError("pre-cleanup backup directory contains an unknown entry")

    state_target = backup / "state.db"
    state_temporary = backup / ".state.db.tmp"
    credential_target = backup / "credentials.json"
    credential_temporary = backup / ".credentials.json.tmp"
    for temporary in (state_temporary, credential_temporary):
        temporary.unlink(missing_ok=True)
    try:
        with closing(sqlite3.connect(state_source, timeout=5)) as source_db:
            source_db.execute("PRAGMA query_only=ON")
            with closing(sqlite3.connect(state_temporary)) as target_db:
                source_db.backup(target_db)
        if _db_check(state_temporary) != "ok":
            raise RuntimeError("pre-cleanup state backup failed integrity validation")
        os.replace(state_temporary, state_target)

        credential_temporary.write_bytes(credential_source.read_bytes())
        os.replace(credential_temporary, credential_target)
        _restrict_backup_access(backup)
    except (OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        state_temporary.unlink(missing_ok=True)
        credential_temporary.unlink(missing_ok=True)
        raise RuntimeError("could not create the protected pre-cleanup backup") from exc

    files = {}
    try:
        for name, path in (("state", state_target), ("credentials", credential_target)):
            raw = path.read_bytes()
            files[name] = {
                "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()
            }
    except OSError as exc:
        raise RuntimeError("could not verify the protected pre-cleanup backup") from exc
    return {"schema": BACKUP_SCHEMA, "access": "restricted", "files": files}


def _state_evidence(path: Path) -> dict[str, int]:
    result = {"active_tasks": 0, "token_leases": 0, "active_remote_attempts": 0, "pending_imports": 0}
    if not path.is_file():
        return result
    queries = {
        "active_tasks": "SELECT count(*) FROM tasks WHERE state IN ('queued','running','pausing','paused')",
        "token_leases": "SELECT count(*) FROM remote_token_leases",
        "active_remote_attempts": (
            "SELECT count(*) FROM remote_run_attempts WHERE github_status IN ('queued','in_progress','waiting','pending') "
            "OR cleanup_state IN ('pending','cleanup_pending')"
        ),
        "pending_imports": (
            "SELECT count(*) FROM automation_imports WHERE state IN "
            "('available','downloading','verifying','importing','cleanup_pending')"
        ),
    }
    try:
        with closing(sqlite3.connect(path, timeout=5)) as db:
            tables = {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for key, query in queries.items():
                table = {
                    "active_tasks": "tasks", "token_leases": "remote_token_leases",
                    "active_remote_attempts": "remote_run_attempts", "pending_imports": "automation_imports",
                }[key]
                if table in tables:
                    result[key] = int(db.execute(query).fetchone()[0])
    except sqlite3.Error:
        result["active_tasks"] = -1
    return result


def _catalog_evidence(root: Path) -> dict[str, object]:
    legacy = root / "runtime" / "data" / "manifest.json"
    database = root / "runtime" / "data" / "state.db"
    result: dict[str, object] = {
        "legacy_present": legacy.is_file(), "database_present": database.is_file(),
        "database_readable": False,
        "legacy_courses": 0, "legacy_lectures": 0, "database_courses": 0,
        "database_lectures": 0, "matches": False,
    }
    if legacy.is_file():
        try:
            raw = json.loads(legacy.read_text(encoding="utf-8"))
            result["legacy_courses"] = len(raw.get("courses") or {})
            result["legacy_lectures"] = len(raw.get("lectures") or {})
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            result["error"] = "legacy_manifest_invalid"
            return result
    if not database.is_file():
        return result
    try:
        with closing(sqlite3.connect(database, timeout=5)) as db:
            result["database_courses"] = int(db.execute("SELECT count(*) FROM catalog_courses").fetchone()[0])
            result["database_lectures"] = int(db.execute("SELECT count(*) FROM catalog_lectures").fetchone()[0])
            result["database_readable"] = True
    except sqlite3.Error:
        result["error"] = "catalog_database_invalid"
        return result
    result["matches"] = bool(result["database_readable"]) and (
        not result["legacy_present"]
        or (
            result["legacy_courses"] == result["database_courses"]
            and result["legacy_lectures"] == result["database_lectures"]
        )
    )
    return result


def _unsafe_entries(root: Path) -> list[str]:
    unsafe: list[str] = []
    reparse_mask = 0x400
    paths = [(relative, root / relative) for relative in TARGETS]
    paths.extend((f"external/{name}", root.parent / name) for name in EXTERNAL_TARGETS)
    workspace = root.parent.parent
    paths.extend((f"workspace/{name}", workspace / name) for name in WORKSPACE_TARGETS)
    for relative, path in paths:
        candidates = [path] if path.exists() or path.is_symlink() else []
        if path.is_dir() and not path.is_symlink():
            candidates.extend(path.rglob("*"))
        for item in candidates:
            try:
                stat = item.lstat()
            except OSError:
                unsafe.append(f"unreadable:{relative}")
                continue
            if item.is_symlink() or bool(getattr(stat, "st_file_attributes", 0) & reparse_mask):
                unsafe.append(f"link:{relative}")
    return sorted(set(unsafe))


def _dependency_blockers(root: Path) -> list[str]:
    """Find live entrypoints that would break after deleting a target."""
    blockers: list[str] = []
    candidates = [root / "src", root / "frontend", root / "start_fudan_courselens.ps1", root / "runtime-assets.json"]
    target_roots = tuple((root / relative).resolve() for relative in TARGETS)
    for candidate in candidates:
        paths = candidate.rglob("*") if candidate.is_dir() else [candidate]
        for path in paths:
            if not path.is_file() or path.suffix.casefold() not in {".py", ".js", ".ps1", ".json"}:
                continue
            resolved = path.resolve()
            if any(resolved == target or target in resolved.parents for target in target_roots):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                blockers.append(f"unreadable:{path.relative_to(root).as_posix()}")
                continue
            for line_no, line in enumerate(text.splitlines(), 1):
                if any(marker in line for marker in LIVE_DEPENDENCY_MARKERS):
                    blockers.append(f"{path.relative_to(root).as_posix()}:{line_no}")
    return sorted(set(blockers))


def _acceptance_summary(path: Path | None) -> dict[str, object]:
    result: dict[str, object] = {
        "present": False,
        "schema_valid": False,
        "evidence_complete": False,
        "local_audit_passed": False,
        "release_ready": False,
        "report_sha256": "",
        "catalog_counts_recorded": False,
        "catalog_counts": {},
    }
    if path is None or not path.is_file():
        return result
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return result
    gates = dict(payload.get("external_gates") or {})
    evidence_complete = set(gates) == set(ACCEPTANCE_GATES) and all(
        dict(gates.get(gate) or {}).get("status") == "passed"
        and len(str(dict(gates.get(gate) or {}).get("evidence_sha256") or "")) == 64
        for gate in ACCEPTANCE_GATES
    )
    schema_valid = payload.get("schema") == ACCEPTANCE_REPORT_SCHEMA
    catalog_counts = dict(
        dict(dict(payload.get("databases") or {}).get("state.db") or {}).get(
            "catalog_counts"
        ) or {}
    )
    catalog_counts_recorded = (
        set(catalog_counts) == {"courses", "lectures"}
        and all(isinstance(value, int) and value >= 0 for value in catalog_counts.values())
    )
    result.update({
        "present": True,
        "schema_valid": schema_valid,
        "evidence_complete": evidence_complete,
        "local_audit_passed": payload.get("local_audit_passed") is True,
        "release_ready": (
            payload.get("release_ready") is True
            and schema_valid
            and evidence_complete
            and catalog_counts_recorded
        ),
        "report_sha256": hashlib.sha256(raw).hexdigest(),
        "catalog_counts_recorded": catalog_counts_recorded,
        "catalog_counts": catalog_counts if catalog_counts_recorded else {},
    })
    return result


def scan(root: Path = ROOT, *, acceptance_report: Path | None = None) -> dict:
    root = root.resolve()
    entries: list[dict[str, object]] = []
    missing: list[str] = []
    all_names: list[str] = []
    total_files = 0
    total_bytes = 0
    for relative in TARGETS:
        path = root / relative
        if not path.exists():
            missing.append(relative)
            continue
        files = _files(path, root)
        names = [item.relative_to(root).as_posix() for item in files]
        sizes = [item.stat().st_size for item in files]
        all_names.extend(names)
        total_files += len(files)
        total_bytes += sum(sizes)
        entries.append({
            "target": relative,
            "file_count": len(files),
            "bytes": sum(sizes),
            "relative_name_sha256": hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest(),
        })
    external_entries: list[dict[str, object]] = []
    for name in EXTERNAL_TARGETS:
        path = root.parent / name
        if not path.exists():
            continue
        files = _files(path, root.parent)
        names = [f"external/{item.relative_to(root.parent).as_posix()}" for item in files]
        sizes = [item.stat().st_size for item in files]
        all_names.extend(names)
        total_files += len(files)
        total_bytes += sum(sizes)
        external_entries.append({
            "target": name,
            "file_count": len(files),
            "bytes": sum(sizes),
            "relative_name_sha256": hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest(),
        })
    workspace_entries: list[dict[str, object]] = []
    workspace = root.parent.parent
    for name in WORKSPACE_TARGETS:
        path = workspace / name
        if not path.exists():
            continue
        files = _files(path, workspace)
        names = [f"workspace/{item.relative_to(workspace).as_posix()}" for item in files]
        sizes = [item.stat().st_size for item in files]
        all_names.extend(names)
        total_files += len(files)
        total_bytes += sum(sizes)
        workspace_entries.append({
            "target": name,
            "file_count": len(files),
            "bytes": sum(sizes),
            "relative_name_sha256": hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest(),
        })
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "targets": list(TARGETS),
        "external_targets": list(EXTERNAL_TARGETS),
        "workspace_targets": list(WORKSPACE_TARGETS),
        "root_fingerprint": hashlib.sha256(str(root).casefold().encode("utf-8")).hexdigest(),
        "missing_targets": missing,
        "file_count": total_files,
        "bytes": total_bytes,
        "relative_name_sha256": hashlib.sha256("\n".join(all_names).encode("utf-8")).hexdigest(),
        "entries": entries,
        "external_entries": external_entries,
        "workspace_entries": workspace_entries,
        "root_valid": all((root / marker).exists() for marker in (".git", "runtime-assets.json", "src")),
        "processes": _processes(root),
        "unsafe_entries": _unsafe_entries(root),
        "dependency_blockers": _dependency_blockers(root),
        "state_db_integrity": _db_check(root / "runtime" / "data" / "state.db"),
        "state_evidence": _state_evidence(root / "runtime" / "data" / "state.db"),
        "catalog_evidence": _catalog_evidence(root),
        "acceptance": _acceptance_summary(acceptance_report),
    }


def validate(report: dict) -> list[str]:
    failures: list[str] = []
    targets = tuple(str(value) for value in report.get("targets") or ())
    if targets != TARGETS:
        failures.append("cleanup target allowlist does not match the reviewed release")
    external_targets = tuple(str(value) for value in report.get("external_targets") or ())
    if external_targets != EXTERNAL_TARGETS:
        failures.append("external cleanup allowlist does not match the reviewed release")
    workspace_targets = tuple(str(value) for value in report.get("workspace_targets") or ())
    if workspace_targets != WORKSPACE_TARGETS:
        failures.append("workspace cleanup allowlist does not match the reviewed release")
    if report.get("root_valid") is not True:
        failures.append("cleanup root is not a CourseLens worktree")
    for item in report.get("files") or []:
        relative = str(item.get("path") or "")
        if any(relative == prefix or relative.startswith(prefix + "/") for prefix in PROTECTED_PREFIXES):
            failures.append(f"protected path selected: {relative}")
    if report.get("processes"):
        failures.append("CourseLens process is still running")
    if report.get("unsafe_entries"):
        failures.append("target contains a symlink, reparse point or unreadable entry")
    if report.get("dependency_blockers"):
        failures.append("live client code still depends on a cleanup target")
    if report.get("state_db_integrity") not in {"missing", "ok"}:
        failures.append(f"state.db integrity check failed: {report.get('state_db_integrity')}")
    for key, value in dict(report.get("state_evidence") or {}).items():
        if int(value) != 0:
            failures.append(f"runtime state is not clean: {key}={value}")
    catalog = dict(report.get("catalog_evidence") or {})
    if (
        catalog.get("database_present") is not True
        or catalog.get("database_readable") is not True
        or catalog.get("matches") is not True
    ):
        failures.append("legacy manifest does not match CatalogRepository")
    acceptance = dict(report.get("acceptance") or {})
    if (
        acceptance.get("schema_valid") is not True
        or acceptance.get("evidence_complete") is not True
        or acceptance.get("local_audit_passed") is not True
        or acceptance.get("release_ready") is not True
    ):
        failures.append("final real acceptance report is missing or not release-ready")
    expected_counts = dict(acceptance.get("catalog_counts") or {})
    current_counts = {
        "courses": catalog.get("database_courses"),
        "lectures": catalog.get("database_lectures"),
    }
    if expected_counts != current_counts:
        failures.append("CatalogRepository counts changed after final acceptance")
    return failures


def apply_cleanup(report: dict, *, root: Path = ROOT) -> None:
    failures = validate(report)
    expected_root = hashlib.sha256(str(root.resolve()).casefold().encode("utf-8")).hexdigest()
    if report.get("root_fingerprint") != expected_root:
        failures.append("cleanup report does not belong to the selected root")
    if failures:
        raise RuntimeError("; ".join(failures))
    for relative in TARGETS:
        _remove_allowlisted_path(root / relative)
    for name in EXTERNAL_TARGETS:
        _remove_allowlisted_path(root.parent / name)
    workspace = root.parent.parent
    for name in WORKSPACE_TARGETS:
        _remove_allowlisted_path(workspace / name)
    catalog = dict(report.get("catalog_evidence") or {})
    legacy_catalog = root / "runtime" / "data" / "manifest.json"
    if catalog.get("legacy_present") and catalog.get("matches"):
        legacy_catalog.unlink(missing_ok=True)


def _remove_allowlisted_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        boundary = path.resolve()

        def clear_readonly(function, value, _error) -> None:
            candidate = Path(value)
            resolved = candidate.resolve()
            if resolved != boundary and boundary not in resolved.parents:
                raise RuntimeError("cleanup retry escaped the allowlisted directory")
            os.chmod(candidate, stat.S_IREAD | stat.S_IWRITE)
            function(value)

        shutil.rmtree(path, onerror=clear_readonly)
        return
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    path.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "runtime" / "reports" / "legacy-cleanup.json")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--acceptance-report",
        type=Path,
        default=ROOT / "runtime" / "reports" / "final-acceptance.json",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-online-only", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    report = scan(root, acceptance_report=args.acceptance_report.resolve())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    failures = validate(report)
    print(json.dumps({"report": str(args.report), "bytes": report["bytes"], "files": report["file_count"], "failures": failures}, ensure_ascii=False))
    if not args.apply:
        return 0
    if not args.confirm_online_only:
        print("refusing deletion: pass --confirm-online-only", file=sys.stderr)
        return 2
    if failures:
        print("refusing deletion: " + "; ".join(failures), file=sys.stderr)
        return 3
    try:
        report["backup"] = _create_safety_backup(root)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except RuntimeError as exc:
        print("refusing deletion: " + str(exc), file=sys.stderr)
        return 4
    apply_cleanup(report, root=root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
