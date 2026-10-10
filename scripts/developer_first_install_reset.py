"""Developer-only first-install reset CLI (contract: docs/developer-first-install-reset-handoff.md).

Returns the developer's own CourseLens client to first-install state by
deleting exactly the two bound managed repositories and the local generated
state.  This is a developer tool: no student-facing UI, no second control
plane, no name-based repository discovery, and no new credential store.  It
reuses the existing ``disconnect_github``, ``CredentialStore``, ``TaskStore``
and remote-preflight paths; the only remote calls are read-only readbacks,
the narrow ``delete_job_token`` primitive, and the two verified repository
deletions.

Safety shape:

* The default invocation is ``--dry-run`` and performs no mutation at all.
* A real run requires, and fail-closes without, an explicit operation id, a
  stdin GO line naming that exact id, a fresh risk-pass reference, exclusive
  write ownership, and a fresh exact zero-state readback (remote disabled,
  global pause on, no local/remote work, no leases, result keys, imports,
  cleanup markers, active Worker runs or unconsumed Mailbox records).
* Before any mutation, an immutable reset binding is stored atomically in
  the existing DPAPI store (one ``update_secrets`` transaction) and read
  back.  Targets are deleted strictly by numeric repository id: the fresh
  ``GET /repos/{full_name}`` must return exactly the bound id, owner,
  visibility and managed marker immediately before each ``DELETE``, and the
  former id is confirmed 404 afterwards.  A same-name repository with a
  different id is an identity-drift hard stop, never a target.
* The fixed order is: binding -> narrow temporary revocation -> Worker ->
  Mailbox -> ``disconnect_github`` plus atomic local cleanup -> isolated
  first-install verification.  Every phase is idempotent and a rerun
  resumes forward only from the persisted binding.  Authorization lost at
  or after either deletion returns ``reauth_required`` with the preserved
  operation and the remaining exact ids; it never clears target facts,
  rediscovers by name, recreates repositories, or replays a completed
  deletion.

The product's own startup path (``TaskStore.recover_for_startup``)
normalizes the global pause for an empty store.  The reset therefore proves
``global_paused=true`` on the settled store before any restart, and proves
``remote_enabled`` disabled both before the restart and on the restarted
first-install application.

Secrets, tokens and course data never appear in this tool's output: the
report is assembled from an explicit whitelist of nonsecret fields only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from credentials import CredentialStore
from path_utils import DEFAULT_DATA_DIR, ensure_inside_project
from src.remote.connection import OPERATION_ID_RE
from src.remote.coordinator import RemoteSettings
from src.remote.github_app import (
    GitHubAppClient,
    GitHubAppError,
    MANAGED_DESCRIPTION,
)
from src.remote.worker_migration import build_signed_template_transition_gate
from src.runtime.task_store import TaskStore

BINDING_SECRET = "developer_reset_binding"
BINDING_SCHEMA = "courselens.developer-reset-binding.v1"
WORKER_EXPECTED_PRIVATE = False
MAILBOX_EXPECTED_PRIVATE = True
TEMPORARY_CREDENTIAL_PREFIXES = (
    "remote_result_private:",
    "process_canary_rerun_bundle:",
    "process_canary_rerun_refresh:",
)
GO_LINE_PREFIX = "GO "
ISOLATION_DIR_NAME = "reset-isolation"
LOCK_PREFIX = "developer-reset-"
LOCK_SUFFIX = ".lock"
# The DPAPI binding holds identifiers and flags only; this whitelist is both
# the schema validator and the "no reusable secret material" proof.
BINDING_ALLOWED_KEYS = frozenset({
    "schema", "operation_id", "created_at", "updated_at",
    "owner_login_casefold", "owner_account_id", "installation_id",
    "worker", "mailbox", "phase", "worker_delete_issued",
    "worker_deleted", "mailbox_delete_issued", "mailbox_deleted",
    "readback",
})
TARGET_ALLOWED_KEYS = frozenset({
    "repo_id", "full_name", "owner_login_casefold", "private", "managed",
})
BINDING_FLAG_NAMES = (
    "worker_delete_issued", "worker_deleted",
    "mailbox_delete_issued", "mailbox_deleted",
)
PHASE_ORDER = (
    "binding",
    "revoke_temporary",
    "delete_worker",
    "delete_mailbox",
    "local_cleanup",
    "verify_first_install",
    "terminal",
)
PHASE_AFTER_DELETION = {
    "worker": "delete_mailbox",
    "mailbox": "local_cleanup",
}
REAUTH_CODES = frozenset({"authorization_revoked", "authorization_not_fresh"})
HARD_AUTH_CODES = frozenset({"permission_denied", "request_rejected"})
AUTH_LOST_CODES = frozenset({"authorization_lost", "authorization_missing"})
EXIT_OK = 0
EXIT_BLOCKED = 2
EXIT_REAUTH_REQUIRED = 3
EXIT_VERIFY_FAILED = 4


class ResetError(RuntimeError):
    """A blocked or failed reset carrying a stable machine-readable code."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.blocked_reason = message or code


class HardStop(ResetError):
    """A fail-closed security stop; the dependent phase never runs."""


class ReauthRequired(ResetError):
    """Authorization was lost at or after a deletion.

    Carries the preserved operation id and the exact remaining deletion
    targets so a later authorized rerun can resume forward only.
    """

    def __init__(self, remaining: list[dict[str, Any]]):
        super().__init__(
            "reauth_required",
            "GitHub authorization was lost around a repository deletion; "
            "re-authorize and rerun to resume forward only",
        )
        self.remaining = remaining


# ---------------------------------------------------------------------------
# Binding storage (DPAPI, atomic, immutable targets, forward-only progress)
# ---------------------------------------------------------------------------


