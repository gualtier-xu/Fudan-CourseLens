"""Resumable, privacy-safe orchestration for CourseLens final acceptance.

The command caches local checks by a content fingerprint and binds real-world
evidence to the exact client, catalog and signed Worker release. Destructive
legacy cleanup remains a separate command and is never invoked here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import webbrowser
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
STATE_SCHEMA = "courselens.final-acceptance-state.v1"
GATE_EVIDENCE_SCHEMA = "courselens.acceptance-gate-evidence.v1"
EVIDENCE_SCHEMA = "courselens.acceptance-evidence.v1"
EXTERNAL_GATES = (
    "complete_lecture",
    "authorized_real_quality_set",
    "clean_windows_onboarding",
    "signed_worker_repair",
    "encrypted_echo_cleanup",
    "browser_matrix",
)
REQUIRED_RELEASE_GATES = (
    "signed_worker_repair",
    "encrypted_echo_cleanup",
    "browser_matrix",
)
LOCAL_CHECKS = (
    "private_tests",
    "worker_tests",
    "compileall",
    "frontend_javascript",
    "static_boundaries",
)
OAUTH_SECRET_NAMES = (
    "github_app_access_token",
    "github_app_access_expires_at",
    "github_app_refresh_token",
    "github_app_refresh_expires_at",
)
ACTIVE_GITHUB_SECRET_NAMES = OAUTH_SECRET_NAMES + (
    "github_app_installation_id",
    "github_worker_repo",
    "github_mailbox_repo",
    "github_worker_verified_manifest",
    "github_worker_verified_tree",
    "worker_box_public_key",
    "worker_signing_public_key",
)
MIGRATABLE_SECRET_NAMES = (
    "deepseek_api_key",
    "network_github_proxy",
    "github_worker_repo",
    "github_mailbox_repo",
    "github_worker_verified_manifest",
    "github_worker_verified_tree",
    "worker_box_public_key",
    "worker_signing_public_key",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(rendered, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _rewrite_json(path: Path, payload: dict[str, Any]) -> None:
    """Rewrite an existing credential file without changing its file ACL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "r+" if path.exists() else "w"
    with path.open(mode, encoding="utf-8", newline="\n") as handle:
        handle.seek(0)
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())


def _load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema": STATE_SCHEMA, "checks": {}, "gates": {}, "credentials": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("acceptance state is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("schema") != STATE_SCHEMA:
        raise RuntimeError("acceptance state schema is invalid")
    payload.setdefault("checks", {})
    payload.setdefault("gates", {})
    payload.setdefault("credentials", {})
    return payload


def _tracked_files(root: Path) -> list[tuple[str, Path]]:
    completed = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    rows = []
    for raw_name in sorted(item for item in completed.stdout.split(b"\0") if item):
        relative = raw_name.decode("utf-8", errors="strict")
        path = root / relative
        if not path.is_file() or path.is_symlink():
            continue
        rows.append((relative.replace("\\", "/"), path))
    return rows


