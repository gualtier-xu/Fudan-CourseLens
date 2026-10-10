"""Fail-closed inventory for the retired signed-template-direct mode.

The direct mode was retired: every account uses a personal Worker plus a
private Mailbox, and the public template never executes real tasks.  This
module keeps the read-only zero-state helpers used by the process canary and
provides the retirement audit; the former migrate/rollback/finalize state
machine is intentionally gone.
"""

from __future__ import annotations

import json
from typing import Any

from .github_app import GitHubAppError, _bundled_worker_config, _same_repository
from .github_client import ISSUE_LABEL as MANAGED_ISSUE_LABEL


ACTIVE_RUN_STATES = frozenset({"queued", "in_progress", "requested", "waiting", "pending"})
TERMINAL_RUN_STATES = frozenset({"completed"})
TASK_WORKFLOWS = ("process.yml", "echo.yml")
TASK_ARTIFACT_PREFIXES = ("courselens-result-", "courselens-checkpoint-")
MANAGED_ISSUE_TITLE_PREFIX = "[courselens-job]"
CONSUMED_MARKER = {"schema": "mailbox.v2", "state": "consumed"}
WORKER_ENVIRONMENT = "courselens-worker"


class SignedTemplateMigrationError(RuntimeError):
    def __init__(self, code: str, *, actions: tuple[str, ...] = ()):
        super().__init__(code)
        self.code = code
        self.actions = actions

    def public(self) -> dict[str, Any]:
        return {
            "status": "action_required",
            "error_code": self.code,
            "actions": list(self.actions),
            "remote_enabled": False,
        }


def _known_mailbox_issues(task_store: Any) -> dict[int, str]:
    """Map locally bound Mailbox issue numbers to "active"/"history" activity."""
    from src.runtime.task_store import REMOTE_RUN_RECOVERABLE_STATES

    known: dict[int, str] = {}
    try:
        runs = task_store.list_remote_runs(limit=50)
    except Exception:
        # A failed local read must not widen into an unknown inventory.
        raise RuntimeError("mailbox_inventory_incomplete")
    for run in runs:
        number = int(run.get("issue_number") or 0)
        if number <= 0 or number in known:
            continue
        known[number] = (
            "active"
            if str(run.get("remote_state") or "") in REMOTE_RUN_RECOVERABLE_STATES
            else "history"
        )
    return known


