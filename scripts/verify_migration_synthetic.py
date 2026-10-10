"""Offline-safe migration synthetic gates.

The commands are deliberately split from the live canary.  With no action
flag this program only describes the required confirmation token; replay and
tamper are entirely in-memory.  Cancellation is a *live* gate and therefore
requires its own exact token before it imports any runtime client.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.distribution import DISTRIBUTION_REPOSITORY
from shared.protocol.wire import (  # Pure protocol primitives only.
    PROTOCOL_VERSION, RESULT_SCHEMA, finalize_job, generate_box_keypair,
    open_result, seal_result,
    validate_job,
)
from nacl.signing import SigningKey
from scripts.migration_gate_evidence import EVIDENCE_SCHEMA, build_evidence

GO = {"cancellation": "PROCESS-CANARY-CANCEL-GO", "replay": "REPLAY-REJECTION-GO", "tamper": "TAMPER-REJECTION-GO"}
CANCEL_LATCH = "migration_synthetic_cancellation_latch"
CANCEL_SCHEMA = "courselens.migration-synthetic-cancellation-latch.v1"
FAILED_CANCELLATION_LOCAL_RECOVERY_GO = "FAILED-CANCELLATION-LOCAL-RECOVERY-COMPLETED-FAILURE"


def _evidence(gate: str, status: str, **counts: Any) -> dict[str, Any]:
    """Emit public-only, independently reproducible migration evidence."""
    return build_evidence(gate, status, **counts)


def _job(task_id: str, result_public: str) -> dict[str, Any]:
    now = time.time()
    return finalize_job({"task_id": task_id, "job_kind": "echo", "created_at": now, "expires_at": now + 600,
                         "result_public_key": result_public, "pipeline": {"version": "synthetic-v1"},
                         "payload": {"text": "migration synthetic"}, "secrets": {}, "requested_outputs": []})


def _result(job: dict[str, Any]) -> dict[str, Any]:
    return {"schema": RESULT_SCHEMA, "protocol_version": PROTOCOL_VERSION, "task_id": job["task_id"], "job_kind": "echo",
            "input_hash": job["input_hash"], "pipeline_fingerprint": "synthetic-v1", "status": "completed",
            "outputs": {"echo": {"text": "ok"}}, "metrics": {}, "warnings": []}

def replay_rejection() -> dict[str, Any]:
    """Prove result envelopes cannot cross task/input-hash or inventory bounds."""
    private, public = generate_box_keypair()
    signing = SigningKey.generate()
    task_a, task_b = uuid.uuid4().hex, uuid.uuid4().hex
    job_a, job_b = _job(task_a, public), _job(task_b, public)
    return _replay_with_keys(private, public, signing, job_a, job_b)


def _b64(raw: bytes) -> str:
    import base64
    return base64.b64encode(raw).decode("ascii")

def _mutate_b64(value: str) -> str:
    """Deterministically change one valid base64 character without no-ops."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    for index, char in enumerate(value):
        if char in alphabet:
            replacement = alphabet[(alphabet.index(char) + 1) % len(alphabet)]
            return value[:index] + replacement + value[index + 1:]
    raise RuntimeError("synthetic base64 fixture is invalid")


def _replay_with_keys(private: str, public: str, signing: SigningKey, job_a: dict[str, Any], job_b: dict[str, Any]) -> dict[str, Any]:
    envelope = seal_result(_result(job_a), public, _b64(signing.encode()))
    verify = _b64(bytes(signing.verify_key))
    if open_result(envelope, private, verify, expected_task_id=job_a["task_id"], expected_input_hash=job_a["input_hash"])["task_id"] != job_a["task_id"]:
        raise RuntimeError("valid result envelope was not accepted")
    rejected = 0
    for task, input_hash in ((job_b["task_id"], job_b["input_hash"]), (job_a["task_id"], "0" * 64)):
        try:
            open_result(envelope, private, verify, expected_task_id=task, expected_input_hash=input_hash)
        except Exception:
            rejected += 1
    from src.remote.github_client import GitHubClient, GitHubRemoteError
    inventory = [{"id": 1, "task_id": job_a["task_id"]}, {"id": 2, "task_id": job_a["task_id"]}]
    try: GitHubClient.require_unique_workflow_inventory(inventory, task_id=job_a["task_id"])
    except GitHubRemoteError: duplicate_rejected = True
    else: duplicate_rejected = False
    if rejected != 2 or not duplicate_rejected:
        raise RuntimeError("replay rejection gate failed")
    return _evidence("replay", "passed", rejection_count=3)


