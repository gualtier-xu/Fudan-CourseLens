"""第四十二案（LOST-RESULT-RECOVERY-1）行为钉。

现场：字幕任务派发后客户端退出，GitHub 上 run 成功完成，本地却被标
``failed · remote_failed``，结果永久悬空。实测真身是「客户端侧闸门失败沿
worker 兜底路径终态化」：媒体授权闸门（Worker 树漂移）在 ``coordinator.execute``
之前求值，本地对远端真值一无所知就下了终态判决；而终态任务再无任何核对路径。

本文件钉住修法的三条行为：
1. 客户端侧闸门失败不再把在飞远端任务打成终态（保持可恢复 + 交结果核对者）；
2. 启动/对账先核远端真值：success→导入、failure→如实 failed、running→继续盯；
3. 导入走既有验签通道并把课程面刷成可见（不新增信任判据、不放宽任何校验）。
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from path_utils import PROJECT_ROOT
from src.application import (
    CourseLensApplication,
    REMOTE_RESULT_FAILURE_CODE,
    REMOTE_RESULT_IMPORT_MAX_ATTEMPTS,
    REMOTE_RESULT_IMPORT_WINDOW_SECONDS,
    REMOTE_TRUTH_PENDING_LABEL,
)
from src.remote.github_app import GitHubAppError
from src.runtime.task_store import USER_PAUSE_INTENT_KEY, TaskStore

_SUBTITLE_RESULT = {
    "input_hash": "input-hash-1",
    "outputs": {"subtitle": {"srt": "1\n", "vtt": "WEBVTT\n", "segments": [{"start": 0.0}]}},
}


class _FakeGitHub:
    def __init__(self, truth: dict | None) -> None:
        self._truth = truth
        self.calls: list[int] = []

    def get_run(self, repository: str, run_id: int) -> dict:
        self.calls.append(run_id)
        if self._truth is None:
            raise RuntimeError("github unreachable")
        return dict(self._truth)


class _FakeCoordinator:
    """Minimal stand-in for RemoteCoordinator's public surface."""

    def __init__(self, store: TaskStore, truth: dict | None, *, error: Exception | None = None) -> None:
        self.settings = SimpleNamespace(public_repo="owner/worker", workflow="worker.yml")
        self.github = _FakeGitHub(truth)
        self.store = store
        self.error = error
        self.allow_dispatch_seen: list[bool] = []
        self.execute_calls = 0

    def execute(self, *, task_id, build_job, import_result, cancel_requested, progress, allow_dispatch):
        self.execute_calls += 1
        self.allow_dispatch_seen.append(bool(allow_dispatch))
        if self.error is not None:
            raise self.error
        import_result(dict(_SUBTITLE_RESULT))
        self.store.upsert_remote_run(
            task_id, remote_state="imported", input_hash="input-hash-1",
        )
        return {"input_hash": "input-hash-1"}


