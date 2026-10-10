from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import Mock

from src.application import CourseLensApplication
from src.runtime.http_api import TASK_ERROR_CODES, make_handler, public_task
from src.runtime.http_api import _task_error_code
from src.runtime.task_store import TaskStore
from tests.http_services import http_services

from tests.frontend_family import family_text


class _TaskService:
    def __init__(self, root: Path):
        self.task_store = TaskStore(root / "state.db")

    @staticmethod
    def authentication_snapshot():
        return {"state": "ready"}

    def control_task(self, task_id: str, action: str):
        task = self.task_store.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if action == "pause":
            task = self.task_store.pause_task(task_id)
        elif action == "resume":
            task = self.task_store.resume_task(task_id)
        elif action == "cancel":
            task = self.task_store.mark_terminal(task_id, "canceled")
        else:
            raise ValueError("unsupported")
        return {"task": task, "accepted_action": action}

    def enqueue_subtitle(self, course_id, sub_id, **kwargs):
        task, _ = self.task_store.add_task(
            "subtitle", course_id, sub_id,
            {"subtitle_mode": "automatic", **kwargs}, config_key="automatic",
        )
        self.enqueue_calls = getattr(self, "enqueue_calls", [])
        self.enqueue_calls.append({
            "course_id": course_id, "sub_id": sub_id, **kwargs,
        })
        return task


