from __future__ import annotations

import contextlib
import importlib
import io
import json
import unittest
from unittest.mock import Mock, patch

from src.remote.github_app import GitHubAppError
from src.remote.worker_migration import (
    SignedTemplateMigrationError,
    _active_task_run_count,
    _task_artifact_count,
    build_signed_template_retirement_audit,
    build_signed_template_transition_gate,
)


class Response:
    def __init__(self, value, status_code=200):
        self.value = value
        self.status_code = status_code

    def json(self):
        return self.value


class SyntheticHardExit(BaseException):
    pass


class Credentials:
    def __init__(self, *, values=None, result_keys=(), cleanup_pending=False, authorized=True):
        self.values = {
            "github_worker_repo": "student/Fudan-CourseLens-Worker",
            "github_mailbox_repo": "student/Fudan-CourseLens-Mailbox",
            **dict(values or {}),
        }
        if not authorized:
            self.values.pop("github_app_access_token", None)
        else:
            self.values.setdefault("github_app_access_token", "token")
        self.result_keys = list(result_keys)
        self.cleanup_pending = cleanup_pending
        if cleanup_pending:
            self.values["github_job_token_cleanup_pending"] = "1"

    def load_secret(self, name):
        return self.values[name]

    def has_secret(self, name):
        return name in self.values

    def list_secret_names(self, *, prefix=""):
        return [name for name in self.result_keys if name.startswith(prefix)]


class Store:
    def __init__(self, *, leases=(), cleanup_pending=0, remote_runs=None):
        self.leases = list(leases)
        self.cleanup_pending = cleanup_pending
        self.remote_runs = list(remote_runs or [])

    def list_remote_token_leases(self):
        return self.leases

    def migration_cleanup_pending_count(self):
        return self.cleanup_pending

    def list_remote_runs(self, *, limit=10):
        return self.remote_runs[: int(limit)]


class GitHubApp:
    def __init__(self, *, active=False, artifact=False, comments=False, content=False, token=False):
        self.active = active
        self.artifact = artifact
        self.comments = comments
        self.content = content
        self.token = token
        self.queried_paths = []

    def access_token(self, **_kwargs):
        return "token"

    def list_worker_secrets(self):
        return [{"name": "COURSELENS_JOB_TOKEN"}] if self.token else []

    def _api(self, _method, path, **_kwargs):
        self.queried_paths.append(path)
        if "/actions/workflows/" in path:
            active = self.active and path.endswith("/process.yml/runs")
            runs = [{"id": 1, "status": "in_progress"}] if active else []
            return Response({"total_count": len(runs), "workflow_runs": runs})
        if path.endswith("/actions/artifacts"):
            artifacts = [{"id": 9, "name": "courselens-cloud-state-history"}]
            if self.artifact:
                artifacts.append({"id": 2, "name": "courselens-result-task"})
            return Response({"artifacts": artifacts})
        if path.endswith("/issues"):
            body = "sealed" if self.content else '{"schema":"mailbox.v2","state":"consumed"}'
            return Response([{
                "number": 7, "title": "[courselens-job] task", "body": body,
                "state": "closed", "comments": 1 if self.comments else 0,
                "labels": [{"name": "courselens-job"}],
            }])
        if path.endswith("/comments"):
            return Response([{"id": 8}] if self.comments else [])
        if path.endswith("/environments/courselens-worker/secrets"):
            names = ["COURSELENS_JOB_TOKEN"] if self.token else []
            return Response({"secrets": [{"name": name} for name in names]})
        raise AssertionError(path)


