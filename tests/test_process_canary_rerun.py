from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.verify_github_process_canary import (
    LATCH_SECRET,
    LATCH_V1_SCHEMA,
    LATCH_V2_SCHEMA,
    PROCESS_CANARY_FIXTURE_BYTES,
    PROCESS_CANARY_FIXTURE_RECORDS,
    PROCESS_CANARY_FIXTURE_SHA256,
    PROCESS_CANARY_PIPELINE,
    PROCESS_CANARY_SCHEMA,
    CLEANUP_VERIFIER_GO,
    REFRESH_PREPARATION_VERIFIER_GO,
    RERUN_VERIFIER_GO,
    _load_latch,
    _create_rerun_bundle,
    _rerun_bundle_name,
    _rerun_refresh_name,
    _save_rerun_refresh_marker,
    _save_latch,
    cleanup_attempt_one_only,
    refresh_attempt_one_preparation_only,
    rerun_once,
)
from src.remote.github_client import DispatchResult
from src.remote.protocol import (
    PROTOCOL_VERSION,
    RESULT_SCHEMA,
    generate_box_keypair,
    generate_signing_keypair,
    open_job,
    open_result,
    seal_result,
)
from src.runtime.task_store import TaskStore


TASK_ID = "1" * 32
COMMIT = "a" * 40
RUN_ID = 17


class MemoryCredentials:
    def __init__(self, values=None):
        self.values = dict(values or {})

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

    def update_secrets(self, updates, *, deletes=()):
        replacement = dict(self.values)
        replacement.update({str(name): str(value) for name, value in updates.items()})
        for name in deletes:
            replacement.pop(str(name), None)
        self.values = replacement

    def list_secret_names(self, *, prefix=""):
        return sorted(name for name in self.values if name.startswith(prefix))


class Response:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


class FakeApp:
    def __init__(self, credentials, store):
        self.credentials = credentials
        self.store = store
        self._job_token_leases = 0
        self.worker_token = False

    def check_worker_integrity(self):
        return {
            "trusted": True,
            "dispatch_mode": "personal-worker",
            "dispatch_head_sha": "",
            "repository": "owner/template",
            "actual_commit": COMMIT,
            "actual_tree": "b" * 40,
            "expected_tree": "b" * 40,
            "manifest_verified": True,
            "manifest_sha256": "c" * 64,
        }

    def access_token(self, **_kwargs):
        return "token"

    def _api(self, method, path, **_kwargs):
        if method == "GET" and "/actions/workflows/" in path and path.endswith("/runs"):
            return Response({"total_count": 0, "workflow_runs": []})
        if method == "GET" and path.endswith("/actions/artifacts"):
            return Response({"artifacts": []})
        if method == "GET" and path == "/repos/owner/mailbox/issues":
            return Response([])
        if method == "GET" and path.endswith("/environments/courselens-worker/secrets"):
            return Response({"secrets": []})
        raise AssertionError((method, path))

    def inspect_managed_resources(self):
        return {
            "installation": {
                "installed": True,
                "installation_id": 42,
                "repository_selection_exact": True,
            },
            "worker": {"full_name": "owner/template"},
            "mailbox": {"full_name": "owner/mailbox"},
            "workflows": {"process.yml": {"exists": True, "state": "active"}},
            "actions_enabled": True,
            "environment_exists": True,
        }

    def list_worker_secrets(self):
        return [{"name": "COURSELENS_JOB_TOKEN"}] if self.worker_token else []

    def acquire_job_token(self, *, task_id, task_store):
        if self._job_token_leases:
            raise AssertionError("live lease reacquired")
        self._job_token_leases = 1
        self.worker_token = True
        self.credentials.save_secret("github_remote_token", "short-lived")
        task_store.set_remote_token_lease(task_id, state="active", expires_at=time.time() + 60)

    def sync_job_token(self):
        self.worker_token = True
        self.credentials.save_secret("github_app_access_token", "fresh-bearer")
        self.credentials.save_secret("github_remote_token", "renewed-token")
        self.credentials.delete_secret("github_job_token_cleanup_pending")
        return "renewed-token"

    def finalize_process_canary_token_lease(self, *, task_id, task_store):
        if task_store.active_remote_run_count():
            raise AssertionError("token deleted while task active")
        self.worker_token = False
        self.credentials.delete_secret("github_remote_token")
        self.credentials.delete_secret("github_job_token_cleanup_pending")
        self._job_token_leases = 0
        task_store.delete_remote_token_lease(task_id)


