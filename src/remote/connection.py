"""Authoritative, evidence-backed GitHub Actions connection state."""

from __future__ import annotations

import copy
import hashlib
import re
import threading
import time
from typing import Any, Callable

from credentials import CredentialStore
from src.runtime.task_store import REMOTE_RUN_RECOVERABLE_STATES, TaskStore

from .github_app import GitHubAppClient, GitHubAppError
from .protocol import PROTOCOL_VERSION
from .worker_migration import (
    _active_task_run_count,
    _known_mailbox_issues,
    _managed_mailbox_inventory,
    _task_artifact_count,
)


COMPONENT_ORDER = (
    "local_backend",
    "app_configuration",
    "github_api",
    "authorization",
    "installation",
    "worker_repository",
    "mailbox_repository",
    "mailbox_history",
    "worker_integrity",
    "workflow",
    "actions",
    "environment",
    "job_token",
    "channel_test",
    "current_runner",
)
REQUIRED_FOR_DISPATCH = (
    "local_backend",
    "app_configuration",
    "github_api",
    "authorization",
    "installation",
    "worker_repository",
    "mailbox_repository",
    "worker_integrity",
    "workflow",
    "actions",
    "environment",
    "channel_test",
)
# REALRUN-1 N1/N2（2026-10-08 真测钉死）：overall 聚合的阻塞件状态优先级
# （小者胜）。此前 blocking 按 COMPONENT_ORDER 取第一个未就绪组件——
# local_backend 排最前且探针记录 TTL 仅 20s，空闲节拍内几乎必然证据过期
# （unknown/stale_evidence），把快照期新鲜推导的可行动结论（如
# authorization=action_required）整个压住：overall 恒 unknown/stale_evidence，
# 首屏连接点与引导行 40 分钟不收敛。其余未知态并列最低档，同级按组件顺序。
_BLOCKING_STATE_PRIORITY = {"action_required": 0, "degraded": 1, "offline": 2}
TERMINAL_GITHUB_STATES = {"completed"}
OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
# Connection polling only handles ordinary recoverable runs, never process-canary reruns.
ACTIVE_REMOTE_STATES = REMOTE_RUN_RECOVERABLE_STATES


class RemoteConnectionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code or "remote_connection_error")