def _managed_mailbox_inventory(
    github_app: Any,
    repository: str,
    token: str,
    *,
    known_issues: dict[int, str] | None = None,
) -> dict[str, Any]:
    """Classified read-only inventory of managed Mailbox issues.

    The listing is unfiltered (state=all, no label filter) so a removed label
    or a renamed title cannot hide payload residue; attribution requires the
    managed title prefix and/or the managed label, or an exact local issue
    binding.  Comment bodies are never downloaded: the issue metadata comment
    counter is only type/range checked, and a closed unconsumed history issue
    is reported as reconcilable instead of failing on its comment volume.
    Issue numbers in the result are for backend operation context only and
    must not enter logs, observations, or UI payloads.
    """
    pending_known = {
        int(number): str(activity) for number, activity in dict(known_issues or {}).items()
    }
    issues: list[Any] = []
    for page in range(1, 11):
        response = github_app._api(
            "GET",
            f"/repos/{repository}/issues",
            token=token,
            expected=(200,),
            params={"state": "all", "per_page": 100, "page": page},
        )
        values = response.json()
        if not isinstance(values, list):
            raise RuntimeError("mailbox_inventory_incomplete")
        issues.extend(values)
        if len(values) < 100:
            break
    else:
        raise RuntimeError("mailbox_issue_inventory_too_large")

    counts = {
        "managed": 0,
        "open_unconsumed": 0,
        "closed_unconsumed": 0,
        "consumed_open": 0,
        "consumed_closed": 0,
        "metadata_drift": 0,
        "active_comments": 0,
        "active_issue_missing": 0,
        "history_issue_missing": 0,
    }
    reconcilable: list[int] = []
    seen: set[int] = set()
    for issue in issues:
        if not isinstance(issue, dict):
            raise RuntimeError("mailbox_inventory_incomplete")
        number = issue.get("number")
        if type(number) is not int or number <= 0 or number in seen:
            raise RuntimeError("mailbox_inventory_incomplete")
        seen.add(number)
        title = issue.get("title")
        if not isinstance(title, str):
            raise RuntimeError("mailbox_inventory_incomplete")
        labels_raw = issue.get("labels")
        if not isinstance(labels_raw, list):
            raise RuntimeError("mailbox_inventory_incomplete")
        label_names: set[str] = set()
        for label in labels_raw:
            if not isinstance(label, dict) or not isinstance(label.get("name"), str):
                raise RuntimeError("mailbox_inventory_incomplete")
            label_names.add(label["name"])
        state = issue.get("state")
        if state not in ("open", "closed"):
            raise RuntimeError("mailbox_inventory_incomplete")
        comment_count = issue.get("comments")
        if type(comment_count) is not int or comment_count < 0:
            raise RuntimeError("mailbox_inventory_incomplete")
        locally_bound = pending_known.pop(number, "")
        has_prefix = title.startswith(MANAGED_ISSUE_TITLE_PREFIX)
        has_label = MANAGED_ISSUE_LABEL in label_names
        if has_prefix and has_label:
            pass  # managed issue
        elif has_prefix or has_label or locally_bound:
            # A halfway-renamed managed issue (or a locally bound object that
            # lost both markers) cannot be classified safely: report drift.
            counts["metadata_drift"] += 1
            continue
        else:
            # Neither managed marker nor a local binding: never guess.
            continue
        try:
            marker = json.loads(str(issue.get("body") or ""))
        except (TypeError, ValueError):
            marker = None
        counts["managed"] += 1
        if marker == CONSUMED_MARKER:
            if state == "open":
                counts["consumed_open"] += 1
            else:
                counts["consumed_closed"] += 1
        elif state == "open":
            # Only live payload comments matter for the bounded gate; the
            # metadata counter replaces downloading hundreds of comments.
            if comment_count >= 100:
                raise RuntimeError("mailbox_comment_inventory_too_large")
            counts["open_unconsumed"] += 1
            counts["active_comments"] += comment_count
        else:
            counts["closed_unconsumed"] += 1
            reconcilable.append(number)
    for number in sorted(pending_known):
        # Locally bound numbers missing from a complete listing get one
        # directed verification instead of a silent pass.
        response = github_app._api(
            "GET",
            f"/repos/{repository}/issues/{number}",
            token=token,
            expected=(200, 404),
        )
        if response.status_code == 404:
            if pending_known[number] == "active":
                counts["active_issue_missing"] += 1
            else:
                counts["history_issue_missing"] += 1
            continue
        # Present in the repository but absent from a complete listing is an
        # unexplainable state; fail closed instead of guessing.
        raise RuntimeError("mailbox_inventory_incomplete")
    return {
        "complete": True,
        "managed_issue_count": counts["managed"],
        "open_unconsumed_count": counts["open_unconsumed"],
        "closed_unconsumed_count": counts["closed_unconsumed"],
        "consumed_open_count": counts["consumed_open"],
        "consumed_closed_count": counts["consumed_closed"],
        "metadata_drift_count": counts["metadata_drift"],
        "active_comment_count": counts["active_comments"],
        "active_issue_missing_count": counts["active_issue_missing"],
        "history_issue_missing_count": counts["history_issue_missing"],
        "reconcilable_issue_numbers": sorted(reconcilable),
    }


def _active_task_run_count(github_app: Any, repository: str, token: str) -> int:
    count = 0
    for workflow in TASK_WORKFLOWS:
        total_count: int | None = None
        raw_count = 0
        run_ids: set[int] = set()
        for page in range(1, 11):
            response = github_app._api(
                "GET",
                f"/repos/{repository}/actions/workflows/{workflow}/runs",
                token=token,
                expected=(200, 404),
                params={"event": "workflow_dispatch", "per_page": 100, "page": page},
            )
            if response.status_code == 404:
                # Reset resume probes between DELETE and its confirmation hit a
                # repository that is already gone; like the secret inventory,
                # a missing repository carries zero residue instead of a
                # dead-stopping readback error.
                return 0
            payload = response.json() or {}
            runs = payload.get("workflow_runs")
            value = payload.get("total_count")
            if (
                not isinstance(runs, list)
                or type(value) is not int
                or value < 0
                or total_count is not None and value != total_count
            ):
                raise RuntimeError("task_run_inventory_incomplete")
            total_count = value
            for run in runs:
                if not isinstance(run, dict):
                    raise RuntimeError("task_run_inventory_incomplete")
                run_id = run.get("id")
                if type(run_id) is not int or run_id <= 0 or run_id in run_ids:
                    raise RuntimeError("task_run_inventory_incomplete")
                run_ids.add(run_id)
                raw_count += 1
                if str(run.get("status") or "").strip().lower() not in TERMINAL_RUN_STATES:
                    count += 1
            if len(runs) < 100:
                if raw_count != total_count:
                    raise RuntimeError("task_run_inventory_incomplete")
                break
        else:
            if raw_count != total_count:
                raise RuntimeError("task_run_inventory_too_large")
    return count