class FakeGitHub:
    def __init__(self, worker_private, signing_private, *, attempt_two_conclusion="success"):
        self.worker_private = worker_private
        self.signing_private = signing_private
        self.attempt = 1
        self.status = "completed"
        self.conclusion = "failure"
        self.attempt_two_conclusion = attempt_two_conclusion
        self.dispatch_count = 0
        self.rerun_count = 0
        self.envelope = None
        self.issue_number = 23
        self.issue_open = False
        self.artifact_deleted = False
        self.mailbox_cleaned = False
        self.cleanup_failures = 0
        self.rerun_status = "completed"
        self.rerun_get_count = 0
        self.drift_on_rerun_get = 0
        self.client_tokens = []
        self.required_mailbox_token = ""

    def dispatch_workflow(self, *_args, **_kwargs):
        self.dispatch_count += 1
        raise AssertionError("workflow_dispatch is forbidden during rerun")

    def find_workflow_run(self, *_args, **_kwargs):
        return DispatchResult(RUN_ID, self.status, COMMIT)

    def get_run(self, *_args, **_kwargs):
        self.rerun_get_count += 1
        if self.drift_on_rerun_get and self.rerun_get_count >= self.drift_on_rerun_get:
            self.attempt = 3
        return {
            "id": RUN_ID,
            "status": self.status,
            "conclusion": self.conclusion,
            "run_attempt": self.attempt,
            "head_sha": COMMIT,
            "event": "workflow_dispatch",
        }

    def get_run_jobs(self, *_args, **_kwargs):
        return [{"steps": [
            {"name": "Process encrypted job", "status": "completed", "conclusion": "failure"},
            {"name": "Upload encrypted result only", "status": "completed", "conclusion": "failure"},
            {"name": "Remove transient data", "status": "completed", "conclusion": "success"},
        ]}]

    def _matching_job_issues(self, _repo, _task_id):
        return ([{"number": self.issue_number}] if self.issue_open else [])

    def publish_job_once(self, _repo, _task_id, envelope):
        if self.envelope is not None and self.envelope != envelope:
            raise AssertionError("payload changed across recovery")
        self.envelope = envelope
        self.issue_open = True
        return {"issue_number": self.issue_number, "comment_ids": [31], "part_count": 1}

    def read_job(self, _repo, _task_id):
        if self.required_mailbox_token and self.client_tokens[-1] != self.required_mailbox_token:
            raise RuntimeError("Mailbox GET 401")
        if not self.issue_open or self.envelope is None:
            raise RuntimeError("missing mailbox")
        return self.envelope, self.issue_number

    def rerun_workflow_once(self, _repo, run_id):
        self.rerun_count += 1
        if self.rerun_count > 1 or run_id != RUN_ID:
            raise AssertionError("rerun requested more than once")
        self.attempt = 2
        self.status = self.rerun_status
        self.conclusion = self.attempt_two_conclusion

    def read_controls(self, *_args, **_kwargs):
        return []

    def list_run_artifacts(self, *_args, **_kwargs):
        if self.attempt == 2 and self.conclusion == "success" and not self.artifact_deleted:
            return [{
                "id": 29,
                "name": f"courselens-result-{TASK_ID}",
                "expired": False,
            }]
        return []

    def download_artifact_file(self, *_args, **_kwargs):
        job = open_job(self.envelope, self.worker_private)
        result = {
            "schema": RESULT_SCHEMA,
            "protocol_version": PROTOCOL_VERSION,
            "task_id": TASK_ID,
            "job_kind": "process_canary",
            "input_hash": job["input_hash"],
            "pipeline_fingerprint": PROCESS_CANARY_PIPELINE,
            "status": "completed",
            "outputs": {"process_canary": {
                "schema": PROCESS_CANARY_SCHEMA,
                "fixture_sha256": PROCESS_CANARY_FIXTURE_SHA256,
                "fixture_bytes": PROCESS_CANARY_FIXTURE_BYTES,
                "fixture_records": PROCESS_CANARY_FIXTURE_RECORDS,
                "worker_commit": COMMIT,
                "workflow_profile": "process-v1",
            }},
            "metrics": {
                "synthetic_bytes": PROCESS_CANARY_FIXTURE_BYTES,
                "synthetic_records": PROCESS_CANARY_FIXTURE_RECORDS,
            },
            "warnings": [],
        }
        return json.dumps(
            seal_result(result, job["result_public_key"], self.signing_private)
        ).encode()

    def delete_artifact(self, *_args, **_kwargs):
        self.artifact_deleted = True

    def cleanup_job(self, *_args, **_kwargs):
        if self.cleanup_failures:
            self.cleanup_failures -= 1
            raise RuntimeError("simulated cleanup failure")
        self.issue_open = False
        self.mailbox_cleaned = True

    def job_cleanup_summary(self, *_args, **_kwargs):
        return {"state": "closed", "consumed": True, "comment_count": 0}

    def download_run_attempt_logs(self, _repo, _run_id, attempt):
        return f"stage=closed attempt={attempt}".encode()