class RetirementAuditTests(unittest.TestCase):
    def test_clean_personal_worker_audit_is_ready_and_queries_only_task_scopes(self):
        github = GitHubApp()
        report = build_signed_template_retirement_audit(
            credentials=Credentials(), task_store=Store(), github_app=github
        )
        self.assertTrue(report["ready"])
        self.assertEqual(report["status"], "ready")
        self.assertFalse(report["legacy_direct_state_detected"])
        self.assertTrue(report["checks"]["no_legacy_direct_state"])
        self.assertEqual(
            [path.rsplit("/", 2)[-2] for path in github.queried_paths if path.endswith("/runs")],
            ["process.yml", "echo.yml"],
        )
        self.assertEqual(sum(path.endswith("/actions/artifacts") for path in github.queried_paths), 1)

    def test_every_residual_blocks_readiness_without_exposing_payloads(self):
        github = GitHubApp(active=True, artifact=True, comments=True, content=True, token=True)
        report = build_signed_template_retirement_audit(
            credentials=Credentials(
                result_keys=["remote_result_private:task"], cleanup_pending=True
            ),
            task_store=Store(leases=[{"task_id": "task"}], cleanup_pending=2),
            github_app=github,
        )
        self.assertFalse(report["ready"])
        self.assertEqual(report["status"], "residue_present")
        observations = report["observations"]
        self.assertEqual(observations["cleanup_pending_count"], 3)
        # 原有严格零残留语义保持：closed 历史未消费正文仍是 temporary 残留。
        self.assertEqual(observations["mailbox_temporary_content_count"], 1)
        for key in (
            "active_task_run_count", "task_artifact_count", "local_token_lease_count",
            "local_result_key_count", "old_worker_temporary_token_count",
        ):
            self.assertEqual(observations[key], 1, key)
        # closed 未消费历史的评论不再计入活动评论门（元数据计数仅用于 open 载荷）。
        self.assertEqual(observations["mailbox_managed_comment_count"], 0)
        self.assertEqual(
            {key: value for key, value in observations.items() if key.endswith("_count")},
            {
                "active_task_run_count": 1, "task_artifact_count": 1,
                "mailbox_managed_comment_count": 0, "mailbox_temporary_content_count": 1,
                "mailbox_metadata_drift_count": 0, "mailbox_active_issue_missing_count": 0,
                "mailbox_history_missing_count": 0, "mailbox_consumed_open_count": 0,
                "local_token_lease_count": 1, "local_result_key_count": 1,
                "cleanup_pending_count": 3, "old_worker_temporary_token_count": 1,
            },
        )
        self.assertNotIn("sealed", repr(report))

    def test_legacy_direct_binding_audits_template_as_extra_executor(self):
        github = GitHubApp(active=True)
        # REPUBLISH-23: the signed-mirror template repo is the renamed
        # gualtier-xu/Fudan-CourseLens-Worker; a direct binding to it is the
        # legacy state this audit exists to catch.
        template_repo = "gualtier-xu/Fudan-CourseLens-Worker"
        report = build_signed_template_retirement_audit(
            credentials=Credentials(values={
                "github_worker_repo": template_repo,
                "github_app_access_token": "token",
            }),
            task_store=Store(),
            github_app=github,
        )
        self.assertTrue(report["legacy_direct_state_detected"])
        self.assertFalse(report["checks"]["no_legacy_direct_state"])
        self.assertFalse(report["ready"])
        template_runs = [
            path for path in github.queried_paths
            if path.startswith(f"/repos/{template_repo}/")
            and path.endswith("/runs")
        ]
        self.assertEqual(
            [path.rsplit("/", 2)[-2] for path in template_runs],
            ["process.yml", "echo.yml"],
            "the template repository is audited exactly once as the legacy executor",
        )

    def test_missing_authorization_yields_closed_status_without_traceback(self):
        github = GitHubApp()
        report = build_signed_template_retirement_audit(
            credentials=Credentials(authorized=False), task_store=Store(), github_app=github
        )
        self.assertFalse(report["ready"])
        self.assertEqual(report["status"], "authorization_missing")
        self.assertEqual(report["checks"], {})
        self.assertEqual(report["observations"], {})
        self.assertEqual(github.queried_paths, [])

    def test_revoked_authorization_yields_closed_status_instead_of_traceback(self):
        class RevokedGitHub(GitHubApp):
            def access_token(self, **_kwargs):
                from src.remote.github_app import GitHubAppError
                raise GitHubAppError("GitHub 授权已过期", code="authorization_revoked")

        github = RevokedGitHub()
        report = build_signed_template_retirement_audit(
            credentials=Credentials(), task_store=Store(), github_app=github
        )
        self.assertFalse(report["ready"])
        self.assertEqual(report["status"], "authorization_missing")

    def test_audit_is_read_only_over_local_envelope(self):
        calls = []

        class RecordingCredentials(Credentials):
            def save_secret(self, name, value):
                calls.append(("save", name))
                return super().save_secret(name, value)

            def delete_secret(self, name):
                calls.append(("delete", name))
                return super().delete_secret(name)

            def update_secrets(self, updates, *, deletes=()):
                calls.append(("update", tuple(sorted(updates))))
                return super().update_secrets(updates, deletes=deletes)

        github = GitHubApp()
        build_signed_template_retirement_audit(
            credentials=RecordingCredentials(), task_store=Store(), github_app=github
        )
        self.assertEqual(calls, [])

    def test_task_run_inventory_is_complete_unique_and_fail_closed(self):
        class PagedGitHub:
            def __init__(self, pages, totals):
                self.pages, self.totals = pages, totals

            def _api(self, _method, path, **kwargs):
                workflow = path.rsplit("/", 2)[-2]
                page = kwargs["params"]["page"]
                runs = self.pages.get(workflow, {}).get(page, [])
                total = self.totals.get(workflow, 0)
                if isinstance(total, list):
                    total = total[page - 1]
                return Response({"total_count": total, "workflow_runs": runs})

        for status in (None, "mystery", "completed"):
            with self.subTest(status=status):
                github = PagedGitHub({"process.yml": {1: [{"id": 1, "status": status}]}}, {"process.yml": 1})
                self.assertEqual(_active_task_run_count(github, "student/worker", "token"), int(status != "completed"))

        page = [{"id": value, "status": "completed"} for value in range(1, 101)]
        cases = (
            ({"process.yml": {1: page, 2: []}}, {"process.yml": 101}),
            ({"process.yml": {1: page, 2: [{"id": 1, "status": "completed"}]}}, {"process.yml": 101}),
            ({"process.yml": {1: page, 2: []}}, {"process.yml": [101, 100]}),
        )
        for pages, totals in cases:
            with self.subTest(pages=pages, totals=totals):
                with self.assertRaisesRegex(RuntimeError, "task_run_inventory_incomplete"):
                    _active_task_run_count(PagedGitHub(pages, totals), "student/worker", "token")

        full_pages = {
            page_number: [
                {"id": offset + value, "status": "completed"}
                for value in range(1, 101)
            ]
            for page_number, offset in enumerate(range(0, 1000, 100), start=1)
        }
        self.assertEqual(
            _active_task_run_count(
                PagedGitHub({"process.yml": full_pages}, {"process.yml": 1000}),
                "student/worker", "token",
            ),
            0,
        )
        with self.assertRaisesRegex(RuntimeError, "task_run_inventory_too_large"):
            _active_task_run_count(
                PagedGitHub({"process.yml": full_pages}, {"process.yml": 1001}),
                "student/worker", "token",
            )

    def test_migration_error_public_shape_is_unchanged(self):
        error = SignedTemplateMigrationError("some_code", actions=("retry-cleanup",))
        self.assertEqual(error.public(), {
            "status": "action_required",
            "error_code": "some_code",
            "actions": ["retry-cleanup"],
            "remote_enabled": False,
        })