class LostResultRecoveryTests(unittest.TestCase):
    @staticmethod
    def _service(db_path: Path) -> CourseLensApplication:
        service = CourseLensApplication.__new__(CourseLensApplication)
        service.task_store = TaskStore(db_path)
        service.credentials = Mock()
        service.credentials.has_secret.return_value = True
        service._lock = threading.RLock()
        service.catalog_repository = Mock()
        service.learning_store = Mock()
        service._request_search_refresh = Mock()
        service._import_remote_subtitle = Mock()
        service._import_remote_summary_result = Mock()
        service._schedule_remote_result_watch = Mock()
        service.remote_settings = None
        service.remote_coordinator = None
        return service

    @staticmethod
    def _remote_task(
        service: CourseLensApplication,
        *,
        kind: str = "subtitle",
        state: str = "running",
        remote_state: str = "running",
        dispatched_at: float | None = None,
        pause_intent: bool = False,
    ) -> dict:
        task, _ = service.task_store.add_task(
            kind, "course-1", "sub-1", {}, config_key="lost-result"
        )
        service.task_store.update_task(task["task_id"], state=state)
        getter = service.task_store.get_task(task["task_id"])
        if pause_intent:
            service.task_store.pause_task(task["task_id"])
            service.task_store.update_task(task["task_id"], state=state)
        service.task_store.upsert_remote_run(
            task["task_id"],
            repository="owner/worker",
            workflow="worker.yml",
            run_id=4242,
            attempt=1,
            issue_number=7,
            input_hash="input-hash-1",
            remote_state=remote_state,
            dispatched_at=float(dispatched_at if dispatched_at is not None else time.time()),
        )
        assert getter is not None
        return service.task_store.get_task(task["task_id"])

    # ---------------------------------------------------------------- U2 三分支

    def test_concluded_success_imports_without_a_new_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._remote_task(service, state="paused")
            coordinator = _FakeCoordinator(
                service.task_store, {"status": "completed", "conclusion": "success"}
            )
            service.remote_coordinator = coordinator

            outcomes = service.reconcile_remote_results_on_startup()

            self.assertEqual(outcomes, {"imported": 1})
            self.assertEqual(coordinator.allow_dispatch_seen, [False], "追补导入绝不允许派发新 run")
            service._import_remote_subtitle.assert_called_once()
            args = service._import_remote_subtitle.call_args.args
            self.assertEqual(args[0], "course-1")
            self.assertEqual(args[1], "sub-1")
            self.assertEqual(args[2]["input_hash"], "input-hash-1")
            self.assertEqual(service.task_store.get_task(task["task_id"])["state"], "completed")
            service._request_search_refresh.assert_called_once_with(["sub-1"])
            self.assertEqual(
                service.task_store.get_remote_run(task["task_id"])["remote_state"], "imported"
            )

    def test_concluded_success_on_failed_task_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._remote_task(service, state="failed")
            service.remote_coordinator = _FakeCoordinator(
                service.task_store, {"status": "completed", "conclusion": "success"}
            )

            self.assertEqual(service.reconcile_remote_results_on_startup(), {"imported": 1})

            self.assertEqual(service.task_store.get_task(task["task_id"])["state"], "completed")

    def test_concluded_failure_is_reported_honestly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._remote_task(service, state="paused")
            service.remote_coordinator = _FakeCoordinator(
                service.task_store, {"status": "completed", "conclusion": "failure"}
            )

            self.assertEqual(service.reconcile_remote_results_on_startup(), {"failed": 1})

            latest = service.task_store.get_task(task["task_id"])
            self.assertEqual(latest["state"], "failed")
            self.assertEqual(latest["error"], REMOTE_RESULT_FAILURE_CODE)
            self.assertEqual(
                service.task_store.get_remote_run(task["task_id"])["remote_state"], "failed"
            )
            # 课程面同步如实：字幕行 → failed（学生看到可重试）
            self.assertIn(
                "failed",
                [call.kwargs.get("subtitle_status") for call in service.catalog_repository.update_lecture_fields.call_args_list],
            )
            service._import_remote_subtitle.assert_not_called()

    def test_live_run_reopens_a_failed_task_instead_of_leaving_it_dead(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._remote_task(service, state="failed")
            service.remote_coordinator = _FakeCoordinator(
                service.task_store, {"status": "in_progress", "conclusion": ""}
            )

            self.assertEqual(service.reconcile_remote_results_on_startup(), {"reopened": 1})

            reopened = service.task_store.get_task(task["task_id"])
            self.assertEqual(reopened["state"], "paused")
            self.assertEqual(reopened["error"], "")
            self.assertEqual(reopened["progress"]["label"], REMOTE_TRUTH_PENDING_LABEL)
            service._schedule_remote_result_watch.assert_called_once_with(task["task_id"])

    def test_live_run_leaves_an_active_task_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._remote_task(service, state="queued")
            service.remote_coordinator = _FakeCoordinator(
                service.task_store, {"status": "in_progress", "conclusion": ""}
            )

            self.assertEqual(service.reconcile_remote_results_on_startup(), {"running": 1})

            self.assertEqual(service.task_store.get_task(task["task_id"])["state"], "queued")

    def test_unreadable_truth_is_retried_never_concluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._remote_task(service, state="failed")
            service.remote_coordinator = _FakeCoordinator(service.task_store, None)

            self.assertEqual(service.reconcile_remote_results_on_startup(), {"retry": 1})

            self.assertEqual(service.task_store.get_task(task["task_id"])["state"], "failed")

    # ------------------------------------------------------------------ 边界

    def test_runs_outside_the_import_window_are_recorded_not_imported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._remote_task(
                service,
                state="paused",
                dispatched_at=time.time() - REMOTE_RESULT_IMPORT_WINDOW_SECONDS - 60,
            )
            coordinator = _FakeCoordinator(
                service.task_store, {"status": "completed", "conclusion": "success"}
            )
            service.remote_coordinator = coordinator

            self.assertEqual(service.reconcile_remote_results_on_startup(), {"expired": 1})

            self.assertEqual(coordinator.github.calls, [], "超期 run 连真值都不再读")
            service._import_remote_subtitle.assert_not_called()
            self.assertEqual(service.task_store.get_task(task["task_id"])["state"], "paused")

    def test_no_coordinator_is_a_silent_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._remote_task(service, state="paused")

            self.assertEqual(service.reconcile_remote_results_on_startup(), {})

            self.assertEqual(service.task_store.get_task(task["task_id"])["state"], "paused")

    def test_missing_one_time_result_key_is_not_a_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._remote_task(service, state="running")
            service.credentials.has_secret.return_value = False

            self.assertIsNone(service._remote_run_awaiting_truth(task["task_id"]))
            self.assertEqual(service._reconcile_remote_task_result(task["task_id"]), "unrecoverable")

    def test_user_pause_keeps_its_38_case_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._remote_task(service, state="paused", pause_intent=True)
            service.remote_coordinator = _FakeCoordinator(
                service.task_store, {"status": "completed", "conclusion": "success"}
            )

            self.assertIsNone(service._remote_run_awaiting_truth(task["task_id"]))
            self.assertEqual(service.reconcile_remote_results_on_startup(), {"unrecoverable": 1})
            service._import_remote_subtitle.assert_not_called()

            latest = service.task_store.get_task(task["task_id"])
            self.assertEqual(latest["state"], "paused")
            self.assertIs(latest["payload"][USER_PAUSE_INTENT_KEY], True)

    def test_canceled_task_is_never_reopened(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._remote_task(service, state="canceled")
            service.remote_coordinator = _FakeCoordinator(
                service.task_store, {"status": "in_progress", "conclusion": ""}
            )

            self.assertEqual(service.reconcile_remote_results_on_startup(), {"unrecoverable": 1})
            self.assertEqual(service.task_store.get_task(task["task_id"])["state"], "canceled")

    # -------------------------------------------------------------------- U1

    def test_client_side_gate_failure_keeps_the_task_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._remote_task(service, state="running")

            deferred = service._defer_remote_task_failure(
                task["task_id"], kind="subtitle", sub_id="sub-1"
            )

            self.assertTrue(deferred)
            latest = service.task_store.get_task(task["task_id"])
            self.assertEqual(latest["state"], "paused", "在飞远端任务绝不因本地闸门终态化")
            self.assertNotIn(latest["state"], {"failed", "canceled"})
            self.assertEqual(latest["progress"]["label"], REMOTE_TRUTH_PENDING_LABEL)
            self.assertIn(
                REMOTE_TRUTH_PENDING_LABEL,
                [call.kwargs.get("subtitle_progress_label") for call in service.catalog_repository.update_lecture_fields.call_args_list],
            )
            service._schedule_remote_result_watch.assert_called_once_with(task["task_id"])

    def test_defer_declines_when_the_remote_conclusion_is_already_settled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._remote_task(service, state="running", remote_state="failed")

            self.assertFalse(
                service._defer_remote_task_failure(task["task_id"], kind="subtitle", sub_id="sub-1")
            )
            self.assertEqual(service.task_store.get_task(task["task_id"])["state"], "running")
            service._schedule_remote_result_watch.assert_not_called()

    def test_defer_declines_for_an_explicit_user_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._remote_task(service, state="running", pause_intent=True)

            self.assertFalse(
                service._defer_remote_task_failure(task["task_id"], kind="subtitle", sub_id="sub-1")
            )
            service._schedule_remote_result_watch.assert_not_called()

    def test_defer_declines_without_a_live_remote_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task, _ = service.task_store.add_task(
                "subtitle", "course-1", "sub-1", {}, config_key="no-run"
            )

            self.assertFalse(
                service._defer_remote_task_failure(task["task_id"], kind="subtitle", sub_id="sub-1")
            )

    def test_startup_requeue_still_owns_live_runs(self) -> None:
        """回归：真值未定 + 材料在手的暂停任务，仍走既有重排队路径。"""
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._remote_task(service, state="paused")
            service.remote_coordinator = _FakeCoordinator(
                service.task_store, {"status": "in_progress", "conclusion": ""}
            )
            service._queue_persisted_task = Mock()
            service._ensure_subtitle_worker = Mock()

            self.assertEqual(service.recover_remote_runs_on_startup(), 1)
            self.assertEqual(service.task_store.get_task(task["task_id"])["state"], "queued")
            service._queue_persisted_task.assert_called_once()

    # ------------------------------------------------------- 导入失败的界

    def test_repeated_import_failures_are_bounded_then_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._remote_task(service, state="paused")
            service.remote_coordinator = _FakeCoordinator(
                service.task_store,
                {"status": "completed", "conclusion": "success"},
                error=RuntimeError("result artifact does not contain valid JSON"),
            )

            outcomes = [
                service._reconcile_remote_task_result(task["task_id"])
                for _ in range(REMOTE_RESULT_IMPORT_MAX_ATTEMPTS + 1)
            ]

            self.assertEqual(outcomes[:-1], ["deferred"] * REMOTE_RESULT_IMPORT_MAX_ATTEMPTS)
            self.assertEqual(outcomes[-1], "failed", "用满重试后如实上报，不无限挂起")
            latest = service.task_store.get_task(task["task_id"])
            self.assertEqual(latest["state"], "failed")
            self.assertTrue(latest["error"])

    # ------------------------------------------------------ 总结 kind 的收敛面

    def test_summary_result_lands_on_the_course_surface(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._remote_task(service, kind="summary", state="paused")
            coordinator = _FakeCoordinator(
                service.task_store, {"status": "completed", "conclusion": "success"}
            )
            service.remote_coordinator = coordinator

            self.assertEqual(service.reconcile_remote_results_on_startup(), {"imported": 1})

            service._import_remote_summary_result.assert_called_once()
            args = service._import_remote_summary_result.call_args.args
            self.assertEqual((args[0], args[1]), ("course-1", "sub-1"))
            self.assertEqual(service.task_store.get_task(task["task_id"])["state"], "completed")
            # 课程面可见：总结行 → done
            self.assertIn(
                "done",
                [call.kwargs.get("summary_status") for call in service.catalog_repository.update_lecture_fields.call_args_list],
            )


class SubtitleWorkerGateFailureTests(unittest.TestCase):
    """真身接线钉：走真实 `_process_subtitle_item`，不 mock 兜底分支。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        self.app.catalog_repository.upsert_course("course", "Course")
        self.app.catalog_repository.upsert_lecture(
            "course",
            {
                "sub_id": "lecture",
                "sub_title": "Lecture",
                "has_playback": True,
                "duration_seconds": 1200,
            },
        )
        self.app._credentials = {"student_id": "configured", "password": "configured"}
        self.app._ensure_subtitle_worker = Mock()
        # 派发前的连接预检由本文件另一支测试单独覆盖；这里只关心 worker 兜底面
        self.app._prepare_remote_coordinator = Mock(return_value=None)

    def tearDown(self) -> None:
        self.app.close()
        self.temporary.cleanup()

    def _queued_task(self, *, with_remote_run: bool) -> dict:
        task = self.app.enqueue_subtitle("course", "lecture")
        if with_remote_run:
            # 一次性结果密钥=唯一能解密远端产物的材料；在飞任务现场必然在手
            self.app.credentials.save_secret(
                f"remote_result_private:{task['task_id']}", "synthetic-one-time-key"
            )
            self.app.task_store.upsert_remote_run(
                task["task_id"],
                repository="owner/worker",
                workflow="worker.yml",
                run_id=4242,
                attempt=1,
                issue_number=7,
                input_hash="input-hash-1",
                remote_state="running",
                dispatched_at=time.time(),
            )
        return task

    def _item(self, task: dict) -> dict:
        return {
            "task_id": task["task_id"],
            "course_id": "course",
            "sub_id": "lecture",
        }

    def test_gate_failure_on_a_live_run_never_terminalizes(self) -> None:
        task = self._queued_task(with_remote_run=True)
        self.app._generate_subtitle_remote = Mock(
            side_effect=GitHubAppError(
                "专属 Worker 已被修改或版本过旧，已阻止发送媒体授权",
                code="worker_tree_drifted",
            )
        )

        self.app._process_subtitle_item(self._item(task))

        latest = self.app.task_store.get_task(task["task_id"])
        self.assertEqual(latest["state"], "paused", "本地闸门失败不得把在飞远端任务判死")
        self.assertEqual(latest["error"], "")
        self.assertEqual(latest["progress"]["label"], REMOTE_TRUTH_PENDING_LABEL)
        lecture = self.app.catalog_repository.get_lecture("lecture") or {}
        self.assertEqual(lecture.get("subtitle_status"), "paused")
        self.assertEqual(lecture.get("subtitle_error"), "")

    def test_genuine_failure_without_a_remote_run_still_terminates(self) -> None:
        """回归：没有远端 run 的本地失败，照既有兜底路径如实终态化。"""
        task = self._queued_task(with_remote_run=False)
        self.app._generate_subtitle_remote = Mock(side_effect=RuntimeError("boom"))

        self.app._process_subtitle_item(self._item(task))

        latest = self.app.task_store.get_task(task["task_id"])
        self.assertEqual(latest["state"], "failed")
        self.assertIn("RuntimeError", str(latest["error"]))
        lecture = self.app.catalog_repository.get_lecture("lecture") or {}
        self.assertEqual(lecture.get("subtitle_status"), "failed")


if __name__ == "__main__":
    unittest.main()