class ProcessCanaryRerunTests(unittest.TestCase):
    def fixture(self, *, conclusion="success"):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = TaskStore(Path(directory.name) / "state.db")
        worker_private, worker_public = generate_box_keypair()
        signing_private, signing_public = generate_signing_keypair()
        credentials = MemoryCredentials({
            "remote_enabled": "0",
            "github_app_access_token": "token",
            "github_app_installation_id": "42",
            "github_worker_repo": "owner/template",
            "github_mailbox_repo": "owner/mailbox",
            "worker_box_public_key": worker_public,
            "worker_signing_public_key": signing_public,
            LATCH_SECRET: json.dumps({
                "schema": LATCH_V1_SCHEMA,
                "task_id": TASK_ID,
                "worker_commit": COMMIT,
                "state": "running",
            }, sort_keys=True, separators=(",", ":")),
        })
        store.upsert_remote_run(
            TASK_ID, repository="owner/template", workflow="process.yml",
            run_id=RUN_ID, attempt=1, remote_state="failed", checkpoint={},
            last_error="oserror",
        )
        store.upsert_remote_attempt(
            TASK_ID, 1, repository="owner/template", workflow="process.yml",
            run_id=RUN_ID, github_status="unknown", conclusion="",
            worker_status="failed", import_state="failed", cleanup_state="best_effort",
            error_code="oserror",
        )
        app = FakeApp(credentials, store)
        github = FakeGitHub(
            worker_private, signing_private, attempt_two_conclusion=conclusion
        )
        return credentials, store, app, github

    @staticmethod
    def audit_stub(_task_id, **_kwargs):
        return {"run_id": RUN_ID, "zero_state": {
            "active_task_run_count": 0,
            "task_artifact_count": 0,
            "mailbox_managed_comment_count": 0,
            "mailbox_temporary_content_count": 0,
            "local_token_lease_count": 0,
            "local_result_key_count": 0,
            "cleanup_pending_count": 0,
            "temporary_job_token_count": 0,
        }}

    def invoke(self, credentials, store, app, github, *, audit=None, **kwargs):
        with (
            patch("scripts.verify_github_process_canary.GitHubClient", return_value=github),
            patch(
                "scripts.verify_github_process_canary.build_zero_state_audit",
                side_effect=audit or self.audit_stub,
            ),
        ):
            return rerun_once(
                credentials=credentials, task_store=store, github_app=app,
                verifier_go=RERUN_VERIFIER_GO, attempt_visibility_timeout=0,
                **kwargs,
            )

    def refresh_fixture(self):
        credentials, store, app, github = self.fixture()
        _save_latch(
            credentials, task_id=TASK_ID, commit=COMMIT,
            state="rerun_preparing", run_id=RUN_ID, rerun_budget=1,
        )
        store.upsert_remote_run(
            TASK_ID, remote_state="rerun_preparing", issue_number=None,
            artifact_id=None, input_hash="", pipeline_version="", checkpoint={},
        )
        store.upsert_remote_attempt(
            TASK_ID, 1, repository="owner/template", workflow="process.yml", run_id=RUN_ID,
            github_status="completed", conclusion="failure", worker_status="failed",
            worker_stage="mailbox_wait", import_state="failed", cleanup_state="best_effort",
            error_code="mailbox_wait", observed_at=time.time(),
        )
        store.upsert_remote_attempt(
            TASK_ID, 2, repository="owner/template", workflow="process.yml", run_id=RUN_ID,
            github_status="not_requested", worker_status="planned", worker_stage="payload_preparing",
            import_state="not_started", cleanup_state="not_started", error_code="", observed_at=time.time(),
        )
        app.acquire_job_token(task_id=TASK_ID, task_store=store)
        _create_rerun_bundle(
            credentials, task_id=TASK_ID, run_id=RUN_ID, worker_commit=COMMIT,
            worker_public_key=credentials.load_secret("worker_box_public_key"),
        )
        bundle = json.loads(credentials.load_secret(_rerun_bundle_name(TASK_ID)))
        bundle["expires_at"] = time.time() - 1
        credentials.save_secret(_rerun_bundle_name(TASK_ID), json.dumps(bundle, sort_keys=True, separators=(",", ":")))
        app._job_token_leases = 0  # model the process that created preparation having crashed
        return credentials, store, app, github

    def invoke_refresh(self, credentials, store, app, github, **kwargs):
        with patch("scripts.verify_github_process_canary.GitHubClient", return_value=github):
            return refresh_attempt_one_preparation_only(
                credentials=credentials, task_store=store, github_app=app,
                verifier_go=REFRESH_PREPARATION_VERIFIER_GO, **kwargs,
            )

    def test_refresh_attempt_one_preparation_rotates_expired_unposted_bundle_only(self):
        credentials, store, app, github = self.refresh_fixture()
        old_key = credentials.load_secret(f"remote_result_private:{TASK_ID}")
        old_bundle = credentials.load_secret(_rerun_bundle_name(TASK_ID))
        report = self.invoke_refresh(credentials, store, app, github)
        self.assertEqual(report["status"], "refreshed")
        self.assertGreater(report["expires_at"], time.time())
        self.assertNotEqual(credentials.load_secret(f"remote_result_private:{TASK_ID}"), old_key)
        self.assertNotEqual(credentials.load_secret(_rerun_bundle_name(TASK_ID)), old_bundle)
        self.assertEqual(len(store.list_remote_token_leases()), 1)
        self.assertEqual(len(credentials.list_secret_names(prefix="remote_result_private:")), 1)
        self.assertEqual(len(credentials.list_secret_names(prefix="process_canary_rerun_bundle:")), 1)
        self.assertEqual(credentials.list_secret_names(prefix="process_canary_rerun_refresh:"), [])
        self.assertEqual(github.dispatch_count, 0)
        self.assertEqual(github.rerun_count, 0)
        self.assertFalse(github.issue_open)
        self.assertEqual(_load_latch(credentials)["state"], "rerun_preparing")
        self.assertEqual(_load_latch(credentials)["rerun_budget"], 1)
        self.assertEqual(store.get_remote_run(TASK_ID)["remote_state"], "rerun_preparing")
        self.assertEqual(store.get_remote_attempt(TASK_ID, 2)["github_status"], "not_requested")

    def test_refresh_rebuilds_client_after_user_credential_refresh(self):
        credentials, store, app, github = self.refresh_fixture()
        client_tokens = []

        def client_for(token, **_kwargs):
            client_tokens.append(token)
            return github

        with patch("scripts.verify_github_process_canary.GitHubClient", side_effect=client_for):
            refresh_attempt_one_preparation_only(
                credentials=credentials, task_store=store, github_app=app,
                verifier_go=REFRESH_PREPARATION_VERIFIER_GO,
            )
        self.assertEqual(client_tokens, ["token", "fresh-bearer"])

    def test_refresh_rejects_nonexpired_bundle_without_mutation(self):
        credentials, store, app, github = self.refresh_fixture()
        bundle = json.loads(credentials.load_secret(_rerun_bundle_name(TASK_ID)))
        bundle["expires_at"] = time.time() + 60
        credentials.save_secret(_rerun_bundle_name(TASK_ID), json.dumps(bundle, sort_keys=True, separators=(",", ":")))
        before = dict(credentials.values)
        with self.assertRaisesRegex(RuntimeError, "not expired"):
            self.invoke_refresh(credentials, store, app, github)
        self.assertEqual(credentials.values, before)
        self.assertEqual(github.dispatch_count, 0)
        self.assertEqual(github.rerun_count, 0)

    def test_refresh_live_attempt_drift_refuses_without_state_change(self):
        credentials, store, app, github = self.refresh_fixture()
        store.upsert_remote_attempt(TASK_ID, 1, github_status="queued", conclusion="")
        github.attempt = 2
        before_credentials = dict(credentials.values)
        before_remote = dict(store.get_remote_run(TASK_ID) or {})
        before_attempt = dict(store.get_remote_attempt(TASK_ID, 1) or {})
        before_leases = list(store.list_remote_token_leases())
        with self.assertRaisesRegex(RuntimeError, "binding drifted"):
            self.invoke_refresh(credentials, store, app, github)
        self.assertEqual(credentials.values, before_credentials)
        self.assertEqual(store.get_remote_run(TASK_ID), before_remote)
        self.assertEqual(store.get_remote_attempt(TASK_ID, 1), before_attempt)
        self.assertEqual(store.list_remote_token_leases(), before_leases)
        self.assertEqual(github.dispatch_count, 0)
        self.assertEqual(github.rerun_count, 0)

    def test_refresh_crash_windows_converge_without_extra_material(self):
        for point in ("R_marker_before_token", "S_token_before_bundle", "T_bundle_before_marker_delete"):
            with self.subTest(point=point):
                credentials, store, app, github = self.refresh_fixture()
                with self.assertRaisesRegex(RuntimeError, point):
                    self.invoke_refresh(
                        credentials, store, app, github,
                        inject_failure=lambda current: (_ for _ in ()).throw(RuntimeError(current))
                        if current == point else None,
                    )
                app._job_token_leases = 0
                report = self.invoke_refresh(credentials, store, app, github)
                self.assertEqual(report["status"], "refreshed")
                self.assertEqual(len(store.list_remote_token_leases()), 1)
                self.assertEqual(len(credentials.list_secret_names(prefix="remote_result_private:")), 1)
                self.assertEqual(len(credentials.list_secret_names(prefix="process_canary_rerun_bundle:")), 1)
                self.assertEqual(credentials.list_secret_names(prefix="process_canary_rerun_refresh:"), [])
                self.assertEqual(github.dispatch_count, 0)
                self.assertEqual(github.rerun_count, 0)

    def test_refresh_reconciles_stale_attempt_one_from_live_before_rotation(self):
        credentials, store, app, github = self.refresh_fixture()
        store.upsert_remote_attempt(TASK_ID, 1, github_status="queued", conclusion="")
        report = self.invoke_refresh(credentials, store, app, github)
        self.assertEqual(report["status"], "refreshed")
        attempt = store.get_remote_attempt(TASK_ID, 1)
        self.assertEqual(attempt["github_status"], "completed")
        self.assertEqual(attempt["conclusion"], "failure")
        self.assertEqual(github.dispatch_count, 0)
        self.assertEqual(github.rerun_count, 0)

    def test_attempt_one_reconcile_remote_drift_is_all_or_nothing(self):
        _credentials, store, _app, _github = self.refresh_fixture()
        store.upsert_remote_attempt(TASK_ID, 1, github_status="queued", conclusion="")
        store.upsert_remote_run(TASK_ID, remote_state="rerun_payload_ready")
        before_remote = dict(store.get_remote_run(TASK_ID) or {})
        before_one = dict(store.get_remote_attempt(TASK_ID, 1) or {})
        before_two = dict(store.get_remote_attempt(TASK_ID, 2) or {})
        self.assertFalse(store.reconcile_process_canary_attempt_one(
            TASK_ID, repository="owner/template", run_id=RUN_ID,
            github_status="completed", conclusion="failure",
        ))
        self.assertEqual(store.get_remote_run(TASK_ID), before_remote)
        self.assertEqual(store.get_remote_attempt(TASK_ID, 1), before_one)
        self.assertEqual(store.get_remote_attempt(TASK_ID, 2), before_two)

    def test_attempt_one_reconcile_attempt_two_drift_is_all_or_nothing(self):
        _credentials, store, _app, _github = self.refresh_fixture()
        store.upsert_remote_attempt(TASK_ID, 1, github_status="queued", conclusion="")
        store.upsert_remote_attempt(TASK_ID, 2, github_status="queued")
        before_remote = dict(store.get_remote_run(TASK_ID) or {})
        before_one = dict(store.get_remote_attempt(TASK_ID, 1) or {})
        before_two = dict(store.get_remote_attempt(TASK_ID, 2) or {})
        self.assertFalse(store.reconcile_process_canary_attempt_one(
            TASK_ID, repository="owner/template", run_id=RUN_ID,
            github_status="completed", conclusion="failure",
        ))
        self.assertEqual(store.get_remote_run(TASK_ID), before_remote)
        self.assertEqual(store.get_remote_attempt(TASK_ID, 1), before_one)
        self.assertEqual(store.get_remote_attempt(TASK_ID, 2), before_two)

    def test_refresh_complete_marker_crash_converges_without_second_rotation(self):
        credentials, store, app, github = self.refresh_fixture()
        report = self.invoke_refresh(credentials, store, app, github)
        self.assertEqual(report["status"], "refreshed")
        key = credentials.load_secret(f"remote_result_private:{TASK_ID}")
        bundle = credentials.load_secret(_rerun_bundle_name(TASK_ID))
        _save_rerun_refresh_marker(
            credentials, task_id=TASK_ID, run_id=RUN_ID, worker_commit=COMMIT,
            state="complete",
        )
        report = self.invoke_refresh(credentials, store, app, github)
        self.assertEqual(report["status"], "refreshed")
        self.assertEqual(credentials.load_secret(f"remote_result_private:{TASK_ID}"), key)
        self.assertEqual(credentials.load_secret(_rerun_bundle_name(TASK_ID)), bundle)
        self.assertFalse(credentials.has_secret(_rerun_refresh_name(TASK_ID)))
        self.assertEqual(github.dispatch_count, 0)
        self.assertEqual(github.rerun_count, 0)

    def test_strict_v2_latch_rejects_extra_null_and_wrong_budget(self):
        base = {
            "schema": LATCH_V2_SCHEMA, "task_id": TASK_ID,
            "worker_commit": COMMIT, "state": "rerun_armed",
            "run_id": RUN_ID, "attempt_cap": 2, "rerun_budget": 0,
        }
        self.assertEqual(_load_latch(MemoryCredentials({LATCH_SECRET: json.dumps(base)})), base)
        for mutated in (
            {**base, "extra": None},
            {**base, "run_id": None},
            {**base, "rerun_budget": 1.0},
            {**base, "attempt_cap": 3},
        ):
            with self.subTest(mutated=mutated), self.assertRaisesRegex(RuntimeError, "latch is invalid"):
                _load_latch(MemoryCredentials({LATCH_SECRET: json.dumps(mutated)}))

    def test_same_run_attempt_two_success_has_no_dispatch_and_final_zero(self):
        credentials, store, app, github = self.fixture()
        _save_rerun_refresh_marker(
            credentials, task_id=TASK_ID, run_id=RUN_ID, worker_commit=COMMIT,
            state="complete",
        )
        report = self.invoke(credentials, store, app, github)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["run_id"], RUN_ID)
        self.assertEqual(report["attempt"], 2)
        self.assertEqual(github.dispatch_count, 0)
        self.assertEqual(github.rerun_count, 1)
        self.assertEqual(store.get_remote_run(TASK_ID)["remote_state"], "imported")
        self.assertEqual(store.get_remote_run(TASK_ID)["attempt"], 2)
        self.assertEqual(store.list_remote_token_leases(), [])
        self.assertEqual(credentials.list_secret_names(prefix="remote_result_private:"), [])
        self.assertEqual(credentials.list_secret_names(prefix="process_canary_rerun_bundle:"), [])
        self.assertEqual(credentials.list_secret_names(prefix="process_canary_rerun_refresh:"), [])
        latch = _load_latch(credentials)
        self.assertEqual(latch["state"], "complete")
        self.assertEqual(latch["rerun_budget"], 0)
        self.assertFalse(app.worker_token)
        self.assertTrue(github.mailbox_cleaned)
        self.assertTrue(github.artifact_deleted)

    def test_attempt_two_failure_cleans_and_never_allows_attempt_three(self):
        credentials, store, app, github = self.fixture(conclusion="failure")
        with self.assertRaisesRegex(RuntimeError, "attempt failed"):
            self.invoke(credentials, store, app, github)
        self.assertEqual(github.rerun_count, 1)
        self.assertEqual(github.dispatch_count, 0)
        self.assertEqual(_load_latch(credentials)["state"], "failed")
        self.assertEqual(store.list_remote_token_leases(), [])
        with self.assertRaisesRegex(RuntimeError, "already terminal"):
            self.invoke(credentials, store, app, github)
        self.assertEqual(github.rerun_count, 1)

    def test_cas_before_latch_crash_only_cleans_attempt_one_and_never_posts(self):
        credentials, store, app, github = self.fixture()
        with self.assertRaisesRegex(RuntimeError, "H"):
            self.invoke(
                credentials, store, app, github,
                inject_failure=lambda point: (_ for _ in ()).throw(RuntimeError(point))
                if point == "H_cas_before_latch" else None,
            )
        self.assertEqual(github.rerun_count, 0)
        self.assertEqual(store.get_remote_run(TASK_ID)["remote_state"], "rerun_armed")
        self.assertEqual(_load_latch(credentials)["state"], "rerun_payload_ready")
        self.assertEqual(_load_latch(credentials)["rerun_budget"], 1)
        app._job_token_leases = 0  # model a new process retaining only durable state
        with self.assertRaisesRegex(RuntimeError, "attempt-one cleanup completed"):
            self.invoke(credentials, store, app, github)
        self.assertEqual(github.rerun_count, 0)
        self.assertEqual(_load_latch(credentials)["state"], "failed")
        self.assertEqual(store.list_remote_token_leases(), [])
        self.assertEqual(store.migration_cleanup_pending_count(), 0)
        self.assertEqual(credentials.list_secret_names(prefix="remote_result_private:"), [])
        self.assertEqual(credentials.list_secret_names(prefix="process_canary_rerun_bundle:"), [])
        self.assertFalse(app.worker_token)
        self.assertTrue(github.mailbox_cleaned)

    def test_attempt_one_cleanup_pending_retry_never_rearms_or_reruns(self):
        credentials, store, app, github = self.fixture()
        with self.assertRaisesRegex(RuntimeError, "H"):
            self.invoke(
                credentials, store, app, github,
                inject_failure=lambda point: (_ for _ in ()).throw(RuntimeError(point))
                if point == "H_cas_before_latch" else None,
            )
        github.cleanup_failures = 1
        app._job_token_leases = 0
        with self.assertRaisesRegex(RuntimeError, "cleanup remains pending"):
            self.invoke(credentials, store, app, github)
        self.assertEqual(_load_latch(credentials)["state"], "cleanup_pending")
        self.assertEqual(github.rerun_count, 0)
        with self.assertRaisesRegex(RuntimeError, "cleanup completed"):
            self.invoke(credentials, store, app, github)
        self.assertEqual(github.rerun_count, 0)
        self.assertEqual(github.dispatch_count, 0)
        self.assertEqual(_load_latch(credentials)["state"], "failed")
        self.assertEqual(store.list_remote_token_leases(), [])
        self.assertEqual(store.migration_cleanup_pending_count(), 0)
        self.assertFalse(app.worker_token)

    def test_wait_attempt_drift_to_three_fails_before_import_or_completion(self):
        credentials, store, app, github = self.fixture()
        github.rerun_status = "in_progress"
        # get_run calls: initial recovery binding, arm preflight,
        # wait-for-attempt-two, outer check, coordinator admission, then the
        # coordinator's wait loop.
        github.drift_on_rerun_get = 6
        with self.assertRaisesRegex(RuntimeError, "attempt drifted"):
            self.invoke(credentials, store, app, github)
        self.assertEqual(github.rerun_count, 1)
        self.assertEqual(github.dispatch_count, 0)
        self.assertEqual(store.get_remote_run(TASK_ID)["remote_state"], "rerun_running")
        self.assertEqual(_load_latch(credentials)["state"], "rerun_running")
        self.assertFalse(github.artifact_deleted)

    def test_decrypt_then_attempt_drift_never_calls_import(self):
        credentials, store, app, github = self.fixture()

        def decrypt_then_drift(*args, **kwargs):
            result = open_result(*args, **kwargs)
            github.attempt = 3
            return result

        with (
            patch("src.remote.coordinator.open_result", side_effect=decrypt_then_drift),
            patch("scripts.verify_github_process_canary.validate_import") as validate_import,
        ):
            with self.assertRaisesRegex(RuntimeError, "drifted before import"):
                self.invoke(credentials, store, app, github)
        validate_import.assert_not_called()
        self.assertEqual(store.get_remote_run(TASK_ID)["remote_state"], "downloading_result")
        self.assertEqual(_load_latch(credentials)["state"], "rerun_running")

    def test_audit_then_attempt_drift_never_completes_latch(self):
        credentials, store, app, github = self.fixture()

        def audit_then_drift(*args, **kwargs):
            report = self.audit_stub(*args, **kwargs)
            github.attempt = 3
            return report

        with self.assertRaisesRegex(RuntimeError, "before completion latch"):
            self.invoke(credentials, store, app, github, audit=audit_then_drift)
        self.assertEqual(_load_latch(credentials)["state"], "result_verified")
        self.assertEqual(store.get_remote_run(TASK_ID)["remote_state"], "imported")

    def test_post_response_crash_reattaches_attempt_two_without_second_post(self):
        credentials, store, app, github = self.fixture()
        with self.assertRaisesRegex(RuntimeError, "I"):
            self.invoke(
                credentials, store, app, github,
                inject_failure=lambda point: (_ for _ in ()).throw(RuntimeError(point))
                if point == "I_post_before_attempt_observed" else None,
            )
        self.assertEqual(github.rerun_count, 1)
        app._job_token_leases = 0
        report = self.invoke(credentials, store, app, github)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(github.rerun_count, 1)
        self.assertEqual(github.dispatch_count, 0)

    def test_refresh_rebuilds_mailbox_client_and_resume_keeps_prepared_resources_idempotent(self):
        credentials, store, app, github = self.fixture()
        client_tokens = []
        refresh_count = 0

        def acquire_with_refresh(*, task_id, task_store):
            nonlocal refresh_count
            refresh_count += 1
            credentials.save_secret(
                "github_app_access_token",
                "stale-bearer" if refresh_count == 1 else "fresh-bearer",
            )
            return FakeApp.acquire_job_token(app, task_id=task_id, task_store=task_store)

        def client_for(token, **_kwargs):
            client_tokens.append(token)
            github.client_tokens.append(token)
            return github

        app.acquire_job_token = acquire_with_refresh
        github.required_mailbox_token = "fresh-bearer"
        with (
            patch("scripts.verify_github_process_canary.GitHubClient", side_effect=client_for),
            patch("scripts.verify_github_process_canary.build_zero_state_audit", side_effect=self.audit_stub),
        ):
            with self.assertRaisesRegex(RuntimeError, "Mailbox GET 401"):
                rerun_once(
                    credentials=credentials, task_store=store, github_app=app,
                    verifier_go=RERUN_VERIFIER_GO, attempt_visibility_timeout=0,
                )
            self.assertTrue(credentials.has_secret(f"process_canary_rerun_bundle:{TASK_ID}"))
            self.assertTrue(github.issue_open)
            self.assertEqual(github.rerun_count, 0)
            # Resume the prepared bundle after the failed Mailbox GET.  The
            # next token refresh must rebuild the client before that GET.
            app._job_token_leases = 0
            app.worker_token = False
            store.delete_remote_token_lease(TASK_ID)
            credentials.delete_secret("github_remote_token")
            report = rerun_once(
                credentials=credentials, task_store=store, github_app=app,
                verifier_go=RERUN_VERIFIER_GO, attempt_visibility_timeout=0,
            )
        self.assertEqual(report["status"], "passed")
        self.assertIn("fresh-bearer", client_tokens)
        self.assertEqual(github.client_tokens[-1], "fresh-bearer")
        self.assertEqual(github.dispatch_count, 0)
        self.assertEqual(github.rerun_count, 1)

    def test_cleanup_only_abandons_exact_unposted_preparation_without_dispatch_or_rerun(self):
        credentials, store, app, github = self.fixture()
        _save_latch(
            credentials, task_id=TASK_ID, commit=COMMIT,
            state="rerun_preparing", run_id=RUN_ID, rerun_budget=1,
        )
        store.upsert_remote_run(
            TASK_ID, remote_state="rerun_preparing", issue_number=None,
            artifact_id=None, input_hash="", pipeline_version="", checkpoint={},
        )
        store.upsert_remote_attempt(
            TASK_ID, 2, repository="owner/template", workflow="process.yml", run_id=RUN_ID,
            github_status="not_requested", worker_status="planned", worker_stage="payload_preparing",
            import_state="not_started", cleanup_state="not_started", error_code="", observed_at=time.time(),
        )
        credentials.save_secret(f"remote_result_private:{TASK_ID}", "result-key")
        credentials.save_secret(_rerun_bundle_name(TASK_ID), "bundle")
        _save_rerun_refresh_marker(
            credentials, task_id=TASK_ID, run_id=RUN_ID, worker_commit=COMMIT,
            state="started",
        )
        with patch("scripts.verify_github_process_canary.GitHubClient", return_value=github):
            report = cleanup_attempt_one_only(
                credentials=credentials, task_store=store, github_app=app,
                verifier_go=CLEANUP_VERIFIER_GO,
            )
        self.assertEqual(report["status"], "cleaned")
        self.assertEqual(github.dispatch_count, 0)
        self.assertEqual(github.rerun_count, 0)
        self.assertEqual(_load_latch(credentials)["state"], "failed")
        self.assertEqual(_load_latch(credentials)["rerun_budget"], 0)
        self.assertEqual(credentials.list_secret_names(prefix="remote_result_private:"), [])
        self.assertEqual(credentials.list_secret_names(prefix="process_canary_rerun_bundle:"), [])
        self.assertEqual(credentials.list_secret_names(prefix="process_canary_rerun_refresh:"), [])

    def test_cleanup_only_live_attempt_drift_refuses_without_any_cleanup_write(self):
        credentials, store, app, github = self.fixture()
        _save_latch(
            credentials, task_id=TASK_ID, commit=COMMIT,
            state="rerun_preparing", run_id=RUN_ID, rerun_budget=1,
        )
        store.upsert_remote_run(
            TASK_ID, remote_state="rerun_preparing", issue_number=None,
            artifact_id=None, input_hash="", pipeline_version="", checkpoint={},
        )
        store.upsert_remote_attempt(
            TASK_ID, 2, repository="owner/template", workflow="process.yml", run_id=RUN_ID,
            github_status="not_requested", worker_status="planned", worker_stage="payload_preparing",
            import_state="not_started", cleanup_state="not_started", error_code="", observed_at=time.time(),
        )
        credentials.save_secret(f"remote_result_private:{TASK_ID}", "result-key")
        credentials.save_secret(f"process_canary_rerun_bundle:{TASK_ID}", "bundle")
        credentials.save_secret("github_remote_token", "token")
        store.set_remote_token_lease(TASK_ID, state="active", expires_at=time.time() + 60)
        github.attempt = 2
        before_credentials = dict(credentials.values)
        before_remote = dict(store.get_remote_run(TASK_ID) or {})
        before_attempt = dict(store.get_remote_attempt(TASK_ID, 2) or {})
        before_leases = list(store.list_remote_token_leases())
        with patch("scripts.verify_github_process_canary.GitHubClient", return_value=github):
            with self.assertRaisesRegex(RuntimeError, "attempt-one binding drifted"):
                cleanup_attempt_one_only(
                    credentials=credentials, task_store=store, github_app=app,
                    verifier_go=CLEANUP_VERIFIER_GO,
                )
        self.assertEqual(credentials.values, before_credentials)
        self.assertEqual(store.get_remote_run(TASK_ID), before_remote)
        self.assertEqual(store.get_remote_attempt(TASK_ID, 2), before_attempt)
        self.assertEqual(store.list_remote_token_leases(), before_leases)
        self.assertEqual(github.dispatch_count, 0)
        self.assertEqual(github.rerun_count, 0)
        self.assertFalse(github.mailbox_cleaned)
        self.assertFalse(github.artifact_deleted)

    def test_remote_truthy_environment_blocks_rerun_before_github(self):
        for name in ("REMOTE_COMPUTE_ENABLED", "FUDAN_COURSELENS_REMOTE_ENABLED"):
            credentials, store, app, github = self.fixture()
            with self.subTest(name=name), patch.dict("os.environ", {name: "1"}, clear=False):
                with self.assertRaisesRegex(RuntimeError, "remain disabled"):
                    self.invoke(credentials, store, app, github)
            self.assertEqual(github.rerun_count, 0)
            self.assertEqual(github.dispatch_count, 0)


if __name__ == "__main__":
    unittest.main()