def tamper_rejection() -> dict[str, Any]:
    """Thirteen production-parser tamper cases; pure memory, no callbacks."""
    private, public = generate_box_keypair()
    signing, wrong = SigningKey.generate(), SigningKey.generate()
    job = _job(uuid.uuid4().hex, public)
    envelope = seal_result(_result(job), public, _b64(signing.encode()))
    checks: dict[str, bool] = {}
    if open_result(envelope, private, _b64(bytes(signing.verify_key)), expected_task_id=job["task_id"], expected_input_hash=job["input_hash"])["task_id"] != job["task_id"]:
        raise RuntimeError("tamper baseline was not accepted")
    def reject(name: str, candidate: dict[str, Any], *, key: Any = signing.verify_key) -> None:
        try:
            open_result(candidate, private, _b64(bytes(key)), expected_task_id=job["task_id"], expected_input_hash=job["input_hash"])
        except Exception:
            checks[name] = True
        else: checks[name] = False
    ciphertext_mutation, signature_mutation = _mutate_b64(envelope["ciphertext"]), _mutate_b64(envelope["signature"])
    if ciphertext_mutation == envelope["ciphertext"] or signature_mutation == envelope["signature"]: raise RuntimeError("tamper mutation was a no-op")
    for name, field, value in (("outer_schema", "schema", "bad.v1"), ("outer_encoding", "encoding", "raw"), ("outer_checksum", "sha256", "0" * 64), ("outer_task", "task_id", "f" * 32), ("outer_protocol", "protocol_version", "999"), ("ciphertext", "ciphertext", ciphertext_mutation), ("signature", "signature", signature_mutation)):
        candidate = copy.deepcopy(envelope); candidate[field] = value
        reject(name, candidate)
    reject("wrong_signing_key", envelope, key=wrong.verify_key)
    # Inner mutations are actually sealed/signed again, so open_result reaches
    # the production decrypted-result parser rather than a changed expectation.
    for name, field, value in (("inner_protocol", "protocol_version", "999"), ("inner_kind", "job_kind", "unknown"), ("inner_input_hash", "input_hash", "0" * 64)):
        result = _result(job); result[field] = value
        reject(name, seal_result(result, public, _b64(signing.encode())))
    for name, field, value in (("inner_schema", "schema", "bad.v1"), ("inner_task", "task_id", "not-a-task")):
        result = _result(job); result[field] = value
        try: seal_result(result, public, _b64(signing.encode()))
        except Exception: checks[name] = True
        else: checks[name] = False
    if set(checks) != {"outer_schema", "outer_encoding", "outer_checksum", "outer_task", "outer_protocol", "ciphertext", "signature", "wrong_signing_key", "inner_schema", "inner_protocol", "inner_kind", "inner_task", "inner_input_hash"} or not all(checks.values()):
        raise RuntimeError("tamper rejection gate failed")
    return _evidence("tamper", "passed", rejection_count=sum(checks.values()), false_positive_count=sum(not rejected for rejected in checks.values()), checks=checks)


def _load_cancel_latch(credentials: Any) -> dict[str, Any]:
    if not credentials.has_secret(CANCEL_LATCH): return {}
    try: value = json.loads(str(credentials.load_secret(CANCEL_LATCH)))
    except Exception as exc: raise RuntimeError("cancellation latch is invalid") from exc
    if not isinstance(value, dict) or value.get("schema") != CANCEL_SCHEMA or value.get("state") not in {"prepared", "dispatched", "dispatch_post_unknown", "cancel_post_unknown", "canceling", "cleanup_pending", "cleanup_only", "complete", "failed"} or value.get("dispatch_budget") not in {0, 1} or value.get("cancel_budget") not in {0, 1}:
        raise RuntimeError("cancellation latch is invalid")
    if value.get("state") == "cleanup_pending" and value.get("cleanup_kind") not in {"cancelled_cleanup", "race_cleanup"}:
        raise RuntimeError("cancellation cleanup kind is invalid")
    return value

