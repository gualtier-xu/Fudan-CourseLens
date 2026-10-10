"""AS2（CLOSED-RESULT-IMPORT-1）行为钉。

现场：客户端关闭期完成的云端任务，本地可能仍停在失败卡上。51 号建了
「启动核真值+验签导入」链，但按远端行驱动、只看最近 10 条，且启动窗
真值读取失败（retry）后无人重排；显式导入入口此前也不存在。本文件钉住
AS2 的加性行为：
1. U1 启动补扫把「有可核远端 run 的终态 failed 行」纳入同一条核真值链
   （success→导入、实败→保持 failed、真值未定→交有界核对者），判据零放宽；
2. U2 失败/暂停卡出现「导入远端结果」显式动作，走同一通道；显式暂停行、
   无 run 行、超窗行分别诚实拒绝；渲染/读面/自动规则绝不自动触发导入；
3. U3 错误文本内嵌的闭集码按码表归位，不再被 github 字样吞成 remote_failed。

51 合同回归（tests/test_lost_result_recovery.py）仍是硬门，本文件只加不改。
"""

from __future__ import annotations

import re
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
    REMOTE_RESULT_IMPORT_WINDOW_SECONDS,
)
from tests.frontend_family import family_text
from src.remote.github_app import GitHubAppError
from src.runtime.http_api import (
    TASK_ERROR_CODES,
    _REMOTE_IMPORT_CANDIDATE_STATES,
    _REMOTE_IMPORT_WINDOW_SECONDS,
    _task_error_code,
    public_task,
)
from src.runtime.task_store import TaskStore

AS2_NEW_CODES = ("worker_tree_drifted", "remote_import_unavailable", "remote_import_expired")


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
    """Minimal stand-in for RemoteCoordinator's public surface (51 同型)."""

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
        import_result({
            "input_hash": "input-hash-1",
            "outputs": {"subtitle": {"srt": "1\n", "vtt": "WEBVTT\n", "segments": [{"start": 0.0}]}},
        })
        self.store.upsert_remote_run(
            task_id, remote_state="imported", input_hash="input-hash-1",
        )
        return {"input_hash": "input-hash-1"}


