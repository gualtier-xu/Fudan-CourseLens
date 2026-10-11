"""Run one canonical, no-course-content process canary through process.yml.

The default command is read-only preflight.  Dispatch requires both
``--dispatch-once`` and the exact verifier token.  Normal remote compute must
remain disabled before, during, and after this one-shot verification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from credentials import CredentialStore
from path_utils import DEFAULT_DATA_DIR
from src.remote.coordinator import RemoteCoordinator, RemoteSettings
from src.remote.github_app import GitHubAppClient, _bundled_worker_config
from src.remote.github_client import GitHubClient, _parse_github_time
from src.remote.protocol import (
    JOB_SCHEMA,
    PROCESS_CANARY_FIXTURE_BYTES,
    PROCESS_CANARY_FIXTURE_RECORDS,
    PROCESS_CANARY_FIXTURE_SHA256,
    PROCESS_CANARY_PIPELINE,
    PROCESS_CANARY_SCHEMA,
    PROTOCOL_VERSION,
    finalize_job,
    generate_box_keypair,
    seal_job,
    validate_process_canary_result,
)
from src.remote.worker_migration import build_signed_template_transition_gate
from src.runtime.task_store import TaskStore


VERIFIER_GO = "PROCESS-CANARY-GO"
RERUN_VERIFIER_GO = "PROCESS-CANARY-RERUN-GO"
CLEANUP_VERIFIER_GO = "PROCESS-CANARY-CLEANUP-GO"
REFRESH_PREPARATION_VERIFIER_GO = "PROCESS-CANARY-REFRESH-PREPARATION-GO"
WORKFLOW = "process.yml"
LATCH_SECRET = "process_canary_latch"
RERUN_BUNDLE_PREFIX = "process_canary_rerun_bundle:"
LATCH_V1_SCHEMA = "courselens.process-canary-latch.v1"
LATCH_V2_SCHEMA = "courselens.process-canary-latch.v2"
RERUN_BUNDLE_SCHEMA = "courselens.process-canary-rerun-bundle.v1"
RERUN_REFRESH_SCHEMA = "courselens.process-canary-rerun-refresh.v1"
RERUN_KIND = "mailbox_wait_without_published_payload"
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_LOG_FORBIDDEN = (
    b'{"records":[[0,1,0,-1]',
    b"'records': [[0, 1, 0, -1]",
    b'fixture.v1',
    b'"source_session"',
    b'"course_id"',
    b'"sub_id"',
    b'"account"',
    b'"password"',
    b'authorization: bearer',
    b'cookie:',
)


def _remote_flag(credentials: Any) -> str:
    if not credentials.has_secret("remote_enabled"):
        raise RuntimeError("remote_enabled must be explicitly false")
    return str(credentials.load_secret("remote_enabled")).strip().lower()


def _require_remote_disabled(credentials: Any) -> None:
    for name in ("FUDAN_COURSELENS_REMOTE_ENABLED", "REMOTE_COMPUTE_ENABLED"):
        if str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}:
            raise RuntimeError("effective remote compute must remain disabled for a process canary")
    if _remote_flag(credentials) not in {"0", "false", "off", "no"}:
        raise RuntimeError("remote_enabled must remain false for a process canary")


def _executor_repositories(credentials: Any, current: str) -> tuple[str, ...]:
    """The only executor is the configured personal Worker repository."""
    return (str(current),)


def _public_template_run_audit(
    github_app: Any, *, token: str, created_before: float = 0.0,
    expected_totals: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Read-only quiescence evidence for the public template repository.

    The public template only publishes signed releases; it must never execute
    real tasks.  The audit pages the pinned template's process/echo workflow
    runs, reports any non-completed run plus, when ``created_before`` is
    given, any run created after that canary window start, and rejects any
    workflow whose total run count grew beyond ``expected_totals``.
    """
    template = str(_bundled_worker_config().get("repository") or "")
    if not template:
        raise RuntimeError("public template repository pin is unavailable")
    non_completed = 0
    late_runs = 0
    total_runs = 0
    workflow_totals: dict[str, int] = {}
    for workflow in (WORKFLOW, "echo.yml"):
        total_count: int | None = None
        raw_count = 0
        for page in range(1, 11):
            payload = github_app._api(
                "GET",
                f"/repos/{template}/actions/workflows/{workflow}/runs",
                token=token,
                expected=(200,),
                params={"per_page": 100, "page": page},
            ).json() or {}
            runs = payload.get("workflow_runs")
            value = payload.get("total_count")
            if (
                not isinstance(runs, list)
                or type(value) is not int
                or value < 0
                or total_count is not None and value != total_count
            ):
                raise RuntimeError("public template run inventory is incomplete")
            total_count = value
            for run in runs:
                if not isinstance(run, dict):
                    raise RuntimeError("public template run inventory is incomplete")
                raw_count += 1
                total_runs += 1
                if str(run.get("status") or "").strip().lower() != "completed":
                    non_completed += 1
                raw_created = str(run.get("created_at") or "")
                created_at = _parse_github_time(raw_created)
                if created_before:
                    # A missing or unparsable timestamp must fail closed, never
                    # silently skip the window comparison.
                    if not raw_created or created_at <= 0.0:
                        raise RuntimeError("public template run created_at is unavailable")
                    if created_at > created_before:
                        late_runs += 1
            if len(runs) < 100:
                break
        else:
            raise RuntimeError("public template run inventory is too large")
        if raw_count != total_count:
            raise RuntimeError("public template run inventory is incomplete")
        workflow_totals[workflow] = int(total_count or 0)
        expected = int((expected_totals or {}).get(workflow) or 0)
        if expected and int(total_count or 0) > expected:
            raise RuntimeError("public template gained new runs during the canary window")
    return {
        "repository": template,
        "total_run_count": total_runs,
        "workflow_run_totals": workflow_totals,
        "non_completed_run_count": non_completed,
        "late_run_count": late_runs,
    }


def _save_latch(
    credentials: Any,
    *,
    task_id: str,
    commit: str,
    state: str,
    run_id: int = 0,
    rerun_budget: int = 1,
) -> None:
    credentials.save_secret(LATCH_SECRET, json.dumps({
        "schema": LATCH_V2_SCHEMA,
        "task_id": task_id,
        "worker_commit": commit,
        "state": state,
        "run_id": int(run_id),
        "attempt_cap": 2,
        "rerun_budget": int(rerun_budget),
    }, sort_keys=True, separators=(",", ":")))


def _abandon_preauthorization_failed_attempt(
    *,
    credentials: Any,
    task_store: Any,
    github: Any,
    latch: dict[str, Any],
) -> bool:
    """Clear only a failed dispatch that never reached job authorization."""
    if latch.get("state") != "running":
        return False
    task_id = str(latch.get("task_id") or "")
    run_id = int(latch.get("run_id") or 0)
    remote = dict(task_store.get_remote_run(task_id) or {})
    attempt = dict(task_store.get_remote_attempt(task_id, 1) or {})
    if (
        int(run_id) <= 0
        or int(remote.get("run_id") or 0) != run_id
        or str(remote.get("remote_state") or "") != "failed"
        or int(remote.get("issue_number") or 0)
        or int(remote.get("artifact_id") or 0)
        or str(attempt.get("cleanup_state") or "") not in {"", "best_effort"}
        or credentials.has_secret(f"remote_result_private:{task_id}")
        or task_store.list_remote_token_leases()
    ):
        return False
    run = dict(github.get_run(str(remote.get("repository") or ""), run_id))
    if (
        str(run.get("status") or "") != "completed"
        or str(run.get("conclusion") or "") == "success"
        or str(run.get("head_sha") or "").lower() != str(latch.get("worker_commit") or "").lower()
        or str(run.get("event") or "") != "workflow_dispatch"
    ):
        return False
    credentials.delete_secret(LATCH_SECRET)
    return True


def _load_latch(credentials: Any) -> dict[str, str]:
    if not credentials.has_secret(LATCH_SECRET):
        return {}
    try:
        value = json.loads(str(credentials.load_secret(LATCH_SECRET)))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("process canary latch is invalid") from exc
    if not isinstance(value, dict):
        raise RuntimeError("process canary latch is invalid")
    schema = value.get("schema")
    common_valid = bool(
        re.fullmatch(r"^[0-9a-f]{32}$", str(value.get("task_id") or ""))
        and _COMMIT_RE.fullmatch(str(value.get("worker_commit") or ""))
    )
    if schema == LATCH_V1_SCHEMA:
        if (
            set(value) != {"schema", "task_id", "worker_commit", "state"}
            or not common_valid
            or value.get("state") not in {
                "prepared", "running", "result_verified", "failed", "complete",
            }
        ):
            raise RuntimeError("process canary latch is invalid")
        return dict(value)
    if (
        schema != LATCH_V2_SCHEMA
        or set(value) != {
            "schema", "task_id", "worker_commit", "state", "run_id",
            "attempt_cap", "rerun_budget",
        }
        or not common_valid
        or value.get("state") not in {
            "prepared", "running", "rerun_preparing", "rerun_payload_ready",
            "rerun_armed", "rerun_running", "result_verified",
            "cleanup_pending", "failed", "complete",
        }
        or type(value.get("run_id")) is not int
        or int(value.get("run_id") or 0) < 0
        or value.get("attempt_cap") != 2
        or type(value.get("rerun_budget")) is not int
        or value.get("rerun_budget") not in {0, 1}
    ):
        raise RuntimeError("process canary latch is invalid")
    return dict(value)