def _content_digest(files: Iterable[tuple[str, Path]], prefixes: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for relative, path in files:
        if not any(relative == prefix or relative.startswith(prefix + "/") for prefix in prefixes):
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _catalog_fingerprint(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "courses": 0, "lectures": 0, "digest": ""}
    try:
        with closing(sqlite3.connect(path)) as db:
            tables = {
                str(row[0])
                for row in db.execute("select name from sqlite_master where type='table'")
            }
            if not {"catalog_courses", "catalog_lectures"}.issubset(tables):
                return {"status": "incomplete", "courses": 0, "lectures": 0, "digest": ""}
            courses = [str(row[0]) for row in db.execute(
                "select course_id from catalog_courses order by course_id"
            )]
            lectures = [str(row[0]) for row in db.execute(
                "select sub_id from catalog_lectures order by sub_id"
            )]
    except sqlite3.Error:
        return {"status": "unreadable", "courses": 0, "lectures": 0, "digest": ""}
    material = json.dumps(
        {"courses": courses, "lectures": lectures},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "status": "ok",
        "courses": len(courses),
        "lectures": len(lectures),
        "digest": _sha256(material),
    }


def build_context(root: Path = ROOT) -> dict[str, Any]:
    files = _tracked_files(root)
    product_prefixes = (
        "frontend", "src", "shared", "credentials.py", "path_utils.py",
        "runtime-assets.json", "requirements-client-py310.lock.txt",
        "OpenFudanCourseLens.cmd",
        "SetupFudanCourseLensRuntime.cmd", "start_fudan_courselens.ps1",
    )
    source_digest = _content_digest(files, product_prefixes)
    assets = json.loads((root / "runtime-assets.json").read_text(encoding="utf-8"))
    active = dict(dict(assets.get("worker_mirror") or {}).get("active") or {})
    data_root = Path(os.environ.get("COURSELENS_DATA_DIR") or root / "runtime" / "data").resolve()
    return {
        "source_content_sha256": source_digest,
        "browser_content_sha256": _content_digest(
            files, ("frontend", "src/app.py", "src/runtime/http_api.py", "src/services")
        ),
        "local_bindings": {
            "private_tests": _content_digest(files, product_prefixes + ("scripts", "tests", "requirements-test-py310.lock.txt")),
            "worker_tests": _content_digest(files, ("worker", "shared", "scripts/worker_mirror_allowlist.json")),
            "compileall": _content_digest(files, ("credentials.py", "path_utils.py", "src", "shared", "scripts", "tests", "worker")),
            "frontend_javascript": _content_digest(files, ("frontend",)),
            "static_boundaries": _content_digest(files, tuple(relative for relative, _path in files)),
        },
        "worker": {
            "repository": str(active.get("repository") or ""),
            "commit": str(active.get("commit") or "").lower(),
            "tree": str(active.get("tree") or "").lower(),
            "manifest_sha256": str(active.get("manifest_sha256") or "").lower(),
            "key_id": str(active.get("signing_key_id") or "").lower(),
            "protocol_versions": list(active.get("protocol_versions") or []),
        },
        "catalog": _catalog_fingerprint(data_root / "state.db"),
    }


def gate_binding(gate: str, context: dict[str, Any]) -> str:
    source = str(context.get("source_content_sha256") or "")
    worker = dict(context.get("worker") or {})
    catalog = dict(context.get("catalog") or {})
    values: dict[str, Any] = {"gate": gate}
    if gate in {"complete_lecture", "authorized_real_quality_set", "cloud_automation"}:
        values.update({"source": source, "worker": worker, "catalog": catalog})
    elif gate in {"signed_worker_repair", "encrypted_echo_cleanup"}:
        values.update({"worker": worker})
    elif gate == "clean_windows_onboarding":
        values.update({"source": source})
    elif gate == "browser_matrix":
        values.update({"browser": str(context.get("browser_content_sha256") or source)})
    else:
        raise ValueError(f"unsupported acceptance gate: {gate}")
    return _sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _python(root: Path) -> str:
    candidate = root / ".venv-test-py310" / "Scripts" / "python.exe"
    return str(candidate if candidate.is_file() else Path(sys.executable))


def _commands(root: Path) -> dict[str, list[tuple[list[str], dict[str, str]]]]:
    python = _python(root)
    worker_env = dict(os.environ)
    worker_env["PYTHONPATH"] = os.pathsep.join((str(root / "worker"), str(root)))
    no_bytecode = dict(os.environ)
    no_bytecode["PYTHONDONTWRITEBYTECODE"] = "1"
    javascript = [
        (["node", "--check", str(path)], dict(os.environ))
        for path in sorted((root / "frontend").rglob("*.js"))
    ]
    return {
        "private_tests": [([python, "-m", "pytest", "-q"], dict(os.environ))],
        "worker_tests": [
            ([python, "-m", "unittest", "discover", "-s", "worker/tests", "-p", "test_*.py", "-q"], worker_env)
        ],
        "compileall": [
            ([python, "-m", "compileall", "-q", "credentials.py", "path_utils.py", "src", "shared", "scripts", "tests", "worker/courselens_worker", "worker/scripts", "worker/tests"], no_bytecode)
        ],
        "frontend_javascript": javascript,
        "static_boundaries": [
            ([python, "scripts/check_text_encoding.py"], dict(os.environ)),
            ([python, "scripts/check_markdown_links.py"], dict(os.environ)),
            ([python, "scripts/check_online_only_residue.py"], dict(os.environ)),
            (["git", "diff", "--check"], dict(os.environ)),
        ],
    }


def run_local_checks(
    state: dict[str, Any], context: dict[str, Any], *, root: Path = ROOT, force: bool = False
) -> bool:
    all_passed = True
    checks = state.setdefault("checks", {})
    for name, commands in _commands(root).items():
        fingerprint = str(dict(context.get("local_bindings") or {}).get(name) or context["source_content_sha256"])
        previous = dict(checks.get(name) or {})
        if not force and previous.get("status") == "passed" and previous.get("binding") == fingerprint:
            continue
        started = time.monotonic()
        output_digest = hashlib.sha256()
        return_code = 0
        for command, environment in commands:
            try:
                completed = subprocess.run(
                    command,
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    text=False,
                    check=False,
                    timeout=30 * 60,
                )
            except subprocess.TimeoutExpired as exc:
                output_digest.update(exc.stdout or b"")
                output_digest.update(exc.stderr or b"")
                return_code = 124
                break
            output_digest.update(completed.stdout)
            output_digest.update(completed.stderr)
            if completed.returncode:
                return_code = int(completed.returncode)
                break
        checks[name] = {
            "status": "passed" if return_code == 0 else "failed",
            "binding": fingerprint,
            "command_count": len(commands),
            "duration_seconds": round(time.monotonic() - started, 3),
            "output_sha256": output_digest.hexdigest(),
            "return_code": return_code,
        }
        all_passed = all_passed and return_code == 0
    return all_passed


def record_gate(
    state: dict[str, Any], gate: str, artifact: Path, context: dict[str, Any]
) -> None:
    if artifact.is_symlink() or not artifact.is_file():
        raise RuntimeError("gate evidence must be a regular file")
    raw = artifact.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("gate evidence must be UTF-8 JSON") from exc
    expected_binding = gate_binding(gate, context)
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != GATE_EVIDENCE_SCHEMA
        or payload.get("gate") != gate
        or payload.get("status") != "passed"
        or payload.get("binding") != expected_binding
    ):
        raise RuntimeError("gate evidence is not a passing result for the current context")
    state.setdefault("gates", {})[gate] = {
        "status": "passed",
        "binding": expected_binding,
        "evidence_sha256": _sha256(raw),
    }