def _task_artifact_count(github_app: Any, repository: str, token: str) -> int:
    ids: set[int] = set()
    for page in range(1, 11):
        response = github_app._api(
            "GET",
            f"/repos/{repository}/actions/artifacts",
            token=token,
            expected=(200, 404),
            params={"per_page": 100, "page": page},
        )
        if response.status_code == 404:
            return 0  # already-deleted repository: zero artifacts
        artifacts = list((response.json() or {}).get("artifacts") or [])
        ids.update(
            int(item.get("id") or 0)
            for item in artifacts
            if int(item.get("id") or 0) > 0
            and str(item.get("name") or "").startswith(TASK_ARTIFACT_PREFIXES)
        )
        if len(artifacts) < 100:
            return len(ids)
    raise RuntimeError("task_artifact_inventory_too_large")


def _repository_secret_names(
    github_app: Any, repository: str, token: str
) -> set[str]:
    response = github_app._api(
        "GET",
        f"/repos/{repository}/environments/{WORKER_ENVIRONMENT}/secrets",
        token=token,
        expected=(200, 404),
        params={"per_page": 100},
    )
    if response.status_code == 404:
        return set()
    return {
        str(item.get("name") or "")
        for item in list((response.json() or {}).get("secrets") or [])
        if str(item.get("name") or "")
    }


# Observations that describe history evidence or reconcile guidance and must
# never derive a blocking ``_zero`` check by themselves.
NON_BLOCKING_OBSERVATIONS = frozenset({
    "executor_count",
    "mailbox_closed_unconsumed_count",
    "mailbox_history_missing_count",
})


def _mailbox_gate_observations(
    inventory: dict[str, Any], *, mode: str
) -> dict[str, int]:
    observations = {
        "mailbox_managed_comment_count": int(inventory["active_comment_count"]),
        "mailbox_metadata_drift_count": int(inventory["metadata_drift_count"]),
        "mailbox_active_issue_missing_count": int(inventory["active_issue_missing_count"]),
        "mailbox_history_missing_count": int(inventory["history_issue_missing_count"]),
        "mailbox_consumed_open_count": int(inventory["consumed_open_count"]),
    }
    if mode == "strict":
        # Migration/deletion gates keep demanding literally zero residue:
        # closed history is still unconsumed payload for them.
        observations["mailbox_temporary_content_count"] = (
            int(inventory["open_unconsumed_count"])
            + int(inventory["closed_unconsumed_count"])
        )
    else:
        # Runnable zero state: only live payload blocks; closed unconsumed
        # history is directed to the explicit reconcile action instead.
        observations["mailbox_open_content_count"] = int(inventory["open_unconsumed_count"])
        observations["mailbox_closed_unconsumed_count"] = int(
            inventory["closed_unconsumed_count"]
        )
    return observations


def build_signed_template_transition_gate(
    *,
    credentials: Any,
    task_store: Any,
    github_app: Any,
    repositories: tuple[str, ...],
    read_only: bool = False,
    mailbox_mode: str = "strict",
) -> dict[str, Any]:
    """Build a privacy-safe zero gate across every possible executor.

    ``mailbox_mode="strict"`` (default) keeps the historical literally-zero
    mailbox residue contract used by migration/deletion gates.
    ``mailbox_mode="runnable"`` demands zero *live* payload and lets closed
    unconsumed history surface as a reported count for the reconcile path.
    """
    if mailbox_mode not in ("strict", "runnable"):
        raise ValueError("mailbox_mode must be strict or runnable")
    token = github_app.access_token(minimum_lifetime_seconds=900, no_refresh=True) if read_only else github_app.access_token(minimum_lifetime_seconds=900)
    mailbox = str(credentials.load_secret("github_mailbox_repo"))
    active_runs = sum(
        _active_task_run_count(github_app, repository, token)
        for repository in repositories
    )
    artifacts = sum(
        _task_artifact_count(github_app, repository, token)
        for repository in repositories
    )
    inventory = _managed_mailbox_inventory(
        github_app, mailbox, token,
        known_issues=_known_mailbox_issues(task_store),
    )
    leases = list(task_store.list_remote_token_leases())
    result_keys = list(credentials.list_secret_names(prefix="remote_result_private:"))
    cleanup_pending = int(task_store.migration_cleanup_pending_count())
    if credentials.has_secret("github_job_token_cleanup_pending"):
        cleanup_pending += 1
    temporary_tokens = sum(
        "COURSELENS_JOB_TOKEN"
        in _repository_secret_names(github_app, repository, token)
        for repository in repositories
    )
    observations = {
        "executor_count": len(set(repository.casefold() for repository in repositories)),
        "active_task_run_count": active_runs,
        "task_artifact_count": artifacts,
        "local_token_lease_count": len(leases),
        "local_result_key_count": len(result_keys),
        "cleanup_pending_count": cleanup_pending,
        "temporary_job_token_count": temporary_tokens,
    }
    observations.update(_mailbox_gate_observations(inventory, mode=mailbox_mode))
    checks = {
        name.removesuffix("_count") + "_zero": count == 0
        for name, count in observations.items()
        if name not in NON_BLOCKING_OBSERVATIONS
    }
    return {
        "schema": "courselens.signed-template-transition-gate.v1",
        "ready": all(checks.values()),
        "checks": checks,
        "observations": observations,
    }


