"""Local supervisor for one encrypted GitHub Actions compute task."""

from __future__ import annotations

import json
import os
import random
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from credentials import CredentialStore
from src.distribution import DISTRIBUTION_REPOSITORY, SOURCE_REPOSITORY
from src.runtime.task_store import TaskStore

from .github_client import DispatchResult, GitHubClient, GitHubRemoteError
from .protocol import (
    PROTOCOL_VERSION,
    finalize_job,
    generate_box_keypair,
    open_control,
    open_result,
    seal_job,
)


# One supervisor lease per task covers the whole execute() lifetime, which
# includes long waits on the GitHub runner.  A crashed supervisor's lease
# expires and is preemptible, so recovery never needs the process alive.
_SUPERVISOR_LEASE_TTL_SECONDS = 6 * 3600

# Dispatch-POST family discipline, mirroring the worker mailbox pacer
# (bounded waits, closed-set retryable statuses): a transient rejection of
# the workflow dispatch or the encrypted mailbox publish must not turn into
# a student-visible terminal failure that only a manual retry can recover.
_DISPATCH_RETRY_WAITS_SECONDS: tuple[float, ...] = (1.0, 2.0)
_DISPATCH_RETRYABLE_STATUS_CODES = frozenset({403, 429, 500, 502, 503, 504})
_DISPATCH_RETRY_JITTER = 0.25
_DISPATCH_HTTP_STATUS_RE = re.compile(r"returned HTTP (\d{3})")
# N11（夜14-R1 T29 定谳）：运行态长等待的轮询退避帽。排队/启动窗保持
# poll_seconds 基线不变；进入运行态的长等待（_wait_until_complete）按
# 基线×2 指数退避到本帽。15s 帽=任务收口侦出延迟 +≤12s（对任务总时长
# 无感），31min 级媒体任务的 status GET 从 ~620 次降到 ~130 次。
_POLL_BACKOFF_CAP_SECONDS = 15.0


class RemoteTaskPaused(RuntimeError):
    """The local user canceled the active GitHub workflow."""


@dataclass(frozen=True)
class RemoteSettings:
    enabled: bool
    public_repo: str = DISTRIBUTION_REPOSITORY
    private_repo: str = SOURCE_REPOSITORY
    workflow: str = "process.yml"
    ref: str = "main"
    github_token: str = ""
    worker_public_key: str = ""
    worker_signing_public_key: str = ""
    expected_worker_commit: str = ""
    proxy_url: str = ""
    poll_seconds: float = 3.0
    handshake_timeout_seconds: int = 600
    transient_retry_seconds: float = 90.0
    dispatch_retry_attempts: int = 3
    dispatch_retry_seconds: float = 45.0

    @classmethod
    def load(cls, credentials: CredentialStore) -> "RemoteSettings":
        def secret(env_name: str, saved_name: str) -> str:
            value = os.environ.get(env_name, "").strip()
            if value:
                return value
            try:
                return credentials.load_secret(saved_name).strip()
            except KeyError:
                return ""

        configured_enabled = os.environ.get("FUDAN_COURSELENS_REMOTE_ENABLED", "").strip()
        if not configured_enabled:
            configured_enabled = os.environ.get("REMOTE_COMPUTE_ENABLED", "").strip()
        if not configured_enabled:
            try:
                configured_enabled = credentials.load_secret("remote_enabled")
            except KeyError:
                configured_enabled = ""
        enabled = configured_enabled.lower() in {
            "1", "true", "yes", "on",
        }
        saved_worker_repo = secret("FUDAN_COURSELENS_PUBLIC_REPO", "github_worker_repo")
        saved_mailbox_repo = secret("FUDAN_COURSELENS_PRIVATE_REPO", "github_mailbox_repo")
        app_token = secret("FUDAN_COURSELENS_GITHUB_TOKEN", "github_app_access_token")
        legacy_token = secret("FUDAN_COURSELENS_GITHUB_TOKEN", "github_remote_token")
        return cls(
            enabled=enabled,
            public_repo=saved_worker_repo or cls.public_repo,
            private_repo=saved_mailbox_repo or cls.private_repo,
            workflow=os.environ.get("FUDAN_COURSELENS_REMOTE_WORKFLOW", cls.workflow).strip(),
            ref=os.environ.get("FUDAN_COURSELENS_REMOTE_REF", cls.ref).strip(),
            github_token=app_token or legacy_token,
            worker_public_key=secret("FUDAN_COURSELENS_WORKER_PUBLIC_KEY", "worker_box_public_key"),
            worker_signing_public_key=secret(
                "FUDAN_COURSELENS_WORKER_SIGNING_PUBLIC_KEY", "worker_signing_public_key"
            ),
            expected_worker_commit=secret(
                "FUDAN_COURSELENS_EXPECTED_WORKER_COMMIT", "github_worker_dispatch_sha"
            ).lower(),
            proxy_url=secret("FUDAN_COURSELENS_GITHUB_PROXY", "network_github_proxy"),
        )

    def validate(self) -> None:
        if not self.enabled:
            return
        missing = [
            name for name, value in (
                ("GitHub token", self.github_token),
                ("worker encryption public key", self.worker_public_key),
                ("worker signing public key", self.worker_signing_public_key),
            ) if not value
        ]
        if missing:
            raise RuntimeError("Remote compute is enabled but missing " + ", ".join(missing))
        if self.expected_worker_commit and (
            len(self.expected_worker_commit) != 40
            or any(char not in "0123456789abcdef" for char in self.expected_worker_commit)
        ):
            raise RuntimeError("Remote compute has an invalid signed Worker commit pin")