class ClosedResultImportTests(unittest.TestCase):
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
    def _failed_task(
        service: CourseLensApplication,
        *,
        state: str = "failed",
        remote_state: str = "running",
        dispatched_at: float | None = None,
        with_run: bool = True,
        pause_intent: bool = False,
    ) -> dict:
        task, _ = service.task_store.add_task(
            "subtitle", "course-1", "sub-1", {}, config_key="closed-result"
        )
        if pause_intent:
            service.task_store.pause_task(task["task_id"])
        service.task_store.update_task(task["task_id"], state=state)
        if with_run:
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
        return service.task_store.get_task(task["task_id"])

    # ------------------------------------------------------- U1 启动补扫

    def test_startup_sweep_imports_concluded_success_on_failed_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._failed_task(service)
            coordinator = _FakeCoordinator(
                service.task_store, {"status": "completed", "conclusion": "success"}
            )
            service.remote_coordinator = coordinator

            outcomes = service._reconcile_terminal_failed_tasks_on_startup()

            self.assertEqual(outcomes, {"imported": 1})
            self.assertEqual(coordinator.allow_dispatch_seen, [False], "追补导入绝不允许派发新 run")
            service._import_remote_subtitle.assert_called_once()
            self.assertEqual(service.task_store.get_task(task["task_id"])["state"], "completed")
            self.assertEqual(
                service.task_store.get_remote_run(task["task_id"])["remote_state"], "imported"
            )

    def test_startup_sweep_is_idempotent_across_reruns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._failed_task(service)
            coordinator = _FakeCoordinator(
                service.task_store, {"status": "completed", "conclusion": "success"}
            )
            service.remote_coordinator = coordinator

            self.assertEqual(
                service._reconcile_terminal_failed_tasks_on_startup(), {"imported": 1}
            )
            self.assertEqual(
                service._reconcile_terminal_failed_tasks_on_startup(), {},
                "重启重入不得重复导入",
            )
            self.assertEqual(coordinator.execute_calls, 1)
            self.assertEqual(service.task_store.get_task(task["task_id"])["state"], "completed")

    def test_startup_sweep_keeps_genuinely_failed_rows_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._failed_task(service, remote_state="failed")
            coordinator = _FakeCoordinator(
                service.task_store, {"status": "completed", "conclusion": "failure"}
            )
            service.remote_coordinator = coordinator

            self.assertEqual(
                service._reconcile_terminal_failed_tasks_on_startup(), {"failed": 1}
            )
            latest = service.task_store.get_task(task["task_id"])
            self.assertEqual(latest["state"], "failed", "远端实败不撤销终态")
            self.assertEqual(latest["error"], REMOTE_RESULT_FAILURE_CODE)
            service._import_remote_subtitle.assert_not_called()

    def test_startup_sweep_hands_unreadable_truth_to_the_bounded_watcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._failed_task(service)
            service.remote_coordinator = _FakeCoordinator(service.task_store, None)

            self.assertEqual(
                service._reconcile_terminal_failed_tasks_on_startup(), {"retry": 1}
            )
            self.assertEqual(service.task_store.get_task(task["task_id"])["state"], "failed")
            service._schedule_remote_result_watch.assert_called_once_with(task["task_id"])

    def test_startup_sweep_reaches_rows_the_run_listing_window_misses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._failed_task(service)
            time.sleep(0.05)
            for index in range(10):
                service.task_store.upsert_remote_run(
                    f"filler-{index}",
                    repository="owner/worker",
                    workflow="worker.yml",
                    run_id=9000 + index,
                    attempt=1,
                    remote_state="imported",
                    dispatched_at=time.time(),
                )
            coordinator = _FakeCoordinator(
                service.task_store, {"status": "completed", "conclusion": "success"}
            )
            service.remote_coordinator = coordinator

            run_centric = service.reconcile_remote_results_on_startup()
            self.assertNotIn("imported", run_centric, "近 10 条远端行窗口外：51 对账看不到目标行")
            self.assertEqual(
                service._reconcile_terminal_failed_tasks_on_startup(), {"imported": 1},
                "任务行补扫把窗口外的存量 failed 行接回来",
            )
            self.assertEqual(service.task_store.get_task(task["task_id"])["state"], "completed")

    def test_startup_sweep_skips_rows_without_verifiable_material(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            self._failed_task(service, with_run=False)
            service.remote_coordinator = _FakeCoordinator(
                service.task_store, {"status": "completed", "conclusion": "success"}
            )

            self.assertEqual(
                service._reconcile_terminal_failed_tasks_on_startup(), {},
                "无远端 run 的行没有可核对的真值，扫而不动",
            )

    # ------------------------------------------------------- U2 显式导入

    def test_manual_import_via_control_task_recovers_failed_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._failed_task(service)
            coordinator = _FakeCoordinator(
                service.task_store, {"status": "completed", "conclusion": "success"}
            )
            service.remote_coordinator = coordinator

            result = service.control_task(task["task_id"], "import_result")

            self.assertEqual(result["accepted_action"], "import_result")
            self.assertEqual(service.task_store.get_task(task["task_id"])["state"], "completed")
            self.assertEqual(coordinator.allow_dispatch_seen, [False], "手动导入同走免派发验签通道")

    def test_manual_import_refuses_without_verifiable_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._failed_task(service, with_run=False)
            service.remote_coordinator = _FakeCoordinator(
                service.task_store, {"status": "completed", "conclusion": "success"}
            )

            with self.assertRaises(ValueError) as caught:
                service.control_task(task["task_id"], "import_result")
            self.assertEqual(str(caught.exception), "remote_import_unavailable")

    def test_manual_import_refuses_rows_beyond_the_import_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._failed_task(
                service,
                dispatched_at=time.time() - REMOTE_RESULT_IMPORT_WINDOW_SECONDS - 60,
            )
            service.remote_coordinator = _FakeCoordinator(
                service.task_store, {"status": "completed", "conclusion": "success"}
            )

            with self.assertRaises(ValueError) as caught:
                service.control_task(task["task_id"], "import_result")
            self.assertEqual(str(caught.exception), "remote_import_expired")

    def test_manual_import_never_touches_user_paused_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._failed_task(service, state="paused", pause_intent=True)
            service.remote_coordinator = _FakeCoordinator(
                service.task_store, {"status": "completed", "conclusion": "success"}
            )

            with self.assertRaises(ValueError) as caught:
                service.control_task(task["task_id"], "import_result")
            self.assertEqual(
                str(caught.exception), "remote_import_unavailable",
                "51 显式暂停语义逐字保留：用户喊停的行不被导入通道翻动",
            )

    def test_manual_import_rejects_non_failed_or_paused_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._failed_task(service, state="completed")

            with self.assertRaises(ValueError) as caught:
                service.control_task(task["task_id"], "import_result")
            self.assertEqual(str(caught.exception), "task_action_invalid")

    def test_manual_import_distinguishes_busy_supervisor_from_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task = self._failed_task(service)
            service.remote_coordinator = _FakeCoordinator(
                service.task_store,
                {"status": "completed", "conclusion": "success"},
                error=GitHubAppError("another supervisor is active", code="remote_supervisor_busy"),
            )

            with self.assertRaises(ValueError) as caught:
                service.control_task(task["task_id"], "import_result")
            self.assertEqual(
                str(caught.exception), "operation_already_running",
                "有监督者在导入：如实说任务在进行中",
            )

            service2 = self._service(Path(tmp) / "state2.db")
            task2 = self._failed_task(service2)
            service2.remote_coordinator = _FakeCoordinator(service2.task_store, None)
            with self.assertRaises(ValueError) as caught2:
                service2.control_task(task2["task_id"], "import_result")
            self.assertEqual(str(caught2.exception), "network_unavailable", "真值读不到：如实说网络")

    # ------------------------------------------------ public_task 可见性组

    def test_public_task_publishes_remote_import_group_only_for_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")

            gated = self._failed_task(service)
            value = public_task(service.task_store.get_task(gated["task_id"]), service.task_store)
            self.assertEqual(value["remote_import"], {"possible": True})

            paused = self._failed_task(service, state="paused")
            value = public_task(service.task_store.get_task(paused["task_id"]), service.task_store)
            self.assertEqual(value["remote_import"], {"possible": True}, "系统暂停卡同可见")

            imported = self._failed_task(service, remote_state="imported")
            value = public_task(service.task_store.get_task(imported["task_id"]), service.task_store)
            self.assertNotIn("remote_import", value, "已导入的行不再诱导导入")

            no_run = self._failed_task(service, with_run=False)
            value = public_task(service.task_store.get_task(no_run["task_id"]), service.task_store)
            self.assertNotIn("remote_import", value, "无 run 行：按钮不出现")

            user_paused = self._failed_task(service, state="paused", pause_intent=True)
            value = public_task(
                service.task_store.get_task(user_paused["task_id"]), service.task_store
            )
            self.assertNotIn("remote_import", value, "显式暂停行不诱导导入（51 语义）")

            expired = self._failed_task(
                service,
                dispatched_at=time.time() - REMOTE_RESULT_IMPORT_WINDOW_SECONDS - 60,
            )
            value = public_task(service.task_store.get_task(expired["task_id"]), service.task_store)
            self.assertNotIn("remote_import", value, "超窗行：按钮不出现")

            queued = self._failed_task(service, state="queued")
            value = public_task(service.task_store.get_task(queued["task_id"]), service.task_store)
            self.assertNotIn("remote_import", value, "非失败/暂停态不出现")

    # ------------------------------------------------------------- U3 透传

    def test_error_text_embedded_codes_pass_through_the_code_table(self) -> None:
        self.assertEqual(
            _task_error_code("GitHubAppError: worker_tree_drifted"),
            "worker_tree_drifted",
            "51 移交案：内嵌闭集码不再被 github 字样吞成 remote_failed",
        )
        self.assertEqual(_task_error_code("runtime failed: remote_import_unavailable"), "remote_import_unavailable")
        # 既有启发式行为逐字不变（无内嵌码时才落兜底）
        self.assertEqual(_task_error_code("github outage"), "remote_failed")
        self.assertEqual(_task_error_code("socket timeout"), "timeout")
        self.assertEqual(_task_error_code("permission denied: x"), "permission_denied")
        self.assertEqual(_task_error_code(""), "")

    def test_new_codes_present_in_backend_table_and_frontend_guidance(self) -> None:
        for code in AS2_NEW_CODES:
            self.assertIn(code, TASK_ERROR_CODES)
        drawer = family_text("tasks-drawer")
        block = drawer[drawer.index("TASK_FAILURE_GUIDANCE = Object.freeze({"):]
        block = block[:block.index("\n});")]
        keys = set(re.findall(r"([a-z0-9_]+): \"", block))
        for code in AS2_NEW_CODES:
            self.assertIn(code, keys, f"前端失败文案缺新键：{code}")

    def test_import_visibility_contract_matches_application_constants(self) -> None:
        self.assertEqual(
            _REMOTE_IMPORT_WINDOW_SECONDS, REMOTE_RESULT_IMPORT_WINDOW_SECONDS,
            "可见性窗口与 51 导入窗同值",
        )
        from src.application import REMOTE_RESULT_RECONCILE_STATES

        self.assertEqual(
            tuple(_REMOTE_IMPORT_CANDIDATE_STATES), tuple(REMOTE_RESULT_RECONCILE_STATES),
            "可见性候选态与 51 真值门白名单同集",
        )

    # --------------------------------------------- 渲染/读面/规则不派发导入

    def test_import_entries_never_reachable_from_rule_or_read_faces(self) -> None:
        """渲染/读面不派发：导入入口只有「启动补扫链」与「显式 control_task」两个。"""
        automation_src = (PROJECT_ROOT / "src" / "runtime" / "automation.py").read_text(encoding="utf-8")
        for symbol in (
            "import_remote_task_result",
            "_reconcile_terminal_failed_tasks_on_startup",
            "_reconcile_remote_task_result",
            '"import_result"',
        ):
            self.assertNotIn(symbol, automation_src, f"自动材料规则面不得触达导入入口：{symbol}")

        app_src = (PROJECT_ROOT / "src" / "application.py").read_text(encoding="utf-8")
        self.assertEqual(
            len(re.findall(r"self\._reconcile_terminal_failed_tasks_on_startup\(\)", app_src)),
            1,
            "启动补扫只在 recover_remote_runs_on_startup 链上调用恰一次",
        )
        self.assertEqual(
            len(re.findall(r"self\.import_remote_task_result\(", app_src)),
            1,
            "手动导入只在 control_task 的显式分支被调用恰一次",
        )
        self.assertEqual(
            len(re.findall(r'"import_result"', (PROJECT_ROOT / "src" / "runtime" / "http_api.py").read_text(encoding="utf-8"))),
            1,
            "http_api 只在 tasks/actions 的动作闭集里出现 import_result 一次",
        )


if __name__ == "__main__":
    unittest.main()