class RetiredScriptCliTests(unittest.TestCase):
    RETIRED_SCRIPTS = (
        "prepare_signed_template_direct",
        "rollback_signed_template_direct",
        "finalize_signed_template_direct",
    )

    def test_retired_scripts_print_one_retirement_object_and_exit_3(self):
        for name in self.RETIRED_SCRIPTS:
            with self.subTest(script=name):
                module = importlib.import_module(f"scripts.{name}")
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    exit_code = module.main([])
                self.assertEqual(exit_code, 3)
                payload = json.loads(buffer.getvalue())
                self.assertEqual(
                    payload["schema"], "courselens.signed-template-direct-retired.v1"
                )
                self.assertEqual(payload["status"], "retired")
                self.assertIn("personal Worker", payload["guidance"])

    def test_preflight_cli_runs_retirement_audit_with_synthetic_doubles(self):
        module = importlib.import_module("scripts.signed_template_direct_preflight")
        for ready, expected_exit in ((True, 0), (False, 1)):
            with self.subTest(ready=ready):
                report = {
                    "schema": "courselens.signed-template-retirement-audit.v1",
                    "status": "ready" if ready else "authorization_missing",
                    "ready": ready,
                    "checks": {},
                    "observations": {},
                }
                audit = Mock(return_value=report)
                constructed = []
                with (
                    patch.object(module, "CredentialStore", side_effect=lambda path: constructed.append(("credentials", str(path))) or Mock()),
                    patch.object(module, "TaskStore", side_effect=lambda path, **kwargs: constructed.append(("store", str(path), kwargs)) or Mock()),
                    patch.object(module, "GitHubAppClient", side_effect=lambda credentials: constructed.append(("app", credentials)) or Mock()),
                    patch.object(module, "build_signed_template_retirement_audit", audit),
                ):
                    buffer = io.StringIO()
                    with contextlib.redirect_stdout(buffer):
                        exit_code = module.main(["--data-dir", "unused"])
                self.assertEqual(exit_code, expected_exit)
                self.assertEqual(json.loads(buffer.getvalue()), report)
                self.assertEqual(constructed[0][0], "credentials")
                self.assertEqual(constructed[1][0], "app")
                self.assertEqual(len(constructed), 2)
                self.assertIsNone(audit.call_args.kwargs["task_store"])
                self.assertEqual(
                    audit.call_args.kwargs.keys(),
                    {"credentials", "task_store", "github_app"},
                )