def _save_cancel_latch(credentials: Any, value: dict[str, Any]) -> None:
    credentials.save_secret(CANCEL_LATCH, json.dumps({"schema": CANCEL_SCHEMA, **value}, sort_keys=True, separators=(",", ":")))


def recover_failed_cancellation_local_only(*, credentials: Any, task_store: Any,
                                           verifier_go: str, task_id: str,
                                           worker_commit: str, run_id: int) -> dict[str, Any]:
    """Permanently close one externally evidenced ``completed/failure`` run.

    This deliberately has no GitHub, Worker, Mailbox, artifact, or token
    parameter.  The exact confirmation attests that the remote terminal fact
    was independently observed before this local-only recovery is invoked.
    """
    if verifier_go != FAILED_CANCELLATION_LOCAL_RECOVERY_GO:
        raise RuntimeError("exact failed cancellation local recovery GO token is required")
    task_id, worker_commit, run_id = str(task_id), str(worker_commit).lower(), int(run_id)
    if len(task_id) != 32 or not task_id.isalnum() or len(worker_commit) != 40 or any(ch not in "0123456789abcdef" for ch in worker_commit) or run_id <= 0:
        raise RuntimeError("failed cancellation local recovery binding is invalid")
    latch = _load_cancel_latch(credentials)
    exact = (str(latch.get("task_id") or "") == task_id
             and str(latch.get("worker_commit") or "").lower() == worker_commit
             and int(latch.get("run_id") or 0) == run_id)
    if not exact:
        raise RuntimeError("failed cancellation local recovery latch binding drifted")
    try:
        residue = (
            list(task_store.list_remote_token_leases())
            or int(task_store.migration_cleanup_pending_count()) != 0
            or any(credentials.list_secret_names(prefix=prefix) for prefix in (
                "remote_result_private:", "process_canary_rerun_bundle:",
                "process_canary_rerun_refresh:",
            ))
        )
    except Exception as exc:
        raise RuntimeError("failed cancellation local recovery residue audit is unavailable") from exc
    if residue:
        raise RuntimeError("failed cancellation local recovery residue is present")
    state = str(latch.get("state") or "")
    if state == "dispatched":
        if latch.get("dispatch_budget") != 0 or latch.get("cancel_budget") != 1:
            raise RuntimeError("failed cancellation local recovery budget is invalid")
        # Persist the no-replay tombstone before touching SQLite.  A crash or
        # transaction failure can only be resumed through this local path.
        latch.update(state="cleanup_only", cleanup_kind="failed_cancellation_local_only", cancel_budget=0)
        _save_cancel_latch(credentials, latch)
    elif state not in {"cleanup_only", "failed"} or latch.get("dispatch_budget") != 0 or latch.get("cancel_budget") != 0 or latch.get("cleanup_kind") != "failed_cancellation_local_only":
        raise RuntimeError("failed cancellation local recovery tombstone is invalid")
    changed = task_store.recover_failed_cancellation_attempt_one(
        task_id, repository=DISTRIBUTION_REPOSITORY, workflow="process.yml", run_id=run_id,
    )
    if state != "failed":
        latch["state"] = "failed"
        _save_cancel_latch(credentials, latch)
    return _evidence("failed_cancellation_local_recovery", "passed", attempt=int(changed))

