"""Offline synthetic tests for the developer first-install reset CLI.

Every test runs against a temporary data directory under the gitignored
runtime/cache root, a dict-backed DPAPI-shaped credential store, the real
TaskStore on a throwaway SQLite database, and a scripted fake of the GitHub
App client surface.  No test touches the network, the real credential
store, or ``runtime/data``.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from credentials import CredentialStore
from src.remote.github_app import GitHubAppError
from src.runtime.task_store import TaskStore
from scripts.developer_first_install_reset import (
    BINDING_SECRET,
    DeveloperResetEngine,
    HardStop,
    ReauthRequired,
    collect_zero_state,
    load_binding,
    main,
    run_reset,
)

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "runtime" / "cache"

FAKE_TOKEN = "fake-access-token-marker"
FAKE_REFRESH = "fake-refresh-token-marker"
FAKE_BOX_KEY = "fake-box-public-key-marker"
WORKER_NAME = "dev/Fudan-CourseLens-Worker"
MAILBOX_NAME = "dev/Fudan-CourseLens-Mailbox"
WORKER_ID = 3001
MAILBOX_ID = 3002
INSTALLATION_ID = 777
OWNER = "dev"
OWNER_ACCOUNT_ID = 1001
OPERATION_ID = "reset-op-20260908"


class FakeRemote:
    """Scripted stand-in for the GitHub API surface the reset touches."""

    def __init__(self):
        self.authorized = True
        self.deleted_log: list[tuple[str, int]] = []
        self.repos: dict[str, dict] = {
            WORKER_NAME: self._repo(WORKER_NAME, WORKER_ID, private=False),
            MAILBOX_NAME: self._repo(MAILBOX_NAME, MAILBOX_ID, private=True),
        }
        self.env_secrets: dict[str, set[str]] = {WORKER_NAME: set()}
        self.workflow_runs: dict[str, list[dict]] = {WORKER_NAME: []}
        self.artifacts: dict[str, list[dict]] = {WORKER_NAME: []}
        self.issues: dict[str, list[dict]] = {MAILBOX_NAME: []}
        self.rules: list[tuple[tuple[str, str], Exception]] = []
        self.recreate_after_delete: dict[str, int] = {}

    @staticmethod
    def _repo(full_name: str, repo_id: int, *, private: bool) -> dict:
        return {
            "id": repo_id,
            "full_name": full_name,
            "owner": {"login": full_name.split("/")[0], "id": OWNER_ACCOUNT_ID},
            "private": private,
            "description": "Managed by Fudan CourseLens desktop client",
            "archived": False,
            "disabled": False,
            "default_branch": "main",
        }

    def arm(self, method: str, path_part: str, exc: Exception) -> None:
        self.rules.append(((method, path_part), exc))

    def _match(self, method: str, path: str) -> Exception | None:
        for index, ((want_method, part), exc) in enumerate(self.rules):
            if want_method == method and part in path:
                self.rules.pop(index)
                return exc
        return None

    def access_token(self, *, minimum_lifetime_seconds: int = 900, no_refresh: bool = False) -> str:
        if not self.authorized:
            raise GitHubAppError("revoked", code="authorization_revoked")
        return FAKE_TOKEN

    def _find_user_installation(self, owner: str, *, token: str = "") -> dict | None:
        if not self.authorized:
            raise GitHubAppError("revoked", code="authorization_revoked")
        if owner.casefold() == OWNER and INSTALLATION_ID:
            return {
                "id": INSTALLATION_ID,
                "app_slug": "courselens",
                "account": {"login": OWNER, "type": "User"},
                "permissions": {"actions": "write"},
            }
        return None

    #: 与真实 ``GitHubAppClient._request_external`` 同源的闭集状态映射：
    #: expected 之外的状态码必须变成同一族 closed-set 错误，而不是被镜像
    #: 静默放行。
    _STATUS_CODE_MAP = {
        401: "authorization_revoked",
        403: "permission_denied",
        404: "resource_missing",
        409: "resource_conflict",
        422: "request_rejected",
        429: "rate_limited",
    }

    @staticmethod
    def _endpoint_class_for_path(path: str) -> str:
        if path == "/user":
            return "user_identity"
        if path.startswith("/user/installations"):
            return "user_installations"
        if path.startswith("/user/repos"):
            return "user_repos"
        if path.startswith("/repos/"):
            return "repos_actions" if "/actions/" in path else "repos_detail"
        return ""

    @staticmethod
    def _enforce_expected(method: str, path: str) -> bool:
        """expected 强制的范围 = 重置引擎自有语义的面：DELETE 与仓库详情 GET。

        仓库子路径探测（commits/workflows/actions/environments…）由
        ``src/remote/worker_migration.py`` 的 signed-template gate 以
        ``expected=(200,)`` 发起；仓库删除后该探测按真客户端语义会得到
        resource_missing 并使 resume HardStop（零状态门探测已删仓库的跨包
        blocker，晨间裁决点，见结果文件 T4）。在 worker_migration 归属修复
        落地前，镜像对子路径保持旧的宽松语义。
        """
        if method == "DELETE":
            return True
        remainder = path.split("/repos/", 1)[1] if path.startswith("/repos/") else ""
        return bool(remainder) and "/" not in remainder

    def _api(self, method: str, path: str, *, token: str = "", expected=(200,), **kwargs):
        status = self._status(method, path)
        if status is None:
            exc = self._match(method, path)
            if exc is not None:
                raise exc
            raise AssertionError("unexpected fake GitHub call: " + method + " " + path)
        if status not in set(expected or ()) and self._enforce_expected(method, path):
            code = self._STATUS_CODE_MAP.get(
                status,
                "github_service_error" if status >= 500 else "github_request_failed",
            )
            raise GitHubAppError(
                "GitHub 返回 HTTP " + str(status) + "（请求 fake）",
                code=code,
                endpoint_class=self._endpoint_class_for_path(path),
            )

        class Response:
            def __init__(self, payload, code):
                self._payload = payload
                self.status_code = code

            def json(self):
                return self._payload

        payload = self._payload(method, path)
        return Response(payload, status)

    def _status(self, method: str, path: str) -> int | None:
        exc = self._match(method, path)
        if exc is not None:
            raise exc
        if path == "/user":
            return 200
        if path == "/user/installations":
            return 200
        if path.startswith("/repos/"):
            repo = path.split("/repos/", 1)[1].split("/", 1)[0]
            full = self._resolve(path)
            if method == "GET":
                return 200 if full in self.repos else 404
            if method == "DELETE":
                if full not in self.repos:
                    return 404
                record = self.repos.pop(full)
                self.deleted_log.append((full, int(record["id"])))
                self.env_secrets.pop(full, None)
                replacement_id = self.recreate_after_delete.get(full)
                if replacement_id is not None:
                    self.repos[full] = self._repo(full, replacement_id, private=bool(record["private"]))
                return 204
            del repo
            return 404
        if "/actions/workflows/" in path and path.endswith("/runs"):
            full = self._resolve(path)
            runs = self.workflow_runs.get(full)
            if runs is None:
                return 404
            return 200
        if path.endswith("/actions/artifacts"):
            full = self._resolve(path)
            return 200 if full in self.artifacts else 404
        if "/issues" in path and "/comments" not in path:
            full = self._resolve(path)
            return 200 if full in self.issues else 404
        if path.endswith("/comments"):
            return 200
        if "/environments/courselens-worker/secrets" in path:
            full = self._resolve(path)
            return 200 if full in self.env_secrets else 404
        return None

    @staticmethod
    def _resolve(path: str) -> str:
        remainder = path.split("/repos/", 1)[1]
        return remainder.split("/actions/", 1)[0].split("/issues", 1)[0].split("/environments", 1)[0]

    def _payload(self, method: str, path: str) -> object:
        if path == "/user":
            return {"login": OWNER, "id": OWNER_ACCOUNT_ID}
        if path == "/user/installations":
            if self.authorized and INSTALLATION_ID:
                return {
                    "total_count": 1,
                    "installations": [{
                        "id": INSTALLATION_ID,
                        "app_slug": "courselens",
                        "account": {"login": OWNER, "type": "User"},
                        "permissions": {"actions": "write"},
                    }],
                }
            return {"total_count": 0, "installations": []}
        if path.startswith("/repos/"):
            full = self._resolve(path)
            if "/actions/workflows/" in path and path.endswith("/runs"):
                runs = self.workflow_runs.get(full, [])
                return {"total_count": len(runs), "workflow_runs": runs}
            if path.endswith("/actions/artifacts"):
                items = self.artifacts.get(full, [])
                return {"total_count": len(items), "artifacts": items}
            if "/issues" in path and "/comments" not in path:
                return list(self.issues.get(full, []))
            if path.endswith("/comments"):
                return []
            if "/environments/courselens-worker/secrets" in path:
                names = sorted(self.env_secrets.get(full, set()))
                return {"secrets": [{"name": name} for name in names]}
            if method == "GET" and full in self.repos:
                return self.repos[full]
            return {}
        return {}

    # -- existing-primitive mirrors used by the engine ----------------------

    def list_worker_secrets(self, credentials: CredentialStore) -> list[dict]:
        repo = credentials.load_secret("github_worker_repo")
        if repo not in self.env_secrets:
            raise GitHubAppError("missing", code="resource_missing")
        return [{"name": name} for name in sorted(self.env_secrets[repo])]

    def delete_job_token(self, credentials: CredentialStore) -> None:
        if not credentials.has_secret("github_worker_repo"):
            return
        token = self.access_token(minimum_lifetime_seconds=60)
        del token
        repo = credentials.load_secret("github_worker_repo")
        self.env_secrets.setdefault(repo, set()).discard("COURSELENS_JOB_TOKEN")
        credentials.delete_secret("github_remote_token")
        credentials.delete_secret("github_job_token_cleanup_pending")

    def disconnect(self, credentials: CredentialStore) -> None:
        for name in (
            "github_app_access_token", "github_app_access_expires_at",
            "github_app_refresh_token", "github_app_refresh_expires_at",
            "github_remote_token", "github_worker_repo", "github_mailbox_repo",
            "worker_box_public_key", "worker_signing_public_key", "remote_enabled",
            "github_app_installation_id", "github_job_token_cleanup_pending",
            "github_worker_verified_tree", "github_worker_verified_manifest",
            "github_worker_dispatch_sha",
        ):
            credentials.delete_secret(name)

    def snapshot(self, credentials: CredentialStore) -> dict:
        def has(name: str) -> bool:
            return credentials.has_secret(name)

        return {
            "authorized": has("github_app_access_token"),
            "installed": has("github_app_installation_id"),
            "bootstrapped": all(
                has(name) for name in (
                    "github_worker_repo", "github_mailbox_repo",
                    "worker_box_public_key", "worker_signing_public_key",
                )
            ),
        }


class FakeGitHubApp:
    """Adapts FakeRemote to the client surface the reset engine calls."""

    def __init__(self, remote: FakeRemote, credentials: CredentialStore):
        self.remote = remote
        self.credentials = credentials

    def access_token(self, **kwargs) -> str:
        return self.remote.access_token(**kwargs)

    def _api(self, method: str, path: str, **kwargs):
        return self.remote._api(method, path, **kwargs)

    def _find_user_installation(self, owner: str, **kwargs):
        return self.remote._find_user_installation(owner, **kwargs)

    def _inspect_fresh_user_installation(
        self, owner: str, *, token: str, require_exact_repository_selection: bool
    ) -> dict:
        if not self.remote.authorized or owner.casefold() != OWNER:
            raise GitHubAppError("missing", code="installation_scope_not_exact")
        if not require_exact_repository_selection:
            raise AssertionError("reset must require exact installation selection")
        return {
            "id": INSTALLATION_ID,
            "app_slug": "courselens",
            "account": {"login": OWNER, "type": "User"},
            "permissions": {"actions": "write"},
        }

    def list_worker_secrets(self) -> list[dict]:
        return self.remote.list_worker_secrets(self.credentials)

    def delete_job_token(self) -> None:
        self.remote.delete_job_token(self.credentials)

    def disconnect(self) -> None:
        self.remote.disconnect(self.credentials)

    def snapshot(self) -> dict:
        return self.remote.snapshot(self.credentials)


class FakeApplication:
    """Mirrors the reused disconnect_github contract without the full app."""

    def __init__(self, credentials: CredentialStore, task_store: TaskStore, github_app: FakeGitHubApp):
        self.credentials = credentials
        self.task_store = task_store
        self.github_app = github_app
        self.disconnect_calls = 0
        self.closed = False

    def disconnect_github(self) -> dict:
        active_remote = any(
            str(item.get("remote_state") or "") in {
                "created", "queued", "awaiting_payload", "running", "canceling",
                "artifact_ready", "downloading_result",
            }
            for item in self.task_store.list_remote_runs(limit=100)
        )
        if self.task_store.list_remote_token_leases() or active_remote:
            raise RuntimeError("仍有远程任务持有临时授权，不能断开 GitHub")
        self.github_app.delete_job_token()
        self.github_app.disconnect()
        self.disconnect_calls += 1
        return self.github_app.snapshot()

    def _remote_configured(self) -> bool:
        app = self.github_app.snapshot()
        return bool(app.get("authorized") and app.get("bootstrapped")) or all(
            self.credentials.has_secret(name)
            for name in (
                "github_remote_token", "worker_box_public_key", "worker_signing_public_key",
            )
        )

    def close(self, timeout: float = 6.0) -> dict:
        self.closed = True
        return {}


class ResetTestCase(unittest.TestCase):
    def setUp(self) -> None:
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self._tmp.cleanup)
        self.output_dir = Path(self._tmp.name)
        self.remote = FakeRemote()
        self.credentials = CredentialStore(self.output_dir / "credentials.json")
        self.seed_bootstrapped_client()
        self.task_store = TaskStore(self.output_dir / "state.db")
        self.addCleanup(self.task_store.close)
        self.task_store.set_global_paused(True)
        self.github_app = FakeGitHubApp(self.remote, self.credentials)
        self.applications: list[FakeApplication] = []

    def seed_bootstrapped_client(self) -> None:
        for name, value in {
            "github_app_access_token": FAKE_TOKEN,
            "github_app_access_expires_at": str(0.0),
            "github_app_refresh_token": FAKE_REFRESH,
            "github_remote_token": FAKE_TOKEN,
            "github_worker_repo": WORKER_NAME,
            "github_mailbox_repo": MAILBOX_NAME,
            "worker_box_public_key": FAKE_BOX_KEY,
            "worker_signing_public_key": FAKE_BOX_KEY,
            "remote_enabled": "0",
            "github_app_installation_id": str(INSTALLATION_ID),
        }.items():
            self.credentials.save_secret(name, value)

    def application_provider(self) -> FakeApplication:
        app = FakeApplication(self.credentials, self.task_store, self.github_app)
        self.applications.append(app)
        return app

    def go_provider(self, line: str = "GO " + OPERATION_ID):
        return lambda: line

    def build_engine(self) -> DeveloperResetEngine:
        return DeveloperResetEngine(
            output_dir=self.output_dir,
            credentials=self.credentials,
            task_store_factory=lambda: TaskStore(self.output_dir / "state.db"),
            github_app=self.github_app,
            application_provider=self.application_provider,
            operation_id=OPERATION_ID,
        )


    def run_reset(self, *, execute: bool, operation_id: str = OPERATION_ID, **kwargs) -> dict:
        return run_reset(
            output_dir=self.output_dir,
            execute=execute,
            operation_id=operation_id,
            risk_pass_ref="risk-pass-2026-09-08-001",
            go_line_provider=kwargs.pop("go_line_provider", self.go_provider()),
            application_provider=kwargs.pop("application_provider", self.application_provider),
            github_app=kwargs.pop("github_app", self.github_app),
            failure_injections=kwargs.pop("failure_injections", None),
        )


class DryRunTests(ResetTestCase):
    def test_dry_run_performs_no_mutation(self) -> None:
        tracked = [self.output_dir / "credentials.json", self.output_dir / "state.db"]
        for suffix in ("-wal", "-shm"):
            tracked.append(self.output_dir / ("state.db" + suffix))
        before = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
            for path in tracked
        }
        report = self.run_reset(execute=False)
        self.assertEqual(report["status"], "dry_run_complete")
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(
            [t["repo_id"] for t in report["plan"]], [WORKER_ID, MAILBOX_ID]
        )
        after = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
            for path in tracked
        }
        self.assertEqual(after, before)
        self.assertEqual(self.remote.deleted_log, [])
        self.assertEqual(self.applications, [])
        self.assertFalse(load_binding(self.credentials))

    def test_dry_run_uses_no_refresh_and_read_only_store(self) -> None:
        calls: list[dict] = []
        original = self.github_app.access_token

        def record(**kwargs):
            calls.append(kwargs)
            return original(**kwargs)

        self.github_app.access_token = record  # type: ignore[method-assign]
        self.run_reset(execute=False)
        self.assertTrue(calls)
        self.assertTrue(all(call.get("no_refresh") is True for call in calls))

    def test_dry_run_reports_first_install_honestly(self) -> None:
        for name in list(self.credentials.list_secret_names()):
            self.credentials.delete_secret(name)
        report = self.run_reset(execute=False)
        self.assertEqual(report["status"], "already_first_install")
        self.assertTrue(report["already_first_install"])

    def query(self, sql: str):
        with self.task_store._connect() as db:
            return db.execute(sql).fetchall()


class GateTests(ResetTestCase):
    def test_execute_requires_operation_id(self) -> None:
        with self.assertRaises(HardStop) as caught:
            self.run_reset(execute=True, operation_id="")
        self.assertEqual(caught.exception.code, "operation_id_invalid")
        self.assertEqual(self.remote.deleted_log, [])

    def test_execute_requires_go_line(self) -> None:
        with self.assertRaises(HardStop) as caught:
            self.run_reset(execute=True, go_line_provider=lambda: "GO some-other-op")
        self.assertEqual(caught.exception.code, "user_go_missing")
        self.assertEqual(self.remote.deleted_log, [])

    def test_execute_requires_risk_pass_reference(self) -> None:
        with self.assertRaises(HardStop) as caught:
            run_reset(
                output_dir=self.output_dir, execute=True, operation_id=OPERATION_ID,
                risk_pass_ref="  ", go_line_provider=self.go_provider(),
                application_provider=self.application_provider, github_app=self.github_app,
            )
        self.assertEqual(caught.exception.code, "risk_pass_missing")
        self.assertEqual(self.remote.deleted_log, [])

    def test_active_local_task_blocks_execute(self) -> None:
        self.task_store.add_task("summary", "course-1", "sub-1", {}, start_paused=False)
        with self.assertRaises(HardStop) as caught:
            self.run_reset(execute=False)
        self.assertEqual(caught.exception.code, "zero_state_nonzero")
        self.assertIn("local_active_tasks_nonzero", caught.exception.blocked_reason)
        self.assertEqual(self.remote.deleted_log, [])

    def test_global_pause_off_blocks_execute(self) -> None:
        self.task_store.set_global_paused(False)
        with self.assertRaises(HardStop) as caught:
            self.run_reset(execute=False)
        self.assertEqual(caught.exception.code, "zero_state_nonzero")
        self.assertIn("global_paused_false", caught.exception.blocked_reason)

    def test_remote_enabled_flag_on_blocks_execute(self) -> None:
        self.credentials.save_secret("remote_enabled", "1")
        with self.assertRaises(HardStop) as caught:
            self.run_reset(execute=False)
        self.assertEqual(caught.exception.code, "remote_enabled_not_zero")

    def test_environment_override_blocks_execute(self) -> None:
        import os

        os.environ["FUDAN_COURSELENS_REMOTE_ENABLED"] = "1"
        try:
            with self.assertRaises(HardStop) as caught:
                self.run_reset(execute=False)
            self.assertEqual(caught.exception.code, "remote_enabled_env_override")
        finally:
            os.environ.pop("FUDAN_COURSELENS_REMOTE_ENABLED", None)

    def test_active_worker_run_blocks_execute(self) -> None:
        self.remote.workflow_runs[WORKER_NAME].append({"id": 5, "status": "in_progress"})
        with self.assertRaises(HardStop) as caught:
            self.run_reset(execute=False)
        self.assertIn("active_task_run_zero", caught.exception.blocked_reason)

    def test_unconsumed_mailbox_record_blocks_execute(self) -> None:
        self.remote.issues[MAILBOX_NAME].append({
            "number": 9,
            "title": "[courselens-job] x",
            "body": "not consumed",
            "labels": [{"name": "courselens-job"}],
            "state": "open",
            "comments": 0,
        })
        with self.assertRaises(HardStop) as caught:
            self.run_reset(execute=False)
        self.assertIn("mailbox_temporary_content_zero", caught.exception.blocked_reason)

    def test_result_key_blocks_execute(self) -> None:
        self.credentials.save_secret("remote_result_private:task1", FAKE_TOKEN)
        with self.assertRaises(HardStop) as caught:
            self.run_reset(execute=False)
        self.assertIn("temporary_credentials_nonzero", caught.exception.blocked_reason)

    def test_job_token_blocks_execute(self) -> None:
        self.remote.env_secrets[WORKER_NAME].add("COURSELENS_JOB_TOKEN")
        with self.assertRaises(HardStop) as caught:
            self.run_reset(execute=False)
        self.assertIn("temporary_job_token_zero", caught.exception.blocked_reason)

    def test_authorization_missing_blocks_execute(self) -> None:
        self.remote.authorized = False
        with self.assertRaises(HardStop) as caught:
            self.run_reset(execute=False)
        self.assertEqual(caught.exception.code, "authorization_lost")
        self.assertEqual(self.remote.deleted_log, [])


class BindingTests(ResetTestCase):
    def test_binding_created_before_any_mutation_and_read_back(self) -> None:
        order: list[str] = []
        original = self.github_app.delete_job_token

        def spy_delete() -> None:
            order.append("delete_job_token")
            original()

        self.github_app.delete_job_token = spy_delete  # type: ignore[method-assign]
        self.run_reset(execute=True)
        # The narrow primitive runs in the revocation phase and again inside
        # the reused disconnect_github; both follow the persisted binding.
        self.assertEqual(order, ["delete_job_token", "delete_job_token"])
        self.assertFalse(self.credentials.has_secret(BINDING_SECRET))

    def test_binding_survives_disconnect_cleanup(self) -> None:
        engine = self.build_engine()
        engine.phase_binding()
        binding = load_binding(self.credentials)
        self.assertEqual(binding["phase"], "binding")
        self.remote.disconnect(self.credentials)
        self.assertIsNotNone(load_binding(self.credentials))

    def test_binding_immutability_enforced(self) -> None:
        engine = self.build_engine()
        engine.phase_binding()
        binding = dict(load_binding(self.credentials) or {})
        binding["worker"] = dict(binding["worker"], repo_id=9999)
        self.credentials.update_secrets(
            {BINDING_SECRET: json.dumps(binding, sort_keys=True, separators=(",", ":"))}
        )
        tampered = load_binding(self.credentials)
        self.assertEqual(int(tampered["worker"]["repo_id"]), 9999)  # store accepted raw write
        with self.assertRaises(HardStop) as caught:
            self.build_engine().phase_binding()
        self.assertEqual(caught.exception.code, "repository_identity_drift")

    def test_operation_id_mismatch_rejected(self) -> None:
        engine = self.build_engine()
        engine.phase_binding()
        with self.assertRaises(HardStop) as caught:
            self.run_reset(execute=False, operation_id="another-op-999")
        self.assertEqual(caught.exception.code, "operation_id_mismatch")

    def test_identity_drift_detected_on_resume(self) -> None:
        engine = self.build_engine()
        engine.phase_binding()
        # The owner of a bound repository changes before the rerun.
        self.remote.repos[WORKER_NAME]["owner"]["login"] = "someone-else"
        with self.assertRaises(HardStop) as caught:
            engine.phase_binding()
        self.assertIn(
            caught.exception.code,
            {"repository_identity_drift", "repository_readback_invalid"},
        )

    def test_same_name_different_id_refuses_deletion(self) -> None:
        engine = self.build_engine()
        engine.phase_binding()
        engine.phase_revoke_temporary()
        self.remote.repos[WORKER_NAME] = FakeRemote._repo(WORKER_NAME, 4242, private=False)
        with self.assertRaises(HardStop) as caught:
            engine.phase_delete_worker()
        self.assertEqual(caught.exception.code, "repository_identity_drift")
        self.assertEqual(self.remote.deleted_log, [])

    def test_same_login_different_owner_id_refuses_deletion(self) -> None:
        engine = self.build_engine()
        engine.phase_binding()
        engine.phase_revoke_temporary()
        self.remote.repos[WORKER_NAME]["owner"]["id"] = OWNER_ACCOUNT_ID + 1
        with self.assertRaises(HardStop) as caught:
            engine.phase_delete_worker()
        self.assertEqual(caught.exception.code, "repository_identity_drift")
        self.assertEqual(self.remote.deleted_log, [])

    def test_exact_installation_selection_is_required(self) -> None:
        def reject_extra(owner: str, *, token: str, require_exact_repository_selection: bool):
            self.assertTrue(require_exact_repository_selection)
            raise GitHubAppError("extra repository", code="installation_scope_not_exact")

        self.github_app._inspect_fresh_user_installation = reject_extra  # type: ignore[method-assign]
        with self.assertRaises(HardStop) as caught:
            self.run_reset(execute=False)
        self.assertEqual(caught.exception.code, "github_unrecoverable_error")

class HappyPathTests(ResetTestCase):
    def test_full_execute_returns_completed(self) -> None:
        report = self.run_reset(execute=True)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(
            self.remote.deleted_log, [(WORKER_NAME, WORKER_ID), (MAILBOX_NAME, MAILBOX_ID)]
        )
        self.assertGreaterEqual(len(self.applications), 2)  # disconnect + verify
        self.assertEqual(
            [app.disconnect_calls for app in self.applications if app.disconnect_calls],
            [1],
        )
        self.assertEqual(self.credentials.list_secret_names(), [])
        store = TaskStore(self.output_dir / "state.db", read_only=True)
        try:
            events = store.list_remote_events(topics=["developer-first-install-reset"], limit=10)
            states = [str(event.get("payload", {}).get("state")) for event in events]
        finally:
            store.close()
        self.assertIn("complete", states)
        self.assertEqual(report["readback"]["installation_id"], INSTALLATION_ID)

    def test_isolated_data_is_quarantined_not_deleted(self) -> None:
        (self.output_dir / "learning.db").write_bytes(b"sqlite")
        report = self.run_reset(execute=True)
        self.assertEqual(report["status"], "completed")
        isolated = self.output_dir / "reset-isolation" / OPERATION_ID
        self.assertTrue((isolated / "learning.db").exists())
        self.assertFalse((self.output_dir / "learning.db").exists())

    def test_duplicate_writer_lock_blocks_second_execute(self) -> None:
        from scripts.developer_first_install_reset import OwnershipLock, HardStop

        with OwnershipLock(self.output_dir, "first-op-123456"):
            with self.assertRaises(HardStop) as caught:
                self.run_reset(execute=True)
            self.assertEqual(caught.exception.code, "writer_conflict")
        self.assertEqual(self.remote.deleted_log, [])

    def test_no_secret_material_in_report_or_stdout(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                ["--dry-run", "--data-dir", str(self.output_dir)],
                stdin=io.StringIO(""),
                application_provider=self.application_provider,
                github_app=self.github_app,
            )
        text = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        for marker in (FAKE_TOKEN, FAKE_REFRESH, FAKE_BOX_KEY):
            self.assertNotIn(marker, text)
        report = self.run_reset(execute=True)
        rendered = json.dumps(report)
        for marker in (FAKE_TOKEN, FAKE_REFRESH, FAKE_BOX_KEY):
            self.assertNotIn(marker, rendered)


class FailureInjectionTests(ResetTestCase):
    def test_unknown_delete_result_never_replays_delete_on_resume(self) -> None:
        engine = self.build_engine()
        engine.phase_binding()
        engine.phase_revoke_temporary()
        self.remote.arm("DELETE", WORKER_NAME.split("/")[1], GitHubAppError("lost", code="authorization_revoked"))
        with self.assertRaises(HardStop) as caught:
            engine.phase_delete_worker()
        self.assertEqual(caught.exception.code, "authorization_lost")
        binding = load_binding(self.credentials)
        self.assertTrue(binding["worker_delete_issued"])
        with self.assertRaises(HardStop) as caught:
            self.build_engine().phase_delete_worker()
        self.assertEqual(caught.exception.code, "worker_delete_outcome_unknown")
        self.assertEqual(self.remote.deleted_log, [])

    def test_recreated_same_name_after_delete_is_identity_drift(self) -> None:
        engine = self.build_engine()
        engine.phase_binding()
        engine.phase_revoke_temporary()
        self.remote.recreate_after_delete[WORKER_NAME] = 4242
        with self.assertRaises(HardStop) as caught:
            engine.phase_delete_worker()
        self.assertEqual(caught.exception.code, "repository_identity_drift")
        self.assertEqual(self.remote.deleted_log, [(WORKER_NAME, WORKER_ID)])
        self.assertFalse(load_binding(self.credentials)["worker_deleted"])

    def test_auth_loss_before_any_deletion_is_hard_stop(self) -> None:
        engine = self.build_engine()
        engine.phase_binding()
        engine.phase_revoke_temporary()
        self.remote.authorized = False
        with self.assertRaises(HardStop) as caught:
            engine.phase_delete_worker()
        self.assertEqual(caught.exception.code, "authorization_lost")
        self.assertEqual(self.remote.deleted_log, [])
        binding = load_binding(self.credentials)
        self.assertFalse(binding["worker_deleted"])

    def test_mfa_style_refusal_is_hard_stop(self) -> None:
        engine = self.build_engine()
        engine.phase_binding()
        engine.phase_revoke_temporary()
        self.remote.arm("DELETE", WORKER_NAME.split("/")[1], GitHubAppError("nope", code="permission_denied"))
        with self.assertRaises(HardStop) as caught:
            engine.run()
        self.assertEqual(caught.exception.code, "mfa_or_interactive_required")
        self.assertEqual(self.remote.deleted_log, [])
        self.assertIsNotNone(load_binding(self.credentials))

    def test_auth_loss_after_worker_deletion_returns_reauth_required(self) -> None:
        engine = self.build_engine()
        engine.phase_binding()
        engine.phase_revoke_temporary()
        engine.phase_delete_worker()
        self.remote.authorized = False
        with self.assertRaises(ReauthRequired) as caught:
            engine.run()
        remaining = caught.exception.remaining
        self.assertEqual(
            [(item["role"], item["repo_id"]) for item in remaining],
            [("mailbox", MAILBOX_ID)],
        )

    def test_reauth_resume_deletes_only_mailbox_forward_only(self) -> None:
        engine = self.build_engine()
        engine.phase_binding()
        engine.phase_revoke_temporary()
        engine.phase_delete_worker()
        self.remote.authorized = False
        with self.assertRaises(ReauthRequired):
            engine.run()
        # The developer re-authorizes; a fresh engine reruns against the
        # same preserved binding and must never re-delete the Worker.
        self.remote.authorized = True
        self.credentials.save_secret("github_app_access_token", FAKE_TOKEN)
        rerun = self.build_engine()
        report = rerun.run()
        self.assertEqual(report["status"], "completed")
        self.assertEqual(self.remote.deleted_log, [(WORKER_NAME, WORKER_ID), (MAILBOX_NAME, MAILBOX_ID)])
        worker_deletes = [name for name, _ in self.remote.deleted_log].count(WORKER_NAME)
        self.assertEqual(worker_deletes, 1)

    def test_interrupt_between_every_boundary_resumes_forward(self) -> None:
        points = (
            "binding:before_write",
            "revoke:after_job_token",
            "delete_worker:before_delete",
            "delete_worker:after_confirm",
            "delete_mailbox:before_delete",
            "delete_mailbox:after_confirm",
            "cleanup:after_disconnect",
            "verify:before",
        )
        for point in points:
            with self.subTest(point=point):
                self.setUp()
                boom = HardStop("injected_failure", point)

                def inject(bomb=boom):
                    raise bomb

                engine = self.build_engine()
                engine.failure_injections = {point: inject}
                with self.assertRaises(HardStop):
                    engine.run()
                # Nothing was deleted unless the injection point sits after
                # the corresponding DELETE call in the fixed order.
                resumed = self.build_engine()
                report = resumed.run()
                self.assertEqual(report["status"], "completed")
                self.assertEqual(
                    sorted(self.remote.deleted_log),
                    [(MAILBOX_NAME, MAILBOX_ID), (WORKER_NAME, WORKER_ID)],
                )
                worker_count = [name for name, _ in self.remote.deleted_log].count(WORKER_NAME)
                mailbox_count = [name for name, _ in self.remote.deleted_log].count(MAILBOX_NAME)
                self.assertLessEqual(worker_count, 2)
                self.assertEqual(mailbox_count, 1)
                self.assertEqual(self.credentials.list_secret_names(), [])

    def test_verify_challenge_failure_keeps_binding(self) -> None:
        engine = self.build_engine()
        engine.phase_binding()
        engine.phase_revoke_temporary()
        engine.phase_delete_worker()
        engine.phase_delete_mailbox()
        engine.phase_local_cleanup()
        # Residue appears after cleanup; verification must fail closed.
        self.credentials.save_secret("github_remote_token", FAKE_TOKEN)
        with self.assertRaises(HardStop) as caught:
            engine.phase_verify_first_install()
        self.assertEqual(caught.exception.code, "first_install_verification_failed")
        self.assertIn("credential_residue_present", caught.exception.blocked_reason)
        self.assertIsNotNone(load_binding(self.credentials))

    def test_rerun_after_terminal_is_honest_noop(self) -> None:
        self.run_reset(execute=True)
        report = self.run_reset(execute=False)
        self.assertEqual(report["status"], "already_first_install")


class RunResetResumeTests(ResetTestCase):
    """Interrupt/resume drills at the CLI (run_reset) level, not the engine."""

    def _boom(self, point: str):
        def inject() -> None:
            raise HardStop("injected_failure", point)
        return inject

    def test_interrupt_during_cleanup_resumes_via_run_reset(self) -> None:
        with self.assertRaises(HardStop):
            self.run_reset(
                execute=True,
                failure_injections={"cleanup:after_disconnect": self._boom("cleanup:after_disconnect")},
            )
        self.assertEqual(
            sorted(self.remote.deleted_log),
            [(MAILBOX_NAME, MAILBOX_ID), (WORKER_NAME, WORKER_ID)],
        )
        # The repo-name secrets are gone here; a dry-run must NOT claim
        # first-install, and the execute rerun must finish forward-only.
        dry = self.run_reset(execute=False)
        self.assertEqual(dry["status"], "dry_run_complete")
        report = self.run_reset(execute=True)
        self.assertEqual(report["status"], "completed")
        worker_count = [name for name, _ in self.remote.deleted_log].count(WORKER_NAME)
        mailbox_count = [name for name, _ in self.remote.deleted_log].count(MAILBOX_NAME)
        self.assertEqual((worker_count, mailbox_count), (1, 1))
        self.assertEqual(self.credentials.list_secret_names(), [])

    def test_interrupt_at_verify_resumes_via_run_reset(self) -> None:
        with self.assertRaises(HardStop):
            self.run_reset(
                execute=True,
                failure_injections={"verify:before": self._boom("verify:before")},
            )
        report = self.run_reset(execute=True)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(self.credentials.list_secret_names(), [])

    def test_mailbox_gate_survives_resume_after_worker_deletion(self) -> None:
        with self.assertRaises(HardStop):
            self.run_reset(
                execute=True,
                failure_injections={
                    "boundary:worker_confirmed": self._boom("boundary:worker_confirmed")
                },
            )
        # An unconsumed Mailbox record appears after the Worker deletion.
        self.remote.issues[MAILBOX_NAME].append({
            "number": 11,
            "title": "[courselens-job] late payload",
            "body": "not consumed",
            "labels": [{"name": "courselens-job"}],
            "state": "open",
            "comments": 0,
        })
        with self.assertRaises(HardStop) as caught:
            self.run_reset(execute=True)
        self.assertEqual(caught.exception.code, "zero_state_nonzero")
        self.assertIn("mailbox_temporary_content_zero", caught.exception.blocked_reason)
        self.assertEqual(
            [name for name, _ in self.remote.deleted_log], [WORKER_NAME]
        )


class RealApplicationTests(ResetTestCase):
    """The reuse seam proven against the genuine CourseLensApplication."""

    def test_full_execute_with_real_application_disconnect(self) -> None:
        from src.application import CourseLensApplication

        built: list[CourseLensApplication] = []

        def provider() -> CourseLensApplication:
            app = CourseLensApplication(self.output_dir)
            app.github_app = self.github_app  # keep the whole run offline
            built.append(app)
            return app

        report = run_reset(
            output_dir=self.output_dir, execute=True, operation_id=OPERATION_ID,
            risk_pass_ref="risk-pass-2026-09-08-001", go_line_provider=self.go_provider(),
            application_provider=provider, github_app=self.github_app,
        )
        self.assertEqual(report["status"], "completed")
        self.assertGreaterEqual(len(built), 2)
        self.assertEqual(
            self.remote.deleted_log, [(WORKER_NAME, WORKER_ID), (MAILBOX_NAME, MAILBOX_ID)]
        )
        self.assertEqual(self.credentials.list_secret_names(), [])

    def test_real_disconnect_auth_loss_after_deletions_is_reauth_required(self) -> None:
        from src.application import CourseLensApplication

        engine = self.build_engine()
        engine.phase_binding()
        engine.phase_revoke_temporary()
        engine.phase_delete_worker()
        engine.phase_delete_mailbox()

        def provider() -> CourseLensApplication:
            app = CourseLensApplication(self.output_dir)
            app.github_app = self.github_app
            return app

        def kill_authorization() -> None:
            self.remote.authorized = False  # delete_job_token will fail 401

        engine.failure_injections = {"cleanup:before_disconnect": kill_authorization}
        engine._application_provider = provider  # type: ignore[assignment]
        with self.assertRaises(ReauthRequired):
            engine.phase_local_cleanup()
        self.assertIsNotNone(load_binding(self.credentials))
        self.assertEqual(
            sorted(self.remote.deleted_log),
            [(MAILBOX_NAME, MAILBOX_ID), (WORKER_NAME, WORKER_ID)],
        )

    def test_real_disconnect_refusal_on_lease_is_hard_stop(self) -> None:
        from src.application import CourseLensApplication

        engine = self.build_engine()
        engine.phase_binding()
        engine.phase_revoke_temporary()
        engine.phase_delete_worker()
        # A lease appears before the disconnect phase; the real reused
        # disconnect_github must refuse and the engine must fail closed.
        def provider() -> CourseLensApplication:
            app = CourseLensApplication(self.output_dir)
            app.github_app = self.github_app
            return app

        def inject_lease() -> None:
            self.task_store.set_remote_token_lease(
                "task-live", state="active", expires_at=0.0
            )

        engine.failure_injections = {"cleanup:before_disconnect": inject_lease}
        engine._application_provider = provider  # type: ignore[assignment]
        with self.assertRaises(HardStop) as caught:
            engine.phase_local_cleanup()
        self.assertEqual(caught.exception.code, "disconnect_refused")
        self.assertEqual(
            [name for name, _ in self.remote.deleted_log], [WORKER_NAME]
        )


class ZeroStateUnitTests(ResetTestCase):
    def test_collect_zero_state_lists_every_nonzero_observation(self) -> None:
        self.task_store.set_global_paused(False)
        self.task_store.add_task("summary", "course-1", "sub-1", {}, start_paused=False)
        self.credentials.save_secret("remote_result_private:taskX", FAKE_TOKEN)
        self.remote.workflow_runs[WORKER_NAME].append({"id": 7, "status": "queued"})
        state = collect_zero_state(self.credentials, self.task_store, self.github_app)
        self.assertFalse(state["ready"])
        for expected in (
            "global_paused_false",
            "local_active_tasks_nonzero",
            "temporary_credentials_nonzero",
            "active_task_run_zero",
        ):
            self.assertIn(expected, state["failures"])


if __name__ == "__main__":
    unittest.main()
