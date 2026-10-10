"""Run the privacy-safe, resumable real cloud automation acceptance lifecycle.

The driver only orchestrates the production AutomationService.  Its durable
state and final evidence contain hashes, counts, GitHub run IDs, and closed-set
status values; account, course, credential, URL, and content values are never
serialized or printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from credentials import CredentialStore
from src.application import CourseLensApplication
from src.remote.github_app import GitHubAppClient, GitHubAppError
from src.runtime.automation import (
    CLOUD_DAILY_WORKFLOW,
    CLOUD_PROTOCOL_VERSION,
    CLOUD_SECRET_NAMES,
    CLOUD_VARIABLE_NAMES,
    CLOUD_VERIFY_WORKFLOW,
)
from src.runtime.catalog_repository import CatalogRepository


STATE_SCHEMA = "courselens.cloud-automation-acceptance-state.v1"
EVIDENCE_SCHEMA = "courselens.acceptance-gate-evidence.v1"
STAGES = (
    "new",
    "configured",
    "verified",
    "enabled",
    "manual_completed_offline",
    "manual_imported",
    "circuit_reset",
    "scheduled_completed",
    "disabled",
    "revoked",
    "erased",
    "passed",
)
TERMINAL_RUN_STATES = {"completed"}
ACTIVE_RUN_STATES = {"queued", "in_progress", "waiting", "requested", "pending"}
SHANGHAI = timezone(timedelta(hours=8))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(_canonical(value) + b"\n")
    os.replace(temporary, path)


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema": STATE_SCHEMA,
            "session": secrets.token_hex(12),
            "stage": "new",
            "runs": {},
            "checks": {},
        }
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA:
        raise RuntimeError("cloud acceptance state has an unsupported schema")
    if value.get("stage") not in STAGES:
        raise RuntimeError("cloud acceptance state has an invalid stage")
    return value


def _fresh_state(*, previous_sha256: str = "") -> dict[str, Any]:
    value = {
        "schema": STATE_SCHEMA,
        "session": secrets.token_hex(12),
        "stage": "new",
        "runs": {},
        "checks": {},
    }
    if previous_sha256:
        value["previous_attempt_sha256"] = previous_sha256
    return value


def _stage_index(value: str) -> int:
    return STAGES.index(str(value))


def _at_least(state: dict[str, Any], stage: str) -> bool:
    return _stage_index(str(state.get("stage") or "new")) >= _stage_index(stage)


def _advance(path: Path, state: dict[str, Any], stage: str, **fields: Any) -> None:
    if _stage_index(stage) < _stage_index(str(state.get("stage") or "new")):
        raise RuntimeError("cloud acceptance stage cannot move backwards")
    state.update(fields)
    state["stage"] = stage
    state["updated_at"] = time.time()
    _atomic_json(path, state)
    print(json.dumps({"stage": stage, "status": "confirmed"}), flush=True)


def _parse_timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _next_schedule_time(now: datetime | None = None) -> tuple[str, float]:
    current = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    candidate = current.replace(minute=30, second=0, microsecond=0)
    if candidate <= current + timedelta(minutes=20):
        candidate += timedelta(hours=1)
    return candidate.strftime("%H:%M"), candidate.timestamp()


def _select_course(data_dir: Path) -> tuple[str, str, int]:
    repository = CatalogRepository(data_dir / "state.db")
    try:
        candidates = [
            str(item.get("course_id") or "")
            for item in repository.courses()
            if str(item.get("authorization_state") or "") == "verified"
            and repository.lectures_for_course(str(item.get("course_id") or ""))
        ]
    finally:
        repository.close()
    if not candidates:
        raise RuntimeError("no verified authorized course is available for cloud acceptance")
    selected = min(candidates, key=lambda value: _sha256(value.encode("utf-8")))
    return selected, _sha256(selected.encode("utf-8")), len(candidates)


def _credentials(data_dir: Path) -> tuple[CredentialStore, str, str, str]:
    store = CredentialStore(data_dir / "credentials.json")
    accounts = [item for item in store.list_accounts() if not item.get("requires_rotation")]
    if len(accounts) != 1:
        raise RuntimeError("cloud acceptance requires exactly one active saved account")
    student_id, password = store.load(str(accounts[0]["student_id"]))
    if not store.has_deepseek_key() or store.deepseek_key_requires_rotation():
        raise RuntimeError("cloud acceptance requires one active saved DeepSeek key")
    return store, student_id, password, store.load_deepseek_key()


def _proxy(store: CredentialStore, explicit: str) -> str:
    if explicit:
        return explicit
    if store.has_secret("network_github_proxy"):
        return store.load_secret("network_github_proxy")
    return ""


def _new_application(data_dir: Path, student_id: str) -> CourseLensApplication:
    os.environ["COURSELENS_DISABLE_AUTOMATION_MONITOR"] = "1"
    application = CourseLensApplication(data_dir)
    application.use_saved_credentials(student_id)
    return application


def _operation_id(state: dict[str, Any], action: str) -> str:
    operations = state.setdefault("operations", {})
    value = str(operations.get(action) or "")
    if not value:
        retry = int(state.get("manual_retry") or 0) if action == "run-now" else 0
        suffix = f"-r{retry}" if retry else ""
        value = f"acceptance-{state['session']}-{action}{suffix}"
        operations[action] = value
    return value


def _perform(
    state_path: Path,
    state: dict[str, Any],
    application: CourseLensApplication,
    action: str,
) -> dict[str, Any]:
    _atomic_json(state_path, state)
    result = application.automation.action(
        action, operation_id=_operation_id(state, action)
    )
    if result.get("state") != "accepted":
        raise RuntimeError(f"automation action {action} was not accepted")
    return result


def _wait(
    description: str,
    fetch: Callable[[], Any],
    accept: Callable[[Any], bool],
    *,
    timeout: float,
    interval: float = 15.0,
) -> Any:
    deadline = time.monotonic() + timeout
    last_notice = 0.0
    last_error = ""
    while time.monotonic() < deadline:
        try:
            value = fetch()
            last_error = ""
            if accept(value):
                return value
        except GitHubAppError as exc:
            last_error = str(exc.code or "github_unreachable")
        now = time.monotonic()
        if now - last_notice >= 60:
            print(json.dumps({
                "stage": description,
                "status": "waiting",
                "last_error": last_error,
                "remaining_seconds": max(0, round(deadline - now)),
            }), flush=True)
            last_notice = now
        time.sleep(min(interval, max(0.1, deadline - time.monotonic())))
    raise TimeoutError(f"timed out while waiting for {description}")


def _new_run(
    github: GitHubAppClient,
    workflow: str,
    *,
    baseline: set[int],
    requested_at: float,
    event: str,
    timeout: float,
) -> dict[str, Any]:
    return _wait(
        f"{workflow}:{event}:created",
        lambda: github.list_workflow_runs(workflow, limit=40),
        lambda rows: any(
            int(row.get("id") or 0) not in baseline
            and str(row.get("event") or "") == event
            and _parse_timestamp(str(row.get("created_at") or "")) >= requested_at - 5
            for row in rows
        ),
        timeout=timeout,
    ) and next(
        row
        for row in github.list_workflow_runs(workflow, limit=40)
        if int(row.get("id") or 0) not in baseline
        and str(row.get("event") or "") == event
        and _parse_timestamp(str(row.get("created_at") or "")) >= requested_at - 5
    )


def _wait_run(github: GitHubAppClient, workflow: str, run_id: int, timeout: float) -> dict[str, Any]:
    rows = _wait(
        f"{workflow}:run:{run_id}",
        lambda: github.list_workflow_runs(workflow, limit=50),
        lambda values: any(
            int(item.get("id") or 0) == run_id
            and str(item.get("status") or "") in TERMINAL_RUN_STATES
            for item in values
        ),
        timeout=timeout,
    )
    run = next(item for item in rows if int(item.get("id") or 0) == run_id)
    if run.get("conclusion") != "success":
        raise RuntimeError(
            f"{workflow} run {run_id} ended with {run.get('conclusion') or 'unknown'}"
        )
    return run


def _wait_verification(application: CourseLensApplication, timeout: float) -> dict[str, Any]:
    snapshot = _wait(
        "cloud-verification",
        lambda: application.automation.reconcile(force=True),
        lambda value: bool(value.get("verified"))
        or str(dict(value.get("verification") or {}).get("state") or "") == "failed",
        timeout=timeout,
    )
    if not snapshot.get("verified"):
        raise RuntimeError("cloud credential verification failed")
    return snapshot


def _cloud_artifacts_for_run(github: GitHubAppClient, run_id: int) -> list[dict[str, Any]]:
    return [
        item
        for item in github.list_worker_artifacts(prefix="courselens-cloud-", limit=100)
        if int(item.get("workflow_run_id") or 0) == int(run_id)
    ]


def _remote_cloud_is_clean(github: GitHubAppClient, store: CredentialStore) -> bool:
    resources = github.inspect_managed_resources()
    secret_names = set(resources.get("secret_names") or [])
    variables = set(dict(resources.get("variables") or {}))
    active_runs = [
        item
        for workflow in (CLOUD_DAILY_WORKFLOW, CLOUD_VERIFY_WORKFLOW)
        for item in github.list_workflow_runs(workflow, limit=40)
        if str(item.get("status") or "") in ACTIVE_RUN_STATES
    ]
    local_keys = any(
        store.has_secret(name)
        for name in ("cloud_result_private_key", "cloud_result_public_key", "cloud_state_key")
    )
    return not any((
        set(CLOUD_SECRET_NAMES) & secret_names,
        set(CLOUD_VARIABLE_NAMES) & variables,
        github.list_worker_artifacts(prefix="courselens-cloud-", limit=100),
        active_runs,
        local_keys,
    ))


def _final_checks(
    application: CourseLensApplication,
    github: GitHubAppClient,
    manual_run: int,
    scheduled_run: int,
) -> dict[str, Any]:
    snapshot = application.automation.reconcile(force=True)
    resources = github.inspect_managed_resources()
    secret_names = set(resources.get("secret_names") or [])
    variables = set(dict(resources.get("variables") or {}))
    artifacts = github.list_worker_artifacts(prefix="courselens-cloud-", limit=100)
    active_runs = [
        item
        for workflow in (CLOUD_DAILY_WORKFLOW, CLOUD_VERIFY_WORKFLOW)
        for item in github.list_workflow_runs(workflow, limit=40)
        if str(item.get("status") or "") in ACTIVE_RUN_STATES
    ]
    pending_imports = [
        item
        for item in snapshot.get("imports") or []
        if str(item.get("state") or "") in {
            "available", "downloading", "verifying", "importing", "cleanup_pending"
        }
    ]
    local_keys = [
        name
        for name in ("cloud_result_private_key", "cloud_result_public_key", "cloud_state_key")
        if application.credentials.has_secret(name)
    ]
    checks = {
        "state_disabled": snapshot.get("state") == "disabled",
        "cloud_secrets_zero": not (set(CLOUD_SECRET_NAMES) & secret_names),
        "cloud_variables_zero": not (set(CLOUD_VARIABLE_NAMES) & variables),
        "cloud_artifacts_zero": len(artifacts) == 0,
        "active_runs_zero": len(active_runs) == 0,
        "pending_imports_zero": len(pending_imports) == 0,
        "local_cloud_keys_zero": len(local_keys) == 0,
        "manual_run_recorded": any(
            int(item.get("github_run_id") or 0) == manual_run
            and item.get("conclusion") == "success"
            for item in snapshot.get("runs") or []
        ),
        "scheduled_run_recorded": any(
            int(item.get("github_run_id") or 0) == scheduled_run
            and item.get("trigger_kind") == "schedule"
            and item.get("conclusion") == "success"
            for item in snapshot.get("runs") or []
        ),
    }
    if not all(checks.values()):
        failed = sorted(name for name, passed in checks.items() if not passed)
        raise RuntimeError("cloud cleanup audit failed: " + ",".join(failed))
    return checks


def run(args: argparse.Namespace) -> Path:
    data_dir = args.data_dir.resolve()
    state_path = args.state.resolve()
    evidence_path = args.evidence.resolve()
    state = _load_state(state_path)
    store, student_id, password, deepseek_key = _credentials(data_dir)
    proxy_url = _proxy(store, args.proxy)
    github = GitHubAppClient(store, proxy_url=proxy_url)
    if (
        state.get("stage") not in {"new", "erased", "passed"}
        and _remote_cloud_is_clean(github, store)
    ):
        prior_digest = _sha256(_canonical(state))
        state = _fresh_state(previous_sha256=prior_digest)
        _atomic_json(state_path, state)
        print(json.dumps({
            "stage": "new",
            "status": "restarted_after_verified_cleanup",
            "previous_attempt_sha256": prior_digest,
        }), flush=True)
    course_id, course_hash, course_count = _select_course(data_dir)
    integrity = github.check_worker_integrity()
    if not integrity.get("trusted"):
        raise RuntimeError("approved Worker integrity verification failed")

    if not state.get("schedule_time"):
        schedule_time, schedule_at = _next_schedule_time()
        state.update({
            "schedule_time": schedule_time,
            "schedule_at": schedule_at,
            "course_hash": course_hash,
            "authorized_course_count": course_count,
            "worker_tree": str(integrity.get("actual_tree") or ""),
            "protocol": CLOUD_PROTOCOL_VERSION,
        })
        _atomic_json(state_path, state)
    elif state.get("course_hash") != course_hash:
        raise RuntimeError("deterministic acceptance course changed after the lifecycle started")

    application: CourseLensApplication | None = _new_application(data_dir, student_id)
    try:
        if not _at_least(state, "configured"):
            application.automation.update_config({
                "config": {
                    "mode": "cloud",
                    "schedule_time": str(state["schedule_time"]),
                    "budget": {
                        "max_lectures": 1,
                        "max_runner_minutes": 60,
                        "max_deepseek_tokens": 1000,
                    },
                },
                "rules": [{
                    "course_id": course_id,
                    "discovery_only": True,
                    "only_new": True,
                    "ocr": False,
                    "summary": False,
                    "chapters": False,
                    "quiz_after_import": False,
                    "priority": 100,
                    "max_lecture_minutes": 600,
                }],
            })
            application.automation.upload_cloud_secrets({
                "student_id": student_id,
                "password": password,
                "deepseek_api_key": deepseek_key,
            })
            _advance(state_path, state, "configured")
        password = ""
        deepseek_key = ""

        if not _at_least(state, "verified"):
            _perform(state_path, state, application, "verify-cloud-credentials")
            verified = _wait_verification(application, args.verify_timeout)
            verification = dict(verified.get("verification") or {})
            state.setdefault("runs", {})["verification"] = int(verification.get("run_id") or 0)
            _advance(state_path, state, "verified")

        if not _at_least(state, "enabled"):
            _perform(state_path, state, application, "enable-cloud")
            baseline = {
                int(item.get("id") or 0)
                for item in github.list_workflow_runs(CLOUD_DAILY_WORKFLOW, limit=40)
            }
            state["manual_baseline"] = sorted(baseline)
            _advance(state_path, state, "enabled")

        if not _at_least(state, "manual_completed_offline"):
            failed_run = int(dict(state.get("runs") or {}).get("manual_failed") or 0)
            if failed_run:
                retries = int(state.get("manual_retry") or 0) + 1
                if retries > 2:
                    raise RuntimeError("manual cloud acceptance exhausted bounded retries")
                state["manual_retry"] = retries
                state.setdefault("operations", {}).pop("run-now", None)
                state.setdefault("runs", {}).pop("manual_failed", None)
                state.pop("last_error", None)
                state["manual_baseline"] = sorted({
                    int(item.get("id") or 0)
                    for item in github.list_workflow_runs(CLOUD_DAILY_WORKFLOW, limit=40)
                })
                _atomic_json(state_path, state)
                print(json.dumps({
                    "stage": "manual-retry",
                    "status": "bounded-retry",
                    "attempt": retries + 1,
                }), flush=True)
            requested_at = time.time()
            _perform(state_path, state, application, "run-now")
            application.close()
            application = None
            manual = _new_run(
                github,
                CLOUD_DAILY_WORKFLOW,
                baseline=set(int(value) for value in state.get("manual_baseline") or []),
                requested_at=requested_at,
                event="workflow_dispatch",
                timeout=args.run_timeout,
            )
            try:
                manual = _wait_run(
                    github, CLOUD_DAILY_WORKFLOW, int(manual["id"]), args.run_timeout
                )
            except (RuntimeError, TimeoutError):
                state.setdefault("runs", {})["manual_failed"] = int(manual["id"])
                state["last_error"] = "manual_run_failed"
                _atomic_json(state_path, state)
                raise
            state.setdefault("runs", {})["manual"] = int(manual["id"])
            state["manual_client_closed_before_completion"] = True
            _advance(state_path, state, "manual_completed_offline")

        if application is None:
            application = _new_application(data_dir, student_id)
        manual_run = int(dict(state.get("runs") or {}).get("manual") or 0)
        if not _at_least(state, "manual_imported"):
            imported = _wait(
                "offline-result-import",
                lambda: application.automation.reconcile(force=True),
                lambda value: any(
                    int(item.get("run_id") or 0) == manual_run
                    and str(item.get("state") or "") == "imported"
                    for item in value.get("imports") or []
                )
                and not _cloud_artifacts_for_run(github, manual_run),
                timeout=args.import_timeout,
            )
            imported_rows = [
                item for item in imported.get("imports") or []
                if int(item.get("run_id") or 0) == manual_run
                and item.get("state") == "imported"
            ]
            state["manual_import_count"] = len(imported_rows)
            _advance(state_path, state, "manual_imported")

        if not _at_least(state, "circuit_reset"):
            _perform(state_path, state, application, "reset-circuit")
            reset = _wait_verification(application, args.verify_timeout)
            state.setdefault("runs", {})["reset"] = int(
                dict(reset.get("verification") or {}).get("run_id") or 0
            )
            _advance(state_path, state, "circuit_reset")

        if not _at_least(state, "scheduled_completed"):
            expected_at = float(state.get("schedule_at") or 0)
            if expected_at < time.time() - 20 * 60:
                raise RuntimeError("saved real schedule window expired before it was observed")
            baseline = {
                int(item.get("id") or 0)
                for item in github.list_workflow_runs(CLOUD_DAILY_WORKFLOW, limit=40)
            }
            scheduled = _new_run(
                github,
                CLOUD_DAILY_WORKFLOW,
                baseline=baseline,
                requested_at=expected_at - 5 * 60,
                event="schedule",
                timeout=max(args.schedule_timeout, expected_at - time.time() + 40 * 60),
            )
            scheduled = _wait_run(
                github, CLOUD_DAILY_WORKFLOW, int(scheduled["id"]), args.run_timeout
            )
            state.setdefault("runs", {})["scheduled"] = int(scheduled["id"])
            application.automation.reconcile(force=True)
            _advance(state_path, state, "scheduled_completed")

        if not _at_least(state, "disabled"):
            _perform(state_path, state, application, "disable-cloud")
            _advance(state_path, state, "disabled")
        if not _at_least(state, "revoked"):
            _perform(state_path, state, application, "revoke-cloud-credentials")
            _advance(state_path, state, "revoked")
        if not _at_least(state, "erased"):
            _perform(state_path, state, application, "erase-cloud-data")
            _advance(state_path, state, "erased")

        manual_run = int(dict(state.get("runs") or {}).get("manual") or 0)
        scheduled_run = int(dict(state.get("runs") or {}).get("scheduled") or 0)
        checks = _final_checks(application, github, manual_run, scheduled_run)
        from scripts.final_acceptance import build_context, gate_binding

        context = build_context(ROOT)
        evidence = {
            "schema": EVIDENCE_SCHEMA,
            "gate": "cloud_automation",
            "status": "passed",
            "binding": gate_binding("cloud_automation", context),
            "protocol": CLOUD_PROTOCOL_VERSION,
            "worker_tree": str(integrity.get("actual_tree") or ""),
            "course_hash": course_hash,
            "authorized_course_count": course_count,
            "runs": {
                "verification": int(dict(state.get("runs") or {}).get("verification") or 0),
                "manual": manual_run,
                "circuit_reset": int(dict(state.get("runs") or {}).get("reset") or 0),
                "scheduled": scheduled_run,
            },
            "offline_import_count": int(state.get("manual_import_count") or 0),
            "checks": checks,
        }
        _atomic_json(evidence_path, evidence)
        _advance(
            state_path,
            state,
            "passed",
            evidence_sha256=_sha256(evidence_path.read_bytes()),
        )
        return evidence_path
    finally:
        password = ""
        deepseek_key = ""
        course_id = ""
        if application is not None:
            application.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "runtime" / "data")
    parser.add_argument(
        "--state", type=Path,
        default=ROOT / "runtime" / "reports" / "cloud-automation-acceptance-state.json",
    )
    parser.add_argument(
        "--evidence", type=Path,
        default=ROOT / "runtime" / "reports" / "cloud-automation-acceptance-evidence.json",
    )
    parser.add_argument("--proxy", default="")
    parser.add_argument("--verify-timeout", type=float, default=20 * 60)
    parser.add_argument("--run-timeout", type=float, default=90 * 60)
    parser.add_argument("--import-timeout", type=float, default=10 * 60)
    parser.add_argument("--schedule-timeout", type=float, default=100 * 60)
    args = parser.parse_args(argv)
    evidence = run(args)
    print(json.dumps({
        "status": "passed",
        "evidence_sha256": _sha256(evidence.read_bytes()),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