def build_process_canary_job(task_id: str, result_public_key: str) -> dict[str, Any]:
    now = time.time()
    return {
        "schema": JOB_SCHEMA,
        "protocol_version": PROTOCOL_VERSION,
        "task_id": task_id,
        "job_kind": "process_canary",
        "created_at": now,
        "expires_at": now + 600,
        "result_public_key": result_public_key,
        "pipeline": {"version": PROCESS_CANARY_PIPELINE},
        "payload": {},
        "secrets": {},
        "requested_outputs": [],
    }


def validate_import(result: dict[str, Any], *, expected_worker_commit: str) -> dict[str, Any]:
    validate_process_canary_result(result)
    canary = dict(dict(result["outputs"])["process_canary"])
    if canary != {
        "schema": PROCESS_CANARY_SCHEMA,
        "fixture_sha256": PROCESS_CANARY_FIXTURE_SHA256,
        "fixture_bytes": PROCESS_CANARY_FIXTURE_BYTES,
        "fixture_records": PROCESS_CANARY_FIXTURE_RECORDS,
        "worker_commit": expected_worker_commit,
        "workflow_profile": "process-v1",
    }:
        raise RuntimeError("process canary proof does not match the signed worker pin")
    return {"verified": True, "worker_commit": expected_worker_commit}


def _preflight_template_totals(preflight: dict[str, Any]) -> dict[str, int]:
    audit = dict((preflight or {}).get("public_template_audit") or {})
    return {
        str(name): int(value)
        for name, value in dict(audit.get("workflow_run_totals") or {}).items()
    }


def _require_configured_dispatch_target(credentials: Any, settings: Any) -> None:
    """Re-bind the dispatch target to the configured personal Worker secret.

    ``RemoteSettings.public_repo`` prefers the ``FUDAN_COURSELENS_PUBLIC_REPO``
    environment override, so every dispatch path re-asserts equality with the
    configured ``github_worker_repo`` before touching the coordinator.
    """
    configured_worker = str(credentials.load_secret("github_worker_repo"))
    if str(settings.public_repo).casefold() != configured_worker.casefold():
        raise RuntimeError(
            "process canary dispatch target must be the configured personal Worker repository"
        )


def build_preflight(
    *, credentials: Any, task_store: Any, github_app: Any, require_zero_state: bool = True,
    read_only: bool = False,
) -> dict[str, Any]:
    _require_remote_disabled(credentials)
    if read_only:
        integrity = dict(github_app.check_worker_integrity(read_only=True))
    else:
        integrity = dict(github_app.check_worker_integrity())
    expected_commit = str(integrity.get("actual_commit") or "").strip().lower()
    if (
        not integrity.get("trusted")
        or integrity.get("dispatch_mode") != "personal-worker"
        or not _COMMIT_RE.fullmatch(expected_commit)
    ):
        raise RuntimeError("personal worker signed pin is unavailable")
    repository = str(integrity.get("repository") or "")
    # RemoteSettings.public_repo prefers an environment override over the
    # configured Worker secret, so the dispatch target must be re-bound to the
    # exact repository the integrity evidence just audited.
    settings = RemoteSettings.load(credentials)
    if str(settings.public_repo).casefold() != repository.casefold():
        raise RuntimeError(
            "process canary dispatch target must be the audited personal Worker repository"
        )
    token = github_app.access_token(
        minimum_lifetime_seconds=900, no_refresh=read_only
    )
    template_audit = _public_template_run_audit(github_app, token=token)
    if template_audit.get("non_completed_run_count") != 0:
        raise RuntimeError("public template process runs are not quiescent")
    if require_zero_state:
        gate = build_signed_template_transition_gate(
            credentials=credentials,
            task_store=task_store,
            github_app=github_app,
            repositories=_executor_repositories(credentials, repository),
            read_only=read_only,
            # Runnable zero state: closed unconsumed Mailbox history is
            # reconcilable residue, not a live-payload blocker; it is reported
            # in the observations so the operator is directed to the repair.
            mailbox_mode="runnable",
        )
        if not gate.get("ready"):
            raise RuntimeError("process canary preflight zero-state gate failed")
        observations = dict(gate.get("observations") or {})
    else:
        observations = {"recovery_mode": 1}
    return {
        "schema": "courselens.process-canary-preflight.v1",
        "ready": True,
        "workflow": WORKFLOW,
        "protocol_version": PROTOCOL_VERSION,
        "expected_worker_commit": expected_commit,
        "audited_repository": repository,
        "public_template_audit": template_audit,
        "zero_state": observations,
        "required_verifier_go": VERIFIER_GO,
    }


def assert_public_log_redaction(raw_logs: bytes) -> None:
    lowered = bytes(raw_logs).lower()
    found = [
        marker.decode("ascii", errors="replace")
        for marker in _LOG_FORBIDDEN
        if marker in lowered
    ]
    if found:
        raise RuntimeError("process canary public log redaction failed")


def build_zero_state_audit(
    task_id: str,
    *,
    credentials: Any,
    task_store: Any,
    github_app: Any,
    github: Any,
    expected_worker_commit: str,
    expected_template_run_totals: dict[str, int] | None = None,
) -> dict[str, Any]:
    _require_remote_disabled(credentials)
    remote = dict(task_store.get_remote_run(task_id) or {})
    attempt = dict(
        task_store.get_remote_attempt(task_id, max(1, int(remote.get("attempt") or 1))) or {}
    )
    run_id = int(remote.get("run_id") or 0)
    issue_number = int(remote.get("issue_number") or 0)
    repository = str(remote.get("repository") or "")
    if not run_id or not issue_number or not repository:
        raise RuntimeError("process canary audit metadata is incomplete")
    run = dict(github.get_run(repository, run_id))
    mailbox = dict(
        github.job_cleanup_summary(
            str(credentials.load_secret("github_mailbox_repo")), issue_number
        )
    )
    # The canary window starts when the audited run was created; the public
    # template must have produced no run after that moment.
    raw_window_start = str(run.get("created_at") or "")
    window_start = _parse_github_time(raw_window_start)
    if not raw_window_start or window_start <= 0.0:
        raise RuntimeError("process canary window start timestamp is unavailable")
    token = github_app.access_token(minimum_lifetime_seconds=900, no_refresh=True)
    template_audit = _public_template_run_audit(
        github_app,
        token=token,
        created_before=window_start,
        expected_totals=dict(expected_template_run_totals or {}),
    )
    global_gate = build_signed_template_transition_gate(
        credentials=credentials,
        task_store=task_store,
        github_app=github_app,
        repositories=_executor_repositories(credentials, repository),
        mailbox_mode="runnable",
    )
    checks = {
        "run_completed": run.get("status") == "completed" and run.get("conclusion") == "success",
        "run_signed_head": str(run.get("head_sha") or "").lower() == expected_worker_commit,
        "stored_workflow": remote.get("workflow") == WORKFLOW,
        "remote_imported": remote.get("remote_state") == "imported",
        "attempt_imported": attempt.get("import_state") == "imported",
        "cleanup_complete": attempt.get("cleanup_state") == "complete",
        "mailbox_closed": mailbox.get("state") == "closed",
        "mailbox_consumed": mailbox.get("consumed") is True,
        "mailbox_comments_zero": int(mailbox.get("comment_count") or 0) == 0,
        "global_zero_state": global_gate.get("ready") is True,
        "template_runs_quiescent": int(template_audit.get("non_completed_run_count") or 0) == 0,
        "template_late_runs_zero": int(template_audit.get("late_run_count") or 0) == 0,
        "all_result_keys_zero": not list(
            credentials.list_secret_names(prefix="remote_result_private:")
        ),
        "all_rerun_bundles_zero": not list(
            credentials.list_secret_names(prefix=RERUN_BUNDLE_PREFIX)
        ),
        "all_rerun_refresh_markers_zero": not list(
            credentials.list_secret_names(prefix="process_canary_rerun_refresh:")
        ),
        "remote_disabled": _remote_flag(credentials) in {"0", "false", "off", "no"},
    }
    if not all(checks.values()):
        raise RuntimeError(
            "process canary postflight failed: "
            + ",".join(sorted(name for name, passed in checks.items() if not passed))
        )
    return {
        "schema": "courselens.process-canary-audit.v1",
        "status": "passed",
        "run_id": run_id,
        "worker_commit": expected_worker_commit,
        "checks": checks,
        "zero_state": dict(global_gate.get("observations") or {}),
        "public_template_audit": template_audit,
    }


def cleanup_recovery_job_token(
    task_id: str,
    *,
    remote: dict[str, Any],
    expected_worker_commit: str,
    task_store: Any,
    github_app: Any,
    github: Any,
) -> None:
    """Clear only this completed canary's crash-stale lease and Worker token."""
    run_id = int(remote.get("run_id") or 0)
    repository = str(remote.get("repository") or "")
    if not run_id or not repository:
        raise RuntimeError("process canary recovery run metadata is incomplete")
    run = dict(github.get_run(repository, run_id))
    if (
        run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or str(run.get("head_sha") or "").lower() != expected_worker_commit
    ):
        raise RuntimeError("process canary recovery run is not a signed success")
    github_app.cleanup_stale_job_token_lease(task_id=task_id, task_store=task_store)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _rerun_bundle_name(task_id: str) -> str:
    return f"{RERUN_BUNDLE_PREFIX}{task_id}"