class DeletedRepoGitHub:
    """Reset-resume double: the bound Worker repository is already deleted."""

    def __init__(self):
        self.queried_paths = []

    def access_token(self, **_kwargs):
        return "token"

    def _api(self, _method, path, **_kwargs):
        self.queried_paths.append(path)
        if path.startswith("/repos/student/Fudan-CourseLens-Worker/"):
            return Response(None, status_code=404)
        if path.endswith("/issues"):
            return Response([])
        raise AssertionError(path)


class RefusingGitHub:
    """Non-404 refusal double: unexpected statuses must keep failing closed."""

    def __init__(self, code):
        self.code = code

    def access_token(self, **_kwargs):
        return "token"

    def _api(self, _method, _path, **_kwargs):
        raise GitHubAppError("GitHub 拒绝", code=self.code)


class DeletedRepositoryResumeGateTests(unittest.TestCase):
    """Reset-resume scenario: the gate probes a repository deleted by the
    previous run (DELETE issued, worker_deleted not yet persisted)."""

    WORKER = "student/Fudan-CourseLens-Worker"

    def test_deleted_repository_probe_counts_read_as_zero(self):
        github = DeletedRepoGitHub()
        self.assertEqual(_active_task_run_count(github, self.WORKER, "token"), 0)
        self.assertEqual(_task_artifact_count(github, self.WORKER, "token"), 0)
        self.assertIn(
            f"/repos/{self.WORKER}/actions/workflows/process.yml/runs",
            github.queried_paths,
        )

    def test_zero_gate_completes_when_worker_repository_already_deleted(self):
        github = DeletedRepoGitHub()
        report = build_signed_template_transition_gate(
            credentials=Credentials(),
            task_store=Store(),
            github_app=github,
            repositories=(self.WORKER,),
        )
        self.assertTrue(report["ready"])
        self.assertEqual(report["observations"]["executor_count"], 1)
        self.assertEqual(report["observations"]["active_task_run_count"], 0)
        self.assertEqual(report["observations"]["task_artifact_count"], 0)
        self.assertEqual(report["observations"]["temporary_job_token_count"], 0)

    def test_live_repository_probes_keep_original_200_semantics(self):
        github = GitHubApp(active=True, artifact=True)
        self.assertEqual(_active_task_run_count(github, self.WORKER, "token"), 1)
        self.assertEqual(_task_artifact_count(github, self.WORKER, "token"), 1)
        report = build_signed_template_transition_gate(
            credentials=Credentials(), task_store=Store(), github_app=github,
            repositories=(self.WORKER,),
        )
        self.assertFalse(report["ready"])
        self.assertFalse(report["checks"]["active_task_run_zero"])
        self.assertFalse(report["checks"]["task_artifact_zero"])
        clean = build_signed_template_transition_gate(
            credentials=Credentials(), task_store=Store(),
            github_app=GitHubApp(), repositories=(self.WORKER,),
        )
        self.assertTrue(clean["ready"])

    def test_non_404_probe_errors_still_fail_closed(self):
        for code in ("permission_denied", "rate_limited", "github_service_error"):
            with self.subTest(code=code):
                github = RefusingGitHub(code)
                with self.assertRaises(GitHubAppError):
                    _active_task_run_count(github, self.WORKER, "token")
                with self.assertRaises(GitHubAppError):
                    _task_artifact_count(github, self.WORKER, "token")
                with self.assertRaises(GitHubAppError):
                    build_signed_template_transition_gate(
                        credentials=Credentials(), task_store=Store(),
                        github_app=github, repositories=(self.WORKER,),
                    )


if __name__ == "__main__":
    unittest.main()