def build_cancellation_zero_state_audit(task_id: str, *, credentials: Any, task_store: Any, github_app: Any, github: Any, expected_worker_commit: str, repository: str, workflow: str, run_id: int) -> dict[str, Any]:
    """Cancellation-specific postflight: no import or successful canary state."""
    from scripts.verify_github_process_canary import WORKFLOW, _executor_repositories
    from src.remote.worker_migration import build_signed_template_transition_gate
    remote = dict(task_store.get_remote_run(task_id) or {}); stored_run_id = int(remote.get("run_id") or 0); repo = str(remote.get("repository") or "")
    attempt = dict(task_store.get_remote_attempt(task_id, 1) or {})
    run = dict(github.get_run(repository, run_id))
    gate = build_signed_template_transition_gate(credentials=credentials, task_store=task_store, github_app=github_app, repositories=_executor_repositories(credentials, repo))
    checks = {"stored_canceled": remote.get("remote_state") == "canceled", "repository": repo == repository, "workflow": remote.get("workflow") == workflow == WORKFLOW, "run_id": stored_run_id == run_id == int(run.get("id") or 0), "attempt_one": int(remote.get("attempt") or 0) == 1, "cleanup_complete": attempt.get("cleanup_state") == "complete", "run_canceled": run.get("status") == "completed" and run.get("conclusion") in {"cancelled","canceled"}, "signed_head": str(run.get("head_sha") or "").lower() == expected_worker_commit, "event": run.get("event") == "workflow_dispatch", "run_attempt": int(run.get("run_attempt") or 0) == 1, "global_zero": gate.get("ready") is True, "result_keys_zero": not list(credentials.list_secret_names(prefix="remote_result_private:")), "bundles_zero": not list(credentials.list_secret_names(prefix="process_canary_rerun_bundle:")), "markers_zero": not list(credentials.list_secret_names(prefix="process_canary_rerun_refresh:"))}
    if not all(checks.values()): raise RuntimeError("cancellation postflight failed: " + ",".join(k for k,v in checks.items() if not v))
    return {"schema":"courselens.migration-cancellation-audit.v1","run_id":run_id,"worker_commit":expected_worker_commit,"checks":checks,"zero_state":dict(gate.get("observations") or {})}

def _unique_bound_run(github: Any, *, repository: str, workflow: str, task_id: str, commit: str, stored_run_id: int = 0) -> dict[str, Any]:
    """Require the client's opaque-task discovery proof and exact run binding."""
    found = github.find_workflow_run(repository, workflow=workflow, ref="main", task_id=task_id, expected_head_sha=commit)
    if found is None: raise RuntimeError("cancellation workflow run is missing")
    run_id = int(found.run_id)
    if stored_run_id and run_id != stored_run_id: raise RuntimeError("cancellation workflow run id drifted")
    run = dict(github.get_run(repository, run_id))
    if int(run.get("id") or 0) != run_id or str(run.get("head_sha") or "").lower() != commit or run.get("event") != "workflow_dispatch" or int(run.get("run_attempt") or 0) != 1:
        raise RuntimeError("cancellation run binding drifted")
    return run

def cleanup_terminal_attempt_one(*, task_id: str, repository: str, credentials: Any, task_store: Any, github_app: Any, github: Any, expected_worker_commit: str) -> None:
    """Synthetic-only terminal attempt-one cleanup; never accepts result import."""
    remote = dict(task_store.get_remote_run(task_id) or {}); run_id = int(remote.get("run_id") or 0)
    run = _unique_bound_run(github, repository=repository, workflow="process.yml", task_id=task_id, commit=expected_worker_commit, stored_run_id=run_id)
    if str(remote.get("repository") or repository) != repository or str(remote.get("workflow") or "process.yml") != "process.yml" or int(remote.get("attempt") or 1) != 1: raise RuntimeError("attempt-one race binding drifted")
    if run.get("status") != "completed" or str(run.get("conclusion") or "") in {"", "cancelled", "canceled"}: raise RuntimeError("attempt-one race is not terminal")
    if int(remote.get("issue_number") or 0) or int(remote.get("artifact_id") or 0): raise RuntimeError("attempt-one race unexpectedly published payload")
    # The exact run is the only deletion scope.  A terminal race must leave no
    # Actions artifacts before any local terminal record is written.
    artifacts = list(github.list_run_artifacts(repository, run_id))
    for artifact in artifacts:
        artifact_id = int(dict(artifact).get("id") or 0)
        if artifact_id <= 0: raise RuntimeError("attempt-one race artifact is invalid")
        github.delete_artifact(repository, artifact_id)
    if list(github.list_run_artifacts(repository, run_id)):
        raise RuntimeError("attempt-one race artifact cleanup did not converge")
    task_store.upsert_remote_run(task_id, repository=repository, workflow="process.yml", run_id=run_id, attempt=1, remote_state="failed")
    task_store.upsert_remote_attempt(task_id, 1, repository=repository, workflow="process.yml", run_id=run_id, github_status="completed", conclusion=str(run.get("conclusion")), worker_status="failed", import_state="not_started", cleanup_state="complete")
    github_app.finalize_process_canary_token_lease(task_id=task_id, task_store=task_store)
    from scripts.verify_github_process_canary import _executor_repositories
    from src.remote.worker_migration import build_signed_template_transition_gate
    gate = build_signed_template_transition_gate(
        credentials=credentials, task_store=task_store, github_app=github_app,
        repositories=_executor_repositories(credentials, repository),
    )
    if gate.get("ready") is not True or any(value is not True for value in dict(gate.get("checks") or {}).values()):
        raise RuntimeError("attempt-one race global zero audit failed")