class RemoteCoordinator:
    def __init__(
        self,
        settings: RemoteSettings,
        task_store: TaskStore,
        credentials: CredentialStore,
        *,
        github: GitHubClient | None = None,
    ):
        settings.validate()
        self.settings = settings
        self.task_store = task_store
        self.credentials = credentials
        self.github = github or GitHubClient(settings.github_token, proxy_url=settings.proxy_url)

    def execute(
        self,
        *,
        task_id: str,
        build_job: Callable[[str], dict[str, Any]],
        import_result: Callable[[dict[str, Any]], None],
        cancel_requested: Callable[[], bool],
        progress: Callable[[str, float | None, str], None],
        reuse_queued_run: bool = False,
        allow_dispatch: bool = True,
    ) -> dict[str, Any]:
        """Supervise one remote task under a per-task lease.

        A second concurrent supervisor for the same task must reuse or queue,
        never race the live run: it fails fast with the closed-set
        ``remote_supervisor_busy`` code while the first supervisor's run stays
        untouched.
        """
        lease_owner = uuid.uuid4().hex
        if not self.task_store.acquire_remote_supervisor_lease(
            task_id, owner=lease_owner, ttl_seconds=_SUPERVISOR_LEASE_TTL_SECONDS
        ):
            raise GitHubRemoteError(
                "another supervisor is already active for this task",
                code="remote_supervisor_busy",
            )
        try:
            return self._execute(
                task_id=task_id,
                build_job=build_job,
                import_result=import_result,
                cancel_requested=cancel_requested,
                progress=progress,
                reuse_queued_run=reuse_queued_run,
                allow_dispatch=allow_dispatch,
            )
        finally:
            self.task_store.release_remote_supervisor_lease(task_id, owner=lease_owner)

    def _execute(
        self,
        *,
        task_id: str,
        build_job: Callable[[str], dict[str, Any]],
        import_result: Callable[[dict[str, Any]], None],
        cancel_requested: Callable[[], bool],
        progress: Callable[[str, float | None, str], None],
        reuse_queued_run: bool = False,
        allow_dispatch: bool = True,
    ) -> dict[str, Any]:
        """Dispatch, publish a fresh authorization package, and import once."""
        prior = self.task_store.get_remote_run(task_id) or {}
        if self._can_resume_result_import(task_id, prior):
            return self._resume_result_import(
                task_id=task_id,
                prior=prior,
                import_result=import_result,
                progress=progress,
            )
        prior_state = str(prior.get("remote_state") or "")
        recovered_dispatch: DispatchResult | None = None
        cancelled_inflight = False
        if prior_state in {"queued", "awaiting_payload", "running", "canceling"} and prior.get("run_id"):
            if (
                prior.get("issue_number")
                and str(prior.get("input_hash") or "")
                and self.credentials.has_secret(f"remote_result_private:{task_id}")
            ):
                return self._resume_existing_run(
                    task_id=task_id,
                    prior=prior,
                    cancel_requested=cancel_requested,
                    import_result=import_result,
                    progress=progress,
                )
            if prior_state in {"running", "canceling"}:
                raise GitHubRemoteError("existing remote run cannot be safely reattached")
            if prior_state == "queued" and reuse_queued_run:
                run = self._get_verified_run(int(prior["run_id"]))
                recovered_dispatch = DispatchResult(
                    run_id=int(prior["run_id"]),
                    status=str(run.get("status") or "queued"),
                    head_sha=str(run.get("head_sha") or ""),
                )
            else:
                # Never cancel an in-flight run unless the replacement dispatch
                # is certain to proceed; a cancelled run with no successor is
                # exactly the phantom this guard exists to prevent.
                if not allow_dispatch:
                    raise GitHubRemoteError("remote dispatch is disabled for this recovery path")
                cancelled_inflight = True
                try:
                    self.github.cancel_run(self.settings.public_repo, int(prior["run_id"]))
                except Exception:
                    pass
        if not allow_dispatch:
            raise GitHubRemoteError("remote dispatch is disabled for this recovery path")
        attempt = (
            max(1, int(prior.get("attempt") or 0))
            if recovered_dispatch
            else int(prior.get("attempt") or 0) + 1
        )
        try:
            dispatched = recovered_dispatch or self._dispatch_workflow_with_retry(
                task_id, progress
            )
        except Exception as exc:
            if not cancelled_inflight:
                raise
            # The prior run is already gone: record the truth on the task's
            # remote row instead of leaving a queued phantom with no successor.
            prior_run_id = int(prior.get("run_id") or 0)
            self.task_store.upsert_remote_run(
                task_id,
                repository=str(prior.get("repository") or self.settings.public_repo),
                workflow=str(prior.get("workflow") or self.settings.workflow),
                run_id=prior_run_id,
                attempt=attempt,
                remote_state="failed",
                finished_at=time.time(),
                last_error=f"{type(exc).__name__}: {str(exc)[:300]}",
            )
            self.task_store.upsert_remote_attempt(
                task_id, attempt, repository=self.settings.public_repo,
                workflow=self.settings.workflow, run_id=prior_run_id,
                worker_status="failed", import_state="failed",
                cleanup_state="best_effort",
                error_code=(
                    str(getattr(exc, "code", "") or "").strip()[:80]
                    or self._safe_error_code(exc)
                ),
                observed_at=time.time(),
            )
            raise
        now = time.time()
        self.task_store.upsert_remote_run(
            task_id,
            repository=self.settings.public_repo,
            workflow=self.settings.workflow,
            run_id=dispatched.run_id,
            attempt=attempt,
            remote_state="queued",
            dispatched_at=now,
            last_error="",
        )
        self.task_store.upsert_remote_attempt(
            task_id, attempt, repository=self.settings.public_repo,
            workflow=self.settings.workflow, run_id=dispatched.run_id,
            github_status=str(dispatched.status or "requested"), observed_at=now,
        )
        progress("remote_queue", None, "Workflow submitted; confirming the GitHub run")
        issue_number: int | None = None
        artifact_id: int | None = None
        input_hash = ""
        result_secret_name = f"remote_result_private:{task_id}"
        try:
            self._wait_until_started(task_id, dispatched.run_id, cancel_requested, progress)
            if attempt > 1:
                # A failed earlier attempt can leave its mailbox issue behind,
                # and this attempt seals a fresh envelope that cannot match it.
                # Retire that stale issue so publish creates a clean one.
                retire = getattr(self.github, "retire_job_issues", None)
                if retire is not None:
                    try:
                        retire(self.settings.private_repo, task_id)
                    except Exception:
                        pass
            result_private, result_public = generate_box_keypair()
            self.credentials.save_secret(result_secret_name, result_private)
            job = finalize_job(build_job(result_public))
            envelope = seal_job(job, self.settings.worker_public_key)
            input_hash = str(job.get("input_hash") or "")
            mailbox = self._publish_job_with_retry(task_id, envelope, progress)
            issue_number = int(mailbox["issue_number"])
            self.task_store.upsert_remote_run(
                task_id,
                repository=self.settings.public_repo,
                workflow=self.settings.workflow,
                run_id=dispatched.run_id,
                attempt=attempt,
                issue_number=issue_number,
                remote_state="running",
                input_hash=input_hash,
                pipeline_version=str(job.get("pipeline", {}).get("version") or "v1"),
                started_at=time.time(),
            )
            self.task_store.upsert_remote_attempt(
                task_id, attempt, repository=self.settings.public_repo,
                workflow=self.settings.workflow, run_id=dispatched.run_id,
                github_status="in_progress", worker_status="waiting",
                worker_stage="awaiting_payload", observed_at=time.time(),
            )
            progress("awaiting_payload", None, "Runner started; publishing encrypted authorization")
            self._wait_until_complete(
                task_id,
                dispatched.run_id,
                issue_number,
                result_secret_name,
                input_hash,
                cancel_requested,
                progress,
            )
            artifact = self._find_result_artifact(dispatched.run_id, task_id)
            artifact_id = int(artifact["id"])
            self.task_store.upsert_remote_run(
                task_id,
                repository=self.settings.public_repo,
                workflow=self.settings.workflow,
                run_id=dispatched.run_id,
                attempt=attempt,
                issue_number=issue_number,
                artifact_id=artifact_id,
                remote_state="downloading_result",
                input_hash=input_hash,
                finished_at=time.time(),
            )
            self.task_store.upsert_remote_attempt(
                task_id, attempt, repository=self.settings.public_repo,
                workflow=self.settings.workflow, run_id=dispatched.run_id,
                github_status="completed", conclusion="success", artifact_id=artifact_id,
                import_state="artifact_ready", observed_at=time.time(),
            )
            progress("remote_result", None, "Encrypted result found; verifying artifact")
            raw = self._transient_retry(
                lambda: self.github.download_artifact_file(
                    self.settings.public_repo, artifact_id, "result.box.json"
                )
            )
            try:
                result_envelope = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GitHubRemoteError("result artifact does not contain valid JSON") from exc
            result = open_result(
                result_envelope,
                self.credentials.load_secret(result_secret_name),
                self.settings.worker_signing_public_key,
                expected_task_id=task_id,
                expected_input_hash=input_hash,
            )
            self._record_result_notices(task_id, result)
            progress("remote_import", None, "Importing verified derived content")
            import_result(result)
            self.task_store.upsert_remote_run(
                task_id,
                repository=self.settings.public_repo,
                workflow=self.settings.workflow,
                run_id=dispatched.run_id,
                attempt=attempt,
                issue_number=issue_number,
                artifact_id=artifact_id,
                remote_state="imported",
                input_hash=input_hash,
                imported_at=time.time(),
            )
            self.credentials.delete_secret(result_secret_name)
            self.task_store.upsert_remote_attempt(
                task_id, attempt, repository=self.settings.public_repo,
                workflow=self.settings.workflow, run_id=dispatched.run_id,
                github_status="completed", conclusion="success", artifact_id=artifact_id,
                import_state="imported", cleanup_state="pending", observed_at=time.time(),
            )
            self._cleanup_imported_job(
                task_id=task_id, attempt=attempt, run_id=dispatched.run_id,
                issue_number=issue_number, artifact_id=artifact_id,
            )
            return result
        except RemoteTaskPaused:
            checkpoint = self._capture_checkpoint(
                dispatched.run_id,
                task_id,
                result_secret_name=result_secret_name,
                expected_input_hash=input_hash,
            )
            if issue_number is not None:
                self.github.cleanup_job(self.settings.private_repo, issue_number)
            self.credentials.delete_secret(result_secret_name)
            self.task_store.upsert_remote_run(
                task_id,
                repository=self.settings.public_repo,
                workflow=self.settings.workflow,
                run_id=dispatched.run_id,
                attempt=attempt,
                issue_number=issue_number,
                remote_state="paused",
                input_hash=input_hash,
                checkpoint=checkpoint,
                finished_at=time.time(),
            )
            self.task_store.upsert_remote_attempt(
                task_id, attempt, repository=self.settings.public_repo,
                workflow=self.settings.workflow, run_id=dispatched.run_id,
                github_status="completed", conclusion="cancelled",
                worker_status="paused", import_state="paused",
                cleanup_state="complete" if issue_number is not None else "not_created",
                observed_at=time.time(),
            )
            raise

        except Exception as exc:
            # A failure before the new payload is published must not erase a
            # checkpoint verified during an earlier attempt. Replace it only
            # when this attempt yields a newer signed checkpoint.
            checkpoint: dict[str, Any] = dict(prior.get("checkpoint") or {})
            # A failure before a final result exists is not recoverable by a
            # later import.  Preserve any verified incremental checkpoint,
            # then remove the encrypted mailbox payload and one-time key.
            # Once ``artifact_id`` is known we deliberately retain all three:
            # a retry resumes the verified result import without re-running
            # the expensive workflow.
            if artifact_id is None:
                if input_hash and self.credentials.has_secret(result_secret_name):
                    try:
                        checkpoint = self._capture_checkpoint(
                            dispatched.run_id,
                            task_id,
                            result_secret_name=result_secret_name,
                            expected_input_hash=input_hash,
                        )
                    except Exception:
                        checkpoint = {}
                if issue_number is not None:
                    try:
                        self.github.cleanup_job(self.settings.private_repo, issue_number)
                    except Exception:
                        pass
                self.credentials.delete_secret(result_secret_name)
            self.task_store.upsert_remote_run(
                task_id,
                repository=self.settings.public_repo,
                workflow=self.settings.workflow,
                run_id=dispatched.run_id,
                attempt=attempt,
                issue_number=issue_number,
                artifact_id=artifact_id,
                remote_state="failed",
                checkpoint=checkpoint,
                finished_at=time.time(),
                last_error=f"{type(exc).__name__}: {str(exc)[:300]}",
            )
            self.task_store.upsert_remote_attempt(
                task_id, attempt, repository=self.settings.public_repo,
                workflow=self.settings.workflow, run_id=dispatched.run_id,
                github_status="completed" if artifact_id is not None else "unknown",
                worker_status="failed", import_state="failed",
                cleanup_state="retained_for_retry" if artifact_id is not None else "best_effort",
                # The worker's signed closed-set code outranks the generic
                # classification: it is the only actionable reason a student
                # can be given, so it must survive into the failure rows.
                error_code=(
                    str(getattr(exc, "code", "") or "").strip()[:80]
                    or self._safe_error_code(exc)
                ),
                observed_at=time.time(),
            )
            raise

    def resume_existing_run_only(
        self,
        *,
        task_id: str,
        expected_run_id: int,
        expected_attempt: int,
        import_result: Callable[[dict[str, Any]], None],
        cancel_requested: Callable[[], bool],
        progress: Callable[[str, float | None, str], None],
        retain_result_secret: bool = False,
    ) -> dict[str, Any]:
        """Reattach one already-authorized run without any dispatch fallback."""
        prior = dict(self.task_store.get_remote_run(task_id) or {})
        run_id = int(prior.get("run_id") or 0)
        attempt = int(prior.get("attempt") or 0)
        if (
            run_id != int(expected_run_id)
            or attempt != int(expected_attempt)
            or int(expected_attempt) != 2
            or str(prior.get("repository") or "").casefold()
            != str(self.settings.public_repo).casefold()
            or str(prior.get("workflow") or "") != self.settings.workflow
            or str(prior.get("remote_state") or "")
            not in {"rerun_running", "running", "awaiting_payload"}
        ):
            raise GitHubRemoteError("existing rerun binding is invalid")
        run = self._get_verified_run(run_id)
        if int(run.get("run_attempt") or 0) != 2:
            raise GitHubRemoteError("existing rerun attempt is not exactly two")
        return self._resume_existing_run(
            task_id=task_id,
            prior=prior,
            cancel_requested=cancel_requested,
            import_result=import_result,
            progress=progress,
            delete_result_secret=not retain_result_secret,
            expected_attempt=2,
        )

    def resume_result_import_only(
        self,
        *,
        task_id: str,
        expected_run_id: int,
        expected_attempt: int,
        import_result: Callable[[dict[str, Any]], None],
        progress: Callable[[str, float | None, str], None],
        retain_result_secret: bool = False,
    ) -> dict[str, Any]:
        """Resume one durable artifact import without dispatch or run mutation."""
        prior = dict(self.task_store.get_remote_run(task_id) or {})
        if (
            int(prior.get("run_id") or 0) != int(expected_run_id)
            or int(prior.get("attempt") or 0) != int(expected_attempt)
            or int(expected_attempt) != 2
            or not self._can_resume_result_import(task_id, prior)
        ):
            raise GitHubRemoteError("rerun result import binding is invalid")
        run = self._get_verified_run(int(expected_run_id))
        if (
            int(run.get("run_attempt") or 0) != 2
            or str(run.get("status") or "") != "completed"
            or str(run.get("conclusion") or "") != "success"
        ):
            raise GitHubRemoteError("rerun result import source is not a signed success")
        return self._resume_result_import(
            task_id=task_id,
            prior=prior,
            import_result=import_result,
            progress=progress,
            delete_result_secret=not retain_result_secret,
        )

    def cleanup_failed_run(self, task_id: str, *, expected_attempt: int = 2) -> dict[str, Any]:
        """Idempotently remove mailbox/artifacts for a terminal failed rerun."""
        prior = dict(self.task_store.get_remote_run(task_id) or {})
        run_id = int(prior.get("run_id") or 0)
        attempt = int(prior.get("attempt") or 0)
        issue_number = int(prior.get("issue_number") or 0)
        if not run_id or attempt != int(expected_attempt) or attempt != 2:
            raise GitHubRemoteError("failed rerun binding is invalid")
        run = self._get_verified_run(run_id)
        if (
            str(run.get("status") or "") != "completed"
            or str(run.get("conclusion") or "") == "success"
            or int(run.get("run_attempt") or 0) != attempt
        ):
            raise GitHubRemoteError("rerun is not a terminal failed attempt")
        try:
            for artifact in self.github.list_run_artifacts(
                self.settings.public_repo, run_id
            ):
                name = str(artifact.get("name") or "")
                if name == f"courselens-result-{task_id}" or name.startswith(
                    f"courselens-checkpoint-{task_id}-"
                ):
                    self.github.delete_artifact(
                        self.settings.public_repo, int(artifact["id"])
                    )
            if issue_number:
                self.github.cleanup_job(self.settings.private_repo, issue_number)
        except Exception as exc:
            self.task_store.upsert_remote_attempt(
                task_id, attempt, repository=self.settings.public_repo,
                workflow=self.settings.workflow, run_id=run_id,
                github_status="completed", conclusion=str(run.get("conclusion") or "failure"),
                worker_status="failed", import_state="failed",
                cleanup_state="cleanup_pending", error_code=self._safe_error_code(exc),
                observed_at=time.time(),
            )
            self.task_store.upsert_remote_run(
                task_id, repository=self.settings.public_repo,
                workflow=self.settings.workflow, run_id=run_id, attempt=attempt,
                issue_number=issue_number or None, remote_state="failed",
                last_error="remote_cleanup_pending",
            )
            raise GitHubRemoteError("failed rerun cleanup remains pending") from exc
        self.task_store.upsert_remote_attempt(
            task_id, attempt, repository=self.settings.public_repo,
            workflow=self.settings.workflow, run_id=run_id,
            github_status="completed", conclusion=str(run.get("conclusion") or "failure"),
            worker_status="failed", import_state="failed", cleanup_state="complete",
            error_code="", observed_at=time.time(),
        )
        self.task_store.upsert_remote_run(
            task_id, repository=self.settings.public_repo,
            workflow=self.settings.workflow, run_id=run_id, attempt=attempt,
            issue_number=issue_number or None, remote_state="failed",
            finished_at=time.time(), last_error="",
        )
        return {"run_id": run_id, "attempt": attempt, "cleanup_state": "complete"}

    def _can_resume_result_import(self, task_id: str, prior: dict[str, Any]) -> bool:
        if str(prior.get("remote_state") or "") not in {"downloading_result", "failed"}:
            return False
        if not prior.get("artifact_id") or not prior.get("issue_number"):
            return False
        if not str(prior.get("input_hash") or ""):
            return False
        return self.credentials.has_secret(f"remote_result_private:{task_id}")

    def _record_result_notices(self, task_id: str, result: dict[str, Any]) -> None:
        """Persist worker result warnings for the task center before importing."""
        warnings = [str(item) for item in (result.get("warnings") or []) if str(item)]
        metrics = dict(result.get("metrics") or {})
        skipped = dict(metrics.get("slides_skipped") or {})
        if not warnings and not skipped:
            return
        notices: dict[str, Any] = {}
        if warnings:
            notices["warnings"] = warnings
        if skipped:
            notices["slides_skipped"] = skipped
        self.task_store.set_result_notices(task_id, notices)

    def _resume_result_import(
        self,
        *,
        task_id: str,
        prior: dict[str, Any],
        import_result: Callable[[dict[str, Any]], None],
        progress: Callable[[str, float | None, str], None],
        delete_result_secret: bool = True,
    ) -> dict[str, Any]:
        """Retry a downloaded/verified result transaction without a new run."""
        artifact_id = int(prior["artifact_id"])
        issue_number = int(prior["issue_number"])
        run_id = int(prior.get("run_id") or 0)
        result_secret_name = f"remote_result_private:{task_id}"
        input_hash = str(prior["input_hash"])
        progress("remote_result", None, "Resuming encrypted result verification")
        raw = self._transient_retry(
            lambda: self.github.download_artifact_file(
                self.settings.public_repo, artifact_id, "result.box.json"
            )
        )
        try:
            result_envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubRemoteError("result artifact does not contain valid JSON") from exc
        result = open_result(
            result_envelope,
            self.credentials.load_secret(result_secret_name),
            self.settings.worker_signing_public_key,
            expected_task_id=task_id,
            expected_input_hash=input_hash,
        )
        progress("remote_import", None, "Resuming verified result import")
        self._record_result_notices(task_id, result)
        import_result(result)
        self.task_store.upsert_remote_run(
            task_id,
            repository=self.settings.public_repo,
            workflow=str(prior.get("workflow") or self.settings.workflow),
            run_id=run_id,
            attempt=int(prior.get("attempt") or 1),
            issue_number=issue_number,
            artifact_id=artifact_id,
            remote_state="imported",
            input_hash=input_hash,
            imported_at=time.time(),
            last_error="",
        )
        if delete_result_secret:
            self.credentials.delete_secret(result_secret_name)
        attempt = int(prior.get("attempt") or 1)
        self.task_store.upsert_remote_attempt(
            task_id, attempt, repository=self.settings.public_repo,
            workflow=str(prior.get("workflow") or self.settings.workflow), run_id=run_id,
            github_status="completed", conclusion="success", artifact_id=artifact_id,
            import_state="imported", cleanup_state="pending", observed_at=time.time(),
        )
        self._cleanup_imported_job(
            task_id=task_id, attempt=attempt, run_id=run_id,
            issue_number=issue_number, artifact_id=artifact_id,
        )
        return result

    def _resume_existing_run(
        self,
        *,
        task_id: str,
        prior: dict[str, Any],
        cancel_requested: Callable[[], bool],
        import_result: Callable[[dict[str, Any]], None],
        progress: Callable[[str, float | None, str], None],
        delete_result_secret: bool = True,
        expected_attempt: int | None = None,
    ) -> dict[str, Any]:
        """Continue an already-dispatched run after a local restart."""
        run_id = int(prior.get("run_id") or 0)
        issue_number = int(prior.get("issue_number") or 0)
        attempt = max(1, int(prior.get("attempt") or 1))
        input_hash = str(prior.get("input_hash") or "")
        result_secret_name = f"remote_result_private:{task_id}"
        if not run_id or not issue_number or not input_hash or not self.credentials.has_secret(result_secret_name):
            raise GitHubRemoteError("existing remote run recovery data is incomplete")
        self.task_store.upsert_remote_attempt(
            task_id, attempt, repository=self.settings.public_repo,
            workflow=str(prior.get("workflow") or self.settings.workflow), run_id=run_id,
            import_state="reattached", observed_at=time.time(), error_code="",
        )
        progress("remote_compute", None, "Reattached to the existing GitHub run")
        try:
            self._wait_until_complete(
                task_id, run_id, issue_number, result_secret_name, input_hash,
                cancel_requested, progress, expected_attempt=expected_attempt,
            )
        except GitHubRemoteError:
            # The failure may itself be a head-sha mismatch: the dispatch pin
            # moved to a newly published mirror while this run started on the
            # previous signed commit. Falling back to the raw run read keeps
            # convergence possible; without it the task keeps remote_state
            # 'running' with its one-time key and durable lease forever.
            try:
                run = self._get_verified_run(run_id)
            except GitHubRemoteError:
                run = self.github.get_run(self.settings.public_repo, run_id)
            if (
                str(run.get("status") or "") == "completed"
                and str(run.get("conclusion") or "") in {"cancelled", "canceled"}
            ):
                self.cleanup_canceled_run(task_id)
                raise RemoteTaskPaused("remote task canceled")
            if (
                str(run.get("status") or "") == "completed"
                and str(run.get("conclusion") or "") == "failure"
            ):
                # A reattach to a remotely failed run must converge the run
                # row, or the failed task keeps remote_state 'running' with
                # its one-time key and durable lease forever and retries loop.
                self.task_store.upsert_remote_run(
                    task_id, repository=self.settings.public_repo,
                    workflow=str(prior.get("workflow") or self.settings.workflow),
                    run_id=run_id, attempt=attempt, issue_number=issue_number,
                    remote_state="failed", input_hash=input_hash,
                    finished_at=time.time(),
                    last_error="GitHub runner concluded with failure",
                )
                self.task_store.upsert_remote_attempt(
                    task_id, attempt, repository=self.settings.public_repo,
                    workflow=str(prior.get("workflow") or self.settings.workflow),
                    run_id=run_id, github_status="completed", conclusion="failure",
                    worker_status="failed", import_state="failed",
                    cleanup_state="best_effort",
                    error_code="githubremoteerror", observed_at=time.time(),
                )
                self.credentials.delete_secret(result_secret_name)
            raise
        terminal = self._get_verified_run(run_id)
        if (
            expected_attempt is not None
            and int(terminal.get("run_attempt") or 0) != int(expected_attempt)
        ):
            raise GitHubRemoteError("existing rerun attempt drifted during recovery")
        artifact = self._find_result_artifact(run_id, task_id)
        artifact_id = int(artifact["id"])
        self.task_store.upsert_remote_run(
            task_id, repository=self.settings.public_repo,
            workflow=str(prior.get("workflow") or self.settings.workflow), run_id=run_id,
            attempt=attempt, issue_number=issue_number, artifact_id=artifact_id,
            remote_state="downloading_result", input_hash=input_hash,
            finished_at=time.time(),
        )
        raw = self._transient_retry(
            lambda: self.github.download_artifact_file(self.settings.public_repo, artifact_id, "result.box.json")
        )
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubRemoteError("result artifact does not contain valid JSON") from exc
        result = open_result(
            envelope, self.credentials.load_secret(result_secret_name),
            self.settings.worker_signing_public_key,
            expected_task_id=task_id, expected_input_hash=input_hash,
        )
        progress("remote_import", None, "Importing the reattached verified result")
        self._record_result_notices(task_id, result)
        if expected_attempt is not None:
            terminal = self._get_verified_run(run_id)
            if (
                int(terminal.get("id") or 0) != run_id
                or int(terminal.get("run_attempt") or 0) != int(expected_attempt)
                or str(terminal.get("status") or "") != "completed"
                or str(terminal.get("conclusion") or "") != "success"
                or str(terminal.get("event") or "") != "workflow_dispatch"
                or str(prior.get("workflow") or "") != self.settings.workflow
            ):
                raise GitHubRemoteError("existing rerun binding drifted before import")
        import_result(result)
        self.task_store.upsert_remote_run(
            task_id, repository=self.settings.public_repo,
            workflow=str(prior.get("workflow") or self.settings.workflow), run_id=run_id,
            attempt=attempt, issue_number=issue_number, artifact_id=artifact_id,
            remote_state="imported", input_hash=input_hash, imported_at=time.time(),
            last_error="",
        )
        if delete_result_secret:
            self.credentials.delete_secret(result_secret_name)
        self.task_store.upsert_remote_attempt(
            task_id, attempt, repository=self.settings.public_repo,
            workflow=str(prior.get("workflow") or self.settings.workflow), run_id=run_id,
            github_status="completed", conclusion="success", artifact_id=artifact_id,
            import_state="imported", cleanup_state="pending", observed_at=time.time(),
        )
        self._cleanup_imported_job(
            task_id=task_id, attempt=attempt, run_id=run_id,
            issue_number=issue_number, artifact_id=artifact_id,
        )
        return result

    def cleanup_canceled_run(self, task_id: str) -> dict[str, Any]:
        """Idempotently remove encrypted state for a confirmed canceled run."""
        prior = self.task_store.get_remote_run(task_id) or {}
        run_id = int(prior.get("run_id") or 0)
        attempt = max(1, int(prior.get("attempt") or 1))
        issue_number = int(prior.get("issue_number") or 0)
        if not run_id:
            raise GitHubRemoteError("canceled run has no durable run identifier")
        try:
            run = self._get_verified_run(run_id)
        except GitHubRemoteError:
            # A run dispatched before a mirror pin update can never verify
            # against the new head; the raw read still proves cancellation.
            run = self.github.get_run(self.settings.public_repo, run_id)
        status = str(run.get("status") or "")
        conclusion = str(run.get("conclusion") or "")
        if status != "completed" or conclusion not in {"cancelled", "canceled"}:
            raise GitHubRemoteError("remote run is not confirmed canceled")

        result_secret_name = f"remote_result_private:{task_id}"
        self.credentials.delete_secret(result_secret_name)
        try:
            for artifact in self.github.list_run_artifacts(
                self.settings.public_repo, run_id
            ):
                name = str(artifact.get("name") or "")
                if name == f"courselens-result-{task_id}" or name.startswith(
                    f"courselens-checkpoint-{task_id}-"
                ):
                    self.github.delete_artifact(
                        self.settings.public_repo, int(artifact["id"])
                    )
            if issue_number:
                self.github.cleanup_job(self.settings.private_repo, issue_number)
        except Exception as exc:
            self.task_store.upsert_remote_attempt(
                task_id, attempt, repository=self.settings.public_repo,
                workflow=str(prior.get("workflow") or self.settings.workflow),
                run_id=run_id, github_status="completed", conclusion="cancelled",
                import_state="canceled", cleanup_state="pending",
                error_code=self._safe_error_code(exc), observed_at=time.time(),
            )
            self.task_store.upsert_remote_run(
                task_id, repository=self.settings.public_repo,
                workflow=str(prior.get("workflow") or self.settings.workflow),
                run_id=run_id, attempt=attempt, issue_number=issue_number or None,
                remote_state="canceling", last_error="remote_cleanup_pending",
            )
            raise GitHubRemoteError("canceled run cleanup remains pending") from exc

        self.task_store.upsert_remote_attempt(
            task_id, attempt, repository=self.settings.public_repo,
            workflow=str(prior.get("workflow") or self.settings.workflow),
            run_id=run_id, github_status="completed", conclusion="cancelled",
            import_state="canceled", cleanup_state="complete", error_code="",
            observed_at=time.time(),
        )
        self.task_store.upsert_remote_run(
            task_id, repository=self.settings.public_repo,
            workflow=str(prior.get("workflow") or self.settings.workflow),
            run_id=run_id, attempt=attempt, issue_number=issue_number or None,
            remote_state="canceled", finished_at=time.time(), last_error="",
        )
        return {"run_id": run_id, "attempt": attempt, "cleanup_state": "complete"}

    def retry_imported_cleanup(
        self, task_id: str, *, retain_result_secret: bool = False
    ) -> dict[str, Any]:
        """Idempotently finish cleanup after a result was already imported."""
        prior = dict(self.task_store.get_remote_run(task_id) or {})
        if str(prior.get("remote_state") or "") != "imported":
            raise GitHubRemoteError("remote result is not in imported cleanup state")
        run_id = int(prior.get("run_id") or 0)
        issue_number = int(prior.get("issue_number") or 0)
        artifact_id = int(prior.get("artifact_id") or 0)
        attempt = max(1, int(prior.get("attempt") or 1))
        if not run_id or not issue_number or not artifact_id:
            raise GitHubRemoteError("imported cleanup metadata is incomplete")
        self._cleanup_imported_job(
            task_id=task_id,
            attempt=attempt,
            run_id=run_id,
            issue_number=issue_number,
            artifact_id=artifact_id,
        )
        current = dict(self.task_store.get_remote_attempt(task_id, attempt) or {})
        if str(current.get("cleanup_state") or "") != "complete":
            raise GitHubRemoteError("imported result cleanup remains pending")
        if not retain_result_secret:
            self.credentials.delete_secret(f"remote_result_private:{task_id}")
        return {"run_id": run_id, "attempt": attempt, "cleanup_state": "complete"}

    def _cleanup_imported_job(
        self, *, task_id: str, attempt: int, run_id: int,
        issue_number: int, artifact_id: int,
    ) -> None:
        """Clean encrypted cloud state without rolling back a committed import."""
        try:
            self.github.delete_artifact(self.settings.public_repo, artifact_id)
            if run_id:
                for checkpoint_artifact in self.github.list_run_artifacts(
                    self.settings.public_repo, run_id
                ):
                    if str(checkpoint_artifact.get("name") or "").startswith(
                        f"courselens-checkpoint-{task_id}-"
                    ):
                        self.github.delete_artifact(
                            self.settings.public_repo, int(checkpoint_artifact["id"])
                        )
            self.github.cleanup_job(self.settings.private_repo, issue_number)
        except Exception as exc:
            self.task_store.upsert_remote_attempt(
                task_id, attempt, repository=self.settings.public_repo,
                workflow=self.settings.workflow, run_id=run_id,
                github_status="completed", conclusion="success", artifact_id=artifact_id,
                import_state="imported", cleanup_state="pending",
                error_code=self._safe_error_code(exc), observed_at=time.time(),
            )
            self.task_store.upsert_remote_run(
                task_id, repository=self.settings.public_repo,
                workflow=self.settings.workflow, run_id=run_id, attempt=attempt,
                issue_number=issue_number, artifact_id=artifact_id,
                remote_state="imported", last_error="remote_cleanup_pending",
            )
            return
        self.task_store.upsert_remote_attempt(
            task_id, attempt, repository=self.settings.public_repo,
            workflow=self.settings.workflow, run_id=run_id,
            github_status="completed", conclusion="success", artifact_id=artifact_id,
            import_state="imported", cleanup_state="complete", error_code="",
            observed_at=time.time(),
        )
        self.task_store.upsert_remote_run(
            task_id,
            repository=self.settings.public_repo,
            workflow=self.settings.workflow,
            run_id=run_id,
            attempt=attempt,
            issue_number=issue_number,
            artifact_id=artifact_id,
            remote_state="imported",
            last_error="",
        )

    @staticmethod
    def _safe_error_code(exc: BaseException) -> str:
        name = type(exc).__name__.lower()
        text = str(exc).lower()
        if "429" in text or "rate" in text and "limit" in text:
            return "rate_limited"
        if "401" in text:
            return "authorization_revoked"
        if "403" in text:
            return "permission_denied"
        if "404" in text:
            return "resource_missing"
        if "timeout" in text:
            return "timeout"
        if "signature" in text or "hash" in text or "mismatch" in text:
            return "integrity_rejected"
        return name[:80] or "remote_error"

    def _wait_until_started(self, task_id: str, run_id: int, cancel, progress) -> None:
        deadline = time.monotonic() + self.settings.handshake_timeout_seconds
        while time.monotonic() < deadline:
            if cancel():
                if self._wait_for_cancel_confirmation(task_id, run_id):
                    raise RemoteTaskPaused("remote task canceled before authorization")
                raise GitHubRemoteError("runner completed before authorization while cancellation was pending")
            run = self._get_verified_run(run_id)
            status = str(run.get("status") or "")
            attempt = max(1, int(run.get("run_attempt") or 1))
            self.task_store.upsert_remote_attempt(
                task_id, attempt, repository=self.settings.public_repo,
                workflow=self.settings.workflow, run_id=run_id,
                github_status=status or "unknown",
                conclusion=str(run.get("conclusion") or ""), observed_at=time.time(),
            )
            if status == "in_progress":
                get_run_jobs = getattr(self.github, "get_run_jobs", None)
                if get_run_jobs is None:
                    return
                jobs = self._transient_retry(
                    lambda: get_run_jobs(self.settings.public_repo, run_id)
                )
                ready = any(
                    str(step.get("status") or "") == "in_progress"
                    and str(step.get("name") or "").startswith("Process encrypted")
                    for job in jobs
                    for step in (job.get("steps") or [])
                )
                if ready:
                    return
                progress("remote_queue", None, "Runner allocated; preparing the compute environment")
            if status == "completed":
                raise GitHubRemoteError(f"runner stopped before authorization ({run.get('conclusion')})")
            progress("remote_queue", None, "Waiting for an available GitHub runner")
            time.sleep(self.settings.poll_seconds)
        self.github.cancel_run(self.settings.public_repo, run_id)
        raise GitHubRemoteError("runner did not start before the authorization timeout")

    def _wait_for_cancel_confirmation(self, task_id: str, run_id: int) -> bool:
        """Return true only after GitHub confirms cancellation; success wins the race."""
        self.github.cancel_run(self.settings.public_repo, run_id)
        remote = self.task_store.get_remote_run(task_id) or {}
        attempt = max(1, int(remote.get("attempt") or 1))
        self.task_store.upsert_remote_run(
            task_id,
            repository=str(remote.get("repository") or self.settings.public_repo),
            workflow=str(remote.get("workflow") or self.settings.workflow),
            run_id=run_id, attempt=attempt, remote_state="canceling",
        )
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            run = self._get_verified_run(run_id)
            status = str(run.get("status") or "")
            conclusion = str(run.get("conclusion") or "")
            attempt = max(1, int(run.get("run_attempt") or attempt))
            self.task_store.upsert_remote_attempt(
                task_id, attempt, repository=self.settings.public_repo,
                workflow=self.settings.workflow, run_id=run_id,
                github_status=status or "unknown", conclusion=conclusion,
                observed_at=time.time(),
            )
            if status == "completed":
                if conclusion in {"cancelled", "canceled"}:
                    return True
                if conclusion == "success":
                    return False
                raise GitHubRemoteError(
                    f"GitHub runner concluded with {conclusion or 'unknown'} during cancellation"
                )
            time.sleep(self.settings.poll_seconds)
        raise GitHubRemoteError("cancel confirmation timeout")

    def _wait_until_complete(
        self,
        task_id: str,
        run_id: int,
        issue_number: int,
        result_secret_name: str,
        input_hash: str,
        cancel,
        progress,
        *,
        expected_attempt: int | None = None,
    ) -> None:
        remote = self.task_store.get_remote_run(task_id) or {}
        attempt = max(1, int(remote.get("attempt") or 1))
        saved_attempt = self.task_store.get_remote_attempt(task_id, attempt) or {}
        last_control_sequence = max(0, int(saved_attempt.get("last_control_sequence") or 0))
        # N11：每次进入长等待都从基线重新爬梯（上一次等待成功收口即回落基线）。
        poll_round = 0
        while True:
            if cancel():
                if self._wait_for_cancel_confirmation(task_id, run_id):
                    raise RemoteTaskPaused("remote task canceled")
                return
            if expected_attempt is not None:
                run = self._get_verified_run(run_id)
                if int(run.get("run_attempt") or 0) != int(expected_attempt):
                    raise GitHubRemoteError("existing rerun attempt drifted while waiting")
            read_controls = getattr(self.github, "read_controls", None)
            controls = self._transient_retry(
                lambda: read_controls(
                    self.settings.private_repo,
                    issue_number,
                    after_sequence=last_control_sequence,
                )
            ) if read_controls is not None else []
            signed_progress_received = False
            for sequence, envelope in controls:
                control = open_control(
                    envelope,
                    self.credentials.load_secret(result_secret_name),
                    self.settings.worker_signing_public_key,
                    expected_task_id=task_id,
                    expected_input_hash=input_hash,
                )
                signed_sequence = int(control.get("sequence") or 0)
                if signed_sequence != int(sequence) or signed_sequence <= last_control_sequence:
                    continue
                last_control_sequence = max(last_control_sequence, sequence)
                control_kind = str(control.get("control_kind") or "")
                payload = dict(control.get("payload") or {})
                if control_kind == "checkpoint":
                    checkpoint = dict(payload.get("checkpoint") or {})
                    if checkpoint:
                        self.task_store.upsert_remote_run(
                            task_id,
                            repository=self.settings.public_repo,
                            workflow=self.settings.workflow,
                            run_id=run_id,
                            remote_state="running",
                            checkpoint=checkpoint,
                        )
                elif control_kind == "progress":
                    stage = str(payload.get("stage") or "remote_compute")
                    worker_status = str(payload.get("status") or "running")
                    completed = payload.get("completed")
                    total = payload.get("total")
                    try:
                        completed_value = max(0, int(completed)) if completed is not None else None
                        total_value = max(0, int(total)) if total is not None else None
                    except (TypeError, ValueError):
                        completed_value = total_value = None
                    percent = (
                        min(100.0, completed_value * 100.0 / total_value)
                        if completed_value is not None and total_value
                        else None
                    )
                    error_code = str(payload.get("error_code") or "")[:80]
                    self.task_store.upsert_remote_attempt(
                        task_id, attempt, repository=self.settings.public_repo,
                        workflow=self.settings.workflow, run_id=run_id,
                        github_status="in_progress", worker_status=worker_status,
                        worker_stage=stage, completed=completed_value, total=total_value,
                        last_control_sequence=last_control_sequence,
                        last_heartbeat_at=time.time(), error_code=error_code,
                        observed_at=time.time(),
                    )
                    progress(stage, percent, "GitHub runner reported signed progress")
                    signed_progress_received = True
            run = self._get_verified_run(run_id)
            if (
                expected_attempt is not None
                and int(run.get("run_attempt") or 0) != int(expected_attempt)
            ):
                raise GitHubRemoteError("existing rerun attempt drifted while waiting")
            github_status = str(run.get("status") or "")
            attempt = max(1, int(run.get("run_attempt") or attempt))
            self.task_store.upsert_remote_attempt(
                task_id, attempt, repository=self.settings.public_repo,
                workflow=self.settings.workflow, run_id=run_id,
                github_status=github_status or "unknown",
                conclusion=str(run.get("conclusion") or ""), observed_at=time.time(),
            )
            if github_status == "completed":
                conclusion = str(run.get("conclusion") or "")
                if conclusion != "success":
                    # B3 词汇对齐：最后一次签名进度里的 worker 闭集码随失败
                    # 上抛（如 platform_challenge_required），让任务失败面
                    # 拿得到可指引的细粒度原因；没有签名证据时保持为空。
                    latest = self.task_store.get_remote_attempt(task_id, attempt) or {}
                    code = str(latest.get("error_code") or "")[:80]
                    if not code and self._runner_lost_signature(run_id, conclusion):
                        # ⑫（LOG1）：步级 cancelled + run 级 failure 且无任何
                        # worker 签名码 = 托管 runner 被 infra 中途杀掉，
                        # 不是学生操作问题。给专属闭集码，触发客户端有界
                        # 自动重试，而不是让学生反复手动点。
                        code = "remote_runner_lost"
                    raise GitHubRemoteError(
                        f"GitHub runner concluded with {conclusion or 'unknown'}",
                        code=code,
                    )
                return
            latest_attempt = self.task_store.get_remote_attempt(task_id, attempt) or {}
            last_heartbeat = float(latest_attempt.get("last_heartbeat_at") or 0.0)
            if not signed_progress_received and (not last_heartbeat or time.time() - last_heartbeat > 20.0):
                progress("remote_compute", None, "Runner is active; awaiting signed progress")
            poll_round += 1
            self._backoff_sleep(self._long_wait_poll_delay(poll_round), cancel)

    def _long_wait_poll_delay(self, consecutive_rounds: int) -> float:
        """N11：运行态长等待的轮询退避梯（基线→基线×2→…→帽）。

        consecutive_rounds=1（本轮刚进入等待）返回基线 poll_seconds；此后
        每轮翻倍直到 _POLL_BACKOFF_CAP_SECONDS。用户显式调大 poll_seconds
        时帽跟随抬高，绝不把用户配置收得比基线更小。逐轮翻倍（与
        _transient_retry 同风格）在帽处自然饱和，不依赖大指数幂。下限
        0.001s 仅防 0/负值空转，不改变任何现行配置（含测试快轮询）的 pacing。
        """
        base = max(float(self.settings.poll_seconds), 0.001)
        cap = max(base, _POLL_BACKOFF_CAP_SECONDS)
        delay = base
        for _ in range(max(0, int(consecutive_rounds) - 1)):
            delay = min(delay * 2.0, cap)
        return delay

    def _backoff_sleep(self, total_seconds: float, cancel) -> None:
        """N11：退避睡眠，但保持本地取消的即时响应。

        分片睡眠（每片 ≤ 基线间隔）：student 点「取消」后最迟一个基线间隔
        内仍会被循环顶部的 cancel() 检查接住（与恒定 3s 时代的取消延迟一致），
        而 API 轮询节奏照常退避——取消响应性不因省配额而回退。
        """
        deadline = time.monotonic() + max(0.0, float(total_seconds))
        slice_seconds = max(float(self.settings.poll_seconds), 0.001)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if cancel():
                return
            time.sleep(min(remaining, slice_seconds))

    def _runner_lost_signature(self, run_id: int, conclusion: str) -> bool:
        """⑫（LOG1 定案签名）：run 级 failure + 步级 cancelled = runner 被杀。

        托管 runner 中途失联时，工作流 run 结论是 failure，而其步骤停在
        cancelled（VM 没了，不是步骤自身失败）。worker 签名码缺席排除
        「worker 主动报错后被取消」的正常路径。任何读取故障一律按否处理
        （宁缺勿错：该码会触发自动重试）。
        """
        if conclusion != "failure":
            return False
        get_run_jobs = getattr(self.github, "get_run_jobs", None)
        if get_run_jobs is None:
            return False
        try:
            jobs = get_run_jobs(self.settings.public_repo, run_id)
        except Exception:
            return False
        job_failed = any(str(job.get("conclusion") or "") == "failure" for job in jobs)
        step_cancelled = any(
            str(step.get("conclusion") or "") == "cancelled"
            for job in jobs
            for step in (job.get("steps") or [])
        )
        return job_failed and step_cancelled

    def _dispatch_workflow_with_retry(
        self,
        task_id: str,
        progress: Callable[[str, float | None, str], None],
    ) -> DispatchResult:
        """Dispatch the workflow POST under the worker-pacer family discipline.

        Transient failures retry with bounded exponential backoff and jitter
        (closed set: network class, 403/429, 5xx; semantic 4xx never retry).
        Before any re-POST whose landing is unconfirmed, the unique-run probe
        adopts a dispatch that landed without its response, so a retry can
        never create a second run for one task.
        """
        attempts = max(1, int(self.settings.dispatch_retry_attempts))
        gate = time.monotonic() + max(0.0, float(self.settings.dispatch_retry_seconds))
        for try_number in range(1, attempts + 1):
            try:
                return self.github.dispatch_workflow(
                    self.settings.public_repo,
                    workflow=self.settings.workflow,
                    ref=self.settings.ref,
                    task_id=task_id,
                    protocol_version=PROTOCOL_VERSION,
                    expected_head_sha=self.settings.expected_worker_commit,
                )
            except GitHubRemoteError as exc:
                kind, status = self._classify_dispatch_failure(exc)
                retryable = kind == "network" or (
                    kind == "http_status" and status in _DISPATCH_RETRYABLE_STATUS_CODES
                )
                if not retryable or try_number >= attempts or time.monotonic() >= gate:
                    self._record_dispatch_event(
                        task_id, "dispatch_workflow", try_number=try_number,
                        outcome="exhausted", failure_class=kind, http_status=status,
                    )
                    raise
                wait = self._retry_wait_seconds(try_number)
                self._record_dispatch_event(
                    task_id, "dispatch_workflow", try_number=try_number,
                    outcome="retry", failure_class=kind, http_status=status,
                    wait_seconds=round(wait, 2),
                )
                progress(
                    "remote_queue", None,
                    f"GitHub briefly refused the dispatch; retrying automatically"
                    f" (attempt {try_number + 1} of {attempts})",
                )
                time.sleep(wait)
                if kind == "http_status" and status in {403, 429}:
                    # An explicit rejection means the request was never
                    # enqueued, so a plain re-POST cannot duplicate a run.
                    continue
                conclusive, recovered = self._adopt_landed_dispatch(task_id)
                if kind == "network" and conclusive and recovered is None:
                    # Run listings can lag the write behind a user proxy; give
                    # the ambiguous POST one more beat before concluding that
                    # it never landed.
                    time.sleep(min(1.5, max(0.0, gate - time.monotonic())))
                    conclusive, recovered = self._adopt_landed_dispatch(task_id)
                if recovered is not None:
                    self._record_dispatch_event(
                        task_id, "dispatch_workflow", try_number=try_number,
                        outcome="adopted",
                    )
                    return recovered
                if not conclusive:
                    # Stale or unreadable run inventory: a blind re-POST could
                    # duplicate a landed run, so degrade through the normal
                    # failure funnel instead of guessing.
                    self._record_dispatch_event(
                        task_id, "dispatch_workflow", try_number=try_number,
                        outcome="exhausted", failure_class=kind, http_status=status,
                        probe_inconclusive=True,
                    )
                    raise
        raise GitHubRemoteError("dispatch retry loop did not produce a run")

    def _publish_job_with_retry(
        self,
        task_id: str,
        envelope: dict[str, Any],
        progress: Callable[[str, float | None, str], None],
    ) -> dict[str, Any]:
        """Publish the encrypted mailbox POSTs under the same family discipline.

        ``publish_job_once`` is resume-by-design — it adopts the existing
        issue, validates every landed part byte-for-byte, and fills only the
        missing indices — so re-entering it after a transient failure is safe.
        Semantic conflicts carry no retryable marker and fail closed at once.
        """
        attempts = max(1, int(self.settings.dispatch_retry_attempts))
        gate = time.monotonic() + max(0.0, float(self.settings.dispatch_retry_seconds))
        for try_number in range(1, attempts + 1):
            try:
                return self.github.publish_job(self.settings.private_repo, task_id, envelope)
            except GitHubRemoteError as exc:
                kind, status = self._classify_dispatch_failure(exc)
                retryable = kind == "network" or (
                    kind == "http_status" and status in _DISPATCH_RETRYABLE_STATUS_CODES
                )
                if not retryable or try_number >= attempts or time.monotonic() >= gate:
                    self._record_dispatch_event(
                        task_id, "publish_job", try_number=try_number,
                        outcome="exhausted", failure_class=kind, http_status=status,
                    )
                    raise
                wait = self._retry_wait_seconds(try_number)
                self._record_dispatch_event(
                    task_id, "publish_job", try_number=try_number,
                    outcome="retry", failure_class=kind, http_status=status,
                    wait_seconds=round(wait, 2),
                )
                progress(
                    "awaiting_payload", None,
                    f"The encrypted payload hit a transient publish failure;"
                    f" retrying automatically (attempt {try_number + 1} of {attempts})",
                )
                time.sleep(wait)
        raise GitHubRemoteError("publish retry loop did not produce a mailbox")

    def _adopt_landed_dispatch(self, task_id: str) -> tuple[bool, DispatchResult | None]:
        """Probe whether an ambiguous dispatch POST actually created its run.

        Returns ``(conclusive, recovered)``: ``recovered`` carries the adopted
        unique run when the dispatch landed; ``conclusive`` is False when the
        run inventory is stale, unreadable, or ambiguous — the caller must
        never guess past it.
        """
        find = getattr(self.github, "find_workflow_run", None)
        if find is None:
            return False, None
        try:
            recovered = find(
                self.settings.public_repo,
                workflow=self.settings.workflow,
                ref=self.settings.ref,
                task_id=task_id,
                expected_head_sha=self.settings.expected_worker_commit,
            )
        except GitHubRemoteError:
            return False, None
        return True, recovered

    @staticmethod
    def _classify_dispatch_failure(exc: BaseException) -> tuple[str, int]:
        """Closed-set triage of one dispatch-POST failure.

        ``("network", 0)`` marks a requests-layer failure with no HTTP
        verdict, ``("http_status", code)`` an explicit GitHub rejection, and
        ``("", 0)`` a semantic error (conflict/validation families) that must
        never retry.
        """
        status = getattr(exc, "status", None)
        if isinstance(status, int):
            return "http_status", status
        text = str(exc)
        match = _DISPATCH_HTTP_STATUS_RE.search(text)
        if match:
            return "http_status", int(match.group(1))
        if " failed: " in text:
            return "network", 0
        return "", 0

    @staticmethod
    def _retry_wait_seconds(try_number: int) -> float:
        """Exponential backoff with jitter for the dispatch retry family."""
        index = min(max(0, int(try_number) - 1), len(_DISPATCH_RETRY_WAITS_SECONDS) - 1)
        base = _DISPATCH_RETRY_WAITS_SECONDS[index]
        return base * random.uniform(1.0 - _DISPATCH_RETRY_JITTER, 1.0 + _DISPATCH_RETRY_JITTER)

    def _record_dispatch_event(self, task_id: str, phase: str, **fields: Any) -> None:
        """Best-effort structured retry evidence (closed-set metadata only)."""
        try:
            self.task_store.append_remote_event(
                "remote-dispatch", str(task_id), {"phase": str(phase), **fields}
            )
        except Exception:
            pass

    def _transient_retry(self, call: Callable[[], Any]) -> Any:
        """Retry one read-only GitHub call through bounded transient failures.

        Polling and artifact downloads run behind a user proxy; a single
        connection blip must not fail a healthy remote run.
        """
        budget = max(0.0, float(self.settings.transient_retry_seconds))
        deadline = time.monotonic() + budget
        delay = 1.0
        while True:
            try:
                return call()
            except GitHubRemoteError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2.0, 10.0)

    def _get_verified_run(self, run_id: int) -> dict[str, Any]:
        run = self._transient_retry(
            lambda: self.github.get_run(self.settings.public_repo, run_id)
        )
        expected = self.settings.expected_worker_commit
        actual = str(run.get("head_sha") or "").strip().lower()
        if expected and actual != expected:
            raise GitHubRemoteError("worker_run_head_sha_mismatch")
        return run

    def _find_result_artifact(self, run_id: int, task_id: str) -> dict[str, Any]:
        expected = f"courselens-result-{task_id}"
        artifacts = self.github.list_run_artifacts(self.settings.public_repo, run_id)
        match = next((item for item in artifacts if item.get("name") == expected and not item.get("expired")), None)
        if match is None:
            raise GitHubRemoteError("successful workflow did not publish its encrypted result")
        return match

    def _capture_checkpoint(
        self,
        run_id: int,
        task_id: str,
        *,
        result_secret_name: str,
        expected_input_hash: str,
    ) -> dict[str, Any]:
        if not expected_input_hash or not self.credentials.has_secret(result_secret_name):
            return {}
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            run = self._get_verified_run(run_id)
            if str(run.get("status") or "") == "completed":
                break
            time.sleep(self.settings.poll_seconds)
        artifacts = self.github.list_run_artifacts(self.settings.public_repo, run_id)
        candidates = [
            item for item in artifacts
            if str(item.get("name") or "").startswith(f"courselens-checkpoint-{task_id}-")
            and not item.get("expired")
        ]
        if not candidates:
            return {}
        artifact = max(candidates, key=lambda item: int(item.get("id") or 0))
        files = self.github.download_artifact_files(
            self.settings.public_repo, int(artifact["id"])
        )
        for name in sorted(files, reverse=True):
            if not name.endswith(".box.json"):
                continue
            try:
                envelope = json.loads(files[name].decode("utf-8"))
                result = open_result(
                    envelope,
                    self.credentials.load_secret(result_secret_name),
                    self.settings.worker_signing_public_key,
                    expected_task_id=task_id,
                    expected_input_hash=expected_input_hash,
                )
                checkpoint = dict(dict(result.get("outputs") or {}).get("checkpoint") or {})
                if checkpoint:
                    self.github.delete_artifact(
                        self.settings.public_repo, int(artifact["id"])
                    )
                    return checkpoint
            except (ValueError, UnicodeDecodeError):
                continue
        return {}