def _validate_binding_shape(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(BINDING_ALLOWED_KEYS):
        raise HardStop("binding_invalid", "reset binding has an unknown or missing field")
    if value.get("schema") != BINDING_SCHEMA:
        raise HardStop("binding_invalid", "reset binding schema mismatch")
    if not OPERATION_ID_RE.fullmatch(str(value.get("operation_id") or "")):
        raise HardStop("binding_invalid", "reset binding operation id is invalid")
    owner = str(value.get("owner_login_casefold") or "")
    try:
        account_id = int(value.get("owner_account_id") or 0)
        installation_id = int(value.get("installation_id") or 0)
    except (TypeError, ValueError) as exc:
        raise HardStop("binding_invalid", "reset binding identity fields are not integers") from exc
    if not owner or account_id <= 0 or installation_id <= 0:
        raise HardStop("binding_invalid", "reset binding identity fields are incomplete")
    for name in ("worker", "mailbox"):
        target = value.get(name)
        if not isinstance(target, dict) or set(target) != set(TARGET_ALLOWED_KEYS):
            raise HardStop("binding_invalid", "reset binding " + name + " target is invalid")
        repo_id = target.get("repo_id")
        if (
            type(repo_id) is not int or repo_id <= 0
            or str(target.get("full_name") or "").count("/") != 1
            or str(target.get("owner_login_casefold") or "") != owner
            or type(target.get("private")) is not bool
            or target.get("managed") is not True
        ):
            raise HardStop("binding_invalid", "reset binding " + name + " target is incomplete")
    if value["worker"]["private"] != WORKER_EXPECTED_PRIVATE or value["mailbox"]["private"] != MAILBOX_EXPECTED_PRIVATE:
        raise HardStop("binding_invalid", "reset binding visibility does not match the managed roles")
    phase = str(value.get("phase") or "")
    if phase not in PHASE_ORDER:
        raise HardStop("binding_invalid", "reset binding phase is unknown")
    for flag in BINDING_FLAG_NAMES:
        if type(value.get(flag)) is not bool:
            raise HardStop("binding_invalid", "reset binding flag " + flag + " is not boolean")
    if not isinstance(value.get("readback"), dict):
        raise HardStop("binding_invalid", "reset binding readback evidence is missing")
    return value


def _binding_immutable_fingerprint(value: dict[str, Any]) -> str:
    return json.dumps(
        {
            key: value.get(key)
            for key in sorted(set(BINDING_ALLOWED_KEYS) - {"phase", *BINDING_FLAG_NAMES, "updated_at", "readback"})
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def load_binding(credentials: CredentialStore) -> dict[str, Any] | None:
    """Read and validate the persisted binding; ``None`` when absent."""
    if not credentials.has_secret(BINDING_SECRET):
        return None
    try:
        raw = credentials.load_secret(BINDING_SECRET)
        value = json.loads(raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise HardStop("binding_unreadable", "stored reset binding is not valid JSON") from exc
    return _validate_binding_shape(value)


def _write_binding(credentials: CredentialStore, binding: dict[str, Any]) -> None:
    # One atomic DPAPI envelope transaction; the JSON holds identifiers only.
    credentials.update_secrets(
        {BINDING_SECRET: json.dumps(binding, sort_keys=True, separators=(",", ":"))}
    )


def _advance_binding(
    credentials: CredentialStore,
    *,
    phase: str | None = None,
    flags: dict[str, bool] | None = None,
    readback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Forward-only binding update: only progress fields may change."""
    binding = dict(load_binding(credentials) or {})
    if not binding:
        raise HardStop("binding_missing", "reset binding disappeared mid-operation")
    fingerprint = _binding_immutable_fingerprint(binding)
    if phase is not None:
        current = PHASE_ORDER.index(str(binding["phase"]))
        target = PHASE_ORDER.index(phase)
        if target < current:
            raise HardStop("binding_phase_regressed", "reset phase attempted to move backwards")
        binding["phase"] = phase
    for name, value in dict(flags or {}).items():
        if name not in BINDING_FLAG_NAMES:
            raise HardStop("binding_invalid", "unknown binding flag " + name)
        if value is False and binding.get(name) is True:
            raise HardStop("binding_phase_regressed", "reset flag " + name + " attempted to move backwards")
        binding[name] = bool(value)
    if readback is not None:
        binding["readback"] = readback
    binding["updated_at"] = time.time()
    if _binding_immutable_fingerprint(binding) != fingerprint:
        raise HardStop("binding_mutated", "immutable reset binding facts changed")
    _write_binding(credentials, binding)
    return load_binding(credentials)  # durable readback of the exact write


# ---------------------------------------------------------------------------
# Fresh readback and zero-state gate
# ---------------------------------------------------------------------------


def _require_remote_disabled(credentials: CredentialStore) -> dict[str, Any]:
    for name in ("FUDAN_COURSELENS_REMOTE_ENABLED", "REMOTE_COMPUTE_ENABLED"):
        if str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}:
            raise HardStop(
                "remote_enabled_env_override",
                name + " environment override keeps remote compute reachable",
            )
    if not credentials.has_secret("remote_enabled"):
        return {"remote_enabled": "absent"}
    flag = str(credentials.load_secret("remote_enabled")).strip().lower()
    if flag not in {"0", "false", "off", "no"}:
        raise HardStop(
            "remote_enabled_not_zero",
            "remote_enabled must already be explicitly disabled before a reset",
        )
    return {"remote_enabled": flag}


def _verified_repository(
    github_app: Any, *, token: str, full_name: str, owner_casefold: str, expected_private: bool
) -> dict[str, Any]:
    """Fresh exact readback of one configured managed repository."""
    response = github_app._api("GET", "/repos/" + full_name, token=token, expected=(200, 404))
    if response.status_code == 404:
        return {"exists": False}
    value = response.json()
    owner_login = str((value.get("owner") or {}).get("login") or "")
    repo_id = value.get("id")
    target = {
        "exists": True,
        "repo_id": int(repo_id) if type(repo_id) is int else 0,
        "full_name": str(value.get("full_name") or ""),
        "owner_login_casefold": owner_login.casefold(),
        "private": bool(value.get("private")),
        "managed": str(value.get("description") or "") == MANAGED_DESCRIPTION,
        "archived": bool(value.get("archived")),
        "disabled": bool(value.get("disabled")),
    }
    if (
        not target["repo_id"]
        or target["owner_login_casefold"] != owner_casefold
        or target["private"] is not expected_private
        or not target["managed"]
        or target["archived"]
        or target["disabled"]
    ):
        raise HardStop(
            "repository_readback_invalid",
            "configured repository " + full_name + " does not match the managed profile",
        )
    return target


def _map_readback_error(exc: GitHubAppError) -> Exception:
    """Fail-closed conversion for preflight/binding readback calls."""
    if exc.code in REAUTH_CODES:
        return HardStop("authorization_lost", "GitHub authorization is no longer usable")
    if exc.code in HARD_AUTH_CODES:
        return HardStop(
            "mfa_or_interactive_required",
            "GitHub refused a reset readback as if it needs interactive confirmation",
        )
    return HardStop(
        "github_unrecoverable_error",
        "GitHub refused a reset readback (" + exc.code + "); rerun to resume forward",
    )


def read_live_targets(
    github_app: Any,
    credentials: CredentialStore,
    *,
    require_bootstrapped: bool = True,
    read_only: bool = False,
) -> dict[str, Any]:
    """Fresh API readback of the owner, installation and both bound repos."""
    if not (
        credentials.has_secret("github_worker_repo")
        and credentials.has_secret("github_mailbox_repo")
    ):
        if require_bootstrapped:
            raise HardStop(
                "not_bootstrapped",
                "no personal Worker/Mailbox binding is configured; nothing to reset",
            )
        return {"bootstrapped": False}
    if not credentials.has_secret("github_app_access_token"):
        raise HardStop(
            "authorization_missing", "GitHub authorization is missing for the reset readback"
        )
    try:
        return _read_live_targets_inner(github_app, credentials, read_only=read_only)
    except GitHubAppError as exc:
        raise _map_readback_error(exc) from exc


def _read_live_targets_inner(
    github_app: Any, credentials: CredentialStore, *, read_only: bool
) -> dict[str, Any]:
    token = github_app.access_token(minimum_lifetime_seconds=900, no_refresh=read_only)
    identity = github_app._api("GET", "/user", token=token, expected=(200,)).json()
    owner = str(identity.get("login") or "").strip()
    account_id = identity.get("id")
    if not owner or type(account_id) is not int or account_id <= 0:
        raise HardStop("identity_unavailable", "GitHub identity readback is incomplete")
    owner_casefold = owner.casefold()
    worker = _verified_repository(
        github_app, token=token,
        full_name=str(credentials.load_secret("github_worker_repo")),
        owner_casefold=owner_casefold, expected_private=WORKER_EXPECTED_PRIVATE,
    )
    mailbox = _verified_repository(
        github_app, token=token,
        full_name=str(credentials.load_secret("github_mailbox_repo")),
        owner_casefold=owner_casefold, expected_private=MAILBOX_EXPECTED_PRIVATE,
    )
    inspect_installation = getattr(github_app, "_inspect_fresh_user_installation", None)
    if not callable(inspect_installation):
        raise HardStop(
            "installation_scope_not_exact",
            "GitHub App exact installation selection check is unavailable",
        )
    try:
        installation = inspect_installation(
            owner_casefold, token=token, require_exact_repository_selection=True
        )
    except GitHubAppError as exc:
        raise _map_readback_error(exc) from exc
    installation_id = int(dict(installation or {}).get("id") or 0)
    if installation_id <= 0:
        raise HardStop("installation_missing", "the CourseLens App installation is missing")
    return {
        "bootstrapped": True,
        "owner_login_casefold": owner_casefold,
        "owner_account_id": int(account_id),
        "installation_id": installation_id,
        "worker": worker,
        "mailbox": mailbox,
        "observed_at": time.time(),
    }


def collect_zero_state(
    credentials: CredentialStore,
    task_store: TaskStore | None,
    github_app: Any,
    *,
    live: dict[str, Any] | None = None,
    remote_checks: bool = True,
    read_only: bool = False,
) -> dict[str, Any]:
    """Fresh exact zero-state readback reusing the existing preflight gate.

    ``live`` is the fresh repository readback; repositories already deleted
    by a confirmed earlier phase are skipped instead of being re-queried (a
    404 there would poison a forward-only resume).  ``remote_checks=False``
    covers the resume path after both deletions are confirmed and the OAuth
    authority was already cleared: only local residue is checked then.
    """
    observations: dict[str, Any] = dict(_require_remote_disabled(credentials))
    failures: list[str] = []
    if task_store is None:
        observations["global_paused"] = False
        failures.append("state_store_missing")
    else:
        observations["global_paused"] = bool(task_store.global_paused)
        if not task_store.global_paused:
            failures.append("global_paused_false")
        local_active = int(task_store.count(states=("queued", "running", "pausing")))
        observations["local_active_task_count"] = local_active
        if local_active:
            failures.append("local_active_tasks_nonzero")
        observations["local_paused_task_count"] = int(task_store.count(states=("paused",)))
        if task_store.list_remote_token_leases():
            failures.append("local_token_lease_zero_false")
    if remote_checks:
        if credentials.has_secret("github_app_access_token"):
            worker_gone = bool(live and not (live.get("worker") or {}).get("exists"))
            mailbox_gone = bool(live and not (live.get("mailbox") or {}).get("exists"))
            if worker_gone and mailbox_gone:
                observations["worker_repository"] = "already_deleted_confirmed_by_binding"
                observations["mailbox_repository"] = "already_deleted_confirmed_by_binding"
            else:
                # The strict mailbox inventory lives inside the shared gate.
                # Once the Worker is deleted it cannot be listed as an
                # executor again, but the Mailbox must stay gated until its
                # own deletion confirms: an empty executor tuple yields
                # exactly the mailbox inventory plus the local checks.
                repositories: tuple[str, ...] = (
                    () if worker_gone else (str(credentials.load_secret("github_worker_repo")),)
                )
                try:
                    gate = build_signed_template_transition_gate(
                        credentials=credentials,
                        task_store=task_store if task_store is not None else _NullStore(),
                    github_app=github_app,
                    repositories=repositories,
                    read_only=read_only,
                    )
                except GitHubAppError as exc:
                    raise _map_readback_error(exc) from exc
                observations.update(dict(gate.get("observations") or {}))
                for name, passed in dict(gate.get("checks") or {}).items():
                    if not passed:
                        failures.append(name)
                if worker_gone:
                    observations["worker_repository"] = "already_deleted_confirmed_by_binding"
        else:
            failures.append("authorization_missing")
    for prefix in TEMPORARY_CREDENTIAL_PREFIXES:
        count = len(list(credentials.list_secret_names(prefix=prefix)))
        observations["temporary_credentials[" + prefix + "]"] = count
        if count:
            failures.append("temporary_credentials_nonzero")
    if remote_checks:
        # Contract zero gate: no unconsumed imports or artifacts may survive
        # into the destructive phases (imports are local rows; artifacts are
        # covered by the shared gate's artifact inventory above).
        if task_store is not None and task_store.list_automation_imports():
            failures.append("automation_imports_zero_false")
        observations["installation_id_present"] = bool(credentials.has_secret("github_app_installation_id"))
        if not observations["installation_id_present"]:
            failures.append("installation_id_missing")
    else:
        # After the credential wipe the DPAPI binding itself carries the
        # immutable installation id; the secret no longer exists by design.
        observations["installation_id_present"] = "retained_in_binding"
    return {"ready": not failures, "failures": sorted(failures), "observations": observations}


class _NullStore:
    """Zero-work stand-in when no local state database exists yet."""

    def list_remote_token_leases(self) -> list[dict[str, Any]]:
        return []

    def list_remote_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return []

    def migration_cleanup_pending_count(self) -> int:
        return 0


# ---------------------------------------------------------------------------
# Exclusive write ownership
# ---------------------------------------------------------------------------


class OwnershipLock:
    """Exclusive, crash-visible writer lock backed by an O_EXCL lock file."""

    def __init__(self, output_dir: Path, operation_id: str):
        self.path = output_dir / (LOCK_PREFIX + operation_id + LOCK_SUFFIX)
        self._handle: Any = None

    def __enter__(self) -> "OwnershipLock":
        stale = sorted(p.name for p in self.path.parent.glob(LOCK_PREFIX + "*" + LOCK_SUFFIX))
        if stale:
            raise HardStop(
                "writer_conflict", "another developer-reset writer holds " + ",".join(stale)
            )
        try:
            self._handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise HardStop(
                "writer_conflict", "another developer-reset writer holds the lock"
            ) from exc
        os.write(self._handle, str(os.getpid()).encode("ascii"))
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._handle is not None:
            try:
                os.close(self._handle)
            finally:
                try:
                    os.unlink(self.path)
                except OSError:
                    pass
                self._handle = None
        return None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class DeveloperResetEngine:
    """Executes the fixed forward-only phase sequence with injected deps."""

    def __init__(
        self,
        *,
        output_dir: Path | str,
        credentials: CredentialStore,
        task_store_factory: Callable[[], TaskStore],
        github_app: Any,
        application_provider: Callable[[], Any],
        operation_id: str,
        fresh_task_store_factory: Callable[[Path], TaskStore] | None = None,
        now: Callable[[], float] = time.time,
    ):
        self.output_dir = ensure_inside_project(Path(output_dir))
        self.credentials = credentials
        self._task_store_factory = task_store_factory
        self.task_store: TaskStore | None = None
        self.github_app = github_app
        self._application_provider = application_provider
        self.operation_id = operation_id
        self._fresh_task_store_factory = fresh_task_store_factory or (
            lambda path: TaskStore(path)
        )
        self._now = now
        self.phases_run: list[dict[str, Any]] = []
        self.failure_injections: dict[str, Callable[[], None]] = {}

    # -- helpers ----------------------------------------------------------

    def _inject(self, point: str) -> None:
        hook = self.failure_injections.get(point)
        if hook is not None:
            hook()

    def _record(self, phase: str, detail: dict[str, Any]) -> None:
        self.phases_run.append({"phase": phase, **detail})

    def _store(self) -> TaskStore:
        if self.task_store is None:
            self.task_store = self._task_store_factory()
        return self.task_store

    def _token(self) -> str:
        try:
            return self.github_app.access_token(minimum_lifetime_seconds=300)
        except GitHubAppError as exc:
            if exc.code in REAUTH_CODES:
                raise HardStop(
                    "authorization_lost", "GitHub authorization is no longer usable"
                ) from exc
            raise

    def _binding(self) -> dict[str, Any]:
        binding = load_binding(self.credentials)
        if not binding:
            raise HardStop("binding_missing", "reset binding disappeared mid-operation")
        return binding

    def _any_deletion_started(self) -> bool:
        binding = self._binding()
        return any(bool(binding.get(flag)) for flag in BINDING_FLAG_NAMES)

    def _both_deletions_confirmed(self) -> bool:
        binding = self._binding()
        return bool(binding.get("worker_deleted") and binding.get("mailbox_deleted"))

    def _authorization_lost(self) -> bool:
        try:
            self.github_app.access_token(minimum_lifetime_seconds=60)
        except GitHubAppError as exc:
            return exc.code in REAUTH_CODES
        except Exception:
            return False
        return False

    def _remaining_targets(self) -> list[dict[str, Any]]:
        binding = self._binding()
        remaining = []
        for role in ("worker", "mailbox"):
            if not binding.get(role + "_deleted"):
                target = dict(binding.get(role) or {})
                remaining.append({
                    "role": role,
                    "repo_id": int(target.get("repo_id") or 0),
                    "full_name": str(target.get("full_name") or ""),
                    "private": bool(target.get("private")),
                })
        return remaining

    def _map_github_error(self, exc: GitHubAppError) -> Exception:
        if exc.code in REAUTH_CODES:
            return HardStop("authorization_lost", "GitHub authorization is no longer usable")
        if exc.code in HARD_AUTH_CODES:
            return HardStop(
                "mfa_or_interactive_required",
                "GitHub refused a reset step as if it needs interactive confirmation",
            )
        # Any other GitHub failure is a fail-closed block: the binding stays,
        # and a rerun resumes forward from the persisted phase.
        return HardStop(
            "github_unrecoverable_error",
            "GitHub refused a reset step (" + exc.code + "); rerun to resume forward",
        )

    # -- phase 1: binding --------------------------------------------------

    def phase_binding(self) -> dict[str, Any]:
        self._inject("binding:before_readback")
        existing = load_binding(self.credentials)
        if existing is not None:
            if str(existing.get("operation_id")) != self.operation_id:
                raise HardStop(
                    "operation_id_mismatch",
                    "a reset binding for another operation id is already stored",
                )
            deletions_pending = PHASE_ORDER.index(str(existing["phase"])) <= PHASE_ORDER.index("delete_mailbox") and not (
                existing.get("worker_deleted") and existing.get("mailbox_deleted")
            )
            if deletions_pending:
                live = read_live_targets(self.github_app, self.credentials)
                self._verify_binding_matches_live(existing, live)
                _advance_binding(self.credentials, readback=self._readback_evidence(live))
            self._record("binding", {"resumed": True, "phase": existing["phase"]})
            return {"resumed": True}
        live = read_live_targets(self.github_app, self.credentials)
        self._inject("binding:before_write")
        if not live.get("bootstrapped"):
            raise HardStop("not_bootstrapped", "no managed repositories are configured")
        binding = {
            "schema": BINDING_SCHEMA,
            "operation_id": self.operation_id,
            "created_at": self._now(),
            "updated_at": self._now(),
            "owner_login_casefold": live["owner_login_casefold"],
            "owner_account_id": int(live["owner_account_id"]),
            "installation_id": int(live["installation_id"]),
            "worker": {
                "repo_id": int(live["worker"]["repo_id"]),
                "full_name": str(live["worker"]["full_name"]),
                "owner_login_casefold": live["owner_login_casefold"],
                "private": bool(live["worker"]["private"]),
                "managed": True,
            },
            "mailbox": {
                "repo_id": int(live["mailbox"]["repo_id"]),
                "full_name": str(live["mailbox"]["full_name"]),
                "owner_login_casefold": live["owner_login_casefold"],
                "private": bool(live["mailbox"]["private"]),
                "managed": True,
            },
            "phase": "binding",
            "worker_delete_issued": False,
            "worker_deleted": False,
            "mailbox_delete_issued": False,
            "mailbox_deleted": False,
            "readback": self._readback_evidence(live),
        }
        _validate_binding_shape(binding)
        if load_binding(self.credentials) is not None:
            raise HardStop("binding_conflict", "a reset binding appeared during creation")
        _write_binding(self.credentials, binding)
        stored = load_binding(self.credentials)
        if _binding_immutable_fingerprint(stored or {}) != _binding_immutable_fingerprint(binding):
            raise HardStop(
                "binding_readback_mismatch", "stored reset binding does not read back identically"
            )
        self._record("binding", {"resumed": False, "phase": stored["phase"]})
        return {"resumed": False}

    @staticmethod
    def _readback_evidence(live: dict[str, Any]) -> dict[str, Any]:
        return {
            "observed_at": live.get("observed_at"),
            "owner_account_id": int(live.get("owner_account_id") or 0),
            "installation_id": int(live.get("installation_id") or 0),
            "worker_repo_id": int((live.get("worker") or {}).get("repo_id") or 0),
            "mailbox_repo_id": int((live.get("mailbox") or {}).get("repo_id") or 0),
        }

    def _verify_binding_matches_live(self, binding: dict[str, Any], live: dict[str, Any]) -> None:
        if int(live.get("owner_account_id") or 0) != int(binding.get("owner_account_id") or 0):
            raise HardStop("identity_drift", "GitHub owner account id changed since the binding was created")
        if str(live.get("owner_login_casefold")) != str(binding.get("owner_login_casefold")):
            raise HardStop("identity_drift", "GitHub owner login changed since the binding was created")
        if int(live.get("installation_id") or 0) != int(binding.get("installation_id") or 0):
            raise HardStop("installation_drift", "App installation id changed since the binding was created")
        for name in ("worker", "mailbox"):
            bound = dict(binding.get(name) or {})
            live_target = dict(live.get(name) or {})
            if not live_target.get("exists"):
                if binding.get(name + "_deleted") or binding.get(name + "_delete_issued"):
                    # Already deleted by this operation (confirmed or issued):
                    # the deletion phase re-proves the former id is 404.
                    continue
                raise HardStop(
                    name + "_missing_before_delete",
                    "bound " + name + " repository is already missing; state needs manual support",
                )
            for field in ("repo_id", "full_name", "owner_login_casefold", "private", "managed"):
                if live_target.get(field) != bound.get(field):
                    raise HardStop(
                        "repository_identity_drift",
                        "bound " + name + " repository " + field
                        + " changed since the binding was created",
                    )

    # -- phase 2: narrow temporary revocation -------------------------------

    def phase_revoke_temporary(self) -> dict[str, Any]:
        self._inject("revoke:before")
        binding = self._binding()
        revoke_pending = PHASE_ORDER.index(str(binding["phase"])) <= PHASE_ORDER.index("revoke_temporary")
        deletions_confirmed = bool(binding.get("worker_deleted") and binding.get("mailbox_deleted"))
        # Keep the pause and remote-disabled explicit for the whole operation.
        if not self.credentials.has_secret("remote_enabled"):
            self.credentials.save_secret("remote_enabled", "0")
        if not self._store().global_paused:
            self._store().set_global_paused(True)
        state = collect_zero_state(
            self.credentials,
            self._store(),
            self.github_app,
            live={
                "worker": {"exists": not binding.get("worker_deleted")},
                "mailbox": {"exists": not binding.get("mailbox_deleted")},
            },
            remote_checks=not deletions_confirmed,
        )
        if not state["ready"]:
            raise HardStop(
                "zero_state_nonzero", "reset zero gate failed: " + ",".join(state["failures"])
            )
        if revoke_pending:
            # Narrow existing primitive: the transient Worker job token.
            try:
                self.github_app.delete_job_token()
            except GitHubAppError as exc:
                raise self._map_github_error(exc) from exc
            self._inject("revoke:after_job_token")
            env_secrets = self._list_worker_secret_names()
            if "COURSELENS_JOB_TOKEN" in env_secrets:
                raise HardStop(
                    "job_token_readback_nonzero", "COURSELENS_JOB_TOKEN is still present after revocation"
                )
            if self.credentials.has_secret("github_remote_token"):
                raise HardStop("job_token_readback_nonzero", "local transient token copy still present")
        # Durable leases, deleted one by exact task id, then read back.
        store = self._store()
        for lease in list(store.list_remote_token_leases()):
            store.delete_remote_token_lease(str(lease.get("task_id") or ""))
        self._inject("revoke:after_leases")
        if store.list_remote_token_leases():
            raise HardStop("lease_readback_nonzero", "token leases remain after revocation")
        # Temporary task credential secrets, deleted by exact name, read back.
        for prefix in TEMPORARY_CREDENTIAL_PREFIXES:
            for name in list(self.credentials.list_secret_names(prefix=prefix)):
                self.credentials.delete_secret(name)
        for prefix in TEMPORARY_CREDENTIAL_PREFIXES:
            if list(self.credentials.list_secret_names(prefix=prefix)):
                raise HardStop("temporary_credential_readback_nonzero", prefix + "* remains")
        self._inject("revoke:after_credentials")
        self._binding()
        if revoke_pending:
            # The deletion authority and installation id must both survive
            # until the deletions are done; afterwards the binding carries
            # the immutable facts and the secrets are gone by design.
            if not self.credentials.has_secret("github_app_installation_id"):
                raise HardStop(
                    "installation_id_lost",
                    "installation id must be retained until both deletions confirm",
                )
            if not (
                self.credentials.has_secret("github_app_access_token")
                or self.credentials.has_secret("github_app_refresh_token")
            ):
                raise HardStop("oauth_authority_lost", "OAuth deletion authority must be retained")
            _advance_binding(self.credentials, phase="delete_worker")
        self._record("revoke_temporary", {"phase": self._binding()["phase"]})
        return {}

    def _list_worker_secret_names(self) -> list[str]:
        try:
            items = self.github_app.list_worker_secrets()
        except GitHubAppError as exc:
            if exc.code == "resource_missing":
                return []  # the Worker repository is already deleted (resume)
            raise
        return [str(item.get("name") or "") for item in list(items or [])]

    # -- phases 3/4: verified deletions -------------------------------------

    def _delete_verified_repository(self, name: str) -> dict[str, Any]:
        """Delete exactly the bound repository id; confirm the former id 404."""
        binding = self._binding()
        bound = dict(binding.get(name) or {})
        repo_id = int(bound.get("repo_id") or 0)
        full_name = str(bound.get("full_name") or "")
        issued_flag = name + "_delete_issued"
        deleted_flag = name + "_deleted"
        try:
            token = self._token()
            # DELETE is not safely replayable: once the request has been
            # issued, only prove the exact bound id is gone.  A 200 is an
            # unknown outcome (or a same-name recreation), never permission
            # to issue a second DELETE.
            if binding.get(issued_flag) and not binding.get(deleted_flag):
                confirm = self.github_app._api(
                    "GET", "/repos/" + full_name, token=token, expected=(200, 404)
                )
                if confirm.status_code == 200:
                    residual_id = int((confirm.json() or {}).get("id") or 0)
                    if residual_id != repo_id:
                        raise HardStop(
                            "repository_identity_drift",
                            "repository " + full_name + " was recreated with a different id",
                        )
                    raise HardStop(
                        name + "_delete_outcome_unknown",
                        "deletion was issued but the exact former repository id still exists",
                    )
                if confirm.status_code != 404:
                    raise HardStop(name + "_delete_confirm_failed", "unexpected confirmation response")
                return self._confirm_deleted_repository(name, binding)
            response = self.github_app._api(
                "GET", "/repos/" + full_name, token=token, expected=(200, 404)
            )
            if response.status_code == 200:
                value = response.json()
                live_id = int(value.get("id") or 0)
                owner_casefold = str((value.get("owner") or {}).get("login") or "").casefold()
                owner_account_id = int((value.get("owner") or {}).get("id") or 0)
                if (
                    live_id != repo_id
                    or owner_casefold != str(binding.get("owner_login_casefold"))
                    or owner_account_id != int(binding.get("owner_account_id") or 0)
                    or bool(value.get("private")) != bool(bound.get("private"))
                    or str(value.get("description") or "") != MANAGED_DESCRIPTION
                ):
                    raise HardStop(
                        "repository_identity_drift",
                        "repository " + full_name
                        + " no longer matches the bound id/owner/visibility; refusing to delete",
                    )
                self._inject("delete_" + name + ":before_delete")
                _advance_binding(self.credentials, flags={issued_flag: True})
                self.github_app._api(
                    "DELETE", "/repos/" + full_name, token=token, expected=(204,)
                )
            elif binding.get(deleted_flag):
                return {}
            else:
                raise HardStop(
                    name + "_missing_before_delete",
                    "bound " + name + " repository disappeared before its deletion",
                )
            confirm = self.github_app._api(
                "GET", "/repos/" + full_name, token=token, expected=(200, 404)
            )
            self._inject("delete_" + name + ":after_confirm")
            if confirm.status_code == 200:
                residual_id = int((confirm.json() or {}).get("id") or 0)
                if residual_id == repo_id:
                    raise HardStop(
                        name + "_delete_not_confirmed",
                        "the exact former repository id still exists after deletion",
                    )
                raise HardStop(
                    "repository_identity_drift",
                    "repository " + full_name + " was recreated with a different id",
                )
            elif confirm.status_code != 404:
                raise HardStop(name + "_delete_confirm_failed", "unexpected confirmation response")
        except GitHubAppError as exc:
            raise self._map_github_error(exc) from exc
        return self._confirm_deleted_repository(name, binding)

    def _confirm_deleted_repository(self, name: str, binding: dict[str, Any]) -> dict[str, Any]:
        """Persist the only successful deletion outcome: former numeric id is 404."""
        repo_id = int((binding.get(name) or {}).get("repo_id") or 0)
        readback = dict(binding.get("readback") or {})
        readback[name + "_confirmed_404_at"] = self._now()
        advanced = _advance_binding(
            self.credentials,
            flags={name + "_deleted": True},
            phase=PHASE_AFTER_DELETION[name],
            readback=readback,
        )
        self._record("delete_" + name, {
            "repo_id": repo_id,
            "confirmed_404": True,
            "phase": advanced["phase"],
        })
        return {"repo_id": repo_id}

    def phase_delete_worker(self) -> dict[str, Any]:
        if self._binding().get("worker_deleted"):
            return {}
        return self._delete_verified_repository("worker")

    def phase_delete_mailbox(self) -> dict[str, Any]:
        if self._binding().get("mailbox_deleted"):
            return {}
        return self._delete_verified_repository("mailbox")

    # -- phase 5: disconnect + atomic local cleanup --------------------------

    def phase_local_cleanup(self) -> dict[str, Any]:
        self._inject("cleanup:before_disconnect")
        binding = self._binding()
        cleanup_pending = (
            PHASE_ORDER.index(str(binding["phase"])) <= PHASE_ORDER.index("local_cleanup")
        )
        if cleanup_pending:
            application = self._application_provider()
            try:
                try:
                    application.disconnect_github()
                except GitHubAppError as exc:
                    raise self._map_github_error(exc) from exc
                except RuntimeError as exc:
                    # The reused disconnect_github wraps every delete_job_token
                    # failure in RuntimeError.  A lease refusal is a nonzero
                    # hard stop; authorization loss once both deletions are
                    # confirmed is the recoverable reauth_required state.
                    if self._both_deletions_confirmed() and self._authorization_lost():
                        raise ReauthRequired(remaining=[]) from exc
                    raise HardStop("disconnect_refused", str(exc)) from exc
            finally:
                close = getattr(application, "close", None)
                if callable(close):
                    close()
            self._inject("cleanup:after_disconnect")
        snapshot = self.github_app.snapshot()
        if snapshot.get("authorized") or snapshot.get("bootstrapped"):
            raise HardStop(
                "disconnect_incomplete", "local GitHub bindings survived disconnect_github"
            )
        # Atomic credential wipe: only the reset binding survives this point.
        deletable = [
            name for name in self.credentials.list_secret_names() if name != BINDING_SECRET
        ]
        if deletable:
            self.credentials.update_secrets({}, deletes=tuple(deletable))
        for account in self.credentials.list_accounts():
            self.credentials.delete_account(str(account.get("student_id") or ""))
        if self.credentials.has_deepseek_key():
            self.credentials.delete_deepseek_key()
        remaining = [
            name for name in self.credentials.list_secret_names() if name != BINDING_SECRET
        ]
        if remaining or self.credentials.list_accounts() or self.credentials.has_deepseek_key():
            raise HardStop(
                "credential_wipe_incomplete", "local credential wipe did not read back clean"
            )
        self._inject("cleanup:after_credentials")
        # Atomic runtime-state clear: a fresh state.db replaces the old one.
        if self.task_store is not None:
            self.task_store.close()
            self.task_store = None
        state_path = self.output_dir / "state.db"
        temporary_path = self.output_dir / "state.db.reset-tmp"
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(temporary_path) + suffix).unlink(missing_ok=True)
            Path(str(state_path) + suffix).unlink(missing_ok=True)
        temporary_path.unlink(missing_ok=True)
        fresh = self._fresh_task_store_factory(temporary_path)
        fresh.close()
        os.replace(temporary_path, state_path)
        settled = self._fresh_task_store_factory(state_path)
        try:
            settled.set_global_paused(True)
            settled.append_remote_event(
                "developer-first-install-reset", self.operation_id,
                {
                    "operation_id": self.operation_id,
                    "state": "local_cleanup_complete",
                    "worker_repo_id": int((self._binding().get("worker") or {}).get("repo_id") or 0),
                    "mailbox_repo_id": int((self._binding().get("mailbox") or {}).get("repo_id") or 0),
                },
            )
        finally:
            settled.close()
        self._inject("cleanup:after_state_clear")
        isolated = self._isolate_local_data()
        _advance_binding(self.credentials, phase="verify_first_install")
        self._record("local_cleanup", {"isolated_entries": isolated, "phase": "verify_first_install"})
        return {"isolated_entries": isolated}

    def _isolate_local_data(self) -> list[str]:
        """Move generated local data aside (never delete) under the data root."""
        keep_exact = {"credentials.json", "state.db", ISOLATION_DIR_NAME, "state.db.reset-tmp"}
        isolation_root = self.output_dir / ISOLATION_DIR_NAME / self.operation_id
        moved: list[str] = []
        for entry in sorted(self.output_dir.iterdir()):
            if entry.name in keep_exact or entry.name.startswith(LOCK_PREFIX):
                continue
            if entry.name.startswith("state.db-"):
                continue  # sidecars of the rebuilt store; none should exist
            isolation_root.mkdir(parents=True, exist_ok=True)
            shutil.move(str(entry), str(isolation_root / entry.name))
            moved.append(entry.name)
        return moved

    # -- phase 6: isolated first-install verification ------------------------

    def phase_verify_first_install(self) -> dict[str, Any]:
        self._inject("verify:before")
        failures: list[str] = []
        # 1. Settled store, before any product start: pause on, zero work.
        settled = TaskStore(self.output_dir / "state.db", read_only=True)
        try:
            if not settled.global_paused:
                failures.append("settled_store_not_paused")
            if settled.list_tasks():
                failures.append("settled_store_has_tasks")
            if settled.list_remote_token_leases():
                failures.append("settled_store_has_leases")
            if settled.list_automation_imports():
                failures.append("settled_store_has_imports")
        finally:
            settled.close()
        # 2. Credentials: the reset binding must be the only survivor.
        survivors = self.credentials.list_secret_names()
        if set(survivors) - {BINDING_SECRET}:
            failures.append("credential_residue_present")
        binding = load_binding(self.credentials)
        if binding is None:
            failures.append("binding_lost_before_verification")
        else:
            if set(binding) != set(BINDING_ALLOWED_KEYS):
                failures.append("binding_has_unknown_fields")
            if binding.get("phase") != "verify_first_install":
                failures.append("binding_phase_not_at_verification")
            if not (binding.get("worker_deleted") and binding.get("mailbox_deleted")):
                failures.append("binding_deletions_not_confirmed")
        post_startup_paused: bool | None = None
        # 3. Restart the real application in isolation.
        if not failures:
            self._inject("verify:before_restart")
            application = self._application_provider()
            try:
                snapshot = dict(application.github_app.snapshot())
                settings = RemoteSettings.load(application.credentials)
                if snapshot.get("authorized") or snapshot.get("installed") or snapshot.get("bootstrapped"):
                    failures.append("restart_still_bound")
                if settings.enabled:
                    failures.append("restart_remote_enabled")
                if application._remote_configured():
                    failures.append("restart_remote_configured")
                post_startup_paused = bool(application.task_store.global_paused)
            finally:
                close = getattr(application, "close", None)
                if callable(close):
                    close()
        if failures:
            raise HardStop(
                "first_install_verification_failed",
                "first-install verification failed: " + ",".join(sorted(failures)),
            )
        # The product's own startup normalizes the pause on an empty store;
        # remote stays disabled either way, which is the load-bearing fact.
        self._record("verify_first_install", {
            "settled_global_paused": True,
            "post_startup_global_paused": post_startup_paused,
            "remote_disabled": True,
            "onboarding_unauthorized": True,
        })
        return {"post_startup_global_paused": post_startup_paused}

    # -- terminal -------------------------------------------------------------

    def phase_terminal(self) -> dict[str, Any]:
        binding = load_binding(self.credentials)
        if binding is not None:
            audit = {
                "operation_id": self.operation_id,
                "state": "complete",
                "worker_repo_id": int(binding.get("worker", {}).get("repo_id") or 0),
                "mailbox_repo_id": int(binding.get("mailbox", {}).get("repo_id") or 0),
                "completed_at": self._now(),
            }
            self.credentials.delete_secret(BINDING_SECRET)
            if self.credentials.has_secret(BINDING_SECRET):
                raise HardStop("binding_clear_failed", "the reset binding could not be cleared")
            settled = self._fresh_task_store_factory(self.output_dir / "state.db")
            try:
                settled.append_remote_event(
                    "developer-first-install-reset", self.operation_id, audit
                )
            finally:
                settled.close()
        self._record("terminal", {"binding_cleared": True})
        return {}

    # -- orchestration -----------------------------------------------------

    def run(self) -> dict[str, Any]:
        try:
            self.phase_binding()
            self.phase_revoke_temporary()
            self.phase_delete_worker()
            self._inject("boundary:worker_confirmed")
            self.phase_delete_mailbox()
            self._inject("boundary:mailbox_confirmed")
            self.phase_local_cleanup()
            self.phase_verify_first_install()
            self.phase_terminal()
        except HardStop as exc:
            # Authorization lost at or after either deletion is recoverable:
            # return reauth_required with the preserved operation and the
            # remaining exact ids instead of a terminal failure.
            if exc.code in AUTH_LOST_CODES and self._any_deletion_started():
                raise ReauthRequired(remaining=self._remaining_targets()) from exc
            raise
        return {
            "operation_id": self.operation_id,
            "status": "completed",
            "phases": list(self.phases_run),
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _plan_from_readback(live: dict[str, Any]) -> list[dict[str, Any]]:
    plan = []
    for role in ("worker", "mailbox"):
        target = dict(live.get(role) or {})
        if live.get("bootstrapped") and target.get("exists"):
            plan.append({
                "role": role,
                "repo_id": int(target.get("repo_id") or 0),
                "full_name": str(target.get("full_name") or ""),
                "private": bool(target.get("private")),
                "managed": bool(target.get("managed")),
            })
    return plan


def build_report(**fields: Any) -> dict[str, Any]:
    """Whitelist-only report assembly: secrets cannot leak by construction."""
    allowed = {
        "operation_id", "mode", "status", "blocked_reason", "phases", "plan",
        "zero_state", "gates", "readback", "remaining", "post_startup_global_paused",
        "already_first_install", "risk_pass_ref",
    }
    return {key: value for key, value in fields.items() if key in allowed}


def _open_existing_store(output_dir: Path, *, read_only: bool = False) -> TaskStore | None:
    """Open the local store only when it already exists (dry-run stays inert)."""
    if not (output_dir / "state.db").is_file():
        return None
    return TaskStore(output_dir / "state.db", read_only=read_only)


def run_reset(
    *,
    output_dir: Path,
    execute: bool,
    operation_id: str,
    risk_pass_ref: str,
    go_line_provider: Callable[[], str],
    application_provider: Callable[[], Any],
    github_app: Any,
    failure_injections: dict[str, Callable[[], None]] | None = None,
) -> dict[str, Any]:
    """Shared entry for dry-run and execute; returns the whitelisted report."""
    output_dir = ensure_inside_project(Path(output_dir))
    credentials = CredentialStore(output_dir / "credentials.json")
    mode = "execute" if execute else "dry-run"
    gates: list[dict[str, Any]] = []

    def task_store_factory() -> TaskStore:
        return TaskStore(output_dir / "state.db")

    if execute:
        if not operation_id or not OPERATION_ID_RE.fullmatch(operation_id):
            raise HardStop("operation_id_invalid", "--execute needs a valid --operation-id")
        if not risk_pass_ref.strip():
            raise HardStop(
                "risk_pass_missing",
                "--execute needs a fresh risk_auditor PASS reference (--risk-pass-ref)",
            )
        gates.append({"gate": "operation_id", "result": "pass"})
        go_line = str(go_line_provider() or "").strip()
        if go_line != GO_LINE_PREFIX + operation_id:
            raise HardStop(
                "user_go_missing",
                "stdin must contain exactly 'GO <operation_id>' naming this operation",
            )
        gates.append({"gate": "user_go", "result": "pass"})
        gates.append({"gate": "risk_pass_fresh", "result": "pass", "reference": risk_pass_ref.strip()})
    else:
        gates.append({"gate": "dry_run_default", "result": "pass"})
        gates.append({"gate": "user_go", "result": "not_required_in_dry_run"})
        gates.append({"gate": "risk_pass_fresh", "result": "not_required_in_dry_run"})

    # Exclusive write ownership: any existing lock file blocks an execute.
    existing_locks = sorted(p.name for p in output_dir.glob(LOCK_PREFIX + "*" + LOCK_SUFFIX))
    if execute and existing_locks:
        raise HardStop(
            "writer_conflict", "another developer-reset writer holds " + ",".join(existing_locks)
        )
    # A live service leaves SQLite sidecars; refuse rather than fight a writer.
    try:
        preflight_store = TaskStore(output_dir / "state.db", read_only=True)
    except FileNotFoundError:
        preflight_store = None  # no local store yet: nothing can be running
    except RuntimeError as exc:
        raise HardStop(
            "writer_conflict", "the local service appears to be writing state: " + str(exc)
        ) from exc
    else:
        preflight_store.close()

    flag_state = _require_remote_disabled(credentials)
    binding = load_binding(credentials)
    if binding is not None and str(binding.get("operation_id")) != operation_id:
        raise HardStop(
            "operation_id_mismatch",
            "a reset binding for operation id " + str(binding.get("operation_id"))
            + " is already stored; name it with --operation-id to resume",
        )
    first_install = not (
        credentials.has_secret("github_worker_repo") or credentials.has_secret("github_mailbox_repo")
    )
    deletions_confirmed = bool(
        binding is not None and binding.get("worker_deleted") and binding.get("mailbox_deleted")
    )
    if binding is not None:
        # A stored binding always wins over the first-install short-circuit:
        # after disconnect_github cleared the repo-name secrets, a rerun must
        # resume forward from the binding, never report a false first-install.
        if deletions_confirmed:
            live = {
                "bootstrapped": True,
                "owner_login_casefold": str(binding.get("owner_login_casefold")),
                "owner_account_id": int(binding.get("owner_account_id") or 0),
                "installation_id": int(binding.get("installation_id") or 0),
                "worker": {"exists": False},
                "mailbox": {"exists": False},
                "observed_at": time.time(),
            }
        else:
            try:
                live = read_live_targets(
                    github_app, credentials, require_bootstrapped=True, read_only=not execute
                )
            except HardStop as exc:
                if exc.code in AUTH_LOST_CODES and any(
                    bool(binding.get(flag)) for flag in BINDING_FLAG_NAMES
                ):
                    remaining = []
                    for role in ("worker", "mailbox"):
                        if not binding.get(role + "_deleted"):
                            target = dict(binding.get(role) or {})
                            remaining.append({
                                "role": role,
                                "repo_id": int(target.get("repo_id") or 0),
                                "full_name": str(target.get("full_name") or ""),
                                "private": bool(target.get("private")),
                            })
                    raise ReauthRequired(remaining=remaining) from exc
                raise
    elif first_install:
        live: dict[str, Any] = {"bootstrapped": False}
    else:
        try:
            live = read_live_targets(
                github_app, credentials, require_bootstrapped=True, read_only=not execute
            )
        except HardStop as exc:
            raise  # no binding: no deletion could have started; stay blocked
    if not live.get("bootstrapped"):
        status = "blocked" if execute else "already_first_install"
        return build_report(
            operation_id=operation_id, mode=mode, status=status,
            blocked_reason="nothing_to_reset" if execute else "",
            gates=gates, already_first_install=True,
        )
    zero_store = _open_existing_store(output_dir, read_only=not execute)
    try:
        zero = collect_zero_state(
            credentials, zero_store, github_app, live=live,
            remote_checks=not deletions_confirmed,
            read_only=not execute,
        )
    finally:
        if zero_store is not None:
            zero_store.close()
    if not zero["ready"]:
        raise HardStop(
            "zero_state_nonzero",
            "reset zero gate failed: " + ",".join(str(f) for f in zero["failures"]),
        )
    gates.append({"gate": "preflight_zero", "result": "pass", "observations": zero["observations"]})
    plan = _plan_from_readback(live)

    if not execute:
        return build_report(
            operation_id=operation_id or "", mode=mode, status="dry_run_complete",
            phases=[], plan=plan, zero_state=zero["observations"], gates=gates,
            readback={
                "owner_account_id": int(live.get("owner_account_id") or 0),
                "installation_id": int(live.get("installation_id") or 0),
            },
        )

    engine = DeveloperResetEngine(
        output_dir=output_dir,
        credentials=credentials,
        task_store_factory=task_store_factory,
        github_app=github_app,
        application_provider=application_provider,
        operation_id=operation_id,
    )
    engine.failure_injections = dict(failure_injections or {})
    with OwnershipLock(output_dir, operation_id):
        try:
            result = engine.run()
        except ReauthRequired as exc:
            return build_report(
                operation_id=operation_id, mode=mode, status="reauth_required",
                blocked_reason=str(exc), remaining=exc.remaining, gates=gates,
                phases=list(engine.phases_run),
            )
    return build_report(
        operation_id=operation_id, mode=mode, status=str(result.get("status")),
        phases=list(result.get("phases") or []), gates=gates,
        readback={
            "owner_account_id": int(live.get("owner_account_id") or 0),
            "installation_id": int(live.get("installation_id") or 0),
        },
    )


def main(
    argv: list[str] | None = None,
    *,
    stdin: Any = None,
    application_provider: Callable[[], Any] | None = None,
    github_app: Any = None,
) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Developer-only first-install reset. Default is dry-run; a real run "
            "needs --execute, an operation id, a fresh risk PASS reference and a "
            "stdin GO line."
        )
    )
    parser.add_argument("--execute", action="store_true", help="perform the reset (default is dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="explicit dry-run (the default)")
    parser.add_argument("--operation-id", default="", help="unique operation id for this reset")
    parser.add_argument(
        "--risk-pass-ref", default="",
        help="reference of the fresh risk_auditor PASS authorizing this exact operation",
    )
    parser.add_argument(
        "--data-dir", default=str(DEFAULT_DATA_DIR),
        help="client data directory (defaults to the managed runtime data dir)",
    )
    args = parser.parse_args(argv)
    if args.execute and args.dry_run:
        parser.error("--execute and --dry-run are mutually exclusive")
    try:
        output_dir = ensure_inside_project(Path(args.data_dir))
    except ValueError as exc:
        print(json.dumps(build_report(
            mode="execute" if args.execute else "dry-run",
            status="blocked", blocked_reason="data_dir_outside_managed_root: " + str(exc),
            operation_id=str(args.operation_id or ""),
        ), sort_keys=True))
        return EXIT_BLOCKED
    credentials = CredentialStore(output_dir / "credentials.json")
    app_client = github_app if github_app is not None else GitHubAppClient(credentials)
    provider = application_provider or _default_application_provider(output_dir)

    def go_line_provider() -> str:
        reader = stdin if stdin is not None else sys.stdin
        return str(reader.readline() or "")

    try:
        report = run_reset(
            output_dir=output_dir,
            execute=bool(args.execute),
            operation_id=str(args.operation_id or ""),
            risk_pass_ref=str(args.risk_pass_ref or ""),
            go_line_provider=go_line_provider,
            application_provider=provider,
            github_app=app_client,
        )
    except HardStop as exc:
        exit_code = (
            EXIT_VERIFY_FAILED
            if exc.code == "first_install_verification_failed"
            else EXIT_BLOCKED
        )
        print(json.dumps(build_report(
            mode="execute" if args.execute else "dry-run",
            status="blocked", blocked_reason=exc.blocked_reason,
            operation_id=str(args.operation_id or ""),
        ), sort_keys=True))
        return exit_code
    except ReauthRequired as exc:
        print(json.dumps(build_report(
            mode="execute" if args.execute else "dry-run",
            status="reauth_required", blocked_reason=str(exc), remaining=exc.remaining,
            operation_id=str(args.operation_id or ""),
        ), sort_keys=True))
        return EXIT_REAUTH_REQUIRED
    except GitHubAppError as exc:
        # Last-resort containment: a GitHub failure outside the mapped
        # paths must still exit as a redacted blocked report, never a raw
        # traceback.
        print(json.dumps(build_report(
            mode="execute" if args.execute else "dry-run",
            status="blocked", blocked_reason="github_error: " + exc.code,
            operation_id=str(args.operation_id or ""),
        ), sort_keys=True))
        return EXIT_BLOCKED
    print(json.dumps(report, sort_keys=True))
    return EXIT_BLOCKED if str(report.get("status")) == "blocked" else EXIT_OK


def _default_application_provider(output_dir: Path) -> Callable[[], Any]:
    def provider() -> Any:
        from src.application import CourseLensApplication

        return CourseLensApplication(output_dir)

    return provider


if __name__ == "__main__":
    raise SystemExit(main())