def _rerun_refresh_name(task_id: str) -> str:
    return f"process_canary_rerun_refresh:{task_id}"


def _save_rerun_refresh_marker(
    credentials: Any, *, task_id: str, run_id: int, worker_commit: str, state: str
) -> None:
    credentials.save_secret(_rerun_refresh_name(task_id), json.dumps({
        "schema": RERUN_REFRESH_SCHEMA, "task_id": task_id, "run_id": int(run_id),
        "worker_commit": worker_commit, "state": state,
    }, sort_keys=True, separators=(",", ":")))


def _load_rerun_refresh_marker(
    credentials: Any, *, task_id: str, run_id: int, worker_commit: str
) -> str:
    name = _rerun_refresh_name(task_id)
    if not credentials.has_secret(name):
        return ""
    try:
        marker = json.loads(str(credentials.load_secret(name)))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("process canary rerun refresh marker is invalid") from exc
    if (
        not isinstance(marker, dict)
        or marker != {
            "schema": RERUN_REFRESH_SCHEMA, "task_id": task_id, "run_id": int(run_id),
            "worker_commit": worker_commit, "state": marker.get("state"),
        }
        or marker.get("state") not in {"started", "complete"}
    ):
        raise RuntimeError("process canary rerun refresh marker is invalid")
    return str(marker["state"])


def _delete_rerun_refresh_marker(credentials: Any, task_id: str) -> None:
    credentials.update_secrets({}, deletes=(_rerun_refresh_name(task_id),))


def _upsert_rerun_remote(
    task_store: Any, task_id: str, prior: dict[str, Any], **updates: Any
) -> dict[str, Any]:
    values = {
        key: prior.get(key)
        for key in (
            "repository", "workflow", "run_id", "attempt", "issue_number",
            "artifact_id", "remote_state", "input_hash", "pipeline_version",
            "checkpoint", "dispatched_at", "started_at", "finished_at",
            "imported_at", "last_error",
        )
    }
    values.update(updates)
    return dict(task_store.upsert_remote_run(task_id, **values))


def _load_rerun_bundle(
    credentials: Any,
    *,
    task_id: str,
    run_id: int,
    worker_commit: str,
    allow_expired: bool = False,
) -> dict[str, Any]:
    name = _rerun_bundle_name(task_id)
    if not credentials.has_secret(name):
        raise RuntimeError("process canary rerun bundle is missing")
    try:
        bundle = json.loads(str(credentials.load_secret(name)))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("process canary rerun bundle is invalid") from exc
    if (
        not isinstance(bundle, dict)
        or set(bundle) != {
            "schema", "task_id", "run_id", "worker_commit", "target_attempt",
            "input_hash", "expires_at", "envelope", "envelope_sha256",
        }
        or bundle.get("schema") != RERUN_BUNDLE_SCHEMA
        or bundle.get("task_id") != task_id
        or type(bundle.get("run_id")) is not int
        or bundle.get("run_id") != int(run_id)
        or bundle.get("worker_commit") != worker_commit
        or bundle.get("target_attempt") != 2
        or not re.fullmatch(r"^[0-9a-f]{64}$", str(bundle.get("input_hash") or ""))
        or not isinstance(bundle.get("envelope"), dict)
        or not re.fullmatch(r"^[0-9a-f]{64}$", str(bundle.get("envelope_sha256") or ""))
        or type(bundle.get("expires_at")) not in {int, float}
    ):
        raise RuntimeError("process canary rerun bundle is invalid")
    digest = hashlib.sha256(_canonical_json_bytes(bundle["envelope"])).hexdigest()
    if digest != bundle["envelope_sha256"]:
        raise RuntimeError("process canary rerun bundle digest is invalid")
    expires_at = float(bundle["expires_at"])
    if expires_at > time.time() + 1800 or (not allow_expired and expires_at <= time.time()):
        raise RuntimeError("process canary rerun bundle is expired")
    result_name = f"remote_result_private:{task_id}"
    if not credentials.has_secret(result_name):
        raise RuntimeError("process canary rerun result key is missing")
    return bundle


def _create_rerun_bundle(
    credentials: Any,
    *,
    task_id: str,
    run_id: int,
    worker_commit: str,
    worker_public_key: str,
) -> dict[str, Any]:
    result_private, result_public = generate_box_keypair()
    job = finalize_job(build_process_canary_job(task_id, result_public))
    if float(job.get("expires_at") or 0) > time.time() + 1800:
        raise RuntimeError("process canary rerun payload lifetime is too long")
    envelope = seal_job(job, worker_public_key)
    bundle = {
        "schema": RERUN_BUNDLE_SCHEMA,
        "task_id": task_id,
        "run_id": int(run_id),
        "worker_commit": worker_commit,
        "target_attempt": 2,
        "input_hash": str(job.get("input_hash") or ""),
        "expires_at": float(job.get("expires_at") or 0),
        "envelope": envelope,
        "envelope_sha256": hashlib.sha256(_canonical_json_bytes(envelope)).hexdigest(),
    }
    credentials.update_secrets({
        f"remote_result_private:{task_id}": result_private,
        _rerun_bundle_name(task_id): json.dumps(
            bundle, sort_keys=True, separators=(",", ":")
        ),
    })
    return _load_rerun_bundle(
        credentials, task_id=task_id, run_id=run_id, worker_commit=worker_commit
    )


def refresh_attempt_one_preparation_only(
    *, credentials: Any, task_store: Any, github_app: Any, verifier_go: str,
    inject_failure: Any = None,
) -> dict[str, Any]:
    """Renew an expired, unposted attempt-one rerun bundle without arming it.

    This deliberately has no path to Mailbox publication, imports, terminal
    latch transitions, or the GitHub rerun endpoint.  The marker makes the
    local token/bundle replacement convergent across process crashes.
    """
    if verifier_go != REFRESH_PREPARATION_VERIFIER_GO:
        raise RuntimeError("exact process canary refresh-preparation verifier GO token is required")
    _require_remote_disabled(credentials)
    latch = _load_latch(credentials)
    if (
        latch.get("schema") != LATCH_V2_SCHEMA
        or latch.get("state") != "rerun_preparing"
        or latch.get("rerun_budget") != 1
        or int(latch.get("run_id") or 0) <= 0
    ):
        raise RuntimeError("process canary refresh-preparation state is not exact")
    task_id = str(latch["task_id"])
    run_id = int(latch["run_id"])
    worker_commit = str(latch["worker_commit"])
    settings = RemoteSettings.load(credentials)
    repository = str(settings.public_repo)
    remote = dict(task_store.get_remote_run(task_id) or {})
    attempt_one = dict(task_store.get_remote_attempt(task_id, 1) or {})
    attempt_two = dict(task_store.get_remote_attempt(task_id, 2) or {})
    if (
        str(remote.get("repository") or "").casefold() != repository.casefold()
        or remote.get("workflow") != WORKFLOW
        or int(remote.get("run_id") or 0) != run_id
        or int(remote.get("attempt") or 0) != 1
        or remote.get("remote_state") != "rerun_preparing"
        or int(remote.get("issue_number") or 0) != 0
        or int(remote.get("artifact_id") or 0) != 0
        or str(remote.get("input_hash") or "")
        or str(remote.get("pipeline_version") or "")
        or dict(remote.get("checkpoint") or {})
        or str(attempt_one.get("repository") or "").casefold() != repository.casefold()
        or str(attempt_one.get("workflow") or "") != WORKFLOW
        or int(attempt_one.get("run_id") or 0) != run_id
        or str(attempt_one.get("cleanup_state") or "") not in {"", "not_started", "best_effort"}
        or int(attempt_two.get("run_id") or 0) != run_id
        or str(attempt_two.get("github_status") or "") != "not_requested"
        or str(attempt_two.get("worker_status") or "") != "planned"
        or str(attempt_two.get("worker_stage") or "") != "payload_preparing"
        or str(attempt_two.get("import_state") or "") != "not_started"
        or str(attempt_two.get("cleanup_state") or "") != "not_started"
    ):
        raise RuntimeError("process canary refresh-preparation state is not exact")
    # Check all live truth before touching local material.  A live binding
    # drift leaves the latch, lease, and old expired pair unchanged.
    github = GitHubClient(settings.github_token, proxy_url=settings.proxy_url)
    live = dict(github.get_run(repository, run_id))
    if (
        int(live.get("id") or 0) != run_id
        or int(live.get("run_attempt") or 0) != 1
        or live.get("status") != "completed"
        or live.get("conclusion") != "failure"
        or str(live.get("event") or "") != "workflow_dispatch"
        or str(live.get("head_sha") or "").lower() != worker_commit
        or _task_artifacts(github, repository, run_id, task_id)
        or github._matching_job_issues(str(settings.private_repo), task_id)
    ):
        raise RuntimeError("process canary refresh-preparation attempt-one binding drifted")
    if (
        str(attempt_one.get("github_status") or "") != "completed"
        or str(attempt_one.get("conclusion") or "") != "failure"
    ) and not task_store.reconcile_process_canary_attempt_one(
        task_id, repository=repository, run_id=run_id,
        github_status="completed", conclusion="failure",
    ):
        raise RuntimeError("process canary refresh-preparation local state changed")
    _require_existing_rerun_token(
        task_id=task_id, task_store=task_store, credentials=credentials, github_app=github_app
    )
    marker = _load_rerun_refresh_marker(
        credentials, task_id=task_id, run_id=run_id, worker_commit=worker_commit
    )
    if marker:
        try:
            bundle = _load_rerun_bundle(
                credentials, task_id=task_id, run_id=run_id, worker_commit=worker_commit
            )
        except RuntimeError as exc:
            if "expired" not in str(exc):
                raise
            # A historical complete marker cannot keep a later refresh from
            # starting; a started marker instead resumes the interrupted work.
            if marker == "complete":
                _delete_rerun_refresh_marker(credentials, task_id)
                marker = ""
        else:
            # New material exists but the process died before removing its
            # marker.  It is already bound and usable: converge without a
            # second token/key rotation.
            _delete_rerun_refresh_marker(credentials, task_id)
            return {"schema": RERUN_REFRESH_SCHEMA, "status": "refreshed", "run_id": run_id,
                    "attempt": 1, "expires_at": bundle["expires_at"]}
    if not marker:
        # A missing marker is eligible only when the currently durable bundle
        # is truly expired.  A completed refresh is recognized above instead.
        _load_rerun_bundle(
            credentials, task_id=task_id, run_id=run_id, worker_commit=worker_commit,
            allow_expired=True,
        )
        try:
            _load_rerun_bundle(
                credentials, task_id=task_id, run_id=run_id, worker_commit=worker_commit,
            )
        except RuntimeError as exc:
            if "expired" not in str(exc):
                raise
        else:
            raise RuntimeError("process canary rerun bundle is not expired")
        _save_rerun_refresh_marker(
            credentials, task_id=task_id, run_id=run_id, worker_commit=worker_commit,
            state="started",
        )
    _fault(inject_failure, "R_marker_before_token")
    # sync overwrites the one Worker secret and local token in place.  Repeating
    # it after a crash renews the same single lease rather than adding one.
    github_app.sync_job_token()
    task_store.set_remote_token_lease(task_id, state="active", expires_at=time.time() + 8 * 60 * 60)
    _fault(inject_failure, "S_token_before_bundle")
    refreshed = RemoteSettings.load(credentials)
    github = GitHubClient(refreshed.github_token, proxy_url=refreshed.proxy_url)
    _create_rerun_bundle(
        credentials, task_id=task_id, run_id=run_id, worker_commit=worker_commit,
        worker_public_key=refreshed.worker_public_key,
    )
    _fault(inject_failure, "T_bundle_before_marker_delete")
    _delete_rerun_refresh_marker(credentials, task_id)
    bundle = _load_rerun_bundle(
        credentials, task_id=task_id, run_id=run_id, worker_commit=worker_commit
    )
    return {"schema": RERUN_REFRESH_SCHEMA, "status": "refreshed", "run_id": run_id,
            "attempt": 1, "expires_at": bundle["expires_at"]}