def write_external_evidence(state: dict[str, Any], context: dict[str, Any], path: Path) -> bool:
    gates: dict[str, Any] = {}
    complete = True
    for gate in EXTERNAL_GATES:
        record = dict(dict(state.get("gates") or {}).get(gate) or {})
        current = record.get("binding") == gate_binding(gate, context)
        passed = record.get("status") == "passed" and current
        digest = str(record.get("evidence_sha256") or "") if passed else ""
        gates[gate] = {
            "status": "passed" if passed else "not_recorded",
            "evidence_sha256": digest,
        }
        complete = complete and passed and len(digest) == 64
    _atomic_json(path, {"schema": EVIDENCE_SCHEMA, "gates": gates})
    return complete


def credential_inventory(paths: Iterable[Path], *, active: Path) -> list[dict[str, Any]]:
    rows = []
    active = active.resolve()
    for path in paths:
        resolved = path.resolve()
        if not resolved.is_file():
            continue
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            rows.append({"path_sha256": _sha256(str(resolved).casefold().encode()), "status": "unreadable"})
            continue
        secrets = dict(payload.get("secrets") or {}) if isinstance(payload, dict) else {}
        rows.append({
            "path_sha256": _sha256(str(resolved).casefold().encode()),
            "content_sha256": _sha256(resolved.read_bytes()),
            "status": "active" if resolved == active else "legacy",
            "bytes": resolved.stat().st_size,
            "account_count": len(dict(payload.get("accounts") or {})) if isinstance(payload, dict) else 0,
            "has_oauth": any(name in secrets for name in OAUTH_SECRET_NAMES),
        })
    return rows


def discover_legacy_credentials(root: Path = ROOT, *, active: Path) -> list[Path]:
    """Return exact known old stores without following links or scanning values."""
    active = active.resolve()
    candidates = {
        root / "downloads" / "credentials.json",
        *(root / "runtime" / "data").glob("acceptance-*/credentials.json"),
    }
    completed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=True,
    )
    for line in completed.stdout.splitlines():
        if line.startswith("worktree "):
            candidates.add(Path(line.removeprefix("worktree ")) / "runtime" / "data" / "credentials.json")
    return sorted(
        (path.resolve() for path in candidates if path.exists() and path.resolve() != active),
        key=lambda path: str(path).casefold(),
    )