def cancellation_once(*, credentials: Any, task_store: Any, github_app: Any, github: Any, coordinator: Any, verifier_go: str) -> dict[str, Any]:
    """One durable dispatch and one cancellation; ambiguity only observes.

    It never creates an envelope, Mailbox issue, result key, rerun bundle, or
    import callback.  The separate latch intentionally cannot affect the
    successful process-canary latch.
    """
    if verifier_go != GO["cancellation"]: raise RuntimeError("exact cancellation verifier GO token is required")
    from scripts.verify_github_process_canary import WORKFLOW, build_preflight
    from src.remote.coordinator import RemoteSettings
    latch = _load_cancel_latch(credentials)
    if latch.get("state") == "cleanup_only":
        raise RuntimeError("failed cancellation local recovery is required; cancellation replay is forbidden")
    if latch.get("state") == "cleanup_pending":
        task_id, commit, run_id = str(latch["task_id"]), str(latch["worker_commit"]), int(latch["run_id"])
        if not task_id or not run_id: raise RuntimeError("cancellation cleanup binding is invalid")
        repo = str(RemoteSettings.load(credentials).public_repo)
        live = _unique_bound_run(github, repository=repo, workflow=WORKFLOW, task_id=task_id, commit=commit, stored_run_id=run_id)
        if latch["cleanup_kind"] == "race_cleanup":
            cleanup_terminal_attempt_one(task_id=task_id, repository=repo, credentials=credentials, task_store=task_store, github_app=github_app, github=github, expected_worker_commit=commit)
            latch["state"] = "failed"; _save_cancel_latch(credentials, latch)
            raise RuntimeError("cancellation race concluded without cancellation")
        if live.get("status") != "completed" or str(live.get("conclusion") or "") not in {"cancelled", "canceled"}:
            raise RuntimeError("cancellation cleanup binding is no longer cancelled")
        coordinator.cleanup_canceled_run(task_id)
        github_app.finalize_process_canary_token_lease(task_id=task_id, task_store=task_store)
        audit = build_cancellation_zero_state_audit(task_id, credentials=credentials, task_store=task_store, github_app=github_app, github=github, expected_worker_commit=commit, repository=repo, workflow=WORKFLOW, run_id=run_id)
        latch["state"] = "complete"; _save_cancel_latch(credentials, latch)
        return _evidence("cancellation", "passed", attempt=1, worker_commit=commit, zero_count=len(dict(audit.get("zero_state") or {})))
    preflight = build_preflight(credentials=credentials, task_store=task_store, github_app=github_app, require_zero_state=latch.get("state") != "cleanup_pending")
    commit = str(preflight["expected_worker_commit"])
    if latch and latch.get("worker_commit") != commit and latch.get("state") not in {"complete", "failed"}: raise RuntimeError("earlier cancellation gate requires recovery")
    if latch.get("state") in {"complete", "failed"}: raise RuntimeError("cancellation gate terminal for signed worker commit")
    if not latch:
        latch = {"task_id": uuid.uuid4().hex, "worker_commit": commit, "run_id": 0, "state": "prepared", "dispatch_budget": 1, "cancel_budget": 1}
        _save_cancel_latch(credentials, latch)
    task_id = str(latch["task_id"]); settings = RemoteSettings.load(credentials); repo = str(settings.public_repo)
    # Rebind before every action.  More than one match is rejected by the client.
    found = github.find_workflow_run(repo, workflow=WORKFLOW, ref="main", task_id=task_id, expected_head_sha=commit)
    if latch.get("state") == "cancel_post_unknown":
        if found is None: raise RuntimeError("cancellation POST outcome remains pending; observe only")
        live = _unique_bound_run(github, repository=repo, workflow=WORKFLOW, task_id=task_id, commit=commit, stored_run_id=int(latch.get("run_id") or found.run_id))
        if live.get("status") != "completed": raise RuntimeError("cancellation POST outcome remains pending; observe only")
        # Preserve the ambiguity state until the normal terminal cleanup path.
        latch["run_id"] = int(found.run_id); _save_cancel_latch(credentials, latch)
    if latch.get("state") == "dispatch_post_unknown":
        if found is None: raise RuntimeError("dispatch POST outcome remains pending; observe only")
        latch["run_id"] = int(found.run_id); latch["state"] = "dispatched"; _save_cancel_latch(credentials, latch)
    if found is not None and latch.get("state") != "cancel_post_unknown":
        latch["run_id"] = int(found.run_id); latch["state"] = "dispatched"; _save_cancel_latch(credentials, latch)
    if not latch["run_id"]:
        if latch["dispatch_budget"] != 1: raise RuntimeError("cancellation dispatch budget exhausted")
        # Keep this lease through ambiguous POST results: a later invocation may
        # only observe the same run, never dispatch another one.
        github_app.acquire_job_token(task_id=task_id, task_store=task_store)
        latch["dispatch_budget"] = 0; latch["state"] = "dispatched"; _save_cancel_latch(credentials, latch)
        try:
            dispatched = github.dispatch_workflow(repo, workflow=WORKFLOW, ref="main", task_id=task_id, protocol_version=PROTOCOL_VERSION, expected_head_sha=commit)
            latch["run_id"] = int(dispatched.run_id); _save_cancel_latch(credentials, latch)
        except Exception:
            # POST outcome is unknown: observe once, never POST a second dispatch.
            latch["state"] = "dispatch_post_unknown"; _save_cancel_latch(credentials, latch)
            found = github.find_workflow_run(repo, workflow=WORKFLOW, ref="main", task_id=task_id, expected_head_sha=commit)
            if found is None: raise RuntimeError("dispatch POST outcome unknown; observation required")
            latch["run_id"] = int(found.run_id); latch["state"] = "dispatched"; _save_cancel_latch(credentials, latch)
    run_id = int(latch["run_id"])
    task_store.upsert_remote_run(task_id, repository=repo, workflow=WORKFLOW, run_id=run_id, attempt=1, remote_state="canceling")
    live = _unique_bound_run(github, repository=repo, workflow=WORKFLOW, task_id=task_id, commit=commit, stored_run_id=run_id)
    if live.get("status") != "completed":
        if latch.get("state") == "cancel_post_unknown":
            raise RuntimeError("cancellation POST outcome remains pending; observe only")
        if latch["cancel_budget"] != 1: raise RuntimeError("cancellation budget exhausted")
        latch["cancel_budget"] = 0; latch["state"] = "canceling"; _save_cancel_latch(credentials, latch)
        try: github.cancel_run(repo, run_id)
        except Exception:
            latch["state"] = "cancel_post_unknown"; _save_cancel_latch(credentials, latch)
            raise RuntimeError("cancellation POST outcome unknown; observe only")
        live = _unique_bound_run(github, repository=repo, workflow=WORKFLOW, task_id=task_id, commit=commit, stored_run_id=run_id)
    if live.get("status") != "completed": raise RuntimeError("cancellation remains pending; observe/cleanup only")
    conclusion = str(live.get("conclusion") or "")
    if conclusion not in {"cancelled", "canceled"}:
        latch["state"] = "cleanup_pending"; latch["cleanup_kind"] = "race_cleanup"; _save_cancel_latch(credentials, latch)
        # A successful/failed race never passes, but it still converges any
        # run-owned residue before becoming terminal.
        cleanup_terminal_attempt_one(task_id=task_id, repository=repo, credentials=credentials, task_store=task_store, github_app=github_app, github=github, expected_worker_commit=commit)
        latch["state"] = "failed"; _save_cancel_latch(credentials, latch)
        raise RuntimeError("cancellation race concluded without cancellation")
    latch["state"] = "cleanup_pending"; latch["cleanup_kind"] = "cancelled_cleanup"; _save_cancel_latch(credentials, latch)
    coordinator.cleanup_canceled_run(task_id)
    github_app.finalize_process_canary_token_lease(task_id=task_id, task_store=task_store)
    audit = build_cancellation_zero_state_audit(task_id, credentials=credentials, task_store=task_store, github_app=github_app, github=github, expected_worker_commit=commit, repository=repo, workflow=WORKFLOW, run_id=run_id)
    latch["state"] = "complete"; _save_cancel_latch(credentials, latch)
    return _evidence("cancellation", "passed", attempt=1, worker_commit=commit, zero_count=len(dict(audit.get("zero_state") or {})))