def _require_rerun_live_trust(
    *,
    credentials: Any,
    task_store: Any,
    github_app: Any,
    require_global_zero: bool,
) -> dict[str, Any]:
    preflight = build_preflight(
        credentials=credentials,
        task_store=task_store,
        github_app=github_app,
        require_zero_state=require_global_zero,
    )
    target = str(credentials.load_secret("github_worker_repo"))
    mailbox = str(credentials.load_secret("github_mailbox_repo"))
    resources = dict(github_app.inspect_managed_resources())
    installation = dict(resources.get("installation") or {})
    worker = dict(resources.get("worker") or {})
    managed_mailbox = dict(resources.get("mailbox") or {})
    workflow = dict(dict(resources.get("workflows") or {}).get(WORKFLOW) or {})
    if not (
        installation.get("installed") is True
        and installation.get("repository_selection_exact") is True
        and str(worker.get("full_name") or "").casefold() == target.casefold()
        and str(managed_mailbox.get("full_name") or "").casefold() == mailbox.casefold()
        and workflow.get("exists") is True
        and str(workflow.get("state") or "") == "active"
        and resources.get("actions_enabled") is True
        and resources.get("environment_exists") is True
    ):
        raise RuntimeError("process canary rerun GitHub scope or permissions drifted")
    return preflight


def _task_artifacts(github: Any, repository: str, run_id: int, task_id: str) -> list[dict[str, Any]]:
    return [
        dict(item) for item in github.list_run_artifacts(repository, run_id)
        if str(item.get("name") or "") == f"courselens-result-{task_id}"
        or str(item.get("name") or "").startswith(f"courselens-checkpoint-{task_id}-")
    ]


