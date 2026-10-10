import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from scripts.verify_migration_synthetic import (
    CANCEL_LATCH, CANCEL_SCHEMA, FAILED_CANCELLATION_LOCAL_RECOVERY_GO,
    GO, cancellation_once, recover_failed_cancellation_local_only,
)
from src.runtime.task_store import TaskStore


TASK_ID = "c" * 32
HEAD = "a" * 40
RUN_ID = 71
REPOSITORY = "gualtier-xu/Fudan-CourseLens"


class Credentials:
    def __init__(self, latch):
        self.values = {CANCEL_LATCH: json.dumps(latch)}

    def has_secret(self, key):
        return key in self.values

    def load_secret(self, key):
        return self.values[key]

    def save_secret(self, key, value):
        self.values[key] = value

    def list_secret_names(self, prefix=""):
        return [key for key in self.values if key.startswith(prefix)]


class FailedCancellationLocalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temp.name) / "state.db")
        self.store.upsert_remote_run(
            TASK_ID, repository=REPOSITORY, workflow="process.yml", run_id=RUN_ID,
            attempt=1, remote_state="canceling",
        )
        self.credentials = Credentials({
            "schema": CANCEL_SCHEMA, "task_id": TASK_ID, "worker_commit": HEAD,
            "run_id": RUN_ID, "state": "dispatched", "dispatch_budget": 0,
            "cancel_budget": 1,
        })

    def tearDown(self):
        self.temp.cleanup()

    def recover(self, **kwargs):
        values = {"worker_commit": HEAD, "run_id": RUN_ID}
        values.update(kwargs)
        return recover_failed_cancellation_local_only(
            credentials=self.credentials, task_store=self.store,
            verifier_go=FAILED_CANCELLATION_LOCAL_RECOVERY_GO, task_id=TASK_ID,
            **values,
        )

    def test_records_exact_failed_tombstone(self):
        self.assertEqual(self.recover()["attempt"], 1)
        latch = json.loads(self.credentials.values[CANCEL_LATCH])
        self.assertEqual((latch["state"], latch["dispatch_budget"], latch["cancel_budget"]), ("failed", 0, 0))
        self.assertEqual(latch["cleanup_kind"], "failed_cancellation_local_only")
        self.assertEqual(self.store.get_remote_run(TASK_ID)["remote_state"], "failed")
        attempt = self.store.get_remote_attempt(TASK_ID, 1)
        self.assertEqual(
            {key: attempt[key] for key in ("repository", "workflow", "run_id", "github_status", "conclusion", "worker_status", "import_state", "cleanup_state")},
            {"repository": REPOSITORY, "workflow": "process.yml", "run_id": RUN_ID,
             "github_status": "completed", "conclusion": "failure", "worker_status": "failed",
             "import_state": "not_started", "cleanup_state": "complete"},
        )

    def test_exact_terminal_reentry_is_idempotent(self):
        self.recover()
        self.assertEqual(self.recover()["attempt"], 0)

    def test_sql_failure_rolls_back_after_tombstone_and_can_resume(self):
        with self.store._connect() as db:
            db.execute("CREATE TRIGGER fail_recovery BEFORE INSERT ON remote_run_attempts BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaisesRegex(Exception, "injected"):
            self.recover()
        self.assertEqual(json.loads(self.credentials.values[CANCEL_LATCH])["state"], "cleanup_only")
        self.assertEqual(self.store.get_remote_run(TASK_ID)["remote_state"], "canceling")
        self.assertIsNone(self.store.get_remote_attempt(TASK_ID, 1))
        with self.store._connect() as db:
            db.execute("DROP TRIGGER fail_recovery")
        self.assertEqual(self.recover()["attempt"], 1)

    def test_binding_and_budget_mismatches_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, "latch binding drifted"):
            self.recover(worker_commit="b" * 40)
        self.credentials.values[CANCEL_LATCH] = json.dumps({
            "schema": CANCEL_SCHEMA, "task_id": TASK_ID, "worker_commit": HEAD,
            "run_id": RUN_ID, "state": "dispatched", "dispatch_budget": 0,
            "cancel_budget": 0,
        })
        with self.assertRaisesRegex(RuntimeError, "budget is invalid"):
            self.recover()
        self.assertEqual(self.store.get_remote_run(TASK_ID)["remote_state"], "canceling")

    def test_stale_cleanup_order_is_local_only(self):
        # This is the crash combination audited in production: the durable
        # lease remains while the run is canceling.  Calling the remote helper
        # here would reject the active run and touch a Worker token.
        self.store.set_remote_token_lease(TASK_ID, state="active", expires_at=time.time() + 60)
        with patch("src.remote.github_app.GitHubAppClient.cleanup_stale_job_token_lease") as cleanup:
            with self.assertRaisesRegex(RuntimeError, "residue is present"):
                self.recover()
        cleanup.assert_not_called()
        self.assertEqual(self.store.get_remote_run(TASK_ID)["remote_state"], "canceling")
        self.assertEqual(len(self.store.list_remote_token_leases()), 1)

    def test_cas_state_mismatch_does_not_create_attempt(self):
        self.store.upsert_remote_run(TASK_ID, repository=REPOSITORY, workflow="process.yml", run_id=RUN_ID, attempt=1, remote_state="queued")
        with self.assertRaisesRegex(RuntimeError, "state drifted"):
            self.recover()
        self.assertEqual(self.store.get_remote_run(TASK_ID)["remote_state"], "queued")
        self.assertIsNone(self.store.get_remote_attempt(TASK_ID, 1))

    def test_parent_artifact_residue_is_rejected_without_db_mutation(self):
        self.store.upsert_remote_run(TASK_ID, repository=REPOSITORY, workflow="process.yml", run_id=RUN_ID, attempt=1, remote_state="canceling", artifact_id=9)
        before = self.store.get_remote_run(TASK_ID)
        with self.assertRaisesRegex(RuntimeError, "binding drifted"):
            self.recover()
        self.assertEqual(self.store.get_remote_run(TASK_ID), before)
        self.assertIsNone(self.store.get_remote_attempt(TASK_ID, 1))

    def test_cas_rechecks_artifact_residue_written_after_select(self):
        original_connect = self.store._connect

        @contextmanager
        def interleaved_connect():
            with original_connect() as db:
                class Connection:
                    def execute(_self, sql, parameters=()):
                        result = db.execute(sql, parameters)
                        if "FROM remote_runs WHERE task_id=?" in sql:
                            other = sqlite3.connect(self.store.path)
                            try:
                                other.execute("UPDATE remote_runs SET artifact_id=9 WHERE task_id=?", (TASK_ID,))
                                other.commit()
                            finally:
                                other.close()
                        return result

                    def __getattr__(_self, name):
                        return getattr(db, name)
                yield Connection()

        with patch.object(self.store, "_connect", interleaved_connect):
            with self.assertRaisesRegex(RuntimeError, "compare-and-set failed"):
                self.recover()
        self.assertEqual(json.loads(self.credentials.values[CANCEL_LATCH])["state"], "cleanup_only")
        self.assertEqual(self.store.get_remote_run(TASK_ID)["remote_state"], "canceling")
        self.assertEqual(self.store.get_remote_run(TASK_ID)["artifact_id"], 9)
        self.assertIsNone(self.store.get_remote_attempt(TASK_ID, 1))
        self.store.upsert_remote_run(TASK_ID, repository=REPOSITORY, workflow="process.yml", run_id=RUN_ID, attempt=1, remote_state="canceling", artifact_id=0)
        self.assertEqual(self.recover()["attempt"], 1)

    def test_terminal_attempt_drift_is_rejected(self):
        self.recover()
        with self.store._connect() as db:
            db.execute("UPDATE remote_run_attempts SET artifact_id=9 WHERE task_id=? AND attempt=1", (TASK_ID,))
        with self.assertRaisesRegex(RuntimeError, "terminal record drifted"):
            self.recover()

    def test_tombstone_blocks_the_live_cancellation_path_before_any_helper(self):
        self.credentials.values[CANCEL_LATCH] = json.dumps({
            "schema": CANCEL_SCHEMA, "task_id": TASK_ID, "worker_commit": HEAD,
            "run_id": RUN_ID, "state": "cleanup_only", "cleanup_kind": "failed_cancellation_local_only",
            "dispatch_budget": 0, "cancel_budget": 0,
        })
        with patch("scripts.verify_github_process_canary.build_preflight") as preflight:
            with self.assertRaisesRegex(RuntimeError, "replay is forbidden"):
                cancellation_once(credentials=self.credentials, task_store=self.store,
                                  github_app=object(), github=object(), coordinator=object(), verifier_go=GO["cancellation"])
        preflight.assert_not_called()