def read_only_cancellation_preflight(*, credentials: Any, task_store: Any, github_app: Any) -> dict[str, Any]:
    """Canonical cancellation gate: observations only, never a dispatch path."""
    from scripts.verify_github_process_canary import (
        LATCH_SECRET, RERUN_BUNDLE_PREFIX, _load_latch, build_preflight,
    )
    checks: dict[str, bool] = {
        "cancellation_latch_absent": not credentials.has_secret(CANCEL_LATCH),
        "process_latch_complete": False,
        "process_rerun_budget_zero": False,
        "remote_disabled": False,
        "signed_integrity": False,
        "installation_scope_exact": False,
        "installation_id_matches_persisted": False,
        "executor_zero": False,
        "mailbox_zero": False,
        "local_task_zero": False,
        "local_remote_run_zero": False,
        "local_attempt_zero": False,
        "local_lease_zero": False,
        "local_auto_import_zero": False,
        "result_key_zero": False,
        "rerun_bundle_zero": False,
        "refresh_marker_zero": False,
        "cleanup_zero": False,
        "replay_rejection": False,
        "tamper_rejection": False,
        "reconstruction": False,
        "local_db_snapshot_stable": False,
    }
    try:
        db_snapshot = task_store.readonly_snapshot()
        process = _load_latch(credentials) if credentials.has_secret(LATCH_SECRET) else {}
        checks["process_latch_complete"] = process.get("state") == "complete"
        checks["process_rerun_budget_zero"] = process.get("rerun_budget") == 0
        preflight = build_preflight(
            credentials=credentials, task_store=task_store, github_app=github_app,
            require_zero_state=True, read_only=True,
        )
        zero = dict(preflight.get("zero_state") or {})
        checks["remote_disabled"] = str(credentials.load_secret("remote_enabled")).strip().lower() in {"0", "false", "off", "no"}
        checks["signed_integrity"] = bool(preflight.get("ready")) and bool(preflight.get("expected_worker_commit"))
        resources = dict(github_app.inspect_managed_resources())
        installation = dict(resources.get("installation") or {})
        persisted = int(credentials.load_secret("github_app_installation_id")) if credentials.has_secret("github_app_installation_id") else 0
        checks["installation_scope_exact"] = installation.get("repository_selection_exact") is True
        checks["installation_id_matches_persisted"] = persisted > 0 and int(installation.get("installation_id") or 0) == persisted
        checks["executor_zero"] = zero.get("active_task_run_count") == 0 and zero.get("task_artifact_count") == 0
        checks["mailbox_zero"] = zero.get("mailbox_managed_comment_count") == 0 and zero.get("mailbox_temporary_content_count") == 0
        checks["local_task_zero"] = int(task_store.active_task_count()) == 0
        checks["local_remote_run_zero"] = int(task_store.active_remote_run_count()) == 0
        checks["local_attempt_zero"] = int(task_store.active_remote_attempt_count()) == 0
        checks["local_lease_zero"] = not list(task_store.list_remote_token_leases())
        checks["local_auto_import_zero"] = int(task_store.active_automation_import_count()) == 0
        checks["result_key_zero"] = not list(credentials.list_secret_names(prefix="remote_result_private:"))
        checks["rerun_bundle_zero"] = not list(credentials.list_secret_names(prefix=RERUN_BUNDLE_PREFIX))
        checks["refresh_marker_zero"] = not list(credentials.list_secret_names(prefix="process_canary_rerun_refresh:"))
        checks["cleanup_zero"] = zero.get("cleanup_pending_count") == 0
        checks["replay_rejection"] = replay_rejection().get("status") == "passed"
        checks["tamper_rejection"] = tamper_rejection().get("status") == "passed"
        from scripts.verify_public_template_reconstruction import reconstruct
        checks["reconstruction"] = reconstruct().get("status") == "passed"
        if task_store.readonly_snapshot() != db_snapshot:
            raise RuntimeError("read-only task-store snapshot drifted during preflight")
        checks["local_db_snapshot_stable"] = True
    except Exception:
        pass
    report = {
        "schema": "courselens.read-only-cancellation-preflight.v1",
        "status": "ready" if all(checks.values()) else "not_ready",
        "checks": checks,
    }
    if report["status"] == "ready":
        report["required_verifier_go_sha256"] = hashlib.sha256(GO["cancellation"].encode()).hexdigest()
        report["required_verifier_go"] = GO["cancellation"]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gate", choices=sorted(GO), nargs="?")
    parser.add_argument("--go", "--verifier-go", dest="go", default="")
    parser.add_argument("--run", "--dispatch-once", dest="run", action="store_true", help="perform the explicitly confirmed gate")
    parser.add_argument("--read-only-cancellation-preflight", action="store_true")
    parser.add_argument("--recover-failed-cancellation-local-only", action="store_true")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--worker-commit", default="")
    parser.add_argument("--run-id", type=int, default=0)
    args = parser.parse_args(argv)
    if args.read_only_cancellation_preflight:
        if args.run or args.gate or args.recover_failed_cancellation_local_only:
            parser.error("read-only cancellation preflight cannot be combined with a gate action")
        from credentials import CredentialStore
        from path_utils import DEFAULT_DATA_DIR
        from src.remote.github_app import GitHubAppClient
        from src.runtime.task_store import TaskStore
        credentials = CredentialStore()
        report = read_only_cancellation_preflight(
            credentials=credentials,
            task_store=TaskStore(DEFAULT_DATA_DIR / "state.db", read_only=True),
            github_app=GitHubAppClient(credentials),
        )
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 0 if report["status"] == "ready" else 1
    if args.recover_failed_cancellation_local_only:
        if args.run or args.gate:
            parser.error("failed cancellation local recovery cannot be combined with a gate action")
        from credentials import CredentialStore
        from path_utils import DEFAULT_DATA_DIR
        from src.runtime.task_store import TaskStore
        report = recover_failed_cancellation_local_only(
            credentials=CredentialStore(), task_store=TaskStore(DEFAULT_DATA_DIR / "state.db"),
            verifier_go=args.go, task_id=args.task_id,
            worker_commit=args.worker_commit, run_id=args.run_id,
        )
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 0
    if not args.gate:
        parser.error("a synthetic gate or --read-only-cancellation-preflight is required")
    if not args.run:
        print(json.dumps(_evidence(args.gate, "preflight", required_go_sha256=hashlib.sha256(GO[args.gate].encode()).hexdigest()), sort_keys=True))
        return 0
    if args.go != GO[args.gate]:
        raise SystemExit("exact verifier GO token is required")
    if args.gate == "cancellation":
        from credentials import CredentialStore
        from path_utils import DEFAULT_DATA_DIR
        from src.remote.github_app import GitHubAppClient
        from src.remote.github_client import GitHubClient
        from src.remote.coordinator import RemoteCoordinator, RemoteSettings
        from src.runtime.task_store import TaskStore
        credentials = CredentialStore(); store = TaskStore(DEFAULT_DATA_DIR / "state.db"); app = GitHubAppClient(credentials)
        settings = RemoteSettings.load(credentials); github = GitHubClient(settings.github_token, proxy_url=settings.proxy_url)
        report = cancellation_once(credentials=credentials, task_store=store, github_app=app, github=github, coordinator=RemoteCoordinator(settings, store, credentials, github=github), verifier_go=args.go)
    else: report = {"replay": replay_rejection, "tamper": tamper_rejection}[args.gate]()
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