def _require_initial_mailbox_wait_failure(
    *,
    credentials: Any,
    task_store: Any,
    github: Any,
    task_id: str,
    expected_worker_commit: str,
    repository: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    remote = dict(task_store.get_remote_run(task_id) or {})
    if (
        str(remote.get("repository") or "").casefold() != repository.casefold()
        or remote.get("workflow") != WORKFLOW
        or int(remote.get("run_id") or 0) <= 0
        or int(remote.get("attempt") or 0) != 1
        or remote.get("remote_state") != "failed"
        or remote.get("issue_number")
        or remote.get("artifact_id")
        or str(remote.get("input_hash") or "")
        or dict(remote.get("checkpoint") or {})
        or credentials.has_secret(f"remote_result_private:{task_id}")
        or credentials.has_secret(_rerun_bundle_name(task_id))
    ):
        raise RuntimeError("process canary rerun initial state is not exact")
    run_id = int(remote["run_id"])
    found = github.find_workflow_run(
        repository, workflow=WORKFLOW, ref="main", task_id=task_id,
        expected_head_sha=expected_worker_commit,
    )
    if found is None or int(found.run_id) != run_id:
        raise RuntimeError("process canary rerun workflow binding is not unique")
    run = dict(github.get_run(repository, run_id))
    if (
        int(run.get("id") or run_id) != run_id
        or str(run.get("head_sha") or "").lower() != expected_worker_commit
        or str(run.get("event") or "") != "workflow_dispatch"
        or int(run.get("run_attempt") or 0) != 1
        or run.get("status") != "completed"
        or run.get("conclusion") != "failure"
    ):
        raise RuntimeError("process canary rerun attempt-one binding drifted")
    failed_steps: set[str] = set()
    cleanup_ok = False
    process_failed = False
    for job in github.get_run_jobs(repository, run_id):
        for step in list(job.get("steps") or []):
            name = str(step.get("name") or "")
            if str(step.get("conclusion") or "") == "failure":
                failed_steps.add(name)
            if name == "Process encrypted job":
                process_failed = (
                    step.get("status") == "completed" and step.get("conclusion") == "failure"
                )
            if name == "Remove transient data":
                cleanup_ok = (
                    step.get("status") == "completed" and step.get("conclusion") == "success"
                )
    if (
        not process_failed
        or not cleanup_ok
        or not failed_steps
        or not failed_steps.issubset({"Process encrypted job", "Upload encrypted result only"})
        or _task_artifacts(github, repository, run_id, task_id)
        or github._matching_job_issues(str(credentials.load_secret("github_mailbox_repo")), task_id)
    ):
        raise RuntimeError("process canary failure is not mailbox_wait_without_published_payload")
    return remote, run


def _fault(inject_failure: Any, point: str) -> None:
    if inject_failure is not None:
        inject_failure(point)


def _require_existing_rerun_token(
    *, task_id: str, task_store: Any, credentials: Any, github_app: Any
) -> None:
    leases = list(task_store.list_remote_token_leases())
    if len(leases) != 1 or str(leases[0].get("task_id") or "") != task_id:
        raise RuntimeError("process canary rerun token lease is not exact")
    if str(leases[0].get("state") or "") != "active":
        raise RuntimeError("process canary rerun token lease is not active")
    if int(task_store.active_remote_run_count()) != 1:
        raise RuntimeError("another active remote task prevents process canary rerun")
    names = {str(item.get("name") or "") for item in github_app.list_worker_secrets()}
    if (
        "COURSELENS_JOB_TOKEN" not in names
        or not credentials.has_secret("github_remote_token")
        or credentials.has_secret("github_job_token_cleanup_pending")
    ):
        raise RuntimeError("process canary rerun token is not durable")


def _ensure_rerun_token(
    *, task_id: str, task_store: Any, credentials: Any, github_app: Any
) -> bool:
    leases = list(task_store.list_remote_token_leases())
    lease_ids = {str(item.get("task_id") or "") for item in leases}
    if lease_ids - {task_id} or len(leases) > 1:
        raise RuntimeError("another token lease prevents process canary rerun")
    live = int(getattr(github_app, "_job_token_leases", 0) or 0)
    if live:
        raise RuntimeError("a live in-process token lease prevents process canary rerun")
    if leases:
        try:
            _require_existing_rerun_token(
                task_id=task_id, task_store=task_store,
                credentials=credentials, github_app=github_app,
            )
            return False
        except RuntimeError:
            # A crash while acquiring the token can leave an "acquiring" row
            # or only one side of the token write.  No rerun has been armed at
            # this stage, so overwriting the same task's transient token is safe.
            pass
    github_app.acquire_job_token(task_id=task_id, task_store=task_store)
    _require_existing_rerun_token(
        task_id=task_id, task_store=task_store,
        credentials=credentials, github_app=github_app,
    )
    return True


def _wait_for_attempt_two(
    github: Any,
    *,
    repository: str,
    run_id: int,
    worker_commit: str,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        run = dict(github.get_run(repository, run_id))
        attempt = int(run.get("run_attempt") or 0)
        if (
            int(run.get("id") or run_id) != run_id
            or str(run.get("head_sha") or "").lower() != worker_commit
            or str(run.get("event") or "") != "workflow_dispatch"
            or attempt >= 3
        ):
            raise RuntimeError("process canary rerun binding drifted")
        if attempt == 2:
            return run
        if attempt != 1 or time.monotonic() >= deadline:
            return None
        time.sleep(1.0)


def _delete_rerun_secrets(credentials: Any, task_id: str) -> None:
    credentials.update_secrets({}, deletes=(
        f"remote_result_private:{task_id}", _rerun_bundle_name(task_id),
        _rerun_refresh_name(task_id),
    ))


def _cleanup_failed_rerun(
    *,
    credentials: Any,
    task_store: Any,
    github_app: Any,
    github: Any,
    coordinator: Any,
    task_id: str,
    run_id: int,
    worker_commit: str,
    attempt: int,
    error_code: str,
) -> None:
    remote = dict(task_store.get_remote_run(task_id) or {})
    issue_number = int(remote.get("issue_number") or 0)
    try:
        _delete_rerun_secrets(credentials, task_id)
        if attempt == 2:
            _upsert_rerun_remote(
                task_store, task_id, remote, attempt=2, remote_state="failed",
                last_error="",
            )
            coordinator.cleanup_failed_run(task_id, expected_attempt=2)
        else:
            for artifact in _task_artifacts(github, str(remote.get("repository") or ""), run_id, task_id):
                github.delete_artifact(str(remote.get("repository") or ""), int(artifact["id"]))
            if issue_number:
                github.cleanup_job(str(credentials.load_secret("github_mailbox_repo")), issue_number)
            task_store.upsert_remote_attempt(
                task_id, 2, repository=str(remote.get("repository") or ""),
                workflow=WORKFLOW, run_id=run_id, github_status="not_requested",
                conclusion="failure", worker_status="failed", import_state="failed",
                cleanup_state="complete", error_code=error_code, observed_at=time.time(),
            )
            _upsert_rerun_remote(
                task_store, task_id, remote, attempt=1, remote_state="failed",
                finished_at=time.time(), last_error="",
            )
        github_app.finalize_process_canary_token_lease(
            task_id=task_id, task_store=task_store
        )
    except Exception as exc:
        current = dict(task_store.get_remote_run(task_id) or remote)
        _upsert_rerun_remote(
            task_store, task_id, current, remote_state="failed",
            last_error="remote_cleanup_pending",
        )
        task_store.upsert_remote_attempt(
            task_id, 2, repository=str(current.get("repository") or ""),
            workflow=WORKFLOW, run_id=run_id, github_status="completed" if attempt == 2 else "not_requested",
            conclusion="failure", worker_status="failed", import_state="failed",
            cleanup_state="cleanup_pending", error_code="cleanup_pending",
            observed_at=time.time(),
        )
        _save_latch(
            credentials, task_id=task_id, commit=worker_commit,
            state="cleanup_pending", run_id=run_id, rerun_budget=0,
        )
        raise RuntimeError("process canary rerun cleanup remains pending") from exc
    _save_latch(
        credentials, task_id=task_id, commit=worker_commit,
        state="failed", run_id=run_id, rerun_budget=0,
    )


def cleanup_attempt_one_only(
    *, credentials: Any, task_store: Any, github_app: Any, verifier_go: str
) -> dict[str, Any]:
    """Abandon only an unposted rerun preparation; never dispatch or rerun."""
    if verifier_go != CLEANUP_VERIFIER_GO:
        raise RuntimeError("exact process canary cleanup verifier GO token is required")
    _require_remote_disabled(credentials)
    latch = _load_latch(credentials)
    if (
        latch.get("schema") != LATCH_V2_SCHEMA
        or latch.get("state") != "rerun_preparing"
        or latch.get("rerun_budget") != 1
        or int(latch.get("run_id") or 0) <= 0
    ):
        raise RuntimeError("process canary cleanup-only state is not exact")
    task_id = str(latch["task_id"])
    run_id = int(latch["run_id"])
    worker_commit = str(latch["worker_commit"])
    settings = RemoteSettings.load(credentials)
    repository = str(settings.public_repo)
    remote = dict(task_store.get_remote_run(task_id) or {})
    attempt_two = dict(task_store.get_remote_attempt(task_id, 2) or {})
    if (
        str(remote.get("repository") or "").casefold() != repository.casefold()
        or remote.get("workflow") != WORKFLOW
        or int(remote.get("run_id") or 0) != run_id
        or int(remote.get("attempt") or 0) != 1
        or remote.get("remote_state") != "rerun_preparing"
        or int(remote.get("issue_number") or 0) != 0
        or int(remote.get("artifact_id") or 0) != 0
        or str(remote.get("input_hash") or "")
        or str(remote.get("pipeline_version") or "")
        or dict(remote.get("checkpoint") or {})
        or int(attempt_two.get("run_id") or 0) != run_id
        or str(attempt_two.get("github_status") or "") != "not_requested"
        or str(attempt_two.get("worker_status") or "") != "planned"
        or str(attempt_two.get("worker_stage") or "") != "payload_preparing"
        or str(attempt_two.get("import_state") or "") != "not_started"
        or str(attempt_two.get("cleanup_state") or "") != "not_started"
    ):
        raise RuntimeError("process canary cleanup-only state is not exact")
    github = GitHubClient(settings.github_token, proxy_url=settings.proxy_url)
    live = dict(github.get_run(repository, run_id))
    if (
        int(live.get("id") or 0) != run_id
        or int(live.get("run_attempt") or 0) != 1
        or live.get("status") != "completed"
        or live.get("conclusion") != "failure"
        or str(live.get("event") or "") != "workflow_dispatch"
        or str(live.get("head_sha") or "").lower() != worker_commit
    ):
        raise RuntimeError("process canary cleanup-only attempt-one binding drifted")
    if _task_artifacts(github, repository, run_id, task_id):
        raise RuntimeError("process canary cleanup-only artifacts are not zero")
    if github._matching_job_issues(str(settings.private_repo), task_id):
        raise RuntimeError("process canary cleanup-only mailbox is not zero")
    coordinator = RemoteCoordinator(settings, task_store, credentials, github=github)
    _cleanup_failed_rerun(
        credentials=credentials, task_store=task_store, github_app=github_app,
        github=github, coordinator=coordinator, task_id=task_id, run_id=run_id,
        worker_commit=worker_commit, attempt=1, error_code="rerun_abandoned_before_post",
    )
    return {
        "schema": "courselens.process-canary-cleanup.v1",
        "status": "cleaned", "run_id": run_id, "attempt": 1,
        "task_sha256": hashlib.sha256(task_id.encode("ascii")).hexdigest(),
    }


def rerun_once(
    *,
    credentials: Any,
    task_store: Any,
    github_app: Any,
    verifier_go: str,
    attempt_visibility_timeout: float = 60.0,
    inject_failure: Any = None,
) -> dict[str, Any]:
    """Recover only the canonical attempt-one mailbox wait crash window."""
    if verifier_go != RERUN_VERIFIER_GO:
        raise RuntimeError("exact process canary rerun verifier GO token is required")
    _require_remote_disabled(credentials)
    latch = _load_latch(credentials)
    if not latch:
        raise RuntimeError("process canary rerun requires an existing latch")
    legacy = latch.get("schema") == LATCH_V1_SCHEMA
    if legacy and latch.get("state") != "running":
        raise RuntimeError("legacy process canary latch is not eligible for rerun")
    if not legacy and latch.get("state") in {"complete", "failed"}:
        raise RuntimeError("process canary rerun is already terminal")
    require_global_zero = bool(
        legacy
        or (
            latch.get("state") == "rerun_preparing"
            and (task_store.get_remote_run(str(latch["task_id"])) or {}).get("remote_state") == "failed"
        )
    )
    trust = _require_rerun_live_trust(
        credentials=credentials, task_store=task_store,
        github_app=github_app, require_global_zero=require_global_zero,
    )
    worker_commit = str(trust["expected_worker_commit"])
    if latch.get("worker_commit") != worker_commit:
        raise RuntimeError("process canary rerun signed pin drifted")
    loaded = RemoteSettings.load(credentials)
    _require_remote_disabled(credentials)
    settings = replace(
        loaded, enabled=True, workflow=WORKFLOW, ref="main",
        expected_worker_commit=worker_commit,
    )
    _require_configured_dispatch_target(credentials, settings)
    github = GitHubClient(settings.github_token, proxy_url=settings.proxy_url)
    coordinator = RemoteCoordinator(settings, task_store, credentials, github=github)
    task_id = str(latch["task_id"])
    repository = str(settings.public_repo)
    remote = dict(task_store.get_remote_run(task_id) or {})

    # The CAS is deliberately ahead of both the latch update and the external
    # rerun POST.  Therefore this exact durable split proves the POST was not
    # reached: the budget is consumed, and recovery may only clean attempt one.
    if (
        latch.get("state") == "rerun_payload_ready"
        and remote.get("remote_state") == "rerun_armed"
    ):
        if (
            latch.get("rerun_budget") != 1
            or int(latch.get("run_id") or 0) != int(remote.get("run_id") or 0)
            or int(remote.get("attempt") or 0) != 1
        ):
            raise RuntimeError("process canary rerun CAS recovery state is invalid")
        _cleanup_failed_rerun(
            credentials=credentials, task_store=task_store,
            github_app=github_app, github=github, coordinator=coordinator,
            task_id=task_id, run_id=int(remote["run_id"]),
            worker_commit=worker_commit, attempt=1,
            error_code="rerun_cas_consumed_before_latch",
        )
        raise RuntimeError("process canary rerun budget was consumed before latch; attempt-one cleanup completed")

    # A prior cleanup failure must be retried as cleanup only.  In particular,
    # its exhausted rerun budget must never re-enter payload or dispatch logic.
    if latch.get("state") == "cleanup_pending":
        cleanup_attempt = int(remote.get("attempt") or 0)
        run_id = int(remote.get("run_id") or 0)
        if (
            cleanup_attempt not in {1, 2}
            or not run_id
            or int(latch.get("run_id") or 0) != run_id
            or latch.get("rerun_budget") != 0
        ):
            raise RuntimeError("process canary rerun cleanup recovery state is invalid")
        _cleanup_failed_rerun(
            credentials=credentials, task_store=task_store,
            github_app=github_app, github=github, coordinator=coordinator,
            task_id=task_id, run_id=run_id, worker_commit=worker_commit,
            attempt=cleanup_attempt, error_code="cleanup_pending_retry",
        )
        raise RuntimeError("process canary rerun cleanup completed; rerun budget remains consumed")

    if legacy or (
        latch.get("state") == "rerun_preparing" and remote.get("remote_state") == "failed"
    ):
        remote, _run = _require_initial_mailbox_wait_failure(
            credentials=credentials, task_store=task_store, github=github,
            task_id=task_id, expected_worker_commit=worker_commit,
            repository=repository,
        )
        run_id = int(remote["run_id"])
        _save_latch(
            credentials, task_id=task_id, commit=worker_commit,
            state="rerun_preparing", run_id=run_id, rerun_budget=1,
        )
        _fault(inject_failure, "A_latch_before_db")
        remote = _upsert_rerun_remote(
            task_store, task_id, remote, remote_state="rerun_preparing",
            attempt=1, issue_number=None, artifact_id=None, input_hash="",
            pipeline_version="", checkpoint={}, last_error="",
        )
        task_store.upsert_remote_attempt(
            task_id, 2, repository=repository, workflow=WORKFLOW, run_id=run_id,
            github_status="not_requested", worker_status="planned",
            worker_stage="payload_preparing", import_state="not_started",
            cleanup_state="not_started", error_code="", observed_at=time.time(),
        )
    else:
        run_id = int(latch.get("run_id") or 0)
        if (
            not run_id
            or int(remote.get("run_id") or 0) != run_id
            or str(remote.get("repository") or "").casefold() != repository.casefold()
            or remote.get("workflow") != WORKFLOW
        ):
            raise RuntimeError("process canary rerun latch and database are not adjacent")

    state = str(_load_latch(credentials).get("state") or "")
    remote = dict(task_store.get_remote_run(task_id) or {})
    if state == "rerun_preparing":
        _fault(inject_failure, "B_db_before_lease")
        refreshed_token = _ensure_rerun_token(
            task_id=task_id, task_store=task_store,
            credentials=credentials, github_app=github_app,
        )
        if refreshed_token:
            # sync_job_token may have refreshed the App user credential.  Do
            # not retain the client made before that refresh for mailbox I/O.
            refreshed = RemoteSettings.load(credentials)
            github = GitHubClient(
                refreshed.github_token, proxy_url=refreshed.proxy_url
            )
            coordinator = RemoteCoordinator(settings, task_store, credentials, github=github)
        _fault(inject_failure, "C_lease_before_bundle")
        result_name = f"remote_result_private:{task_id}"
        bundle_name = _rerun_bundle_name(task_id)
        has_result = credentials.has_secret(result_name)
        has_bundle = credentials.has_secret(bundle_name)
        if has_result != has_bundle:
            raise RuntimeError("process canary rerun atomic secret pair is incomplete")
        bundle = (
            _load_rerun_bundle(
                credentials, task_id=task_id, run_id=run_id,
                worker_commit=worker_commit,
            )
            if has_bundle else
            _create_rerun_bundle(
                credentials, task_id=task_id, run_id=run_id,
                worker_commit=worker_commit,
                worker_public_key=settings.worker_public_key,
            )
        )
        _fault(inject_failure, "D_bundle_before_db")
        mailbox = github.publish_job_once(
            settings.private_repo, task_id, dict(bundle["envelope"])
        )
        _fault(inject_failure, "E_mailbox_partial_or_complete")
        readback, readback_number = github.read_job(settings.private_repo, task_id)
        if (
            int(mailbox.get("issue_number") or 0) != int(readback_number)
            or readback != bundle["envelope"]
        ):
            raise RuntimeError("process canary rerun mailbox readback mismatch")
        _fault(inject_failure, "F_mailbox_before_db")
        remote = _upsert_rerun_remote(
            task_store, task_id, remote, remote_state="rerun_payload_ready",
            attempt=1, issue_number=int(readback_number), artifact_id=None,
            input_hash=str(bundle["input_hash"]),
            pipeline_version=PROCESS_CANARY_PIPELINE, last_error="",
        )
        task_store.upsert_remote_attempt(
            task_id, 2, repository=repository, workflow=WORKFLOW, run_id=run_id,
            github_status="not_requested", worker_status="ready",
            worker_stage="awaiting_rerun", import_state="not_started",
            cleanup_state="not_started", error_code="", observed_at=time.time(),
        )
        _save_latch(
            credentials, task_id=task_id, commit=worker_commit,
            state="rerun_payload_ready", run_id=run_id, rerun_budget=1,
        )
        state = "rerun_payload_ready"

    posted_here = False
    if state == "rerun_payload_ready":
        _require_remote_disabled(credentials)
        _require_rerun_live_trust(
            credentials=credentials, task_store=task_store,
            github_app=github_app, require_global_zero=False,
        )
        bundle = _load_rerun_bundle(
            credentials, task_id=task_id, run_id=run_id,
            worker_commit=worker_commit,
        )
        _require_existing_rerun_token(
            task_id=task_id, task_store=task_store,
            credentials=credentials, github_app=github_app,
        )
        readback, readback_number = github.read_job(settings.private_repo, task_id)
        live = dict(github.get_run(repository, run_id))
        if (
            readback != bundle["envelope"]
            or int(readback_number) != int(remote.get("issue_number") or 0)
            or int(live.get("run_attempt") or 0) != 1
            or live.get("status") != "completed"
            or live.get("conclusion") != "failure"
            or str(live.get("head_sha") or "").lower() != worker_commit
            or str(live.get("event") or "") != "workflow_dispatch"
            or _task_artifacts(github, repository, run_id, task_id)
        ):
            raise RuntimeError("process canary rerun arm preflight drifted")
        _fault(inject_failure, "G_payload_ready_before_cas")
        if not task_store.arm_process_canary_rerun(task_id, run_id=run_id):
            raise RuntimeError("process canary rerun CAS lost")
        _fault(inject_failure, "H_cas_before_latch")
        _save_latch(
            credentials, task_id=task_id, commit=worker_commit,
            state="rerun_armed", run_id=run_id, rerun_budget=0,
        )
        state = "rerun_armed"
        posted_here = True
        _fault(inject_failure, "H_cas_before_post")
        try:
            github.rerun_workflow_once(repository, run_id)
        except Exception:
            # The budget is intentionally still consumed.  Reconciliation
            # below may observe attempt 2, but this call is never repeated.
            pass
        _fault(inject_failure, "I_post_before_attempt_observed")

    if state == "rerun_armed":
        live = _wait_for_attempt_two(
            github, repository=repository, run_id=run_id,
            worker_commit=worker_commit,
            timeout_seconds=attempt_visibility_timeout,
        )
        if live is None:
            _cleanup_failed_rerun(
                credentials=credentials, task_store=task_store,
                github_app=github_app, github=github, coordinator=coordinator,
                task_id=task_id, run_id=run_id, worker_commit=worker_commit,
                attempt=1, error_code="rerun_call_ambiguous_or_not_accepted",
            )
            raise RuntimeError("process canary rerun call was ambiguous or not accepted")
        remote = dict(task_store.get_remote_run(task_id) or {})
        remote = _upsert_rerun_remote(
            task_store, task_id, remote, attempt=2,
            remote_state="rerun_running", last_error="",
        )
        task_store.upsert_remote_attempt(
            task_id, 2, repository=repository, workflow=WORKFLOW, run_id=run_id,
            github_status=str(live.get("status") or "unknown"),
            conclusion=str(live.get("conclusion") or ""), worker_status="running",
            worker_stage="awaiting_payload", import_state="not_started",
            cleanup_state="not_started", error_code="", observed_at=time.time(),
        )
        _save_latch(
            credentials, task_id=task_id, commit=worker_commit,
            state="rerun_running", run_id=run_id, rerun_budget=0,
        )
        state = "rerun_running"
        _fault(inject_failure, "J_attempt2_running_before_reattach")

    imported: dict[str, Any] = {}
    def import_result(result: dict[str, Any]) -> None:
        imported.update(validate_import(result, expected_worker_commit=worker_commit))
        _save_latch(
            credentials, task_id=task_id, commit=worker_commit,
            state="result_verified", run_id=run_id, rerun_budget=0,
        )

    def progress(stage: str, percent: float | None, _label: str) -> None:
        allowed = {
            "remote_queue", "awaiting_payload", "remote_compute", "remote_result",
            "remote_import", "remote_cleanup",
        }
        safe_stage = stage if stage in allowed else "remote_compute"
        value = "unknown" if percent is None else str(max(0, min(100, int(percent))))
        print(f"stage={safe_stage} progress={value}", flush=True)

    remote = dict(task_store.get_remote_run(task_id) or {})
    live = dict(github.get_run(repository, run_id))
    live_attempt = int(live.get("run_attempt") or 0)
    if live_attempt >= 3 or live_attempt != 2:
        raise RuntimeError("process canary rerun attempt drifted")
    if live.get("status") == "completed" and live.get("conclusion") != "success":
        for attempt in (1, 2):
            assert_public_log_redaction(
                github.download_run_attempt_logs(repository, run_id, attempt)
            )
        _cleanup_failed_rerun(
            credentials=credentials, task_store=task_store,
            github_app=github_app, github=github, coordinator=coordinator,
            task_id=task_id, run_id=run_id, worker_commit=worker_commit,
            attempt=2, error_code="rerun_attempt_failed",
        )
        raise RuntimeError("process canary rerun attempt failed")

    try:
        remote_state = str(remote.get("remote_state") or "")
        if remote_state == "imported":
            coordinator.retry_imported_cleanup(task_id, retain_result_secret=True)
        elif remote_state in {"downloading_result", "failed"} and remote.get("artifact_id"):
            coordinator.resume_result_import_only(
                task_id=task_id, expected_run_id=run_id, expected_attempt=2,
                import_result=import_result, progress=progress,
                retain_result_secret=True,
            )
        else:
            coordinator.resume_existing_run_only(
                task_id=task_id, expected_run_id=run_id, expected_attempt=2,
                import_result=import_result, cancel_requested=lambda: False,
                progress=progress, retain_result_secret=True,
            )
        _fault(inject_failure, "L_result_verified_before_imported")
    except Exception:
        terminal = dict(github.get_run(repository, run_id))
        if (
            int(terminal.get("run_attempt") or 0) == 2
            and terminal.get("status") == "completed"
            and terminal.get("conclusion") != "success"
        ):
            _cleanup_failed_rerun(
                credentials=credentials, task_store=task_store,
                github_app=github_app, github=github, coordinator=coordinator,
                task_id=task_id, run_id=run_id, worker_commit=worker_commit,
                attempt=2, error_code="rerun_attempt_failed",
            )
        raise

    current = dict(task_store.get_remote_run(task_id) or {})
    if current.get("remote_state") != "imported":
        raise RuntimeError("process canary rerun import did not commit")
    terminal = dict(github.get_run(repository, run_id))
    if (
        int(terminal.get("run_attempt") or 0) != 2
        or terminal.get("status") != "completed"
        or terminal.get("conclusion") != "success"
    ):
        raise RuntimeError("process canary rerun attempt drifted before completion")
    _fault(inject_failure, "M_imported_before_secret_delete")
    _delete_rerun_secrets(credentials, task_id)
    _fault(inject_failure, "N_cleanup_before_token")
    github_app.finalize_process_canary_token_lease(
        task_id=task_id, task_store=task_store
    )
    _fault(inject_failure, "O_token_before_lease_complete")
    for attempt in (1, 2):
        assert_public_log_redaction(
            github.download_run_attempt_logs(repository, run_id, attempt)
        )
    audit = build_zero_state_audit(
        task_id, credentials=credentials, task_store=task_store,
        github_app=github_app, github=github,
        expected_worker_commit=worker_commit,
        expected_template_run_totals=_preflight_template_totals(trust),
    )
    if (
        credentials.has_secret(_rerun_bundle_name(task_id))
        or credentials.has_secret(_rerun_refresh_name(task_id))
    ):
        raise RuntimeError("process canary rerun preparation cleanup failed")
    final_live = dict(github.get_run(repository, run_id))
    if (
        int(final_live.get("id") or 0) != run_id
        or int(final_live.get("run_attempt") or 0) != 2
        or final_live.get("status") != "completed"
        or final_live.get("conclusion") != "success"
        or str(final_live.get("head_sha") or "").lower() != worker_commit
        or str(final_live.get("event") or "") != "workflow_dispatch"
    ):
        raise RuntimeError("process canary rerun binding drifted before completion latch")
    _save_latch(
        credentials, task_id=task_id, commit=worker_commit,
        state="complete", run_id=run_id, rerun_budget=0,
    )
    return {
        "schema": "courselens.process-canary-rerun.v1",
        "status": "passed",
        "run_id": run_id,
        "attempt": 2,
        "worker_commit": worker_commit,
        "zero_state": audit["zero_state"],
        "task_sha256": hashlib.sha256(task_id.encode("ascii")).hexdigest(),
    }


def run_once(
    *, credentials: Any, task_store: Any, github_app: Any, verifier_go: str
) -> dict[str, Any]:
    if verifier_go != VERIFIER_GO:
        raise RuntimeError("exact process canary verifier GO token is required")
    latch = _load_latch(credentials)
    preflight = build_preflight(
        credentials=credentials,
        task_store=task_store,
        github_app=github_app,
        require_zero_state=not bool(
            latch and latch.get("state") in {"running", "result_verified"}
        ),
    )
    expected_commit = str(preflight["expected_worker_commit"])
    if latch and latch.get("state") == "failed":
        failed_task_id = str(latch.get("task_id") or "")
        failed_run_id = int(latch.get("run_id") or 0)
        failed_remote = dict(task_store.get_remote_run(failed_task_id) or {})
        failed_attempt = dict(task_store.get_remote_attempt(failed_task_id, 1) or {})
        if (
            failed_run_id
            and int(failed_remote.get("run_id") or 0) == failed_run_id
            and str(failed_remote.get("remote_state") or "") == "failed"
            and not failed_remote.get("issue_number")
            and not failed_remote.get("artifact_id")
            and str(failed_attempt.get("cleanup_state") or "") in {"", "best_effort"}
            and not credentials.has_secret(f"remote_result_private:{failed_task_id}")
            and not task_store.list_remote_token_leases()
        ):
            credentials.delete_secret(LATCH_SECRET)
            return {
                "schema": "courselens.process-canary-run.v1",
                "status": "abandoned_preauthorization_failure",
                "run_id": failed_run_id,
                "rerun_required": True,
            }
    if latch and latch.get("state") in {
        "rerun_preparing", "rerun_payload_ready", "rerun_armed", "rerun_running",
        "cleanup_pending",
    }:
        raise RuntimeError("process canary requires the dedicated rerun recovery command")
    if latch and latch["worker_commit"] == expected_commit and latch["state"] == "complete":
        raise RuntimeError("process canary already completed for this signed worker commit")
    if latch and latch["worker_commit"] == expected_commit and latch["state"] == "failed":
        raise RuntimeError("process canary already failed for this signed worker commit")
    if (
        latch
        and latch["worker_commit"] != expected_commit
        and latch["state"] not in {"complete", "failed"}
    ):
        old_task_id = str(latch.get("task_id") or "")
        old_remote = dict(task_store.get_remote_run(old_task_id) or {})
        old_run_id = int(old_remote.get("run_id") or 0)
        old_attempt = dict(task_store.get_remote_attempt(old_task_id, 1) or {})
        if (
            old_run_id
            and int(old_remote.get("run_id") or 0) == old_run_id
            and str(old_remote.get("remote_state") or "") == "failed"
            and not old_remote.get("issue_number")
            and not old_remote.get("artifact_id")
            and str(old_attempt.get("cleanup_state") or "") in {"", "best_effort"}
            and not credentials.has_secret(f"remote_result_private:{old_task_id}")
            and not task_store.list_remote_token_leases()
        ):
            credentials.delete_secret(LATCH_SECRET)
            latch = {}
        else:
            raise RuntimeError("an earlier process canary requires recovery before a new signed commit")
    if (
        latch
        and latch["worker_commit"] != expected_commit
        and latch.get("state") == "failed"
    ):
        old_task_id = str(latch.get("task_id") or "")
        old_run_id = int(latch.get("run_id") or 0)
        old_remote = dict(task_store.get_remote_run(old_task_id) or {})
        old_attempt = dict(task_store.get_remote_attempt(old_task_id, 1) or {})
        if (
            old_run_id
            and int(old_remote.get("run_id") or 0) == old_run_id
            and str(old_remote.get("remote_state") or "") == "failed"
            and not old_remote.get("issue_number")
            and not old_remote.get("artifact_id")
            and str(old_attempt.get("cleanup_state") or "") in {"", "best_effort"}
            and not credentials.has_secret(f"remote_result_private:{old_task_id}")
            and not task_store.list_remote_token_leases()
        ):
            credentials.delete_secret(LATCH_SECRET)
            latch = {}
        else:
            raise RuntimeError("an earlier failed process canary requires recovery before a new signed commit")
    if not latch or latch["worker_commit"] != expected_commit:
        latch = {"task_id": uuid.uuid4().hex, "worker_commit": expected_commit, "state": "prepared"}
        _save_latch(
            credentials,
            task_id=latch["task_id"],
            commit=latch["worker_commit"],
            state=latch["state"],
        )
    recovering_running = latch["state"] == "running"
    loaded = RemoteSettings.load(credentials)
    _require_remote_disabled(credentials)
    settings = replace(
        loaded,
        enabled=True,
        workflow=WORKFLOW,
        ref="main",
        expected_worker_commit=expected_commit,
    )
    _require_configured_dispatch_target(credentials, settings)
    github = GitHubClient(settings.github_token, proxy_url=settings.proxy_url)
    coordinator = RemoteCoordinator(settings, task_store, credentials, github=github)
    if _abandon_preauthorization_failed_attempt(
        credentials=credentials, task_store=task_store, github=github, latch=latch
    ):
        return {
            "schema": "courselens.process-canary-run.v1",
            "status": "abandoned_preauthorization_failure",
            "run_id": int(latch.get("run_id") or 0),
            "rerun_required": True,
        }
    task_id = str(latch["task_id"])
    imported: dict[str, Any] = {}

    def import_result(result: dict[str, Any]) -> None:
        # This is an in-memory proof only.  It deliberately cannot write to any
        # course, lecture, note, search, material, or learning table.
        imported.update(validate_import(result, expected_worker_commit=expected_commit))
        current_run_id = int((task_store.get_remote_run(task_id) or {}).get("run_id") or 0)
        _save_latch(
            credentials, task_id=task_id, commit=expected_commit, state="result_verified",
            run_id=current_run_id,
        )

    def progress(stage: str, percent: float | None, _label: str) -> None:
        allowed = {
            "remote_queue", "awaiting_payload", "remote_compute", "remote_result",
            "remote_import", "remote_cleanup",
        }
        safe_stage = stage if stage in allowed else "remote_compute"
        value = "unknown" if percent is None else str(max(0, min(100, int(percent))))
        print(f"stage={safe_stage} progress={value}", flush=True)

    remote_before = dict(task_store.get_remote_run(task_id) or {})
    attempt_before = dict(
        task_store.get_remote_attempt(
            task_id, max(1, int(remote_before.get("attempt") or 1))
        ) or {}
    )
    if latch["state"] == "running" and remote_before.get("remote_state") in {
        "paused", "canceled", "imported",
    }:
        _save_latch(
            credentials, task_id=task_id, commit=expected_commit, state="failed",
            run_id=int(remote_before.get("run_id") or 0),
        )
        raise RuntimeError("process canary cannot redispatch after a terminal remote state")
    if (
        latch["state"] == "running"
        and remote_before.get("remote_state") == "failed"
        and not (
            remote_before.get("artifact_id")
            and credentials.has_secret(f"remote_result_private:{task_id}")
        )
    ):
        if (
            not remote_before.get("issue_number")
            and not task_store.list_remote_token_leases()
        ):
            credentials.delete_secret(LATCH_SECRET)
            return {
                "schema": "courselens.process-canary-run.v1",
                "status": "abandoned_preauthorization_failure",
                "run_id": int(remote_before.get("run_id") or 0),
                "rerun_required": True,
            }
        _save_latch(
            credentials, task_id=task_id, commit=expected_commit, state="failed",
            run_id=int(remote_before.get("run_id") or 0),
        )
        raise RuntimeError("process canary failed before a resumable signed result")
    artifact_recovery_state = remote_before.get("remote_state") in {
        "downloading_result", "failed",
    }
    artifact_ready = bool(
        artifact_recovery_state
        and remote_before.get("artifact_id")
        and remote_before.get("issue_number")
        and str(remote_before.get("input_hash") or "")
        and credentials.has_secret(f"remote_result_private:{task_id}")
    )
    if (
        latch["state"] in {"running", "result_verified"}
        and artifact_recovery_state
        and not artifact_ready
    ):
        _save_latch(
            credentials, task_id=task_id, commit=expected_commit, state="failed",
            run_id=int(remote_before.get("run_id") or 0),
        )
        raise RuntimeError("process canary artifact recovery metadata is incomplete")
    recovery_terminal = bool(
        latch["state"] in {"running", "result_verified"}
        and (artifact_ready or remote_before.get("remote_state") == "imported")
    )
    if recovery_terminal:
        cleanup_recovery_job_token(
            task_id,
            remote=remote_before,
            expected_worker_commit=expected_commit,
            task_store=task_store,
            github_app=github_app,
            github=github,
        )
    if remote_before.get("remote_state") == "imported" and latch["state"] == "result_verified":
        coordinator.retry_imported_cleanup(task_id)
    elif artifact_ready:
        coordinator.execute(
            task_id=task_id,
            build_job=lambda result_public_key: build_process_canary_job(
                task_id, result_public_key
            ),
            import_result=import_result,
            cancel_requested=lambda: False,
            progress=progress,
            reuse_queued_run=True,
        )
        if imported != {"verified": True, "worker_commit": expected_commit}:
            raise RuntimeError("process canary resumed import proof is incomplete")
    elif latch["state"] == "result_verified":
        if remote_before.get("remote_state") != "imported":
            raise RuntimeError("verified process canary import cannot be safely resumed")
    else:
        _save_latch(
            credentials, task_id=task_id, commit=expected_commit, state="running",
            run_id=int(remote_before.get("run_id") or 0),
        )
        try:
            with github_app.job_token_lease(task_id=task_id, task_store=task_store):
                if recovering_running and not task_store.get_remote_run(task_id):
                    recovered = github.find_workflow_run(
                        settings.public_repo,
                        workflow=WORKFLOW,
                        ref="main",
                        task_id=task_id,
                        expected_head_sha=expected_commit,
                    )
                    if recovered is not None:
                        task_store.upsert_remote_run(
                            task_id,
                            repository=settings.public_repo,
                            workflow=WORKFLOW,
                            run_id=recovered.run_id,
                            attempt=1,
                            remote_state="queued",
                            dispatched_at=time.time(),
                            last_error="",
                        )
                coordinator.execute(
                    task_id=task_id,
                    build_job=lambda result_public_key: build_process_canary_job(
                        task_id, result_public_key
                    ),
                    import_result=import_result,
                    cancel_requested=lambda: False,
                    progress=progress,
                    reuse_queued_run=True,
                )
        finally:
            _require_remote_disabled(credentials)
        if imported != {"verified": True, "worker_commit": expected_commit}:
            raise RuntimeError("process canary import proof is incomplete")
    remote = dict(task_store.get_remote_run(task_id) or {})
    run_id = int(remote.get("run_id") or 0)
    assert_public_log_redaction(github.download_run_logs(settings.public_repo, run_id))
    audit = build_zero_state_audit(
        task_id,
        credentials=credentials,
        task_store=task_store,
        github_app=github_app,
        github=github,
        expected_worker_commit=expected_commit,
        expected_template_run_totals=_preflight_template_totals(preflight),
    )
    _save_latch(
        credentials, task_id=task_id, commit=expected_commit, state="complete",
        run_id=int(audit["run_id"]), rerun_budget=1,
    )
    return {
        "schema": "courselens.process-canary-run.v1",
        "status": "passed",
        "run_id": audit["run_id"],
        "worker_commit": expected_commit,
        "zero_state": audit["zero_state"],
        "task_sha256": hashlib.sha256(task_id.encode("ascii")).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy", default="")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dispatch-once", action="store_true")
    action.add_argument("--rerun-once", action="store_true")
    action.add_argument("--cleanup-attempt-one-only", action="store_true")
    action.add_argument("--refresh-attempt-one-preparation-only", action="store_true")
    action.add_argument("--read-only-preflight", action="store_true", help="verify without creating, migrating, refreshing, or saving local state")
    parser.add_argument("--verifier-go", default="")
    args = parser.parse_args(argv)
    if not any((args.dispatch_once, args.rerun_once, args.cleanup_attempt_one_only,
                args.refresh_attempt_one_preparation_only, args.read_only_preflight)):
        parser.error("an explicit action or --read-only-preflight is required")
    if args.proxy:
        os.environ["HTTP_PROXY"] = args.proxy
        os.environ["HTTPS_PROXY"] = args.proxy
    credentials = CredentialStore()
    task_store = TaskStore(DEFAULT_DATA_DIR / "state.db", read_only=bool(args.read_only_preflight))
    github_app = GitHubAppClient(credentials, proxy_url=str(args.proxy or ""))
    if args.rerun_once:
        report = rerun_once(
            credentials=credentials,
            task_store=task_store,
            github_app=github_app,
            verifier_go=str(args.verifier_go or ""),
        )
    elif args.cleanup_attempt_one_only:
        report = cleanup_attempt_one_only(
            credentials=credentials,
            task_store=task_store,
            github_app=github_app,
            verifier_go=str(args.verifier_go or ""),
        )
    elif args.refresh_attempt_one_preparation_only:
        report = refresh_attempt_one_preparation_only(
            credentials=credentials, task_store=task_store, github_app=github_app,
            verifier_go=str(args.verifier_go or ""),
        )
    elif args.read_only_preflight:
        report = build_preflight(
            credentials=credentials, task_store=task_store, github_app=github_app,
            require_zero_state=True, read_only=True,
        )
    elif not args.dispatch_once:
        report = build_preflight(
            credentials=credentials, task_store=task_store, github_app=github_app
        )
    else:
        report = run_once(
            credentials=credentials,
            task_store=task_store,
            github_app=github_app,
            verifier_go=str(args.verifier_go or ""),
        )
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
