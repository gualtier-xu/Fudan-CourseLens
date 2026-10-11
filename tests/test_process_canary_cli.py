from __future__ import annotations

import contextlib
import copy
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from src.remote.coordinator import RemoteSettings, RemoteTaskPaused
from src.remote.protocol import (
    PROCESS_CANARY_FIXTURE_BYTES,
    PROCESS_CANARY_FIXTURE_RECORDS,
    PROCESS_CANARY_FIXTURE_SHA256,
    PROCESS_CANARY_PIPELINE,
    PROCESS_CANARY_SCHEMA,
    PROTOCOL_VERSION,
    RESULT_SCHEMA,
)
from scripts.verify_github_process_canary import (
    LATCH_SECRET,
    VERIFIER_GO,
    assert_public_log_redaction,
    build_preflight,
    build_process_canary_job,
    build_zero_state_audit,
    run_once,
    validate_import,
    main,
)


COMMIT = "a" * 40
TASK_ID = "1" * 32


class Credentials:
    def __init__(self, values=None):
        self.values = {"remote_enabled": "0", **dict(values or {})}

    def has_secret(self, name):
        return name in self.values

    def load_secret(self, name):
        if name not in self.values:
            raise KeyError(name)
        return self.values[name]

    def save_secret(self, name, value):
        self.values[name] = str(value)

    def delete_secret(self, name):
        self.values.pop(name, None)

    def list_secret_names(self, *, prefix=""):
        return sorted(name for name in self.values if name.startswith(prefix))


def result(commit=COMMIT):
    return {
        "schema": RESULT_SCHEMA,
        "protocol_version": PROTOCOL_VERSION,
        "task_id": TASK_ID,
        "job_kind": "process_canary",
        "input_hash": "2" * 64,
        "pipeline_fingerprint": PROCESS_CANARY_PIPELINE,
        "status": "completed",
        "outputs": {"process_canary": {
            "schema": PROCESS_CANARY_SCHEMA,
            "fixture_sha256": PROCESS_CANARY_FIXTURE_SHA256,
            "fixture_bytes": PROCESS_CANARY_FIXTURE_BYTES,
            "fixture_records": PROCESS_CANARY_FIXTURE_RECORDS,
            "worker_commit": commit,
            "workflow_profile": "process-v1",
        }},
        "metrics": {
            "synthetic_bytes": PROCESS_CANARY_FIXTURE_BYTES,
            "synthetic_records": PROCESS_CANARY_FIXTURE_RECORDS,
        },
        "warnings": [],
    }


