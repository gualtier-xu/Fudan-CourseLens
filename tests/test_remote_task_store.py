from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.runtime.task_store import (
    ACTIVE_REMOTE_STATES,
    REMOTE_RUN_LIFECYCLE_STATES,
    TaskStore,
)


class RemoteTaskStoreTests(unittest.TestCase):
    def test_v3_metadata_upsert_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.db")
            task, _ = store.add_task("subtitle", "1", "2", {}, config_key="automatic")
            first = store.upsert_v3_metadata(
                task["task_id"], dedupe_key="d" * 64,
                requested_outputs=["subtitle"], stage="queued",
            )
            second = store.upsert_v3_metadata(
                task["task_id"], dedupe_key="d" * 64, stage="remote_running",
            )
            self.assertEqual(first["task_id"], second["task_id"])
            self.assertEqual(store.get_v3_metadata(task["task_id"])["stage"], "remote_running")

    def test_read_only_store_never_creates_missing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.db"
            with self.assertRaises(FileNotFoundError):
                TaskStore(path, read_only=True)
            self.assertFalse(path.exists())

    def test_read_only_store_rejects_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            TaskStore(path)
            store = TaskStore(path, read_only=True)
            with self.assertRaises(Exception):
                store.set_feature_flag("readonly-test", True)
    def test_remote_run_upsert_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.db")
            task, _ = store.add_task("subtitle", "course", "lecture", {})
            created = store.upsert_remote_run(
                task["task_id"],
                repository="owner/public",
                workflow="process.yml",
                run_id=41,
                remote_state="queued",
            )
            self.assertEqual(created["run_id"], 41)
            updated = store.upsert_remote_run(
                task["task_id"],
                remote_state="running",
                issue_number=7,
                input_hash="a" * 64,
                checkpoint={"completed_chunks": 2},
            )
            self.assertEqual(updated["repository"], "owner/public")
            self.assertEqual(updated["remote_state"], "running")
            self.assertEqual(updated["checkpoint"]["completed_chunks"], 2)

    def test_recent_remote_runs_can_be_filtered_by_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.db")
            for index, workflow in enumerate(("echo.yml", "process.yml", "echo.yml"), start=1):
                store.upsert_remote_run(
                    f"task-{index}",
                    repository="owner/public",
                    workflow=workflow,
                    run_id=index,
                    remote_state="imported",
                    imported_at=float(index),
                )
            echoes = store.list_remote_runs(limit=10, workflow="echo.yml")
            self.assertEqual([row["run_id"] for row in echoes], [3, 1])
            self.assertTrue(all(row["workflow"] == "echo.yml" for row in echoes))

    def test_migration_cleanup_count_covers_every_persistent_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.db")
            store.upsert_remote_run(
                "task-1", repository="owner/public", workflow="process.yml",
                run_id=1, last_error="remote_cleanup_pending",
            )
            store.upsert_remote_attempt(
                "task-2", 1, repository="owner/public", workflow="echo.yml",
                run_id=2, cleanup_state="cleanup_pending",
            )
            store.upsert_automation_import(
                3, artifact_name="courselens-cloud-result", state="cleanup_pending"
            )
            self.assertEqual(store.migration_cleanup_pending_count(), 3)

    def test_active_remote_run_count_is_not_truncated_by_recent_terminal_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.db")
            store.upsert_remote_run(
                "active-task", repository="owner/public", workflow="process.yml",
                run_id=1, remote_state="running",
            )
            for index in range(60):
                store.upsert_remote_run(
                    f"terminal-{index}", repository="owner/public",
                    workflow="process.yml", run_id=index + 2,
                    remote_state="imported",
                )
            self.assertEqual(len(store.list_remote_runs(limit=100)), 50)
            self.assertEqual(store.active_remote_run_count(), 1)
            self.assertTrue(store.has_active_remote_run())

    def test_active_remote_run_check_covers_canonical_states(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.db")
            for state in ACTIVE_REMOTE_STATES:
                with self.subTest(state=state):
                    store.upsert_remote_run(
                        "active-task", repository="owner/public", workflow="process.yml",
                        run_id=1, remote_state=state,
                    )
                    self.assertTrue(store.has_active_remote_run())
            store.upsert_remote_run(
                "active-task", repository="owner/public", workflow="process.yml",
                run_id=1, remote_state="imported",
            )
            self.assertFalse(store.has_active_remote_run())

    def test_lifecycle_and_token_cleanup_include_all_active_remote_states(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.db")
            for state in REMOTE_RUN_LIFECYCLE_STATES:
                with self.subTest(state=state):
                    store.upsert_remote_run(
                        "active-task", repository="owner/public", workflow="process.yml",
                        run_id=1, remote_state=state,
                    )
                    self.assertTrue(store.has_active_remote_run())
                    self.assertEqual(store.active_remote_run_count(), 1)
            store.upsert_remote_run(
                "active-task", repository="owner/public", workflow="process.yml",
                run_id=1, remote_state="failed",
            )
            self.assertFalse(store.has_active_remote_run())
            self.assertEqual(store.active_remote_run_count(), 0)

    def test_read_only_gate_counts_allow_terminal_history_and_find_live_records(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.db")
            task, _ = store.add_task("subtitle", "course", "lecture", {})
            store.update_task(task["task_id"], state="completed")
            store.upsert_remote_run("terminal", repository="owner/public", workflow="process.yml", run_id=1, remote_state="imported")
            store.upsert_remote_attempt("terminal", 1, run_id=1, github_status="completed", conclusion="success", worker_status="completed", import_state="imported", cleanup_state="complete")
            store.upsert_automation_import(1, state="imported")
            self.assertEqual(store.count(states=("queued", "running", "pausing", "paused")), 0)
            self.assertEqual(store.active_remote_run_count(), 0)
            self.assertEqual(store.active_remote_attempt_count(), 0)
            self.assertEqual(store.active_automation_import_count(), 0)
            store.upsert_remote_attempt("live", 1, github_status="in_progress")
            store.upsert_automation_import(2, state="importing")
            self.assertEqual(store.active_remote_attempt_count(), 1)
            self.assertEqual(store.active_automation_import_count(), 1)

    def test_active_task_count_is_fail_closed_for_unknown_local_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.db")
            task, _ = store.add_task("subtitle", "course", "lecture", {})
            store.update_task(task["task_id"], state="exception")
            self.assertEqual(store.active_task_count(), 1)
            store.update_task(task["task_id"], state="completed")
            self.assertEqual(store.active_task_count(), 0)

    def test_remote_run_liveness_is_fail_closed_for_every_nonterminal_or_unknown_state(self):
        live = ("created", "queued", "awaiting_payload", "running", "downloading_result", "paused", "canceling", "rerun_preparing", "rerun_payload_ready", "rerun_armed", "rerun_running", "unknown")
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.db")
            for index, state in enumerate(live, start=1):
                store.upsert_remote_run(str(index), repository="owner/public", workflow="process.yml", run_id=index, remote_state=state)
            self.assertEqual(store.active_remote_run_count(), len(live))
            for index, state in enumerate(("completed", "imported", "failed", "canceled"), start=100):
                store.upsert_remote_run(str(index), repository="owner/public", workflow="process.yml", run_id=index, remote_state=state)
            self.assertEqual(store.active_remote_run_count(), len(live))

    def test_remote_attempt_liveness_uses_exact_terminal_parent_relation(self):
        terminal = (
            ("imported", "success", "", "imported"),
            ("canceled", "cancelled", "", "canceled"),
            ("failed", "failure", "failed", "failed"),
        )
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.db")
            for index, (run_state, conclusion, worker, imported) in enumerate(terminal, start=1):
                task_id = f"terminal-{index}"
                store.upsert_remote_run(task_id, repository="owner/public", workflow="process.yml", run_id=index, remote_state=run_state)
                store.upsert_remote_attempt(task_id, 1, run_id=index, github_status="completed", conclusion=conclusion, worker_status=worker, import_state=imported, cleanup_state="complete")
            self.assertEqual(store.active_remote_attempt_count(), 0)
            # Historical tuples, including durable rerun planning, are allowed
            # only when their exact parent run is terminal; orphan is live.
            for index, values in enumerate((
                {"github_status": "mystery"},
                {"github_status": "completed", "conclusion": "success", "import_state": "imported", "cleanup_state": "pending"},
                {"github_status": "not_requested", "worker_status": "planned", "worker_stage": "payload_preparing", "import_state": "not_started", "cleanup_state": "not_started"},
            ), start=10):
                task_id = f"live-{index}"
                store.upsert_remote_run(task_id, repository="owner/public", workflow="process.yml", run_id=index, remote_state="failed")
                store.upsert_remote_attempt(task_id, 1, run_id=index, **values)
            store.upsert_remote_attempt("orphan", 1, run_id=99, github_status="completed", conclusion="success", import_state="imported", cleanup_state="complete")
            self.assertEqual(store.active_remote_attempt_count(), 1)
            store.upsert_remote_run("live-parent", repository="owner/public", workflow="process.yml", run_id=100, remote_state="rerun_running")
            store.upsert_remote_attempt("live-parent", 1, run_id=100, github_status="completed", conclusion="success", import_state="imported", cleanup_state="complete")
            self.assertEqual(store.active_remote_attempt_count(), 2)

    def test_terminal_parent_allows_real_rerun_attempt_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.db")
            store.upsert_remote_run("canary", repository="owner/public", workflow="process.yml", run_id=7, attempt=2, remote_state="imported")
            # Attempt one can legitimately remain best-effort after its rerun.
            store.upsert_remote_attempt("canary", 1, run_id=7, github_status="unknown", worker_status="failed", import_state="failed", cleanup_state="best_effort")
            store.upsert_remote_attempt("canary", 2, run_id=7, github_status="completed", conclusion="success", import_state="imported", cleanup_state="complete")
            self.assertEqual(store.active_remote_attempt_count(), 0)

    def test_unrecoverable_automation_import_is_terminal_but_unknown_is_live(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.db")
            store.upsert_automation_import(1, state="unrecoverable")
            self.assertEqual(store.active_automation_import_count(), 0)
            store.upsert_automation_import(2, state="unknown")
            self.assertEqual(store.active_automation_import_count(), 1)


if __name__ == "__main__":
    unittest.main()