def verification_fingerprint(*, tree: str, box_public_key: str, signing_public_key: str) -> str:
    value = "\n".join((str(tree), str(PROTOCOL_VERSION), str(box_public_key), str(signing_public_key)))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class RemoteConnectionSupervisor:
    """Reconcile local configuration with live GitHub evidence.

    Browser requests only read this service.  They never infer readiness from
    credential presence or from a historical workflow result.
    """

    def __init__(
        self,
        task_store: TaskStore,
        credentials: CredentialStore,
        github_provider: Callable[[], GitHubAppClient],
        *,
        active_work: Callable[[], bool] | None = None,
        idle_probe_seconds: float = 60.0,
        active_probe_seconds: float = 5.0,
    ):
        self.task_store = task_store
        self.credentials = credentials
        self.github_provider = github_provider
        self.active_work = active_work or (lambda: False)
        self.idle_probe_seconds = max(15.0, float(idle_probe_seconds))
        self.active_probe_seconds = max(2.0, float(active_probe_seconds))
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._probing = False
        self._last_probe_at = 0.0
        self._next_probe_at = 0.0
        self._backoff_until = 0.0
        self._probe_seq = 0
        self._last_good_snapshot: dict[str, Any] | None = None
        self._record(
            "local_backend", state="ready", source="local", code="local_backend_ready",
            actions=[], evidence={}, ttl=90.0,
        )
        self._record_local_configuration()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="remote-connection-supervisor", daemon=True
            )
            self._thread.start()
        self.request_probe()

    def stop(self, timeout: float = 3.0) -> bool:
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
        return not bool(thread and thread.is_alive())

    def request_probe(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            wait_for = max(0.0, self._next_probe_at - now)
            self._wake.wait(wait_for if wait_for else 0.1)
            self._wake.clear()
            if self._stop.is_set():
                return
            now = time.time()
            if now < self._backoff_until:
                self._next_probe_at = self._backoff_until
                continue
            if now + 0.01 < self._next_probe_at:
                continue
            try:
                self.probe()
            except Exception:
                # probe() records a closed-set failure before returning.  The
                # monitor itself must stay alive for later recovery.
                pass
            interval = self.active_probe_seconds if self._has_active_remote_work() else self.idle_probe_seconds
            self._next_probe_at = time.time() + interval

    def _has_active_remote_work(self) -> bool:
        try:
            if self.active_work():
                return True
        except Exception:
            return True
        return any(
            str(item.get("remote_state") or "") in ACTIVE_REMOTE_STATES
            for item in self.task_store.list_remote_runs(limit=50)
        )

    def _record_local_configuration(self) -> None:
        client = self.github_provider()
        snapshot = client.snapshot()
        configured = bool(snapshot.get("app_configured"))
        self._record(
            "app_configuration",
            state="ready" if configured else "action_required",
            source="local",
            code="app_configured" if configured else "app_not_configured",
            actions=[] if configured else ["diagnose"],
            evidence={},
            ttl=90.0,
        )
        if not snapshot.get("authorized"):
            self._record(
                "authorization", state="action_required", source="local",
                code="authorization_missing", actions=["start-authorization"],
                evidence={"refresh_available": bool(snapshot.get("refresh_available"))}, ttl=90.0,
            )

    def probe(self) -> dict[str, Any]:
        with self._lock:
            if self._probing:
                return self.snapshot()
            self._probing = True
        now = time.time()
        try:
            self._record(
                "local_backend", state="ready", source="local", code="local_backend_ready",
                actions=[], evidence={}, ttl=90.0,
            )
            self._record_local_configuration()
            client = self.github_provider()
            local = client.snapshot()
            if not local.get("app_configured"):
                self._unknown_downstream("app_not_configured", "diagnose")
                return self.snapshot()
            if not local.get("authorized"):
                self._unknown_downstream("authorization_missing", "start-authorization")
                return self.snapshot()
            try:
                live = client.inspect_managed_resources()
            except GitHubAppError as exc:
                self._record_probe_error(exc)
                return self.snapshot()

            rate = dict(live.get("rate_limit") or {})
            remaining = int(rate.get("remaining") if rate.get("remaining") is not None else -1)
            reset_at = float(rate.get("reset_at") or 0)
            if remaining == 0:
                self._backoff_until = max(time.time() + 30.0, reset_at)
            self._record(
                "github_api", state="degraded" if 0 <= remaining < 100 else "ready",
                source="github_api", code="rate_limit_low" if 0 <= remaining < 100 else "github_api_ready",
                actions=["diagnose"] if 0 <= remaining < 100 else [],
                evidence={"rate_limit_remaining": remaining, "rate_limit_reset_at": reset_at}, ttl=90.0,
            )
            identity = dict(live.get("identity") or {})
            self._record(
                "authorization", state="ready", source="github_api", code="authorization_valid",
                actions=[], evidence={"login": str(identity.get("login") or "")}, ttl=90.0,
            )
            installation = dict(live.get("installation") or {})
            installed = bool(installation.get("installed"))
            scope_exact = bool(installation.get("repository_selection_exact"))
            selection_error = str(installation.get("repository_selection_error") or "")
            # T6 深分类：仓库级 403 必须按「安装是否在」分流——未安装时的仓库
            # 403 是安装缺失的必然后果，不是独立的范围/权限故障。
            repos_denied = str(live.get("repos_access_denied") or "")
            if installed and selection_error:
                # A transient inventory failure leaves the selection unknown:
                # fail closed without fabricating a missing-repository list.
                self._record(
                    "installation", state="unknown", source="github_api",
                    code="installation_selection_unknown",
                    actions=["diagnose"],
                    evidence={"reason": selection_error}, ttl=90.0,
                )
            elif installed and not scope_exact:
                evidence: dict[str, Any] = {
                    "missing": list(installation.get("missing_installation_repositories") or []),
                    "unexpected": list(installation.get("unexpected_installation_repositories") or []),
                }
                installation_id = int(installation.get("installation_id") or 0)
                if installation_id > 0:
                    evidence["settings_url"] = (
                        f"https://github.com/settings/installations/{installation_id}"
                    )
                self._record(
                    "installation", state="action_required", source="github_api",
                    code="installation_scope_not_exact",
                    actions=["restrict-github-app-installation"], evidence=evidence, ttl=90.0,
                )
            else:
                # First-run state: the App is not installed.  When the two
                # managed repositories already exist, the evidence carries the
                # trusted backend-composed installation URL with both
                # repository ids preselected, and the resume action is a
                # bootstrap (never a forced re-authorization).
                evidence: dict[str, Any] = {}
                if not installed:
                    setup_url = str(live.get("installation_setup_url") or "")
                    if setup_url:
                        evidence["installation_setup_url"] = setup_url
                    if repos_denied:
                        # 未安装 + 仓库被拒：闭集端点类挂进安装证据，由前端安装
                        # 指引统一解释「安装前读不了仓库属正常」；五个下游组件
                        # 不再渲染为未知/拒绝行（见下方分流）。
                        evidence["repos_denied"] = repos_denied
                self._record(
                    "installation", state="ready" if installed else "action_required",
                    source="github_api",
                    code="installation_present" if installed else "installation_missing",
                    actions=[] if installed else ["bootstrap"],
                    evidence=evidence, ttl=90.0,
                )
            owner = str(identity.get("login") or "").casefold()
            worker = dict(live.get("worker") or {})
            worker_pre_install = bool(worker.get("pre_install"))
            worker_valid = bool(
                worker.get("exists") and not worker.get("private") and not worker.get("archived")
                and not worker.get("disabled") and worker.get("managed")
                and str(worker.get("owner") or "").casefold() == owner
                and str(worker.get("default_branch") or "") == "main"
            )
            worker_code = "worker_repository_ready" if worker_valid else (
                "worker_repository_awaiting_installation" if worker_pre_install
                else "worker_repository_missing" if not worker.get("exists") else "worker_repository_invalid"
            )
            # Cross-account stale bindings surface as a closed-set evidence
            # marker on the missing-repository guidance so the client can
            # explain why an existing binding was ignored.
            worker_evidence: dict[str, Any] = (
                {"binding_owner_mismatch": True} if worker.get("binding_owner_mismatch") else {}
            )
            self._record(
                "worker_repository", state="ready" if worker_valid else "action_required",
                source="github_api", code=worker_code,
                actions=[] if worker_valid else ["bootstrap"], evidence=worker_evidence, ttl=90.0,
            )
            mailbox = dict(live.get("mailbox") or {})
            mailbox_pre_install = bool(mailbox.get("pre_install"))
            mailbox_valid = bool(
                mailbox.get("exists") and mailbox.get("private") and mailbox.get("has_issues")
                and not mailbox.get("archived") and not mailbox.get("disabled") and mailbox.get("managed")
                and str(mailbox.get("owner") or "").casefold() == owner
            )
            mailbox_code = "mailbox_repository_ready" if mailbox_valid else (
                "mailbox_repository_awaiting_installation" if mailbox_pre_install
                else "mailbox_repository_missing" if not mailbox.get("exists") else "mailbox_repository_invalid"
            )
            mailbox_evidence: dict[str, Any] = (
                {"binding_owner_mismatch": True} if mailbox.get("binding_owner_mismatch") else {}
            )
            self._record(
                "mailbox_repository", state="ready" if mailbox_valid else "action_required",
                source="github_api", code=mailbox_code,
                actions=[] if mailbox_valid else ["bootstrap"], evidence=mailbox_evidence, ttl=90.0,
            )
            expected_tree = str(live.get("expected_tree") or "")
            if repos_denied and installed:
                # A repo-level 403 with the App installed is a scope/permission
                # refusal: record the deeper sections unknown instead of
                # deriving misleading remediation from empty evidence.
                # (未安装时的同型 403 走上方安装证据分流，不再渲染为独立故障。)
                self._record_repo_denial_unknown(repos_denied)
            elif not repos_denied:
                tree = str(live.get("worker_tree") or "")
                commit = str(live.get("worker_commit") or "")
                trusted = bool(tree and expected_tree and tree == expected_tree)
                self._record(
                    "worker_integrity", state="ready" if trusted else "action_required",
                    source="github_api", code="worker_tree_trusted" if trusted else "worker_tree_drifted",
                    actions=[] if trusted else ["repair-worker"],
                    evidence={"commit": commit, "tree": tree}, ttl=90.0,
                )
                workflows = dict(live.get("workflows") or {})
                workflow_ready = all(
                    bool((workflows.get(name) or {}).get("exists"))
                    and str((workflows.get(name) or {}).get("state") or "") == "active"
                    for name in ("process.yml", "echo.yml")
                )
                self._record(
                    "workflow", state="ready" if workflow_ready else "action_required",
                    source="github_api", code="workflows_active" if workflow_ready else "workflow_missing_or_disabled",
                    actions=[] if workflow_ready else ["repair-worker"], evidence={}, ttl=90.0,
                )
                actions_ready = bool(live.get("actions_enabled"))
                self._record(
                    "actions", state="ready" if actions_ready else "action_required",
                    source="github_api", code="actions_enabled" if actions_ready else "actions_disabled",
                    actions=[] if actions_ready else ["diagnose"], evidence={}, ttl=90.0,
                )
                secret_names = set(str(item) for item in live.get("secret_names") or [])
                variables = dict(live.get("variables") or {})
                missing_secrets = sorted(
                    {"WORKER_INPUT_PRIVATE_KEY", "WORKER_SIGNING_PRIVATE_KEY"} - secret_names
                )
                environment_ready = bool(
                    live.get("environment_exists")
                    and not missing_secrets
                    and str(variables.get("COURSELENS_MAILBOX_REPO") or "")
                    == str(local.get("mailbox_repo") or "")
                )
                repair_environment_action = (
                    "rotate-worker-keys"
                    if missing_secrets
                    and self.credentials.has_secret("worker_box_public_key")
                    and self.credentials.has_secret("worker_signing_public_key")
                    else "bootstrap"
                )
                self._record(
                    "environment", state="ready" if environment_ready else "action_required",
                    source="github_api", code="environment_ready" if environment_ready else "environment_incomplete",
                    actions=[] if environment_ready else [repair_environment_action],
                    evidence={
                        "job_token_present": "COURSELENS_JOB_TOKEN" in secret_names,
                        "missing_secrets": missing_secrets,
                    }, ttl=90.0,
                )
                leases = self.task_store.list_remote_token_leases()
                cleanup_pending = bool(local.get("job_token_cleanup_pending"))
                job_token_present = "COURSELENS_JOB_TOKEN" in secret_names
                if cleanup_pending:
                    token_state, token_code, token_actions = "action_required", "job_token_cleanup_pending", ["retry-cleanup"]
                elif leases and not job_token_present:
                    token_state, token_code, token_actions = "action_required", "job_token_missing_for_active_run", ["retry-cleanup"]
                else:
                    token_state, token_code, token_actions = "ready", (
                        "job_token_active" if job_token_present else "job_token_not_issued"
                    ), []
                self._record(
                    "job_token", state=token_state, source="github_api", code=token_code,
                    actions=token_actions,
                    evidence={"active_leases": len(leases), "present": job_token_present}, ttl=90.0,
                )
            if mailbox_valid and bool(worker.get("exists")) and str(local.get("worker_repo") or ""):
                self._record_mailbox_history(client, live, local)
            else:
                self._record(
                    "mailbox_history", state="unknown", source="github_api",
                    code="mailbox_repository_unavailable", actions=["bootstrap"],
                    evidence={}, ttl=90.0,
                )
            self._record_channel_test(expected_tree)
            self._reconcile_runs(client)
            self._last_probe_at = now
            return self.snapshot()
        finally:
            with self._lock:
                self._probing = False
                self._probe_seq += 1

    def _record_mailbox_history(
        self, client: GitHubAppClient, live: dict[str, Any], local: dict[str, Any]
    ) -> None:
        """Classify Mailbox history into a closed set, fail-closed on unknown.

        Reconcile is only advertised for closed unconsumed history at global
        zero activity; live payload, drift, missing issues, and unknown
        evidence stay non-repairable here.  Evidence carries counts only —
        never issue numbers, bodies, or comment content.
        """
        mailbox_repo = str(local.get("mailbox_repo") or "")
        worker_repo = str(local.get("worker_repo") or "")
        try:
            token = client.access_token(minimum_lifetime_seconds=900, no_refresh=True)
            inventory = _managed_mailbox_inventory(
                client, mailbox_repo, token,
                known_issues=_known_mailbox_issues(self.task_store),
            )
            activity_conflict = bool(
                self.active_work()
                or _active_task_run_count(client, worker_repo, token) > 0
                or _task_artifact_count(client, worker_repo, token) > 0
                or "COURSELENS_JOB_TOKEN" in set(live.get("secret_names") or [])
                or list(self.credentials.list_secret_names(prefix="remote_result_private:"))
                or list(self.task_store.list_remote_token_leases())
                or bool(local.get("job_token_cleanup_pending"))
                or int(self.task_store.migration_cleanup_pending_count()) > 0
            )
        except Exception:
            # Rate limits, timeouts, partial pagination, or an unreadable
            # local store must never read as a clean mailbox.
            self._record(
                "mailbox_history", state="unknown", source="github_api",
                code="github_state_unknown", actions=["diagnose"],
                evidence={}, ttl=30.0,
            )
            return
        evidence = {
            "open_unconsumed": int(inventory["open_unconsumed_count"]),
            "closed_unconsumed": int(inventory["closed_unconsumed_count"]),
            "consumed_open": int(inventory["consumed_open_count"]),
            "metadata_drift": int(inventory["metadata_drift_count"]),
            "history_missing": int(inventory["history_issue_missing_count"]),
        }
        if int(inventory["active_issue_missing_count"]) > 0:
            self._record(
                "mailbox_history", state="action_required", source="github_api",
                code="mailbox_issue_missing_active", actions=[],
                evidence=evidence, ttl=90.0,
            )
        elif evidence["metadata_drift"] > 0:
            self._record(
                "mailbox_history", state="action_required", source="github_api",
                code="mailbox_metadata_drift", actions=[], evidence=evidence, ttl=90.0,
            )
        elif evidence["open_unconsumed"] > 0:
            self._record(
                "mailbox_history", state="action_required", source="github_api",
                code="mailbox_open_unconsumed", actions=[], evidence=evidence, ttl=90.0,
            )
        elif evidence["closed_unconsumed"] > 0:
            if activity_conflict:
                self._record(
                    "mailbox_history", state="action_required", source="github_api",
                    code="mailbox_closed_active_conflict", actions=[],
                    evidence=evidence, ttl=90.0,
                )
            else:
                self._record(
                    "mailbox_history", state="action_required", source="github_api",
                    code="mailbox_closed_unconsumed",
                    actions=["reconcile-mailbox-history"], evidence=evidence, ttl=90.0,
                )
        elif evidence["consumed_open"] > 0:
            self._record(
                "mailbox_history", state="action_required", source="github_api",
                code="mailbox_consumed_reopened", actions=[], evidence=evidence, ttl=90.0,
            )
        elif evidence["history_missing"] > 0:
            self._record(
                "mailbox_history", state="degraded", source="github_api",
                code="mailbox_history_missing", actions=[], evidence=evidence, ttl=90.0,
            )
        else:
            self._record(
                "mailbox_history", state="ready", source="github_api",
                code="mailbox_clean", actions=[], evidence=evidence, ttl=90.0,
            )

    def _record_repo_denial_unknown(self, endpoint_class: str) -> None:
        """Record the repo-probe-dependent components unknown after a 403.

        ``endpoint_class`` is a closed github_app.ENDPOINT_CLASSES value
        (``repos_detail``/``repos_actions``); no free text reaches the
        observations.
        """
        for component in ("worker_integrity", "workflow", "actions", "environment", "job_token"):
            self._record(
                component, state="unknown", source="github_api",
                code="repos_access_denied",
                actions=["diagnose"],
                evidence={"endpoint_class": str(endpoint_class or "")}, ttl=90.0,
            )

    def _record_channel_test(self, expected_tree: str) -> None:
        verification = dict(self.task_store.get_app_state("remote_channel_verification", {}) or {})
        box_key = self.credentials.load_secret("worker_box_public_key") if self.credentials.has_secret("worker_box_public_key") else ""
        signing_key = self.credentials.load_secret("worker_signing_public_key") if self.credentials.has_secret("worker_signing_public_key") else ""
        current = verification_fingerprint(
            tree=expected_tree, box_public_key=box_key, signing_public_key=signing_key
        ) if expected_tree and box_key and signing_key else ""
        verified = bool(
            verification.get("verified_at") and current
            and str(verification.get("fingerprint") or "") == current
        )
        legacy = any(
            str(item.get("remote_state") or "") == "imported"
            for item in self.task_store.list_remote_runs(limit=1, workflow="echo.yml")
        )
        self._record(
            "channel_test",
            state="ready" if verified else "action_required",
            source="local",
            code="channel_test_valid" if verified else (
                "legacy_channel_test" if legacy else "channel_test_required"
            ),
            actions=[] if verified else ["test-channel"],
            evidence={"verified_at": float(verification.get("verified_at") or 0)}, ttl=90.0,
        )

    def _reconcile_runs(self, client: GitHubAppClient) -> None:
        active = []
        for run in self.task_store.list_remote_runs(limit=50):
            remote_state = str(run.get("remote_state") or "")
            if remote_state not in ACTIVE_REMOTE_STATES:
                continue
            run_id = int(run.get("run_id") or 0)
            if not run_id:
                continue
            try:
                payload = client._api(
                    "GET", f"/repos/{run.get('repository')}/actions/runs/{run_id}"
                ).json()
            except GitHubAppError as exc:
                self.task_store.upsert_remote_attempt(
                    str(run.get("task_id") or ""), int(run.get("attempt") or 1),
                    repository=str(run.get("repository") or ""), workflow=str(run.get("workflow") or ""),
                    run_id=run_id, error_code=exc.code, observed_at=time.time(),
                )
                continue
            github_status = str(payload.get("status") or "unknown")
            conclusion = str(payload.get("conclusion") or "")
            # 行键=任务级派发 attempt（run 行上的 attempt 列），不是 GitHub 单个
            # run 内部的 run_attempt（run 首跑恒为 1）。此前用 payload.run_attempt
            # 作行键：任务第 3 次派发的每次轮询都会写出一行幻影 attempt=1 记录，
            # 其 import_state 拷贝 remote_state，行转终态被本循环跳过后冻结在
            # "running"（REALTEST-H1/H2 同根：同一 run 出现在两条 attempt 行）。
            attempt = max(1, int(run.get("attempt") or 1))
            value = self.task_store.upsert_remote_attempt(
                str(run.get("task_id") or ""), attempt,
                repository=str(run.get("repository") or ""), workflow=str(run.get("workflow") or ""),
                run_id=run_id, github_status=github_status, conclusion=conclusion,
                import_state="imported" if remote_state == "imported" else remote_state,
                artifact_id=run.get("artifact_id"), observed_at=time.time(), error_code="",
            )
            if github_status not in TERMINAL_GITHUB_STATES:
                active.append(value)
        if active:
            self._record(
                "current_runner", state="ready", source="github_run", code="managed_run_active",
                actions=["cancel-run"], evidence={"active_count": len(active)}, ttl=20.0,
            )
        else:
            self._record(
                "current_runner", state="ready", source="local", code="no_managed_run",
                actions=[], evidence={"active_count": 0}, ttl=90.0,
            )

    def _record_probe_error(self, exc: GitHubAppError) -> None:
        code = str(getattr(exc, "code", "github_error") or "github_error")
        request_id = str(getattr(exc, "request_id", "") or "")
        retry_after = float(getattr(exc, "retry_after", 0.0) or 0.0)
        # Closed-set endpoint class (github_app.ENDPOINT_CLASSES): lets the
        # frontend tell installation/scope refusals apart from identity/token
        # refusals without exposing any GitHub payload text.
        endpoint_class = str(getattr(exc, "endpoint_class", "") or "")
        if retry_after:
            self._backoff_until = max(self._backoff_until, time.time() + retry_after)
        if code == "github_unreachable":
            self._record(
                "github_api", state="offline", source="github_api", code=code,
                actions=["diagnose"],
                evidence={"request_id": request_id, "endpoint_class": endpoint_class}, ttl=20.0,
            )
        elif code in {"authorization_revoked", "permission_denied"}:
            self._record(
                "github_api", state="ready", source="github_api", code="github_api_reached",
                actions=[],
                evidence={"request_id": request_id, "endpoint_class": endpoint_class}, ttl=90.0,
            )
            self._record(
                "authorization", state="action_required", source="github_api", code=code,
                actions=["start-authorization"],
                evidence={"request_id": request_id, "endpoint_class": endpoint_class}, ttl=90.0,
            )
        elif code == "rate_limited":
            self._record(
                "github_api", state="degraded", source="github_api", code=code,
                actions=["diagnose"],
                evidence={
                    "request_id": request_id, "endpoint_class": endpoint_class,
                    "retry_at": self._backoff_until,
                }, ttl=90.0,
            )
        else:
            self._record(
                "github_api", state="degraded", source="github_api", code=code,
                actions=["diagnose"],
                evidence={"request_id": request_id, "endpoint_class": endpoint_class}, ttl=30.0,
            )

    def _unknown_downstream(self, code: str, action: str) -> None:
        for component in COMPONENT_ORDER:
            if component in {"local_backend", "app_configuration", "authorization"}:
                continue
            self._record(
                component, state="unknown", source="local", code=code,
                actions=[action], evidence={}, ttl=30.0,
            )

    def _record(
        self,
        component: str,
        *,
        state: str,
        source: str,
        code: str,
        actions: list[str],
        evidence: dict[str, Any],
        ttl: float,
    ) -> dict[str, Any]:
        now = time.time()
        previous = self.task_store.get_remote_observation(component)
        value = self.task_store.upsert_remote_observation(
            component, state=state, source=source, code=code,
            actions=actions, evidence=evidence, observed_at=now, expires_at=now + max(1.0, ttl),
        )
        comparable = (state, source, code, actions, evidence)
        prior_comparable = None if previous is None else (
            previous.get("state"), previous.get("source"), previous.get("code"),
            previous.get("actions"), previous.get("evidence"),
        )
        if comparable != prior_comparable:
            self.task_store.append_remote_event(
                "remote-connection", component,
                {key: value.get(key) for key in (
                    "component", "state", "source", "code", "actions", "observed_at", "expires_at"
                )},
            )
        return value

    def snapshot(self) -> dict[str, Any]:
        """Stale-while-revalidate read path (FRONTEND-SMOOTH-1 unit C).

        动作执行期间本地凭据库/状态库被动作线程占用时，快照构建可能瞬态
        失败——此时返回最近一次成功快照（组件自带 observed_at/expires_at
        时效），绝不向 HTTP 层抛错把卡面清成「账号未知」；从未成功过才如实
        上抛。
        """
        try:
            value = self._snapshot_uncached()
        except Exception:
            last = self._last_good_snapshot
            if last is None:
                raise
            return copy.deepcopy(last)
        self._last_good_snapshot = copy.deepcopy(value)
        return value

    def fresh_snapshot(self, *, max_age_seconds: float = 5.0, timeout: float = 20.0) -> dict[str, Any]:
        """等待窗新鲜语义（INIT-PATH-POLISH-1 单元A/终验发现⑨）。

        等待窗轮询（安装等待/收紧等待）必须读到一次比 ``max_age_seconds``
        更新的已收口探针，否则「学生已在 GitHub 完成安装，客户端 3s 轮询
        却在最长一个空闲节拍（60s）+ 记录时效（90s）内恒读旧证据」。
        只读探测加速，不改任何动作形状：最新探针足够新时直接返回（合并
        等待窗的重复轮询，探针速率有上界）；否则把监视线程的下一次探针
        提前到现在并有界等待其收口。限流退避（_backoff_until）优先于强制，
        超时诚实降级为普通 stale-while-revalidate 快照——绝不抛错、绝不
        伪造就绪。
        """
        with self._lock:
            baseline = self._probe_seq
            probing = self._probing
            last_probe = self._last_probe_at
            thread_alive = bool(self._thread and self._thread.is_alive())
        if not probing and time.time() - last_probe <= max_age_seconds:
            return self.snapshot()
        if not thread_alive:
            # 监视线程未启动（未 start / 单测环境）：在调用线程同步补一次，
            # _probing 守护保证与其他调用者互斥。
            try:
                self.probe()
            except Exception:
                pass
            return self.snapshot()
        with self._lock:
            self._next_probe_at = 0.0
        self._wake.set()
        deadline = time.time() + max(1.0, float(timeout))
        while time.time() < deadline:
            with self._lock:
                if self._probe_seq > baseline:
                    break
            time.sleep(0.05)
        return self.snapshot()

    def _snapshot_uncached(self) -> dict[str, Any]:
        now = time.time()
        by_name = {str(item.get("component")): dict(item) for item in self.task_store.list_remote_observations()}
        components = []
        for name in COMPONENT_ORDER:
            value = by_name.get(name) or {
                "component": name, "state": "unknown", "source": "local", "code": "not_observed",
                "actions": ["diagnose"], "evidence": {}, "observed_at": 0.0, "expires_at": 0.0,
            }
            stale = bool(value.get("expires_at") and float(value.get("expires_at") or 0) < now)
            if stale:
                value["last_state"] = value.get("state")
                value["state"] = "unknown"
                value["code"] = "stale_evidence"
                value["actions"] = ["diagnose"]
            value["stale"] = stale
            components.append(value)
        local = self.github_provider().snapshot()
        by_component = {item["component"]: item for item in components}
        if not local.get("app_configured"):
            by_component["app_configuration"].update(
                state="action_required", code="app_not_configured", stale=False,
                actions=["diagnose"], source="local", observed_at=now, expires_at=now + 90,
            )
        if not local.get("authorized"):
            by_component["authorization"].update(
                state="action_required", code="authorization_missing", stale=False,
                actions=["start-authorization"], source="local", observed_at=now, expires_at=now + 90,
            )
            self._unknown_snapshot_components(by_component, now, "authorization_missing", "start-authorization")
        elif float(local.get("access_expires_at") or 0) and float(local.get("access_expires_at") or 0) <= now:
            code = "authorization_refresh_required" if local.get("refresh_available") else "authorization_revoked"
            by_component["authorization"].update(
                state="checking" if local.get("refresh_available") else "action_required",
                code=code, stale=False, actions=["diagnose"] if local.get("refresh_available") else ["start-authorization"],
                source="local", observed_at=now, expires_at=now + 30,
            )
        if not local.get("bootstrapped") and local.get("authorized"):
            for name in ("worker_repository", "mailbox_repository", "mailbox_history", "worker_integrity", "workflow", "actions", "environment"):
                # 创建即存：绑定在案即仓库已建——保留 probe 的实时证据（含
                # 「已建待安装」降级态），其余未完成段仍按未初始化收敛。
                if name == "worker_repository" and local.get("worker_repo"):
                    continue
                if name == "mailbox_repository" and local.get("mailbox_repo"):
                    continue
                by_component[name].update(
                    state="unknown", code="worker_setup_incomplete", stale=False,
                    actions=["bootstrap"], source="local", observed_at=now, expires_at=now + 30,
                )
        required = {item["component"]: item for item in components if item["component"] in REQUIRED_FOR_DISPATCH}
        ready = all(item.get("state") == "ready" and not item.get("stale") for item in required.values())
        active_attempts = [
            item for item in self.task_store.list_remote_attempts(limit=50)
            if str(item.get("github_status") or "") not in {"", "completed"}
            and float(item.get("observed_at") or 0)
            and now - float(item.get("observed_at") or 0) <= 20.0
        ]
        blocking_candidates = [
            item for item in components
            if item["component"] in REQUIRED_FOR_DISPATCH and item.get("state") != "ready"
        ]
        component_rank = {name: index for index, name in enumerate(COMPONENT_ORDER)}

        def _blocking_rank(item: dict[str, Any]) -> tuple[int, int]:
            priority = _BLOCKING_STATE_PRIORITY.get(str(item.get("state")), 3)
            # D-20261009-11（REMOTE-E2E-1-R4 from-zero 收紧安装范围后续跑死锁）：
            # bootstrap 待续跑是 fresh 本地推导（code=worker_setup_incomplete、
            # 非 stale unknown——stale 已被上方改写为 stale_evidence），与
            # action_required 同档、组件序定并列。否则收紧后 overall 直接跳
            # channel_test_required，前端 MF-7 自动续跑守卫永不命中、主按钮只
            # 剩无效果的加密测试（真实学生卡死无路；R4 实证单动作即愈=能力
            # 在、编排断）。
            if priority >= 3 and str(item.get("code")) == "worker_setup_incomplete":
                priority = _BLOCKING_STATE_PRIORITY["action_required"]
            return (
                priority,
                component_rank.get(str(item.get("component")), len(component_rank)),
            )

        blocking = min(
            blocking_candidates,
            key=_blocking_rank,
            default=None,
        )
        overall_state = "ready" if ready else (
            "offline" if blocking and blocking.get("state") == "offline" else
            "action_required" if blocking and blocking.get("state") == "action_required" else
            "degraded" if blocking and blocking.get("state") == "degraded" else "unknown"
        )
        allowed_actions = ["diagnose"]
        for component in components:
            for action in list(component.get("actions") or []):
                if action not in allowed_actions:
                    allowed_actions.append(action)
        without_channel = all(
            item.get("state") == "ready" and not item.get("stale")
            for name, item in required.items() if name != "channel_test"
        )
        if without_channel and "test-channel" not in allowed_actions:
            allowed_actions.append("test-channel")
        return {
            "schema": "courselens.remote-connection.v1",
            "overall": {
                "state": overall_state,
                "activity": "running" if active_attempts else "idle",
                "ready_for_dispatch": ready,
                "code": "ready_for_dispatch" if ready else str((blocking or {}).get("code") or "status_unknown"),
                "observed_at": max((float(item.get("observed_at") or 0) for item in components), default=0.0),
                "expires_at": min(
                    (float(item.get("expires_at") or 0) for item in required.values() if item.get("expires_at")),
                    default=0.0,
                ),
            },
            "components": components,
            "channel_test": next((item for item in components if item["component"] == "channel_test"), {}),
            # Trusted backend-provided App installation URL (from the provider's
            # bundled configuration).  The frontend may only render it as a link;
            # it must never auto-navigate or persist it.
            "installation_url": str(local.get("installation_url") or ""),
            "active_runs": self.remote_runs_snapshot(active_only=True),
            "configured_repositories": {
                "worker": str(local.get("worker_repo") or ""),
                "mailbox": str(local.get("mailbox_repo") or ""),
            },
            "allowed_actions": allowed_actions,
            "probing": self._probing,
            "next_check_at": self._next_probe_at,
        }

    @staticmethod
    def _unknown_snapshot_components(
        by_component: dict[str, dict[str, Any]], now: float, code: str, action: str
    ) -> None:
        for name in REQUIRED_FOR_DISPATCH:
            if name in {"local_backend", "app_configuration", "authorization"}:
                continue
            by_component[name].update(
                state="unknown", code=code, stale=False, actions=[action], source="local",
                observed_at=now, expires_at=now + 30,
            )

    def remote_runs_snapshot(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        attempts = self.task_store.list_remote_attempts(limit=100)
        allowed_repository = (
            self.credentials.load_secret("github_worker_repo")
            if self.credentials.has_secret("github_worker_repo") else ""
        )
        output = []
        for item in attempts:
            status = str(item.get("github_status") or "")
            observed = float(item.get("observed_at") or 0)
            if active_only and (
                status in {"", "completed"} or not observed or time.time() - observed > 20.0
            ):
                continue
            run_id = int(item.get("run_id") or 0)
            repository = str(item.get("repository") or "")
            output.append({
                key: item.get(key) for key in (
                    "task_id", "attempt", "workflow", "run_id", "github_status", "conclusion",
                    "worker_status", "worker_stage", "completed", "total",
                    "last_heartbeat_at", "import_state", "cleanup_state", "error_code",
                    "observed_at", "updated_at",
                )
            } | {
                "diagnostic_url": f"https://github.com/{repository}/actions/runs/{run_id}"
                if repository and repository == allowed_repository and run_id else "",
            })
        return output

    def preflight(
        self, *, maximum_age_seconds: float = 30.0, require_channel_test: bool = True
    ) -> dict[str, Any]:
        snapshot = self.snapshot()
        observed = float((snapshot.get("overall") or {}).get("observed_at") or 0)
        def acceptable(value: dict[str, Any]) -> bool:
            if require_channel_test:
                return bool((value.get("overall") or {}).get("ready_for_dispatch"))
            required = {
                item.get("component"): item for item in value.get("components") or []
                if item.get("component") in REQUIRED_FOR_DISPATCH and item.get("component") != "channel_test"
            }
            return all(item.get("state") == "ready" and not item.get("stale") for item in required.values())

        if not acceptable(snapshot) or time.time() - observed > maximum_age_seconds:
            snapshot = self.probe()
        if not acceptable(snapshot):
            raise RemoteConnectionError(
                str(snapshot["overall"].get("code") or "remote_not_ready"),
                "GitHub online service is not ready for a secure dispatch",
            )
        return snapshot


__all__ = [
    "OPERATION_ID_RE", "RemoteConnectionError", "RemoteConnectionSupervisor",
    "verification_fingerprint",
]