class UnifiedTaskCenterTests(unittest.TestCase):
    def test_public_task_exposes_evidence_not_payload_or_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            task, _ = store.add_task(
                "subtitle", "1", "2",
                {"cookie": "secret", "subtitle_mode": "automatic", "source_kind": "remote-actions"},
                config_key="automatic",
            )
            now = time.time()
            task = store.update_task(
                task["task_id"], state="running", started_at=now - 30,
                checkpoint={"signed_url": "must-not-leak"},
                progress={
                    "stage": "asr", "label": "正在识别", "percent": 25,
                    "processed_media_seconds": 75, "media_duration_seconds": 300,
                    "observed_at": now,
                },
                estimate={"elapsed_seconds": 30, "remaining_seconds": 90, "confidence": "medium"},
            )
            value = public_task(task, store, now=now)
            self.assertEqual(value["completed"], 75)
            self.assertEqual(value["total"], 300)
            self.assertEqual(value["remaining_seconds"], 90)
            self.assertEqual(value["actions"], ["pause", "cancel"])
            self.assertNotIn("checkpoint", value)
            self.assertNotIn("cookie", json.dumps(value))
            self.assertNotIn("signed_url", json.dumps(value))

    def test_active_progress_expires_instead_of_staying_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            task, _ = store.add_task("summary", "1", "2", {}, config_key="summary")
            task = store.update_task(
                task["task_id"], state="running",
                progress={"percent": 50, "observed_at": 100.0},
                estimate={"remaining_seconds": 60, "confidence": "high"},
            )
            value = public_task(task, store, now=121.0)
            self.assertTrue(value["stale"])
            self.assertIsNone(value["percent"])
            self.assertIsNone(value["remaining_seconds"])

    def test_legacy_terminal_placeholder_is_not_exposed_as_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            task, _ = store.add_task("subtitle", "1", "2", {}, config_key="legacy")
            task = store.update_task(
                task["task_id"],
                state="failed",
                error="remote failed",
                progress={
                    "stage": "remote_compute",
                    "label": "GitHub runner is processing the encrypted job",
                    "percent": 50,
                },
            )

            value = public_task(task, store)

            self.assertEqual(value["state"], "failed")
            self.assertEqual(value["label"], "任务失败")
            self.assertIsNone(value["percent"])
            self.assertIsNone(value["remaining_seconds"])

    def test_v3_stage_estimate_is_kept_without_measurable_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            task, _ = store.add_task("subtitle", "1", "2", {}, config_key="v3")
            task = store.update_task(
                task["task_id"],
                state="running",
                progress={
                    "schema_version": 3,
                    "stage": "proofread",
                    "label": "正在校对",
                    "percent": 40,
                    "observed_at": time.time(),
                },
            )

            value = public_task(task, store)

            self.assertEqual(value["percent"], 40)

    def test_public_task_exposes_worker_result_notices(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            task, _ = store.add_task("summary", "1", "2", {}, config_key="summary")
            store.upsert_v3_metadata(task["task_id"], dedupe_key="summary:1:2")
            store.set_result_notices(
                task["task_id"],
                {"warnings": ["slides_skipped"], "slides_skipped": {"unidentified_image": 1}},
            )
            value = public_task(store.get_task(task["task_id"]), store)
            self.assertEqual(
                value["result_notices"],
                {"warnings": ["slides_skipped"], "slides_skipped": {"unidentified_image": 1}},
            )

    def test_public_task_defaults_to_empty_result_notices(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            task, _ = store.add_task("summary", "1", "2", {}, config_key="summary")
            value = public_task(store.get_task(task["task_id"]), store)
            self.assertEqual(value["result_notices"], {})
            self.assertNotIn("slides_skipped", json.dumps(value))

    def test_b3_worker_challenge_code_is_admitted_task_vocabulary(self):
        """B3 词汇对齐钉：worker 挑战码进入任务错误词表且不被启发式改写；
        透传白名单是 TASK_ERROR_CODES 的子集；文案键等集由既有钉保证。"""
        from src.application import _REMOTE_WORKER_GUIDANCE_CODES, _task_failure_message

        self.assertIn("platform_challenge_required", TASK_ERROR_CODES)
        self.assertEqual(
            _task_error_code("platform_challenge_required"),
            "platform_challenge_required",
        )
        self.assertLessEqual(_REMOTE_WORKER_GUIDANCE_CODES, TASK_ERROR_CODES)
        exc = RuntimeError("GitHub runner concluded with failure")
        exc.code = "platform_challenge_required"
        self.assertEqual(_task_failure_message(exc), "platform_challenge_required")
        other = RuntimeError("GitHub runner concluded with failure")
        other.code = "platform_unknown_code"
        self.assertEqual(_task_failure_message(other), "RuntimeError:platform_unknown_code")
        self.assertEqual(_task_failure_message(RuntimeError("boom")), "RuntimeError")

    def _subtitle_enqueue_service(self, root: Path, *, coordinator):
        """Partial app carrying exactly the subtitle enqueue gate surface."""
        service = CourseLensApplication.__new__(CourseLensApplication)
        service.task_store = TaskStore(root / "state.db")
        service._lock = threading.RLock()
        service._credentials = {"student_id": "2026001", "password": "secret"}
        service._deepseek_key = Mock(return_value="")
        service.catalog_repository = Mock()
        service.catalog_repository.get_lecture.return_value = {
            "course_id": "c1", "duration_seconds": 0,
        }
        service._refresh_subtitle_task_estimate = Mock(side_effect=lambda task: task)
        service._subtitle_queue = []
        service._ensure_subtitle_worker = Mock()
        # 门集：连接就绪（preflight）+ 协调器存在（U1 起由连接真值建立，无主开关）
        service.remote_connection = Mock()
        service.github_app = Mock()
        service.github_app.snapshot.return_value = {"bootstrapped": True}
        service._reload_remote_coordinator = Mock()
        service.remote_settings = Mock()
        service.remote_coordinator = coordinator
        return service

    def test_subtitle_enqueue_gate_set_admits_manual_tasks_without_automation(self):
        """字幕解耦门正钉：主同意+连接就绪即放行手动字幕——每日自动化
        （enable-cloud/课程选择/验证态）不在入队门集内，未开启也可派发。"""
        with tempfile.TemporaryDirectory() as tmp:
            service = self._subtitle_enqueue_service(Path(tmp), coordinator=object())
            task = service.enqueue_subtitle("c1", "s1")
            self.assertEqual(task["state"], "queued")
            service.remote_connection.preflight.assert_called_once()
            service._ensure_subtitle_worker.assert_called_once()
            stored = service.task_store.get_task(task["task_id"])
            self.assertEqual(stored["state"], "queued")

    def test_subtitle_enqueue_chain_never_consults_automation_state(self):
        """字幕解耦门反钉：入队链源面不读任何 automation 状态（结构级钉）。"""
        source = (Path(__file__).resolve().parents[1] / "src" / "application.py").read_text(
            encoding="utf-8"
        )
        start = source.index("    def enqueue_subtitle(")
        end = source.index("\n    def ", start)
        body = source[start:end].lower()
        self.assertNotIn("automation", body, "入队链不得引用自动化状态")
        self.assertNotIn("enable-cloud", body)

    def test_subtitle_enqueue_still_requires_master_consent(self):
        """门集未松动：主同意缺失（协调器不存在）仍以闭集码拒绝。"""
        with tempfile.TemporaryDirectory() as tmp:
            service = self._subtitle_enqueue_service(Path(tmp), coordinator=None)
            with self.assertRaises(Exception) as captured:
                service.enqueue_subtitle("c1", "s1")
            self.assertEqual(str(getattr(captured.exception, "code", "")), "cloud_setup_required")

    def test_recorded_operation_persists_terminal_backend_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CourseLensApplication.__new__(CourseLensApplication)
            service.task_store = TaskStore(Path(tmp) / "state.db")
            result = service._recorded_operation(
                "quiz", "1", "2", "quiz-v2", {"course_id": "1", "sub_id": "2"},
                lambda: {"count": 3}, result_version="quiz-v2",
            )
            self.assertEqual(result["count"], 3)
            task = service.task_store.list_tasks(limit=1)[0]
            self.assertEqual(task["state"], "completed")
            self.assertEqual(task["progress"]["percent"], 100.0)
            self.assertEqual(task["progress"]["result_version"], "quiz-v2")
            self.assertTrue(task["progress"]["input_hash"])

    def test_recovered_recorded_operation_resumes_without_sticking_in_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CourseLensApplication.__new__(CourseLensApplication)
            service.task_store = TaskStore(Path(tmp) / "state.db")
            service._lock = threading.RLock()
            service._paused = True
            service._classify_smart_timeline_impl = Mock(
                side_effect=ValueError("timeline_transcript_unavailable")
            )
            task, _ = service.task_store.add_task(
                "timeline_classification", "1", "2",
                {"course_id": "1", "sub_id": "2"}, config_key="timeline",
                start_paused=True,
            )
            service.task_store.set_global_paused(True)
            result = service.control_task(task["task_id"], "resume")
            self.assertEqual(result["task"]["state"], "failed")
            self.assertEqual(result["task"]["error"], "timeline_transcript_unavailable")
            self.assertFalse(result["global_paused"])

    def test_crashed_document_import_is_truthfully_non_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CourseLensApplication.__new__(CourseLensApplication)
            service.task_store = TaskStore(Path(tmp) / "state.db")
            task, _ = service.task_store.add_task(
                "document_import", "1", "2", {"content_hash": "abc"},
                config_key="abc",
            )
            service.task_store.update_task(task["task_id"], state="running")
            service.task_store.recover_for_startup()
            service._normalize_recorded_operation_recovery()
            recovered = service.task_store.get_task(task["task_id"])
            self.assertEqual(recovered["state"], "failed")
            self.assertEqual(recovered["error"], "document_payload_not_recoverable")

    def test_remote_restart_recovery_without_material_is_publicly_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CourseLensApplication.__new__(CourseLensApplication)
            service.task_store = TaskStore(Path(tmp) / "state.db")
            service.credentials = Mock()
            service.credentials.has_secret.return_value = False
            service._lock = threading.RLock()
            task, _ = service.task_store.add_task(
                "subtitle", "1", "2", {}, config_key="recovery", start_paused=True,
            )
            service.task_store.upsert_remote_run(
                task["task_id"], repository="owner/worker", workflow="worker.yml",
                run_id=1, remote_state="downloading_result",
            )

            self.assertEqual(service.recover_remote_runs_on_startup(), 0)
            recovered = service.task_store.get_task(task["task_id"])
            public = public_task(recovered, service.task_store)

            self.assertEqual(recovered["state"], "paused")
            self.assertEqual(recovered["error"], "remote_recovery_material_unavailable")
            self.assertEqual(public["error_code"], "remote_recovery_material_unavailable")
            self.assertEqual(public["display_state"], "remote_recovery_material_unavailable")
            self.assertEqual(public["label"], "重启后未自动恢复：本地恢复材料不可用")
            self.assertEqual(public["actions"], ["cancel"])
            self.assertNotIn("remote_result_private", json.dumps(public))

    def test_remote_restart_recovery_with_material_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CourseLensApplication.__new__(CourseLensApplication)
            service.task_store = TaskStore(Path(tmp) / "state.db")
            service.credentials = Mock()
            service.credentials.has_secret.return_value = True
            service._lock = threading.RLock()
            service._queue_persisted_task = Mock()
            service._subtitle_cancel_events = {}
            service._ensure_subtitle_worker = Mock()
            task, _ = service.task_store.add_task(
                "subtitle", "1", "2", {}, config_key="recovery", start_paused=True,
            )
            service.task_store.upsert_remote_run(
                task["task_id"], repository="owner/worker", workflow="worker.yml",
                run_id=1, remote_state="running",
            )

            self.assertEqual(service.recover_remote_runs_on_startup(), 1)
            self.assertEqual(service.task_store.get_task(task["task_id"])["state"], "queued")
            service._queue_persisted_task.assert_called_once()

    def _detach_service(self, tmp: str):
        service = CourseLensApplication.__new__(CourseLensApplication)
        service.task_store = TaskStore(Path(tmp) / "state.db")
        service._lock = threading.RLock()
        service._progress_trackers = {}
        service._paused = False
        service._subtitle_current = None
        # SRC-SYNDROME-1 U5：字幕族按任务记取消事件与活跃集合
        service._subtitle_cancel_events = {}
        service._summary_cancel = threading.Event()
        service._question_cancel = threading.Event()
        service._active_subtitle_task_ids = set()
        service._active_summary_task_id = ""
        service._active_question_task_id = ""
        return service

    def test_pause_cancels_a_live_remote_run_immediately(self):
        """第廿三案（U2 用户拍板）：暂停=立即停止云端机器，不留 run 在云端。"""
        with tempfile.TemporaryDirectory() as tmp:
            service = self._detach_service(tmp)
            service.credentials = Mock()
            service.credentials.has_secret.return_value = True
            service._queue_persisted_task = Mock()
            service._ensure_subtitle_worker = Mock()
            task, _ = service.task_store.add_task(
                "subtitle", "1", "2", {}, config_key="detach-live", start_paused=True,
            )
            service.task_store.update_task(task["task_id"], state="running")
            service.task_store.upsert_remote_run(
                task["task_id"], repository="owner/worker", workflow="worker.yml",
                run_id=1, remote_state="running",
            )
            service._active_subtitle_task_ids = {task["task_id"]}
            service._subtitle_cancel_events = {task["task_id"]: threading.Event()}

            service.pause()

            self.assertTrue(service._subtitle_cancel_events[task["task_id"]].is_set())
            self.assertEqual(
                service.task_store.get_task(task["task_id"])["state"], "pausing"
            )

    def test_user_paused_task_is_not_auto_redispatched_on_startup(self):
        """U1：用户暂停的任务（run 已取消、remote_state=paused）跨重启保持
        暂停——启动恢复绝不拿旧证据自动重新派发，只有用户恢复才全新派发。"""
        with tempfile.TemporaryDirectory() as tmp:
            service = self._detach_service(tmp)
            service.credentials = Mock()
            service.credentials.has_secret.return_value = True
            service._queue_persisted_task = Mock()
            service._ensure_subtitle_worker = Mock()
            task, _ = service.task_store.add_task(
                "subtitle", "1", "2", {}, config_key="user-paused", start_paused=True,
            )
            service.task_store.update_task(task["task_id"], state="paused")
            service.task_store.upsert_remote_run(
                task["task_id"], repository="owner/worker", workflow="worker.yml",
                run_id=1, remote_state="paused",
            )

            self.assertEqual(service.recover_remote_runs_on_startup(), 0)
            self.assertEqual(
                service.task_store.get_task(task["task_id"])["state"], "paused"
            )
            service._queue_persisted_task.assert_not_called()

    def test_pause_still_stops_work_without_a_live_remote_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._detach_service(tmp)
            task, _ = service.task_store.add_task(
                "subtitle", "1", "2", {}, config_key="detach-local", start_paused=True,
            )
            service.task_store.update_task(task["task_id"], state="running")
            service.task_store.upsert_remote_run(
                task["task_id"], repository="owner/worker", workflow="worker.yml",
                run_id=1, remote_state="failed",
            )
            service._active_subtitle_task_ids = {task["task_id"]}
            service._subtitle_cancel_events = {task["task_id"]: threading.Event()}

            service.pause()

            self.assertTrue(service._subtitle_cancel_events[task["task_id"]].is_set())

    def test_single_task_pause_cancels_a_live_remote_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._detach_service(tmp)
            task, _ = service.task_store.add_task(
                "subtitle", "1", "2", {}, config_key="detach-single", start_paused=True,
            )
            service.task_store.update_task(task["task_id"], state="running")
            service.task_store.upsert_remote_run(
                task["task_id"], repository="owner/worker", workflow="worker.yml",
                run_id=1, remote_state="awaiting_payload",
            )
            service._active_subtitle_task_ids = {task["task_id"]}
            service._subtitle_cancel_events = {task["task_id"]: threading.Event()}

            result = service.control_task(task["task_id"], "pause")

            self.assertTrue(service._subtitle_cancel_events[task["task_id"]].is_set())
            self.assertEqual(result["task"]["state"], "pausing")

    def test_explicit_cancel_still_requests_run_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._detach_service(tmp)
            task, _ = service.task_store.add_task(
                "subtitle", "1", "2", {}, config_key="detach-cancel", start_paused=True,
            )
            service.task_store.update_task(task["task_id"], state="running")
            service.task_store.upsert_remote_run(
                task["task_id"], repository="owner/worker", workflow="worker.yml",
                run_id=1, remote_state="running",
            )
            service._active_subtitle_task_ids = {task["task_id"]}
            service._subtitle_cancel_events = {task["task_id"]: threading.Event()}

            result = service.control_task(task["task_id"], "cancel")

            self.assertTrue(service._subtitle_cancel_events[task["task_id"]].is_set())
            self.assertTrue(
                bool(dict(result["task"]["payload"] or {}).get("cancel_requested"))
            )

    def test_parallel_subtitle_workers_spawn_up_to_cloud_run_limit(self):
        """SRC-SYNDROME-1 U5：队列深度 N → 消费者 min(N, cloud_run_limit) 路，
        两个在飞任务真并发（双向屏障证明），队列清空后计数归零。"""
        with tempfile.TemporaryDirectory() as tmp:
            service = self._detach_service(tmp)
            service._subtitle_queue = [{"task_id": f"t{i}"} for i in range(4)]
            service._subtitle_workers = 0
            service._subtitle_worker_seq = 0
            service._cloud_run_limit = Mock(return_value=2)
            # 满载家族第 5 例（FN11 在案：spawn 计时单跑 0.33s 复绿）：按 FN9
            # 升格注记改测内宽限——屏障/轮询 5s→30s，断言语义不变（真回退=
            # 消费者不 spawn/队列不消化，仍会被抓到，只是判慢不判误）。
            barrier = threading.Barrier(2, timeout=30)
            lock = threading.Lock()
            seen: list[str] = []
            # FLAKE-FIX-1：首对过双屏障后钳在 hold 点，计数快照免竞速——
            # 负载注入实测两消费者可在主线程被饿期间自行会师→消化→退出归零
            # （0 != 2 假红；屏障只互钳彼此、不钳主线程，该面与线程创建
            # 压力无关）。断言语义不变，真并发证据仍是双屏障会师。
            hold_pair = threading.Event()

            def _fake_process(item):
                with lock:
                    seen.append(str(item["task_id"]))
                    in_first_pair = len(seen) <= 2
                if in_first_pair:
                    barrier.wait()
                    barrier.wait()
                    hold_pair.wait(timeout=30)

            service._process_subtitle_item = _fake_process

            def _canary_start_failure() -> str | None:
                """FLAKE-FIX-1 金丝雀：受控小规模试创建。None=宿主可创建线程；
                否则返回闭集失败因（资源压力下 _beginthreadex 失败类）。"""
                done = threading.Event()

                def _noop():
                    done.set()

                try:
                    threading.Thread(
                        target=_noop, name="subtitle-spawn-canary", daemon=True
                    ).start()
                except Exception as exc:
                    return f"{type(exc).__name__}: {exc}"
                done.wait(timeout=5)
                if not done.is_set():
                    return "canary_started_but_did_not_run"
                return None

            # FLAKE-FIX-1（F-1 金丝雀+skipTest）：主机级线程创建瞬时压力下，
            # 生产侧 except 静默降级（_ensure_subtitle_worker）是正确语义，不判红。
            # ① spawn 前金丝雀探测（受控小规模试创建）；② ensure 期间对真实
            # spawn 的 Thread.start 挂只记不吞的观测包装——时间点采样探测存在
            # 「探过/失败」错拍，对实际 spawn 取证才是确定性判据。任一门触发 →
            # 闭集 SKIP 记因（顺带捕获压力窗异常实锤文本）；无 start 失败而
            # 计数未满 → 原断言原样判红（真回退不放过，静默吸收回归钉不删；
            # 与 FN9/FN11 升格注记家族同风格，不改断言语义）。
            canary = _canary_start_failure()
            if canary is not None:
                self.skipTest(
                    f"宿主线程创建暂不可用（金丝雀前置探测失败：{canary}），闭集跳过"
                )
            spawn_failures: list[str] = []
            real_start = threading.Thread.start

            def _observing_start(thread, *args, **kwargs):
                try:
                    return real_start(thread, *args, **kwargs)
                except Exception as exc:
                    spawn_failures.append(f"{type(exc).__name__}: {exc}")
                    raise

            threading.Thread.start = _observing_start
            try:
                service._ensure_subtitle_worker()
            finally:
                threading.Thread.start = real_start
            if spawn_failures:
                self.skipTest(
                    "宿主线程创建压力窗口（ensure 期间 "
                    f"{len(spawn_failures)} 次 Thread.start 失败：{spawn_failures[0]}），闭集跳过"
                )
            self.assertEqual(service._subtitle_workers, 2, "4 深队列×上限 2 → 恰 2 路消费者")
            hold_pair.set()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and len(seen) < 4:
                time.sleep(0.01)
            self.assertEqual(sorted(seen), ["t0", "t1", "t2", "t3"], "队列必须被消化完")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and service._subtitle_workers:
                time.sleep(0.01)
            self.assertEqual(service._subtitle_workers, 0, "队列空后消费者退出")

    def test_as1_subtitle_dispatch_releases_global_lock_during_remote(self):
        """AS1：慢远端期间派发线程不得持有全局锁——否则一路在飞字幕派发冻结
        全部请求线程（health/快照读面超时 → 断连横幅 + 刷新卡死）。
        用 0.5s 有界获取证明锁空闲；任务体照常走到完成终态。"""
        with tempfile.TemporaryDirectory() as tmp:
            service = self._detach_service(tmp)
            service.catalog_repository = Mock()
            service.catalog_repository.get_lecture.return_value = {"sub_title": "L1"}
            service._queue_persisted_task = Mock()
            service._request_search_refresh = Mock()
            task, _ = service.task_store.add_task("subtitle", "c1", "s1", {})
            entered = threading.Event()
            release = threading.Event()

            def slow_remote(*args, **kwargs):
                entered.set()
                release.wait(timeout=10)

            service._generate_subtitle_remote = slow_remote
            item = {"course_id": "c1", "sub_id": "s1", "task_id": task["task_id"]}
            worker = threading.Thread(
                target=service._process_subtitle_item, args=(item,), daemon=True
            )
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=5), "任务体未进入慢远端段")
                self.assertTrue(
                    service._lock.acquire(timeout=0.5),
                    "慢远端期间全局锁仍被派发线程持有（AS1 冻结根因复现）",
                )
                service._lock.release()
            finally:
                release.set()
                worker.join(timeout=10)
            self.assertFalse(worker.is_alive(), "任务体未收口")
            self.assertEqual(
                service.task_store.get_task(task["task_id"])["state"],
                "completed",
                "锁外任务体必须照常完成",
            )

    def test_as1_subtitle_task_body_is_lexically_outside_app_lock(self):
        """AS1 结构钉：_process_subtitle_item 的每个 with self._lock 块内不得
        出现慢远端调用（_generate_subtitle_remote）或 sleep——锁内只做共享登记。"""
        import ast

        source = (Path(__file__).resolve().parents[1] / "src" / "application.py").read_text(
            encoding="utf-8-sig"
        )
        target = None
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_process_subtitle_item":
                target = node
                break
        self.assertIsNotNone(target, "未找到 _process_subtitle_item")
        remote_calls = {
            sub.func.attr
            for sub in ast.walk(target)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
        }
        self.assertIn("_generate_subtitle_remote", remote_calls, "钉错了函数：无远端调用")
        for sub in ast.walk(target):
            if not isinstance(sub, ast.With):
                continue
            holds_app_lock = any(
                isinstance(item.context_expr, ast.Attribute) and item.context_expr.attr == "_lock"
                for item in sub.items
            )
            if not holds_app_lock:
                continue
            locked_calls = {
                call.func.attr
                for call in ast.walk(sub)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            }
            self.assertNotIn(
                "_generate_subtitle_remote", locked_calls, "慢远端调用被锁包裹"
            )
            self.assertNotIn("sleep", locked_calls, "锁内出现 sleep")

    def test_cancelling_one_subtitle_task_spares_its_sibling(self):
        """SRC-SYNDROME-1 U5：按任务取消不连坐——只置目标任务的旗标。"""
        with tempfile.TemporaryDirectory() as tmp:
            service = self._detach_service(tmp)
            task_a, _ = service.task_store.add_task(
                "subtitle", "1", "2", {}, config_key="pair-a", start_paused=True,
            )
            task_b, _ = service.task_store.add_task(
                "subtitle", "1", "3", {}, config_key="pair-b", start_paused=True,
            )
            service._active_subtitle_task_ids = {task_a["task_id"], task_b["task_id"]}
            service._subtitle_cancel_events = {
                task_a["task_id"]: threading.Event(),
                task_b["task_id"]: threading.Event(),
            }
            service.task_store.update_task(task_a["task_id"], state="running")
            service.task_store.update_task(task_b["task_id"], state="running")

            service.control_task(task_a["task_id"], "cancel")

            self.assertTrue(service._subtitle_cancel_events[task_a["task_id"]].is_set())
            self.assertFalse(
                service._subtitle_cancel_events[task_b["task_id"]].is_set(),
                "取消 A 不得连坐在飞的 B",
            )

    def test_cancel_stopped_remote_recovery_converges_without_remote_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CourseLensApplication.__new__(CourseLensApplication)
            service.task_store = TaskStore(Path(tmp) / "state.db")
            service.credentials = Mock()
            service.credentials.has_secret.return_value = False
            service._lock = threading.RLock()
            service._active_subtitle_task_ids = set()
            service._subtitle_cancel_events = {}
            service._active_summary_task_id = ""
            service._active_question_task_id = ""
            service._remote_echo_thread = None
            task, _ = service.task_store.add_task(
                "subtitle", "1", "2", {}, config_key="recovery", start_paused=True,
            )
            service.task_store.upsert_remote_run(
                task["task_id"], repository="owner/worker", workflow="worker.yml",
                run_id=1, remote_state="downloading_result",
            )
            service.recover_remote_runs_on_startup()
            self.assertTrue(service.has_active_work())

            canceled = service.control_task(task["task_id"], "cancel")["task"]
            remote = service.task_store.get_remote_run(task["task_id"])

            self.assertEqual(canceled["state"], "canceled")
            self.assertEqual(remote["remote_state"], "failed")
            self.assertEqual(remote["last_error"], "remote_recovery_material_unavailable")
            self.assertFalse(service.has_active_work())
            with self.assertRaisesRegex(ValueError, "task_cancel_requires_active_state"):
                service.control_task(task["task_id"], "cancel")

            unrelated, _ = service.task_store.add_task(
                "subtitle", "1", "3", {}, config_key="unrelated", start_paused=True,
            )
            service.task_store.upsert_remote_run(
                unrelated["task_id"], repository="owner/worker", workflow="worker.yml",
                run_id=2, remote_state="running",
            )
            service.control_task(unrelated["task_id"], "cancel")
            self.assertEqual(
                service.task_store.get_remote_run(unrelated["task_id"])["remote_state"], "running"
            )

            unconfirmed, _ = service.task_store.add_task(
                "subtitle", "1", "4", {}, config_key="unconfirmed", start_paused=True,
            )
            service.task_store.update_task(
                unconfirmed["task_id"], error="remote_recovery_material_unavailable"
            )
            with self.assertRaisesRegex(ValueError, "remote_recovery_cancel_not_safe"):
                service.control_task(unconfirmed["task_id"], "cancel")
            self.assertEqual(service.task_store.get_task(unconfirmed["task_id"])["state"], "paused")

    def test_subtitle_enqueue_accepts_bounded_excerpt_and_rejects_invalid_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = _TaskService(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(service), root))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"

                def enqueue(payload: dict):
                    body = json.dumps({
                        "kind": "subtitle", "course_id": "c1", "sub_id": "s1", **payload,
                    }).encode()
                    return Request(
                        f"{base}/api/v3/tasks/enqueue", data=body,
                        headers={"Content-Type": "application/json"}, method="POST",
                    )

                with urlopen(enqueue({
                    "start_seconds": 30, "duration_seconds": 300,
                })) as response:
                    self.assertEqual(response.status, 202)
                self.assertEqual(service.enqueue_calls[-1], {
                    "course_id": "c1", "sub_id": "s1",
                    "start_seconds": 30.0, "duration_seconds": 300.0,
                })

                with urlopen(enqueue({})) as response:
                    self.assertEqual(response.status, 202)
                self.assertEqual(service.enqueue_calls[-1], {
                    "course_id": "c1", "sub_id": "s1",
                })

                with self.assertRaises(HTTPError) as rejected:
                    urlopen(enqueue({"duration_seconds": -5}))
                self.assertEqual(rejected.exception.code, 400)
                self.assertEqual(
                    json.loads(rejected.exception.read())["error_code"], "task_excerpt_invalid"
                )
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(enqueue({"start_seconds": "soon"}))
                self.assertEqual(rejected.exception.code, 400)
                self.assertEqual(
                    json.loads(rejected.exception.read())["error_code"], "task_excerpt_invalid"
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_enqueue_catalog_rejection_stays_in_closed_set_not_runtime_escape(self):
        """目录守卫拒绝（讲次与 course_id 不匹配）必须落在任务词表闭集码上：
        曾以 FileNotFoundError 逃过出口闭集、被顶层兜底洗成 500
        runtime_failed（MEDIA-RETRY-1 实证），此钉防回退。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = _TaskService(root)

            def reject_out_of_catalog(course_id, sub_id, **kwargs):
                error = RuntimeError("Lecture is not in the authorized catalog")
                error.code = "lecture_not_found"
                raise error

            service.enqueue_subtitle = reject_out_of_catalog
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(service), root))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                body = json.dumps({
                    "kind": "subtitle", "course_id": "c1", "sub_id": "s1",
                }).encode()
                request = Request(
                    f"{base}/api/v3/tasks/enqueue", data=body,
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(request)
                self.assertEqual(rejected.exception.code, 400)
                payload = json.loads(rejected.exception.read())
                self.assertIn(payload["error_code"], TASK_ERROR_CODES)
                self.assertNotEqual(payload["error_code"], "runtime_failed")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_enqueue_explicit_lecture_not_found_code_survives_to_student(self):
        """APP500-CLOSE 同族收口（码吞没腿）：enqueue 源上抛的显式码
        lecture_not_found 曾被 _task_error_code 吞成通用 task_failed——闭集码
        到不了学生面前，前端「这门讲次不在当前课程目录里了。刷新目录即可同步。」
        的人话文案收不到该码。显式码必须逐字幸存到响应（不放宽 HTTP 400 形态）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = _TaskService(root)

            def reject_out_of_catalog(course_id, sub_id, **kwargs):
                error = RuntimeError("Lecture is not in the authorized catalog")
                error.code = "lecture_not_found"
                raise error

            service.enqueue_subtitle = reject_out_of_catalog
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(service), root))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                body = json.dumps({
                    "kind": "subtitle", "course_id": "c1", "sub_id": "s1",
                }).encode()
                request = Request(
                    f"{base}/api/v3/tasks/enqueue", data=body,
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(request)
                self.assertEqual(rejected.exception.code, 400)
                payload = json.loads(rejected.exception.read())
                self.assertEqual(payload["error_code"], "lecture_not_found")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_lecture_not_found_is_registered_closed_code_vocabulary(self):
        """APP500-CLOSE 合同钉（防码漂移）：lecture_not_found 入任务词表闭集；
        显式码直通；自然语言消息形态（无显式码）仍归 task_failed——扩键只
        放行显式码，不放宽启发式兜底。"""
        self.assertIn("lecture_not_found", TASK_ERROR_CODES)
        explicit = RuntimeError("Lecture is not in the authorized catalog")
        explicit.code = "lecture_not_found"
        self.assertEqual(_task_error_code(explicit), "lecture_not_found")
        plain = RuntimeError("Lecture is not in the authorized catalog")
        self.assertEqual(_task_error_code(plain), "task_failed")

    def test_task_action_is_idempotent_and_returns_confirmed_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = _TaskService(root)
            task, _ = service.task_store.add_task("subtitle", "1", "2", {}, config_key="automatic")
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(service), root))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                body = json.dumps({
                    "task_id": task["task_id"], "action": "pause",
                    "operation_id": "task-operation-0001",
                }).encode()
                request = lambda: Request(
                    f"{base}/api/v3/tasks/actions", data=body,
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with urlopen(request()) as response:
                    first = json.loads(response.read())["data"]
                with urlopen(request()) as response:
                    second = json.loads(response.read())["data"]
                self.assertEqual(first["task"]["state"], "paused")
                self.assertEqual(second["task"]["state"], "paused")
                self.assertEqual(first["operation"]["state"], "accepted")
                conflict_body = json.dumps({
                    "task_id": task["task_id"], "action": "cancel",
                    "operation_id": "task-operation-0001",
                }).encode()
                with self.assertRaises(HTTPError) as conflict:
                    urlopen(Request(
                        f"{base}/api/v3/tasks/actions", data=conflict_body,
                        headers={"Content-Type": "application/json"}, method="POST",
                    ))
                self.assertEqual(conflict.exception.code, 409)
                self.assertEqual(
                    json.loads(conflict.exception.read())["error_code"], "operation_id_conflict"
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_task_action_input_gate_cross_task_replay_and_failed_ledger(self):
        """T5-R（夜14-R7 P1 幂等账本 http_api 腿）：①入参闭集执法（坏
        action/缺 task_id/坏 operation_id 形 → 400 task_action_invalid）；
        ②同 operation_id 换目标任务 → 409（防一次授权被挪用到别的任务）；
        ③未知任务 KeyError → 404 task_not_found 且账本落 failed，重放同一
        operation_id 返回存储的 failed 态（finish 落账语义钉）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = _TaskService(root)
            task, _ = service.task_store.add_task("subtitle", "1", "2", {}, config_key="automatic")
            other, _ = service.task_store.add_task("subtitle", "1", "3", {}, config_key="automatic")
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(service), root))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def post(payload):
                return urlopen(Request(
                    f"http://127.0.0.1:{server.server_port}/api/v3/tasks/actions",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"}, method="POST",
                ))

            try:
                for body in (
                    {"task_id": "", "action": "pause", "operation_id": "op-gate-000001"},
                    {"task_id": task["task_id"], "action": "rm_rf", "operation_id": "op-gate-000001"},
                    {"task_id": task["task_id"], "action": "pause", "operation_id": "short"},
                    {"task_id": task["task_id"], "action": "pause"},
                ):
                    with self.subTest(body=body):
                        with self.assertRaises(HTTPError) as rejected:
                            post(body)
                        self.assertEqual(rejected.exception.code, 400)
                        self.assertEqual(
                            json.loads(rejected.exception.read())["error_code"],
                            "task_action_invalid",
                        )

                with post({"task_id": task["task_id"], "action": "pause", "operation_id": "op-cross-000001"}) as response:
                    self.assertEqual(response.status, 202)
                with self.assertRaises(HTTPError) as moved:
                    post({"task_id": other["task_id"], "action": "pause", "operation_id": "op-cross-000001"})
                self.assertEqual(moved.exception.code, 409)
                self.assertEqual(
                    json.loads(moved.exception.read())["error_code"], "operation_id_conflict",
                )

                ghost = "task:does-not-exist"
                with self.assertRaises(HTTPError) as missing:
                    post({"task_id": ghost, "action": "pause", "operation_id": "op-ghost-000001"})
                self.assertEqual(missing.exception.code, 404)
                self.assertEqual(
                    json.loads(missing.exception.read())["error_code"], "task_not_found",
                )
                # 账本重放：同 id 同 action 同目标 → 返回存储的 failed 态，零二次执行。
                with post({"task_id": ghost, "action": "pause", "operation_id": "op-ghost-000001"}) as response:
                    replay = json.loads(response.read())["data"]
                self.assertEqual(replay["operation"]["state"], "failed")
                self.assertEqual(replay["operation"]["error_code"], "task_not_found")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_frontend_contains_non_optimistic_task_center(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "frontend" / "index.html").read_text(encoding="utf-8")
        tasks = family_text("tasks-drawer")
        settings = family_text("settings")
        self.assertIn('id="task-drawer"', html)
        self.assertIn('id="task-list"', html)
        self.assertIn('postV3("tasks/actions"', tasks)
        self.assertIn("await loadTasks(store)", tasks)
        self.assertIn("value.operation?.state", settings)
        self.assertNotRegex(tasks, r"task\.state\s*=(?!=)")
        self.assertNotIn("fixedProgress", tasks)

    def test_frontend_task_progress_uses_backend_evidence_and_stale_copy(self):
        root = Path(__file__).resolve().parents[1]
        js = family_text("tasks-drawer")
        for field in (
            "task.completed", "task.total", "task.percent", "task.remaining_seconds",
            "task.elapsed_seconds", "task.state", "task.progress_unit",
        ):
            self.assertIn(field, js)
        self.assertIn("完成状态确认中", js)  # COPY-SWEEP 20261007：去「后端证据」工程腔
        # 合同 §5.2 ETA 闭集映射（S09-C 更新）：stale → 等待重新确认；
        # 资料不足 → 正在估算；排队三段式：已等待/通常还需/开始后预计
        self.assertIn('return "等待重新确认";', js)
        self.assertIn('return "重新估算中";', js)
        self.assertIn("正在估算 · 已运行 ${friendlyMinutes(elapsed)} 分钟", js)
        self.assertIn("预计约 ${friendlyMinutes(remaining)} 分钟", js)
        self.assertIn("通常还需 ${a}–${b} 分钟开始", js)
        self.assertIn("开始后预计 ${a}–${b} 分钟", js)

    def test_frontend_maps_remote_recovery_stop_to_cancel_only(self):
        root = Path(__file__).resolve().parents[1]
        tasks = family_text("tasks-drawer")
        self.assertIn("remote_recovery_material_unavailable", tasks)
        self.assertIn("重启后未自动恢复：本地恢复材料不可用", tasks)
        self.assertIn("(task.actions || [])", tasks)
        self.assertNotIn('return ["cancel"]', tasks)

    def test_frontend_task_progress_does_not_coerce_missing_evidence_to_zero(self):
        root = Path(__file__).resolve().parents[1]
        js = family_text("tasks-drawer")
        helpers = (
            "const lastEvidenceByTask = new Map();\nfunction taskNumber"
            + js.split("function taskNumber", 1)[1].split("function renderTasks", 1)[0]
        )
        script = "\n".join((
            helpers,
            "const values = [",
            "  { completed: null, total: null, percent: null, remaining_seconds: null },",
            "  { completed: 0, total: 100, remaining_seconds: 0 },",
            "  { stale: true, completed: 50, total: 100, percent: 50, remaining_seconds: 90 },",
            "];",
            "console.log(JSON.stringify(values.map(taskProgressEvidence)));",
        ))
        result = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        missing, zero, stale = json.loads(result.stdout)
        self.assertEqual(missing, "进度未知 · 正在估算")
        self.assertNotIn("0%", missing)
        self.assertNotIn("0 秒", missing)
        self.assertEqual(zero, "进度 0 / 100 项 · 预计剩余 0 秒")
        # SWEEPFIX-2 T5（SWEEP1-T5）：stale 证据行退役（返回空串，不渲染）——
        # 「等待重新确认」+「证据已过期…」双行复读收敛为唯一 stale 注记行
        # 「进度信息已过期，等待重新确认」；本钉守的意图（stale 绝不捏造进度
        # 百分比/秒数）由「空串零捏造 + 缺失/零两形态仍等值钉」共同保持。
        self.assertEqual(stale, "")
        self.assertNotIn("%", stale)
        self.assertNotIn("秒", stale)


if __name__ == "__main__":
    unittest.main()
