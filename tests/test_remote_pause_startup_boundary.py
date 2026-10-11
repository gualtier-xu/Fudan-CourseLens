from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from src.application import CourseLensApplication
from src.runtime.task_store import TaskStore, USER_PAUSE_INTENT_KEY


class RemotePauseStartupBoundaryTests(unittest.TestCase):
    @staticmethod
    def _service(db_path: Path) -> CourseLensApplication:
        service = CourseLensApplication.__new__(CourseLensApplication)
        service.task_store = TaskStore(db_path)
        service.credentials = Mock()
        service.credentials.has_secret.return_value = True
        service._lock = threading.RLock()
        service._progress_trackers = {}
        service._active_subtitle_task_ids = set()
        service._subtitle_cancel_events = {}
        service._active_summary_task_id = ""
        service._active_question_task_id = ""
        service._summary_cancel = threading.Event()
        service._question_cancel = threading.Event()
        service._queue_persisted_task = Mock()
        service._ensure_subtitle_worker = Mock()
        return service

    @staticmethod
    def _running_remote_task(service: CourseLensApplication) -> dict:
        task, _ = service.task_store.add_task(
            "subtitle", "1", "2", {}, config_key="startup-boundary"
        )
        service.task_store.update_task(task["task_id"], state="running")
        service.task_store.upsert_remote_run(
            task["task_id"], repository="owner/worker", workflow="worker.yml",
            run_id=1, remote_state="running",
        )
        return service.task_store.get_task(task["task_id"])

    def test_explicit_pause_then_exit_does_not_reattach_on_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            before_exit = self._service(db_path)
            task = self._running_remote_task(before_exit)

            before_exit.control_task(task["task_id"], "pause")
            restarted = self._service(db_path)
            restarted.task_store.recover_for_startup()

            self.assertEqual(restarted.recover_remote_runs_on_startup(), 0)
            recovered = restarted.task_store.get_task(task["task_id"])
            self.assertEqual(recovered["state"], "paused")
            self.assertIs(recovered["payload"][USER_PAUSE_INTENT_KEY], True)
            restarted._queue_persisted_task.assert_not_called()

    def test_unpaused_exit_still_recovers_remote_run_on_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            before_exit = self._service(db_path)
            task = self._running_remote_task(before_exit)

            restarted = self._service(db_path)
            restarted.task_store.recover_for_startup()

            self.assertEqual(restarted.recover_remote_runs_on_startup(), 1)
            self.assertEqual(
                restarted.task_store.get_task(task["task_id"])["state"], "queued"
            )
            restarted._queue_persisted_task.assert_called_once()


if __name__ == "__main__":
    unittest.main()
