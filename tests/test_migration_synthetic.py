import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from scripts.verify_migration_synthetic import GO, CANCEL_LATCH, cancellation_once, cleanup_terminal_attempt_one, read_only_cancellation_preflight, replay_rejection, tamper_rejection
from src.remote.github_app import GitHubAppClient
from src.runtime.task_store import TaskStore

ROOT = Path(__file__).resolve().parents[1]

class SyntheticGateTests(unittest.TestCase):
    def cancellation_fakes(self):
        class Credentials:
            def __init__(self): self.values = {"remote_enabled": "false", "process_canary_latch": "canonical-success", "github_mailbox_repo": "owner/mailbox"}
            def has_secret(self, key): return key in self.values
            def load_secret(self, key): return self.values[key]
            def save_secret(self, key, value): self.values[key] = value
            def list_secret_names(self, prefix=""): return []
        class Run: run_id = 7
        store = Mock()
        store.list_remote_runs.return_value = []
        return Credentials(), store, Mock(), Mock(), Mock(), Run()

    def cancellation_call(self, creds, store, app, github, coordinator):
        return cancellation_once(credentials=creds, task_store=store, github_app=app, github=github, coordinator=coordinator, verifier_go=GO["cancellation"])

    def test_in_memory_replay_and_tamper_pass(self):
        self.assertEqual(replay_rejection()["status"], "passed")
        tamper = tamper_rejection()
        self.assertEqual(tamper["rejection_count"], 13)
        self.assertEqual(tamper["false_positive_count"], 0)
        self.assertTrue(all(tamper["checks"].values()))
        self.assertEqual(len(tamper["checks"]), 13)

    def test_tamper_mutations_never_noop_across_ephemeral_keys(self):
        for _ in range(100):
            evidence = tamper_rejection()
            self.assertEqual(evidence["false_positive_count"], 0)
            self.assertEqual(set(evidence["checks"].values()), {True})

    def test_default_cli_is_preflight_and_wrong_go_never_runs(self):
        command = [sys.executable, "scripts/verify_migration_synthetic.py", "replay"]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["status"], "preflight")
        bad = subprocess.run(command + ["--run", "--go", "no"], cwd=ROOT, capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)

    def test_live_cancellation_has_no_implicit_dispatch(self):
        result = subprocess.run([sys.executable, "scripts/verify_migration_synthetic.py", "cancellation"], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["status"], "preflight")

    def test_read_only_preflight_is_fail_closed_and_never_discloses_go_when_not_ready(self):
        credentials = Mock()
        credentials.has_secret.return_value = False
        report = read_only_cancellation_preflight(
            credentials=credentials, task_store=Mock(), github_app=Mock()
        )
        self.assertEqual(report["status"], "not_ready")
        self.assertNotIn("required_verifier_go", report)
        self.assertNotIn("required_verifier_go_sha256", report)
        credentials.save_secret.assert_not_called()
        credentials.update_secrets.assert_not_called()

    def test_read_only_preflight_cli_rejects_mixed_action_before_construction(self):
        from scripts.verify_migration_synthetic import main
        with patch("credentials.CredentialStore") as credentials, patch("src.runtime.task_store.TaskStore") as store:
            with self.assertRaises(SystemExit):
                main(["cancellation", "--read-only-cancellation-preflight"])
        credentials.assert_not_called()
        store.assert_not_called()

    def test_cancellation_dispatches_and_cancels_once(self):
        creds, store, app, github, coordinator, Run = self.cancellation_fakes()
        events = []
        worker_token = {"present": False}
        app.acquire_job_token.side_effect = lambda **_kwargs: (events.append("lease"), worker_token.__setitem__("present", True))
        app.finalize_process_canary_token_lease.side_effect = lambda **_kwargs: (events.append("release"), worker_token.__setitem__("present", False))
        github.find_workflow_run.side_effect = [None, Run, Run]
        def dispatch(*_args, **_kwargs):
            self.assertTrue(worker_token["present"])
            events.append("dispatch")
            return Run
        github.dispatch_workflow.side_effect = dispatch
        github.get_run.side_effect = [
            {"id": 7, "head_sha": "a" * 40, "event": "workflow_dispatch", "run_attempt": 1, "status": "queued"},
            {"id": 7, "head_sha": "a" * 40, "event": "workflow_dispatch", "run_attempt": 1, "status": "completed", "conclusion": "cancelled"},
        ]
        with (patch("scripts.verify_github_process_canary.build_preflight", return_value={"expected_worker_commit": "a" * 40}),
              patch("src.remote.coordinator.RemoteSettings.load", return_value=Mock(public_repo="owner/template")),
              patch("scripts.verify_migration_synthetic.build_cancellation_zero_state_audit", return_value={"zero_state": {}})):
            report = cancellation_once(credentials=creds, task_store=store, github_app=app, github=github, coordinator=coordinator, verifier_go=GO["cancellation"])
        self.assertEqual(report["status"], "passed")
        github.dispatch_workflow.assert_called_once(); github.cancel_run.assert_called_once(); coordinator.cleanup_canceled_run.assert_called_once_with(json.loads(creds.values[CANCEL_LATCH])["task_id"])
        app.acquire_job_token.assert_called_once_with(task_id=json.loads(creds.values[CANCEL_LATCH])["task_id"], task_store=store)
        app.finalize_process_canary_token_lease.assert_called_once_with(task_id=json.loads(creds.values[CANCEL_LATCH])["task_id"], task_store=store)
        self.assertEqual(events, ["lease", "dispatch", "release"])
        self.assertFalse(worker_token["present"])
        self.assertEqual(creds.values["process_canary_latch"], "canonical-success")

    def test_dispatch_post_crash_is_durable_observe_only(self):
        creds, store, app, github, coordinator, Run = self.cancellation_fakes()
        github.find_workflow_run.side_effect = [None, Run, Run]
        github.dispatch_workflow.side_effect = RuntimeError("post uncertain")
        github.get_run.side_effect = [{"id":7,"head_sha":"a"*40,"event":"workflow_dispatch","run_attempt":1,"status":"completed","conclusion":"cancelled"}]
        with (patch("scripts.verify_github_process_canary.build_preflight", return_value={"expected_worker_commit":"a"*40}), patch("src.remote.coordinator.RemoteSettings.load", return_value=Mock(public_repo="owner/template")), patch("scripts.verify_migration_synthetic.build_cancellation_zero_state_audit", return_value={"zero_state":{}})):
            self.assertEqual(self.cancellation_call(creds,store,app,github,coordinator)["status"], "passed")
        github.dispatch_workflow.assert_called_once(); github.cancel_run.assert_not_called()

    def test_cancel_post_crash_observes_without_second_cancel(self):
        creds, store, app, github, coordinator, Run = self.cancellation_fakes()
        github.find_workflow_run.return_value = Run; github.cancel_run.side_effect = RuntimeError("post uncertain")
        github.get_run.side_effect = [{"id":7,"head_sha":"a"*40,"event":"workflow_dispatch","run_attempt":1,"status":"queued"},{"id":7,"head_sha":"a"*40,"event":"workflow_dispatch","run_attempt":1,"status":"completed","conclusion":"cancelled"},{"id":7,"head_sha":"a"*40,"event":"workflow_dispatch","run_attempt":1,"status":"completed","conclusion":"cancelled"}]
        with (patch("scripts.verify_github_process_canary.build_preflight", return_value={"expected_worker_commit":"a"*40}), patch("src.remote.coordinator.RemoteSettings.load", return_value=Mock(public_repo="owner/template")), patch("scripts.verify_migration_synthetic.build_cancellation_zero_state_audit", return_value={"zero_state":{}})):
            with self.assertRaisesRegex(RuntimeError, "outcome unknown"): self.cancellation_call(creds,store,app,github,coordinator)
            self.assertEqual(self.cancellation_call(creds,store,app,github,coordinator)["status"], "passed")
        github.cancel_run.assert_called_once(); github.dispatch_workflow.assert_not_called()
        app.acquire_job_token.assert_not_called(); app.finalize_process_canary_token_lease.assert_called_once()

    def test_cancel_post_unknown_observes_three_pending_then_cancelled_without_second_cancel(self):
        creds, store, app, github, coordinator, Run = self.cancellation_fakes()
        github.find_workflow_run.side_effect = [None] + [Run] * 20
        github.dispatch_workflow.return_value = Run
        queued = {"id":7,"head_sha":"a"*40,"event":"workflow_dispatch","run_attempt":1,"status":"queued"}
        done = {**queued, "status":"completed", "conclusion":"cancelled"}
        github.get_run.side_effect = [queued, queued, queued, done, done]
        github.cancel_run.side_effect = RuntimeError("post uncertain")
        with (patch("scripts.verify_github_process_canary.build_preflight", return_value={"expected_worker_commit":"a"*40}), patch("src.remote.coordinator.RemoteSettings.load", return_value=Mock(public_repo="owner/template")), patch("scripts.verify_migration_synthetic.build_cancellation_zero_state_audit", return_value={"zero_state":{}})):
            with self.assertRaises(RuntimeError): self.cancellation_call(creds,store,app,github,coordinator)
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "pending"):
                    self.cancellation_call(creds,store,app,github,coordinator)
                latch=json.loads(creds.values[CANCEL_LATCH]); self.assertEqual(latch["state"], "cancel_post_unknown"); self.assertEqual(latch["cancel_budget"], 0); self.assertEqual(github.cancel_run.call_count, 1); app.finalize_process_canary_token_lease.assert_not_called()
            self.assertEqual(self.cancellation_call(creds,store,app,github,coordinator)["status"], "passed")
        self.assertEqual(github.cancel_run.call_count, 1)
        app.acquire_job_token.assert_called_once()
        app.finalize_process_canary_token_lease.assert_called_once()

    def test_success_race_fails_and_cleans_failed_run(self):
        creds, store, app, github, coordinator, Run = self.cancellation_fakes(); github.find_workflow_run.return_value=Run
        store.get_remote_run.return_value={"run_id":7,"issue_number":0,"artifact_id":0}
        store.list_remote_token_leases.return_value=[]; store.migration_cleanup_pending_count.return_value=0
        github.list_run_artifacts.return_value=[]
        app.access_token.return_value="token"
        app._api.side_effect=lambda _method, path, **kwargs: type("R", (), {"status_code":404, "json":lambda self: [] if "/issues" in path or "/secrets" in path else {"total_count":0,"workflow_runs":[],"artifacts":[]}})()
        github.get_run.return_value={"id":7,"head_sha":"a"*40,"event":"workflow_dispatch","run_attempt":1,"status":"completed","conclusion":"success"}
        with (patch("scripts.verify_github_process_canary.build_preflight", return_value={"expected_worker_commit":"a"*40}), patch("src.remote.coordinator.RemoteSettings.load", return_value=Mock(public_repo="owner/template"))):
            with self.assertRaisesRegex(RuntimeError,"race concluded"): self.cancellation_call(creds,store,app,github,coordinator)
        app.finalize_process_canary_token_lease.assert_called_once(); github.dispatch_workflow.assert_not_called(); github.cancel_run.assert_not_called()

    def test_terminal_race_finalizes_real_lease_after_terminal_state(self):
        class Credentials:
            def __init__(self):
                self.values = {
                    "github_app_access_token": "access",
                    "github_app_access_expires_at": str(10**10),
                    "github_mailbox_repo": "owner/mailbox",
                    "github_remote_token": "worker",
                    "github_worker_repo": "owner/worker",
                }

            def has_secret(self, key): return key in self.values
            def load_secret(self, key): return self.values[key]
            def save_secret(self, key, value): self.values[key] = value
            def delete_secret(self, key): return self.values.pop(key, None) is not None
            def list_secret_names(self, prefix=""): return [key for key in self.values if key.startswith(prefix)]

        class GitHub:
            def __init__(self, conclusion): self.conclusion = conclusion
            def find_workflow_run(self, *_args, **_kwargs): return type("Run", (), {"run_id": 7})()
            def get_run(self, *_args): return {"id": 7, "head_sha": "a" * 40, "event": "workflow_dispatch", "run_attempt": 1, "status": "completed", "conclusion": self.conclusion}
            def list_run_artifacts(self, *_args): return []

        class Response:
            status_code = 204
            def __init__(self, value): self.value = value
            def json(self): return self.value

        for conclusion, live_lease in (("success", 1), ("failure", 0)):
            with self.subTest(conclusion=conclusion, live_lease=live_lease), tempfile.TemporaryDirectory() as directory:
                credentials = Credentials()
                store = TaskStore(Path(directory) / "state.db")
                task_id = "c" * 32
                store.upsert_remote_run(task_id, repository="owner/template", workflow="process.yml", run_id=7, attempt=1, remote_state="canceling")
                store.set_remote_token_lease(task_id, state="active")
                app = GitHubAppClient(credentials, client_id="client-id", app_slug="fudan-courselens")
                app._job_token_leases = live_lease
                app._api = Mock(side_effect=lambda method, *_args, **_kwargs: Response({"secrets": [{"name": "COURSELENS_JOB_TOKEN"}]} if method == "GET" else {}))
                with patch("src.remote.worker_migration.build_signed_template_transition_gate", return_value={"ready": True, "checks": {"zero": True}}):
                    cleanup_terminal_attempt_one(task_id=task_id, repository="owner/template", credentials=credentials, task_store=store, github_app=app, github=GitHub(conclusion), expected_worker_commit="a" * 40)
                self.assertEqual(store.active_remote_run_count(), 0)
                self.assertEqual(store.list_remote_token_leases(), [])
                self.assertEqual(app._job_token_leases, 0)
                self.assertFalse(credentials.has_secret("github_remote_token"))

    def test_race_cleanup_uses_real_transition_gate_and_retries_after_residue(self):
        creds, store, app, github, coordinator, Run = self.cancellation_fakes()
        task_id = "c" * 32
        creds.save_secret(CANCEL_LATCH, json.dumps({"schema":"courselens.migration-synthetic-cancellation-latch.v1","task_id":task_id,"worker_commit":"a"*40,"run_id":7,"state":"cleanup_pending","cleanup_kind":"race_cleanup","dispatch_budget":0,"cancel_budget":0}))
        residue = ["remote_result_private:residue"]
        creds.list_secret_names=lambda prefix="": list(residue) if prefix == "remote_result_private:" else []
        store.get_remote_run.return_value={"run_id":7,"repository":"owner/template","workflow":"process.yml","attempt":1,"issue_number":0,"artifact_id":0}
        store.list_remote_token_leases.return_value=[]; store.migration_cleanup_pending_count.return_value=0
        github.find_workflow_run.return_value=Run; github.get_run.return_value={"id":7,"head_sha":"a"*40,"event":"workflow_dispatch","run_attempt":1,"status":"completed","conclusion":"success"}; github.list_run_artifacts.return_value=[]
        app.access_token.return_value="token"
        class Response:
            status_code=404
            def __init__(self, residue): self.residue=residue
            def json(self): return {"workflow_runs":[],"artifacts":([{"id":9,"name":"courselens-result"}] if self.residue else [])}
        app._api.side_effect=lambda _method, path, **kwargs: type("R", (), {"status_code":404, "json":lambda self: [] if "/issues" in path or "/secrets" in path else {"total_count":0,"workflow_runs":[],"artifacts":[{"id":9,"name":"courselens-result"}]}})()
        with patch("src.remote.coordinator.RemoteSettings.load", return_value=Mock(public_repo="owner/template")):
            with self.assertRaisesRegex(RuntimeError, "global zero audit"):
                self.cancellation_call(creds,store,app,github,coordinator)
        self.assertEqual(json.loads(creds.values[CANCEL_LATCH])["state"], "cleanup_pending")
        residue.clear()
        app._api.side_effect=lambda _method, path, **kwargs: type("R", (), {"status_code":404, "json":lambda self: [] if "/issues" in path or "/secrets" in path else {"total_count":0,"workflow_runs":[],"artifacts":[]}})()
        with patch("src.remote.coordinator.RemoteSettings.load", return_value=Mock(public_repo="owner/template")):
            with self.assertRaisesRegex(RuntimeError, "race concluded"):
                self.cancellation_call(creds,store,app,github,coordinator)
        self.assertEqual(json.loads(creds.values[CANCEL_LATCH])["state"], "failed")

    def test_dispatch_post_find_zero_fails_closed_without_retry(self):
        creds, store, app, github, coordinator, Run = self.cancellation_fakes()
        github.find_workflow_run.side_effect=[None, None]; github.dispatch_workflow.side_effect=RuntimeError("unknown")
        with (patch("scripts.verify_github_process_canary.build_preflight", return_value={"expected_worker_commit":"a"*40}), patch("src.remote.coordinator.RemoteSettings.load", return_value=Mock(public_repo="owner/template"))):
            with self.assertRaisesRegex(RuntimeError,"outcome unknown"): self.cancellation_call(creds,store,app,github,coordinator)
        github.dispatch_workflow.assert_called_once(); github.cancel_run.assert_not_called(); coordinator.cleanup_canceled_run.assert_not_called()
        app.acquire_job_token.assert_called_once(); app.finalize_process_canary_token_lease.assert_not_called()

    def test_multiple_run_discovery_fails_closed_without_cleanup(self):
        creds, store, app, github, coordinator, Run = self.cancellation_fakes()
        github.find_workflow_run.side_effect=RuntimeError("multiple workflow runs")
        with (patch("scripts.verify_github_process_canary.build_preflight", return_value={"expected_worker_commit":"a"*40}), patch("src.remote.coordinator.RemoteSettings.load", return_value=Mock(public_repo="owner/template"))):
            with self.assertRaisesRegex(RuntimeError,"multiple workflow"): self.cancellation_call(creds,store,app,github,coordinator)
        github.dispatch_workflow.assert_not_called(); github.cancel_run.assert_not_called(); coordinator.cleanup_canceled_run.assert_not_called()

    def test_cleanup_pending_reentry_only_retries_cleanup(self):
        creds, store, app, github, coordinator, Run = self.cancellation_fakes()
        creds.save_secret(CANCEL_LATCH, json.dumps({"schema":"courselens.migration-synthetic-cancellation-latch.v1","task_id":"b"*32,"worker_commit":"a"*40,"run_id":7,"state":"cleanup_pending","cleanup_kind":"cancelled_cleanup","dispatch_budget":0,"cancel_budget":0}))
        github.find_workflow_run.return_value=Run; github.get_run.return_value={"id":7,"head_sha":"a"*40,"event":"workflow_dispatch","run_attempt":1,"status":"completed","conclusion":"cancelled"}
        coordinator.cleanup_canceled_run.side_effect=[RuntimeError("pending"), None]
        scope = (patch("scripts.verify_github_process_canary.build_preflight", return_value={"expected_worker_commit":"a"*40}), patch("src.remote.coordinator.RemoteSettings.load", return_value=Mock(public_repo="owner/template")), patch("scripts.verify_migration_synthetic.build_cancellation_zero_state_audit", return_value={"zero_state":{}}))
        with scope[0], scope[1], scope[2]:
            with self.assertRaisesRegex(RuntimeError,"pending"): self.cancellation_call(creds,store,app,github,coordinator)
            self.assertEqual(self.cancellation_call(creds,store,app,github,coordinator)["status"],"passed")
        github.dispatch_workflow.assert_not_called(); github.cancel_run.assert_not_called(); self.assertEqual(coordinator.cleanup_canceled_run.call_count,2)

    def test_signed_run_binding_drift_is_rejected(self):
        for field, value in (("head_sha","b"*40),("event","push"),("run_attempt",2)):
            with self.subTest(field=field):
                creds, store, app, github, coordinator, Run = self.cancellation_fakes(); github.find_workflow_run.return_value=Run
                live={"id":7,"head_sha":"a"*40,"event":"workflow_dispatch","run_attempt":1,"status":"queued"}; live[field]=value; github.get_run.return_value=live
                with (patch("scripts.verify_github_process_canary.build_preflight", return_value={"expected_worker_commit":"a"*40}), patch("src.remote.coordinator.RemoteSettings.load", return_value=Mock(public_repo="owner/template"))):
                    with self.assertRaisesRegex(RuntimeError,"binding drifted"): self.cancellation_call(creds,store,app,github,coordinator)
                github.cancel_run.assert_not_called(); coordinator.cleanup_canceled_run.assert_not_called()