def build_signed_template_retirement_audit(
    *, credentials: Any, task_store: Any, github_app: Any
) -> dict[str, Any]:
    """Read-only audit proving the retired direct mode left no residue.

    The audit never mutates local or remote state.  It inspects the currently
    configured repositories and, when the legacy direct binding is still
    present, additionally audits the public template as an executor so a
    leftover direct state cannot pass unnoticed.
    """
    worker_repository = (
        str(credentials.load_secret("github_worker_repo"))
        if credentials.has_secret("github_worker_repo") else ""
    )
    mailbox_repository = (
        str(credentials.load_secret("github_mailbox_repo"))
        if credentials.has_secret("github_mailbox_repo") else ""
    )
    bundled = _bundled_worker_config()
    template_repository = str(bundled.get("repository") or "")
    legacy_direct_state_detected = bool(
        worker_repository
        and template_repository
        and _same_repository(worker_repository, template_repository)
    )

    def closed(status: str) -> dict[str, Any]:
        return {
            "schema": "courselens.signed-template-retirement-audit.v1",
            "status": status,
            "ready": False,
            "legacy_direct_state_detected": legacy_direct_state_detected,
            "worker_repository": worker_repository,
            "mailbox_repository": mailbox_repository,
            "checks": {},
            "observations": {},
        }

    if not credentials.has_secret("github_app_access_token"):
        return closed("authorization_missing")
    try:
        token = github_app.access_token(minimum_lifetime_seconds=900, no_refresh=True)
        candidates = [worker_repository]
        if legacy_direct_state_detected and template_repository:
            candidates.append(template_repository)
        # Deduplicate case-insensitively while keeping declaration order.
        seen: set[str] = set()
        audited_executors: list[str] = []
        for repository in candidates:
            if not repository:
                continue
            key = repository.casefold()
            if key in seen:
                continue
            seen.add(key)
            audited_executors.append(repository)
        active_runs = sum(
            _active_task_run_count(github_app, repository, token)
            for repository in audited_executors
        )
        task_artifacts = sum(
            _task_artifact_count(github_app, repository, token)
            for repository in audited_executors
        )
        if mailbox_repository:
            mailbox_inventory = _managed_mailbox_inventory(
                github_app, mailbox_repository, token,
                known_issues=_known_mailbox_issues(task_store),
            )
        else:
            mailbox_inventory = {
                "active_comment_count": 0,
                "metadata_drift_count": 0,
                "active_issue_missing_count": 0,
                "history_issue_missing_count": 0,
                "consumed_open_count": 0,
                "open_unconsumed_count": 0,
                "closed_unconsumed_count": 0,
            }
        leases = list(task_store.list_remote_token_leases())
        result_keys = list(credentials.list_secret_names(prefix="remote_result_private:"))
        cleanup_pending = int(task_store.migration_cleanup_pending_count())
        if credentials.has_secret("github_job_token_cleanup_pending"):
            cleanup_pending += 1
        temporary_tokens = sum(
            "COURSELENS_JOB_TOKEN"
            in _repository_secret_names(github_app, repository, token)
            for repository in audited_executors
        )
    except GitHubAppError as exc:
        if exc.code in {"authorization_revoked", "authorization_not_fresh"}:
            return closed("authorization_missing")
        raise
    observations = {
        "active_task_run_count": active_runs,
        "task_artifact_count": task_artifacts,
        "local_token_lease_count": len(leases),
        "local_result_key_count": len(result_keys),
        "cleanup_pending_count": cleanup_pending,
        "old_worker_temporary_token_count": temporary_tokens,
    }
    observations.update(_mailbox_gate_observations(mailbox_inventory, mode="strict"))
    checks = {
        name.removesuffix("_count") + "_zero": count == 0
        for name, count in observations.items()
        if name not in NON_BLOCKING_OBSERVATIONS
    }
    checks["no_legacy_direct_state"] = not legacy_direct_state_detected
    ready = all(checks.values())
    return {
        "schema": "courselens.signed-template-retirement-audit.v1",
        "status": "ready" if ready else "residue_present",
        "ready": ready,
        "legacy_direct_state_detected": legacy_direct_state_detected,
        "worker_repository": worker_repository,
        "mailbox_repository": mailbox_repository,
        "checks": checks,
        "observations": observations,
    }


__all__ = [
    "ACTIVE_RUN_STATES",
    "TASK_ARTIFACT_PREFIXES",
    "TASK_WORKFLOWS",
    "SignedTemplateMigrationError",
    "build_signed_template_retirement_audit",
    "build_signed_template_transition_gate",
]