def migrate_legacy_credentials(
    *, active: Path, source: Path, allowed_sources: Iterable[Path]
) -> dict[str, Any]:
    active = active.resolve()
    source = source.resolve()
    allowed = {path.resolve() for path in allowed_sources}
    if source not in allowed:
        raise RuntimeError("credential source is outside the discovered legacy allowlist")
    if source == active or source.is_symlink() or not source.is_file():
        raise RuntimeError("credential source is not a legacy regular file")
    try:
        source_payload = json.loads(source.read_text(encoding="utf-8"))
        active_payload = (
            json.loads(active.read_text(encoding="utf-8"))
            if active.is_file()
            else {"version": 1, "accounts": {}, "secrets": {}}
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("credential migration input is unreadable") from exc
    if not isinstance(source_payload, dict) or not isinstance(active_payload, dict):
        raise RuntimeError("credential migration input has an invalid shape")
    source_accounts = dict(source_payload.get("accounts") or {})
    source_secrets = dict(source_payload.get("secrets") or {})
    _validate_migration_ciphertext(source_accounts, source_secrets)
    active_accounts = active_payload.setdefault("accounts", {})
    active_secrets = active_payload.setdefault("secrets", {})
    if not isinstance(active_accounts, dict) or not isinstance(active_secrets, dict):
        raise RuntimeError("active credential store has an invalid shape")
    account_conflicts = [
        key for key, value in source_accounts.items()
        if key in active_accounts and active_accounts[key] != value
    ]
    if account_conflicts:
        raise RuntimeError("active account credentials conflict with the selected legacy source")
    preserved_active_secret_count = sum(
        key in source_secrets and key in active_secrets for key in MIGRATABLE_SECRET_NAMES
    )
    for key, value in source_accounts.items():
        active_accounts.setdefault(key, value)
    for key in MIGRATABLE_SECRET_NAMES:
        if key in source_secrets:
            active_secrets.setdefault(key, source_secrets[key])
    _rewrite_json(active, active_payload)

    # DPAPI decryption validates copied ciphertext without retaining plaintext.
    from credentials import CredentialStore
    store = CredentialStore(active)
    for row in store.list_accounts():
        store.load(str(row["student_id"]))
    if store.has_deepseek_key():
        store.load_deepseek_key()
    for key in MIGRATABLE_SECRET_NAMES:
        if key != "deepseek_api_key" and store.has_secret(key):
            store.load_secret(key)
    return {
        "source_path_sha256": _sha256(str(source).casefold().encode()),
        "source_content_sha256": _sha256(source.read_bytes()),
        "account_count": len(source_accounts),
        "secret_count": sum(key in source_secrets for key in MIGRATABLE_SECRET_NAMES),
        "preserved_active_secret_count": preserved_active_secret_count,
        "oauth_migrated": False,
    }


def _validate_migration_ciphertext(
    accounts: dict[str, Any], secrets: dict[str, Any]
) -> None:
    """Validate selected DPAPI ciphertext before changing the active store."""
    from credentials import _unprotect

    try:
        for student_id, record in accounts.items():
            payload = json.loads(_unprotect(str(dict(record)["password"])).decode("utf-8"))
            if str(payload.get("student_id") or "") != str(student_id):
                raise RuntimeError("account ciphertext identity does not match")
        for name in MIGRATABLE_SECRET_NAMES:
            if name not in secrets:
                continue
            payload = json.loads(
                _unprotect(str(dict(secrets[name])["value"])).decode("utf-8")
            )
            if name == "deepseek_api_key":
                if not str(payload.get("api_key") or ""):
                    raise RuntimeError("DeepSeek ciphertext is empty")
            elif str(payload.get("name") or "") != name:
                raise RuntimeError("integration ciphertext name does not match")
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("legacy credential ciphertext failed DPAPI validation") from exc


def verify_active_credentials(
    state: dict[str, Any], active: Path, context: dict[str, Any], *, proxy_url: str = ""
) -> None:
    from credentials import CredentialStore
    from src.remote.github_app import GitHubAppClient

    store = CredentialStore(active)
    missing = [name for name in ACTIVE_GITHUB_SECRET_NAMES if not store.has_secret(name)]
    if missing:
        raise RuntimeError("active credential store is missing required GitHub state")
    client = GitHubAppClient(store, proxy_url=proxy_url)
    authorization = client.verify_user_authorization(minimum_lifetime_seconds=6 * 60 * 60)
    if not authorization.get("authorized") or not authorization.get("installed"):
        raise RuntimeError("GitHub authorization or App installation could not be verified")
    # An existing installation is no longer sufficient: it must select exactly
    # the personal Worker and Mailbox repositories.
    installation = client.inspect_managed_resources().get("installation") or {}
    if not installation.get("repository_selection_exact"):
        raise RuntimeError(
            "GitHub App installation does not select exactly the managed Worker and Mailbox repositories"
        )
    integrity = client.check_worker_integrity()
    if not integrity.get("trusted") or integrity.get("actual_tree") != context["worker"]["tree"]:
        raise RuntimeError("active authorization does not verify the approved Worker tree")
    state["credentials"] = {
        "status": "verified",
        "active_content_sha256": _sha256(active.read_bytes()),
        "worker_binding": gate_binding("signed_worker_repair", context),
        "verified_at": time.time(),
    }


def authorize_active_credentials(
    active: Path, *, open_browser: bool = True, proxy_url: str = ""
) -> None:
    from credentials import CredentialStore
    from src.remote.github_app import GitHubAppClient

    client = GitHubAppClient(CredentialStore(active), proxy_url=proxy_url)
    authorization = client.start_device_authorization()
    print(json.dumps(authorization.public(), ensure_ascii=False), flush=True)
    if open_browser:
        webbrowser.open(authorization.verification_uri)
    interval = authorization.interval
    while time.time() < authorization.expires_at:
        time.sleep(interval)
        result = client.poll_device_authorization(authorization)
        if result.get("state") == "pending":
            interval += 5 if result.get("slow_down") else 0
            continue
        if result.get("state") == "authorized":
            client.bootstrap_student_repositories()
            print(json.dumps({"state": "authorized", "installed": bool(result.get("installed"))}))
            return
        raise RuntimeError(f"GitHub Device Flow ended with state {result.get('state')}")
    raise RuntimeError("GitHub Device Flow expired")


def retire_legacy_credentials(
    state: dict[str, Any], *, active: Path, candidates: Iterable[Path],
    context: dict[str, Any], allowed_candidates: Iterable[Path] | None = None,
) -> list[dict[str, Any]]:
    active = active.resolve()
    verification = dict(state.get("credentials") or {})
    if verification.get("status") != "verified":
        raise RuntimeError("active credentials have not passed live verification")
    if verification.get("active_content_sha256") != _sha256(active.read_bytes()):
        raise RuntimeError("active credentials changed after verification")
    if verification.get("worker_binding") != gate_binding("signed_worker_repair", context):
        raise RuntimeError("Worker release changed after credential verification")
    if time.time() - float(verification.get("verified_at") or 0) > 15 * 60:
        raise RuntimeError("active credential verification is stale")
    allowed = {
        path.resolve() for path in (
            allowed_candidates
            if allowed_candidates is not None
            else discover_legacy_credentials(ROOT, active=active)
        )
    }
    retired = []
    for candidate in candidates:
        path = candidate.resolve()
        if path not in allowed:
            raise RuntimeError("legacy credential candidate is outside the discovered allowlist")
        if path == active:
            raise RuntimeError("refusing to retire the active credential store")
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("legacy credential candidate is not a regular file")
        raw = path.read_bytes()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("legacy credential candidate is not valid JSON") from exc
        if not isinstance(payload, dict) or not {"accounts", "secrets"}.intersection(payload):
            raise RuntimeError("legacy credential candidate has an unexpected shape")
        row = {
            "path_sha256": _sha256(str(path).casefold().encode()),
            "content_sha256": _sha256(raw),
            "bytes": len(raw),
        }
        path.unlink()
        row["status"] = "retired" if not path.exists() else "failed"
        retired.append(row)
    verification["retired"] = retired
    state["credentials"] = verification
    return retired


def summary(state: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    checks = dict(state.get("checks") or {})
    gates = dict(state.get("gates") or {})
    external = {
        gate: (
            "passed"
            if dict(gates.get(gate) or {}).get("status") == "passed"
            and dict(gates.get(gate) or {}).get("binding") == gate_binding(gate, context)
            else "pending"
        )
        for gate in EXTERNAL_GATES
    }
    return {
        "schema": STATE_SCHEMA,
        "local": {
            name: (
                "passed"
                if dict(checks.get(name) or {}).get("status") == "passed"
                and dict(checks.get(name) or {}).get("binding") == (
                    dict(context.get("local_bindings") or {}).get(name)
                    or context["source_content_sha256"]
                )
                else "pending"
            )
            for name in LOCAL_CHECKS
        },
        "external": external,
        "required_external": {
            gate: external[gate] for gate in REQUIRED_RELEASE_GATES
        },
        "optional_external": {
            gate: status
            for gate, status in external.items()
            if gate not in REQUIRED_RELEASE_GATES
        },
        "credentials": str(dict(state.get("credentials") or {}).get("status") or "pending"),
        "catalog": context["catalog"],
        "worker_tree": context["worker"]["tree"],
        "bindings": {gate: gate_binding(gate, context) for gate in EXTERNAL_GATES},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state", type=Path,
        default=ROOT / "runtime" / "reports" / "final-acceptance-state.json",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run-local")
    run_parser.add_argument("--force", action="store_true")
    subparsers.add_parser("status")
    record_parser = subparsers.add_parser("record")
    record_parser.add_argument("--gate", required=True, choices=EXTERNAL_GATES)
    record_parser.add_argument("--artifact", required=True, type=Path)
    authorize_parser = subparsers.add_parser("authorize")
    authorize_parser.add_argument("--no-open-browser", action="store_true")
    authorize_parser.add_argument("--proxy", default="", help="Explicit HTTP/HTTPS proxy for GitHub")
    migrate_parser = subparsers.add_parser("migrate-credentials")
    migrate_parser.add_argument("--active", type=Path, default=ROOT / "runtime" / "data" / "credentials.json")
    migrate_parser.add_argument("--source", type=Path, required=True)
    verify_parser = subparsers.add_parser("verify-credentials")
    verify_parser.add_argument("--active", type=Path, default=ROOT / "runtime" / "data" / "credentials.json")
    verify_parser.add_argument("--proxy", default="", help="Explicit HTTP/HTTPS proxy for GitHub")
    inventory_parser = subparsers.add_parser("credential-inventory")
    inventory_parser.add_argument("--active", type=Path, default=ROOT / "runtime" / "data" / "credentials.json")
    inventory_parser.add_argument("--candidate", action="append", type=Path, default=[])
    retire_parser = subparsers.add_parser("retire-credentials")
    retire_parser.add_argument("--active", type=Path, default=ROOT / "runtime" / "data" / "credentials.json")
    retire_parser.add_argument("--candidate", action="append", type=Path, required=True)
    args = parser.parse_args(argv)

    state_path = args.state.resolve()
    state = _load_state(state_path)
    context = build_context(ROOT)
    if args.command == "run-local":
        passed = run_local_checks(state, context, root=ROOT, force=args.force)
        _atomic_json(state_path, state)
        print(json.dumps(summary(state, context), ensure_ascii=False))
        return 0 if passed else 1
    if args.command == "status":
        print(json.dumps(summary(state, context), ensure_ascii=False, indent=2))
        return 0
    if args.command == "record":
        record_gate(state, args.gate, args.artifact.resolve(), context)
        _atomic_json(state_path, state)
    elif args.command == "authorize":
        active = ROOT / "runtime" / "data" / "credentials.json"
        authorize_active_credentials(
            active, open_browser=not args.no_open_browser, proxy_url=args.proxy
        )
    elif args.command == "migrate-credentials":
        active = args.active.resolve()
        result = migrate_legacy_credentials(
            active=active,
            source=args.source.resolve(),
            allowed_sources=discover_legacy_credentials(ROOT, active=active),
        )
        state.setdefault("credentials", {})["migration"] = result
        _atomic_json(state_path, state)
    elif args.command == "verify-credentials":
        verify_active_credentials(
            state, args.active.resolve(), context, proxy_url=args.proxy
        )
        _atomic_json(state_path, state)
    elif args.command == "credential-inventory":
        print(json.dumps(credential_inventory(args.candidate, active=args.active), indent=2))
        return 0
    elif args.command == "retire-credentials":
        retired = retire_legacy_credentials(
            state, active=args.active.resolve(), candidates=args.candidate, context=context,
            allowed_candidates=discover_legacy_credentials(ROOT, active=args.active.resolve()),
        )
        _atomic_json(state_path, state)
        print(json.dumps({"retired": retired}, indent=2))
        return 0
    evidence = ROOT / "runtime" / "reports" / "final-acceptance-evidence.json"
    write_external_evidence(state, context, evidence)
    print(json.dumps(summary(state, context), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