class ProcessCanaryCliTests(unittest.TestCase):
    def test_default_cli_rejects_before_constructing_local_or_github_clients(self):
        with (
            patch("scripts.verify_github_process_canary.CredentialStore") as credentials,
            patch("scripts.verify_github_process_canary.TaskStore") as store,
            patch("scripts.verify_github_process_canary.GitHubAppClient") as app,
            self.assertRaises(SystemExit) as raised,
        ):
            main([])
        self.assertEqual(raised.exception.code, 2)
        credentials.assert_not_called()
        store.assert_not_called()
        app.assert_not_called()

    @staticmethod
    def configured_credentials(*, latch_state=""):
        values = {
            "github_remote_token": "token",
            "github_worker_repo": "owner/template",
            "github_mailbox_repo": "owner/mailbox",
            "worker_box_public_key": "box",
            "worker_signing_public_key": "sign",
        }
        if latch_state:
            values[LATCH_SECRET] = json.dumps({
                "schema": "courselens.process-canary-latch.v1",
                "task_id": TASK_ID,
                "worker_commit": COMMIT,
                "state": latch_state,
            })
        return Credentials(values)

    @staticmethod
    def settings():
        return RemoteSettings(
            enabled=False, public_repo="owner/template", private_repo="owner/mailbox",
            github_token="token", worker_public_key="box",
            worker_signing_public_key="sign", expected_worker_commit=COMMIT,
        )

    def test_job_builder_contains_no_user_content_channel(self):
        job = build_process_canary_job(TASK_ID, "public-key")
        self.assertEqual(job["payload"], {})
        self.assertEqual(job["secrets"], {})
        self.assertEqual(job["requested_outputs"], [])
        self.assertEqual(job["pipeline"], {"version": PROCESS_CANARY_PIPELINE})
        self.assertNotIn("nonce", job)

    def test_preflight_requires_remote_false_signed_pin_executor_zero_and_quiet_template(self):
        credentials = Credentials({"github_worker_repo": "owner/template"})
        app = Mock()
        app.check_worker_integrity.return_value = {
            "trusted": True, "dispatch_mode": "personal-worker",
            "actual_commit": COMMIT, "repository": "owner/template",
        }
        app._api.return_value.json.return_value = {"total_count": 0, "workflow_runs": []}
        with patch(
            "scripts.verify_github_process_canary.build_signed_template_transition_gate",
            return_value={"ready": True, "observations": {"active_task_run_count": 0}},
        ) as gate:
            report = build_preflight(credentials=credentials, task_store=Mock(), github_app=app)
        self.assertTrue(report["ready"])
        self.assertEqual(
            gate.call_args.kwargs["repositories"],
            ("owner/template",),
        )
        self.assertEqual(report["public_template_audit"]["non_completed_run_count"], 0)
        self.assertEqual(report["audited_repository"], "owner/template")
        self.assertEqual(
            report["public_template_audit"]["workflow_run_totals"],
            {"process.yml": 0, "echo.yml": 0},
        )

    def test_preflight_rejects_env_override_pointing_at_the_public_template(self):
        credentials = Credentials({"github_worker_repo": "owner/template"})
        app = Mock()
        app.check_worker_integrity.return_value = {
            "trusted": True, "dispatch_mode": "personal-worker",
            "actual_commit": COMMIT, "repository": "owner/template",
        }
        with patch.dict(
            os.environ, {"FUDAN_COURSELENS_PUBLIC_REPO": "gualtier-xu/Fudan-CourseLens-Worker-Release"},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "dispatch target"):
                build_preflight(credentials=credentials, task_store=Mock(), github_app=app)
        app._api.assert_not_called()

    def test_preflight_fails_when_public_template_runs_are_not_quiescent(self):
        credentials = Credentials({"github_worker_repo": "owner/template"})
        app = Mock()
        app.check_worker_integrity.return_value = {
            "trusted": True, "dispatch_mode": "personal-worker",
            "actual_commit": COMMIT, "repository": "owner/template",
        }
        app._api.return_value.json.return_value = {
            "total_count": 1,
            "workflow_runs": [{"id": 5, "status": "in_progress", "created_at": ""}],
        }
        with self.assertRaisesRegex(RuntimeError, "not quiescent"):
            build_preflight(credentials=credentials, task_store=Mock(), github_app=app)

    def test_preflight_rejects_enabled_remote_without_github_calls(self):
        credentials = Credentials({"remote_enabled": "1"})
        app = Mock()
        with self.assertRaisesRegex(RuntimeError, "must remain false"):
            build_preflight(credentials=credentials, task_store=Mock(), github_app=app)
        app.check_worker_integrity.assert_not_called()

    def test_preflight_rejects_effective_remote_environment_override(self):
        for name in ("REMOTE_COMPUTE_ENABLED", "FUDAN_COURSELENS_REMOTE_ENABLED"):
            with self.subTest(name=name), patch.dict(os.environ, {name: "1"}, clear=False):
                app = Mock()
                with self.assertRaisesRegex(RuntimeError, "effective remote compute"):
                    build_preflight(
                        credentials=Credentials(), task_store=Mock(), github_app=app
                    )
                app.check_worker_integrity.assert_not_called()

    def test_recovery_preflight_revalidates_pin_without_requiring_zero_state(self):
        credentials = Credentials({"github_worker_repo": "owner/template"})
        app = Mock()
        app.check_worker_integrity.return_value = {
            "trusted": True, "dispatch_mode": "personal-worker",
            "actual_commit": COMMIT, "repository": "owner/template",
        }
        app._api.return_value.json.return_value = {"total_count": 0, "workflow_runs": []}
        with patch(
            "scripts.verify_github_process_canary.build_signed_template_transition_gate"
        ) as gate:
            report = build_preflight(
                credentials=credentials, task_store=Mock(), github_app=app,
                require_zero_state=False,
            )
        self.assertEqual(report["zero_state"], {"recovery_mode": 1})
        gate.assert_not_called()

    def test_import_rejects_wrong_signed_commit_and_extra_result_field(self):
        with self.assertRaisesRegex(RuntimeError, "signed worker pin"):
            validate_import(result("b" * 40), expected_worker_commit=COMMIT)
        extra = copy.deepcopy(result())
        extra["outputs"]["process_canary"]["text"] = "forbidden"
        with self.assertRaises(Exception):
            validate_import(extra, expected_worker_commit=COMMIT)

    def test_log_redaction_rejects_fixture_and_sensitive_fields(self):
        assert_public_log_redaction(b"stage=remote_compute completed=3 total=3")
        for raw in (
            b'{"records":[[0,1,0,-1]', b'Cookie: value', b'"course_id":"1"',
        ):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(RuntimeError, "redaction failed"):
                    assert_public_log_redaction(raw)

    def test_exact_go_is_required_before_any_preflight_or_dispatch(self):
        app = Mock()
        with self.assertRaisesRegex(RuntimeError, "exact process canary"):
            run_once(
                credentials=Credentials(), task_store=Mock(), github_app=app,
                verifier_go="yes",
            )
        app.assert_not_called()

    def test_one_shot_latch_dispatches_once_and_refuses_same_commit_again(self):
        credentials = self.configured_credentials()
        store = Mock()
        store.get_remote_run.return_value = {"run_id": 17}
        store.get_remote_attempt.return_value = {}
        app = Mock()
        app.job_token_lease.return_value = contextlib.nullcontext()
        github = Mock()
        github.download_run_logs.return_value = b"stage=remote_compute completed=3 total=3"
        coordinator = Mock()

        def execute(**kwargs):
            canary = result()
            canary["task_id"] = TASK_ID
            kwargs["import_result"](canary)

        coordinator.execute.side_effect = execute
        settings = self.settings()
        with (
            patch("scripts.verify_github_process_canary.uuid.uuid4", return_value=Mock(hex=TASK_ID)),
            patch("scripts.verify_github_process_canary.build_preflight", return_value={
                "expected_worker_commit": COMMIT,
            }),
            patch("scripts.verify_github_process_canary.RemoteSettings.load", return_value=settings),
            patch("scripts.verify_github_process_canary.GitHubClient", return_value=github),
            patch("scripts.verify_github_process_canary.RemoteCoordinator", return_value=coordinator),
            patch("scripts.verify_github_process_canary.build_zero_state_audit", return_value={
                "run_id": 17, "zero_state": {},
            }),
        ):
            report = run_once(
                credentials=credentials, task_store=store, github_app=app,
                verifier_go=VERIFIER_GO,
            )
            self.assertEqual(report["status"], "passed")
            with self.assertRaisesRegex(RuntimeError, "already completed"):
                run_once(
                    credentials=credentials, task_store=store, github_app=app,
                    verifier_go=VERIFIER_GO,
                )
        coordinator.execute.assert_called_once()
        app.job_token_lease.assert_called_once_with(task_id=TASK_ID, task_store=store)
        self.assertEqual(json.loads(credentials.values[LATCH_SECRET])["state"], "complete")

    def test_cancellation_keeps_same_latched_task_for_recovery(self):
        credentials = self.configured_credentials()
        app = Mock()
        app.job_token_lease.return_value = contextlib.nullcontext()
        coordinator = Mock()
        coordinator.execute.side_effect = RemoteTaskPaused("paused")
        settings = self.settings()
        store = Mock()
        store.get_remote_run.return_value = {}
        store.get_remote_attempt.return_value = {}
        with (
            patch("scripts.verify_github_process_canary.uuid.uuid4", return_value=Mock(hex=TASK_ID)),
            patch("scripts.verify_github_process_canary.build_preflight", return_value={
                "expected_worker_commit": COMMIT,
            }),
            patch("scripts.verify_github_process_canary.RemoteSettings.load", return_value=settings),
            patch("scripts.verify_github_process_canary.GitHubClient", return_value=Mock()),
            patch("scripts.verify_github_process_canary.RemoteCoordinator", return_value=coordinator),
        ):
            with self.assertRaises(RemoteTaskPaused):
                run_once(
                    credentials=credentials, task_store=store, github_app=app,
                    verifier_go=VERIFIER_GO,
                )
        latch = json.loads(credentials.values[LATCH_SECRET])
        self.assertEqual(latch["task_id"], TASK_ID)
        self.assertEqual(latch["state"], "running")

    def test_running_artifact_crash_resumes_import_and_cleans_stale_lease_without_acquire(self):
        credentials = self.configured_credentials(latch_state="running")
        credentials.save_secret(f"remote_result_private:{TASK_ID}", "result-key")
        credentials.save_secret("github_job_token_cleanup_pending", "1")
        leases = [{"task_id": TASK_ID}]
        store = Mock()
        store.get_remote_run.return_value = {
            "run_id": 17, "artifact_id": 19, "remote_state": "downloading_result",
            "attempt": 1, "repository": "owner/template", "issue_number": 23,
            "input_hash": "2" * 64,
        }
        store.get_remote_attempt.return_value = {"cleanup_state": "pending"}
        store.list_remote_token_leases.side_effect = lambda: list(leases)
        app = Mock()
        github = Mock()
        github.download_run_logs.return_value = b"stage=remote_compute"
        github.get_run.return_value = {
            "status": "completed", "conclusion": "success", "head_sha": COMMIT,
        }
        coordinator = Mock()

        def execute(**kwargs):
            kwargs["import_result"](result())
            credentials.delete_secret(f"remote_result_private:{TASK_ID}")

        def cleanup_stale(**_kwargs):
            leases.clear()
            credentials.delete_secret("github_remote_token")
            credentials.delete_secret("github_job_token_cleanup_pending")

        coordinator.execute.side_effect = execute
        app.cleanup_stale_job_token_lease.side_effect = cleanup_stale
        with (
            patch("scripts.verify_github_process_canary.build_preflight", return_value={
                "expected_worker_commit": COMMIT,
            }),
            patch("scripts.verify_github_process_canary.RemoteSettings.load", return_value=self.settings()),
            patch("scripts.verify_github_process_canary.GitHubClient", return_value=github),
            patch("scripts.verify_github_process_canary.RemoteCoordinator", return_value=coordinator),
            patch("scripts.verify_github_process_canary.build_zero_state_audit", return_value={
                "run_id": 17, "zero_state": {},
            }),
        ):
            report = run_once(
                credentials=credentials, task_store=store, github_app=app,
                verifier_go=VERIFIER_GO,
            )
        self.assertEqual(report["status"], "passed")
        coordinator.execute.assert_called_once()
        app.job_token_lease.assert_not_called()
        app.cleanup_stale_job_token_lease.assert_called_once_with(
            task_id=TASK_ID, task_store=store
        )
        self.assertEqual(leases, [])
        self.assertFalse(credentials.has_secret(f"remote_result_private:{TASK_ID}"))
        self.assertFalse(credentials.has_secret("github_job_token_cleanup_pending"))

    def test_result_verified_imported_crash_retries_only_idempotent_cleanup(self):
        credentials = self.configured_credentials(latch_state="result_verified")
        credentials.save_secret(f"remote_result_private:{TASK_ID}", "stale-result-key")
        credentials.save_secret("github_job_token_cleanup_pending", "1")
        leases = [{"task_id": TASK_ID}]
        store = Mock()
        store.get_remote_run.return_value = {
            "run_id": 17, "artifact_id": 19, "issue_number": 23,
            "remote_state": "imported", "attempt": 1,
            "repository": "owner/template",
        }
        store.get_remote_attempt.return_value = {"cleanup_state": "pending"}
        store.list_remote_token_leases.side_effect = lambda: list(leases)
        app = Mock()
        github = Mock()
        github.download_run_logs.return_value = b"stage=remote_compute"
        github.get_run.return_value = {
            "status": "completed", "conclusion": "success", "head_sha": COMMIT,
        }
        coordinator = Mock()
        coordinator.retry_imported_cleanup.side_effect = lambda _task_id: credentials.delete_secret(
            f"remote_result_private:{TASK_ID}"
        )

        def cleanup_stale(**_kwargs):
            leases.clear()
            credentials.delete_secret("github_remote_token")
            credentials.delete_secret("github_job_token_cleanup_pending")

        app.cleanup_stale_job_token_lease.side_effect = cleanup_stale
        with (
            patch("scripts.verify_github_process_canary.build_preflight", return_value={
                "expected_worker_commit": COMMIT,
            }),
            patch("scripts.verify_github_process_canary.RemoteSettings.load", return_value=self.settings()),
            patch("scripts.verify_github_process_canary.GitHubClient", return_value=github),
            patch("scripts.verify_github_process_canary.RemoteCoordinator", return_value=coordinator),
            patch("scripts.verify_github_process_canary.build_zero_state_audit", return_value={
                "run_id": 17, "zero_state": {},
            }),
        ):
            report = run_once(
                credentials=credentials, task_store=store, github_app=app,
                verifier_go=VERIFIER_GO,
            )
        self.assertEqual(report["status"], "passed")
        coordinator.retry_imported_cleanup.assert_called_once_with(TASK_ID)
        coordinator.execute.assert_not_called()
        app.job_token_lease.assert_not_called()
        app.cleanup_stale_job_token_lease.assert_called_once_with(
            task_id=TASK_ID, task_store=store
        )
        self.assertEqual(leases, [])
        self.assertFalse(credentials.has_secret(f"remote_result_private:{TASK_ID}"))
        self.assertFalse(credentials.has_secret("github_job_token_cleanup_pending"))

    def test_failed_nonresumable_run_is_latched_and_never_redispatched(self):
        credentials = self.configured_credentials(latch_state="running")
        store = Mock()
        store.get_remote_run.return_value = {
            "run_id": 17, "remote_state": "failed", "attempt": 1,
        }
        store.get_remote_attempt.return_value = {"cleanup_state": "complete"}
        coordinator = Mock()
        with (
            patch("scripts.verify_github_process_canary.build_preflight", return_value={
                "expected_worker_commit": COMMIT,
            }),
            patch("scripts.verify_github_process_canary.RemoteSettings.load", return_value=self.settings()),
            patch("scripts.verify_github_process_canary.GitHubClient", return_value=Mock()),
            patch("scripts.verify_github_process_canary.RemoteCoordinator", return_value=coordinator),
        ):
            with self.assertRaisesRegex(RuntimeError, "failed before a resumable"):
                run_once(
                    credentials=credentials, task_store=store, github_app=Mock(),
                    verifier_go=VERIFIER_GO,
                )
        coordinator.execute.assert_not_called()
        self.assertEqual(json.loads(credentials.values[LATCH_SECRET])["state"], "failed")

    def test_incomplete_artifact_metadata_never_reaches_production_coordinator_dispatch(self):
        from src.runtime.task_store import TaskStore

        credentials = self.configured_credentials(latch_state="running")
        credentials.save_secret(f"remote_result_private:{TASK_ID}", "result-key")

        class NoDispatchGitHub:
            def __init__(self):
                self.dispatch_count = 0

            def dispatch_workflow(self, *_args, **_kwargs):
                self.dispatch_count += 1
                raise AssertionError("dispatch must not be reached")

        github = NoDispatchGitHub()
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.db")
            store.upsert_remote_run(
                TASK_ID, repository="owner/template", workflow="process.yml",
                run_id=17, attempt=1, artifact_id=19,
                remote_state="downloading_result",
                # Deliberately omit issue_number and input_hash.
            )
            with (
                patch("scripts.verify_github_process_canary.build_preflight", return_value={
                    "expected_worker_commit": COMMIT,
                }),
                patch("scripts.verify_github_process_canary.RemoteSettings.load", return_value=self.settings()),
                patch("scripts.verify_github_process_canary.GitHubClient", return_value=github),
            ):
                with self.assertRaisesRegex(RuntimeError, "metadata is incomplete"):
                    run_once(
                        credentials=credentials, task_store=store, github_app=Mock(),
                        verifier_go=VERIFIER_GO,
                    )
        self.assertEqual(github.dispatch_count, 0)

    def test_zero_state_audit_requires_cleanup_and_signed_head(self):
        credentials = Credentials({"github_mailbox_repo": "owner/mailbox"})
        store = Mock()
        store.get_remote_run.return_value = {
            "run_id": 17, "issue_number": 23, "repository": "owner/template",
            "workflow": "process.yml", "attempt": 1, "remote_state": "imported",
        }
        store.get_remote_attempt.return_value = {
            "import_state": "imported", "cleanup_state": "complete",
        }
        github = Mock()
        github.get_run.return_value = {
            "status": "completed", "conclusion": "success", "head_sha": COMMIT,
            "created_at": "2026-09-07T00:00:00Z",
        }
        github.job_cleanup_summary.return_value = {
            "state": "closed", "consumed": True, "comment_count": 0,
        }
        github_app = Mock()
        github_app._api.return_value.json.return_value = {"total_count": 0, "workflow_runs": []}
        with patch(
            "scripts.verify_github_process_canary.build_signed_template_transition_gate",
            return_value={"ready": True, "observations": {"task_artifact_count": 0}},
        ):
            audit = build_zero_state_audit(
                TASK_ID, credentials=credentials, task_store=store,
                github_app=github_app, github=github, expected_worker_commit=COMMIT,
            )
        self.assertEqual(audit["status"], "passed")
        self.assertEqual(audit["public_template_audit"]["late_run_count"], 0)

    def test_zero_state_audit_fails_on_template_run_created_after_window_start(self):
        credentials = Credentials({"github_mailbox_repo": "owner/mailbox"})
        store = Mock()
        store.get_remote_run.return_value = {
            "run_id": 17, "issue_number": 23, "repository": "owner/template",
            "workflow": "process.yml", "attempt": 1, "remote_state": "imported",
        }
        store.get_remote_attempt.return_value = {
            "import_state": "imported", "cleanup_state": "complete",
        }
        github = Mock()
        github.get_run.return_value = {
            "status": "completed", "conclusion": "success", "head_sha": COMMIT,
            "created_at": "2026-09-07T00:00:00Z",
        }
        github.job_cleanup_summary.return_value = {
            "state": "closed", "consumed": True, "comment_count": 0,
        }
        github_app = Mock()
        github_app._api.return_value.json.return_value = {
            "total_count": 1,
            "workflow_runs": [{
                "id": 6, "status": "completed", "created_at": "2026-09-07T01:00:00Z",
            }],
        }
        with patch(
            "scripts.verify_github_process_canary.build_signed_template_transition_gate",
            return_value={"ready": True, "observations": {"task_artifact_count": 0}},
        ):
            with self.assertRaisesRegex(RuntimeError, "template_late_runs_zero"):
                build_zero_state_audit(
                    TASK_ID, credentials=credentials, task_store=store,
                    github_app=github_app, github=github, expected_worker_commit=COMMIT,
                )

    def test_zero_state_audit_fails_closed_on_unparsable_template_run_timestamp(self):
        credentials = Credentials({"github_mailbox_repo": "owner/mailbox"})
        store = Mock()
        store.get_remote_run.return_value = {
            "run_id": 17, "issue_number": 23, "repository": "owner/template",
            "workflow": "process.yml", "attempt": 1, "remote_state": "imported",
        }
        store.get_remote_attempt.return_value = {
            "import_state": "imported", "cleanup_state": "complete",
        }
        github = Mock()
        github.get_run.return_value = {
            "status": "completed", "conclusion": "success", "head_sha": COMMIT,
            "created_at": "2026-09-07T00:00:00Z",
        }
        github.job_cleanup_summary.return_value = {
            "state": "closed", "consumed": True, "comment_count": 0,
        }
        github_app = Mock()
        github_app._api.return_value.json.return_value = {
            "total_count": 1,
            "workflow_runs": [{"id": 6, "status": "completed", "created_at": ""}],
        }
        with patch(
            "scripts.verify_github_process_canary.build_signed_template_transition_gate",
            return_value={"ready": True, "observations": {"task_artifact_count": 0}},
        ):
            with self.assertRaisesRegex(RuntimeError, "created_at is unavailable"):
                build_zero_state_audit(
                    TASK_ID, credentials=credentials, task_store=store,
                    github_app=github_app, github=github, expected_worker_commit=COMMIT,
                )

    def test_zero_state_audit_fails_closed_on_unparsable_window_start(self):
        credentials = Credentials({"github_mailbox_repo": "owner/mailbox"})
        store = Mock()
        store.get_remote_run.return_value = {
            "run_id": 17, "issue_number": 23, "repository": "owner/template",
            "workflow": "process.yml", "attempt": 1, "remote_state": "imported",
        }
        store.get_remote_attempt.return_value = {
            "import_state": "imported", "cleanup_state": "complete",
        }
        github = Mock()
        github.get_run.return_value = {
            "status": "completed", "conclusion": "success", "head_sha": COMMIT,
        }
        github.job_cleanup_summary.return_value = {
            "state": "closed", "consumed": True, "comment_count": 0,
        }
        github_app = Mock()
        with patch(
            "scripts.verify_github_process_canary.build_signed_template_transition_gate",
            return_value={"ready": True, "observations": {"task_artifact_count": 0}},
        ):
            with self.assertRaisesRegex(RuntimeError, "window start timestamp is unavailable"):
                build_zero_state_audit(
                    TASK_ID, credentials=credentials, task_store=store,
                    github_app=github_app, github=github, expected_worker_commit=COMMIT,
                )
        github_app._api.assert_not_called()

    def test_zero_state_audit_diffs_preinstalled_template_run_totals(self):
        credentials = Credentials({"github_mailbox_repo": "owner/mailbox"})
        store = Mock()
        store.get_remote_run.return_value = {
            "run_id": 17, "issue_number": 23, "repository": "owner/template",
            "workflow": "process.yml", "attempt": 1, "remote_state": "imported",
        }
        store.get_remote_attempt.return_value = {
            "import_state": "imported", "cleanup_state": "complete",
        }
        github = Mock()
        github.get_run.return_value = {
            "status": "completed", "conclusion": "success", "head_sha": COMMIT,
            "created_at": "2026-09-07T00:00:00Z",
        }
        github.job_cleanup_summary.return_value = {
            "state": "closed", "consumed": True, "comment_count": 0,
        }
        github_app = Mock()
        # A backdated run (created before the window) still trips the total
        # count diff against the preflight audit.
        github_app._api.return_value.json.return_value = {
            "total_count": 2,
            "workflow_runs": [
                {"id": 6, "status": "completed", "created_at": "2026-09-06T00:00:00Z"},
                {"id": 7, "status": "completed", "created_at": "2026-09-05T00:00:00Z"},
            ],
        }
        with patch(
            "scripts.verify_github_process_canary.build_signed_template_transition_gate",
            return_value={"ready": True, "observations": {"task_artifact_count": 0}},
        ):
            with self.assertRaisesRegex(RuntimeError, "gained new runs"):
                build_zero_state_audit(
                    TASK_ID, credentials=credentials, task_store=store,
                    github_app=github_app, github=github, expected_worker_commit=COMMIT,
                    expected_template_run_totals={"process.yml": 1, "echo.yml": 0},
                )


if __name__ == "__main__":
    unittest.main()
