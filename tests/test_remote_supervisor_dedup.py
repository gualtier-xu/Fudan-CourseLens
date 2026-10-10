from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.application import _REMOTE_WORKER_GUIDANCE_CODES, _task_failure_message
from src.remote.coordinator import (
    RemoteCoordinator,
    RemoteSettings,
    RemoteTaskPaused,
)
from src.remote.github_client import DispatchResult, GitHubRemoteError
from src.remote.protocol import (
    PROTOCOL_VERSION,
    RESULT_SCHEMA,
    generate_box_keypair,
    generate_signing_keypair,
    open_job,
)
from src.runtime.task_store import TaskStore


TASK_ID = "0123456789abcdef0123456789abcdef"
OTHER_TASK_ID = "fedcba9876543210fedcba9876543210"


class MemorySecrets:
    def __init__(self):
        self.values = {}

    def save_secret(self, name, value):
        self.values[name] = value

    def load_secret(self, name):
        if name not in self.values:
            raise KeyError(name)
        return self.values[name]

    def has_secret(self, name):
        return name in self.values

    def delete_secret(self, name):
        return self.values.pop(name, None) is not None


class FakeGitHub:
    def __init__(self, worker_private, signing_private):
        self.worker_private = worker_private
        self.signing_private = signing_private
        self.run_reads = 0
        self.envelope = None
        self.cleaned = False
        self.dispatch_count = 0
        self.conclusion = "success"
        self.cancel_count = 0
        self.dispatch_error: Exception | None = None

    def dispatch_workflow(self, *args, **kwargs):
        self.dispatch_count += 1
        if self.dispatch_error is not None:
            raise self.dispatch_error
        return DispatchResult(run_id=99, status="queued")

    def get_run(self, *args, **kwargs):
        self.run_reads += 1
        return {
            "status": "in_progress" if self.run_reads == 1 else "completed",
            "conclusion": "" if self.run_reads == 1 else self.conclusion,
        }

    def get_run_jobs(self, *args, **kwargs):
        return [{"steps": [{"name": "Process encrypted job", "status": "in_progress"}]}]

    def publish_job(self, repo, task_id, envelope):
        self.envelope = envelope
        return {"issue_number": 7, "comment_ids": [8], "part_count": 1}

    def list_run_artifacts(self, *args, **kwargs):
        return [{"id": 12, "name": f"courselens-result-{TASK_ID}", "expired": False}]

    def download_artifact_file(self, repo, artifact_id, filename):
        from src.remote.protocol import (
            PROTOCOL_VERSION,
            RESULT_SCHEMA,
            open_job,
            seal_result,
        )

        job = open_job(self.envelope, self.worker_private)
        result = {
            "schema": RESULT_SCHEMA,
            "protocol_version": PROTOCOL_VERSION,
            "task_id": TASK_ID,
            "job_kind": "echo",
            "input_hash": job["input_hash"],
            "status": "completed",
            "outputs": {"echo": {"ok": True}},
            "metrics": {},
            "warnings": [],
        }
        return json_dumps_sealed(result, job["result_public_key"], self.signing_private)

    def cleanup_job(self, repo, issue_number, **kwargs):
        self.cleaned = True

    def cancel_run(self, *args, **kwargs):
        self.cancel_count += 1


def json_dumps_sealed(result, result_public_key, signing_private):
    import json

    from src.remote.protocol import seal_result

    return json.dumps(
        seal_result(result, result_public_key, signing_private)
    ).encode()


def _build_job(result_public_key):
    now = time.time()
    return {
        "task_id": TASK_ID,
        "job_kind": "echo",
        "created_at": now,
        "expires_at": now + 300,
        "result_public_key": result_public_key,
        "pipeline": {"version": "test"},
        "payload": {},
    }


def _noop_progress(*_args):
    return None


class SupervisorDedupTestBase(unittest.TestCase):
    def _coordinator(self, directory):
        worker_private, worker_public = generate_box_keypair()
        signing_private, signing_public = generate_signing_keypair()
        github = FakeGitHub(worker_private, signing_private)
        secrets = MemorySecrets()
        settings = RemoteSettings(
            enabled=True,
            github_token="token",
            worker_public_key=worker_public,
            worker_signing_public_key=signing_public,
            poll_seconds=0.001,
        )
        store = TaskStore(Path(directory) / "state.db")
        return (
            RemoteCoordinator(settings, store, secrets, github=github),
            store,
            secrets,
            github,
        )


class SupervisorLeaseTests(SupervisorDedupTestBase):
    def test_second_owner_cannot_acquire_while_lease_is_live(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.db")
            self.assertTrue(
                store.acquire_remote_supervisor_lease(
                    TASK_ID, owner="a", ttl_seconds=60
                )
            )
            self.assertFalse(
                store.acquire_remote_supervisor_lease(
                    TASK_ID, owner="b", ttl_seconds=60
                )
            )
            # 同一 owner 再取 = 续约，不是冲突。
            self.assertTrue(
                store.acquire_remote_supervisor_lease(
                    TASK_ID, owner="a", ttl_seconds=60
                )
            )

    def test_expired_lease_is_preempted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.db")
            self.assertTrue(
                store.acquire_remote_supervisor_lease(
                    TASK_ID, owner="a", ttl_seconds=0.05
                )
            )
            time.sleep(0.12)
            self.assertTrue(
                store.acquire_remote_supervisor_lease(
                    TASK_ID, owner="b", ttl_seconds=60
                )
            )

    def test_release_only_removes_own_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.db")
            self.assertTrue(
                store.acquire_remote_supervisor_lease(
                    TASK_ID, owner="a", ttl_seconds=60
                )
            )
            store.release_remote_supervisor_lease(TASK_ID, owner="b")
            self.assertFalse(
                store.acquire_remote_supervisor_lease(
                    TASK_ID, owner="b", ttl_seconds=60
                )
            )
            store.release_remote_supervisor_lease(TASK_ID, owner="a")
            self.assertTrue(
                store.acquire_remote_supervisor_lease(
                    TASK_ID, owner="b", ttl_seconds=60
                )
            )

    def test_renew_rejects_foreign_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.db")
            self.assertTrue(
                store.acquire_remote_supervisor_lease(
                    TASK_ID, owner="a", ttl_seconds=60
                )
            )
            self.assertFalse(
                store.renew_remote_supervisor_lease(
                    TASK_ID, owner="b", ttl_seconds=60
                )
            )
            self.assertTrue(
                store.renew_remote_supervisor_lease(
                    TASK_ID, owner="a", ttl_seconds=60
                )
            )


class SupervisorCollisionTests(SupervisorDedupTestBase):
    def test_second_concurrent_execute_is_queued_not_dispatched(self):
        """并发双 execute 同任务：第二入场必须排队（busy 码），旧 run 必须存活。

        红签名（昨夜行为复现）：第二入场取消在途 run 并自行派发
        （cancel_count=1、dispatch_count=2），第一监督者的 run 被夺走。
        """
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, secrets, github = self._coordinator(directory)
            gate = threading.Event()
            original_get_run = github.get_run
            get_run_calls = {"count": 0}

            def gated_get_run(*args, **kwargs):
                get_run_calls["count"] += 1
                if get_run_calls["count"] == 1:
                    # 第一监督者的首个轮询：停在 queued 窗口（未发布信箱）。
                    gate.wait(timeout=15)
                return original_get_run(*args, **kwargs)

            github.get_run = gated_get_run

            started = threading.Event()
            outcome: dict = {}

            def first_supervisor():
                started.set()
                try:
                    coordinator.execute(
                        task_id=TASK_ID,
                        build_job=_build_job,
                        import_result=lambda _value: None,
                        cancel_requested=lambda: False,
                        progress=_noop_progress,
                    )
                    outcome["state"] = "imported"
                except Exception as exc:  # pragma: no cover - diagnostics only
                    outcome["state"] = "failed"
                    outcome["error"] = exc

            worker = threading.Thread(target=first_supervisor, daemon=True)
            worker.start()
            self.assertTrue(started.wait(timeout=5))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                row = store.get_remote_run(TASK_ID) or {}
                if str(row.get("remote_state") or "") == "queued":
                    break
                time.sleep(0.01)
            self.assertEqual(
                str((store.get_remote_run(TASK_ID) or {}).get("remote_state") or ""),
                "queued",
            )

            with self.assertRaises(GitHubRemoteError) as captured:
                coordinator.execute(
                    task_id=TASK_ID,
                    build_job=_build_job,
                    import_result=lambda _value: None,
                    cancel_requested=lambda: False,
                    progress=_noop_progress,
                )
            self.assertEqual(
                str(captured.exception.code), "remote_supervisor_busy"
            )
            self.assertEqual(github.cancel_count, 0)
            self.assertEqual(github.dispatch_count, 1)

            gate.set()
            worker.join(timeout=20)
            self.assertEqual(outcome.get("state"), "imported")
            row = store.get_remote_run(TASK_ID) or {}
            self.assertEqual(str(row.get("remote_state") or ""), "imported")

    def test_disabled_dispatch_keeps_inflight_queued_run_alive(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, _secrets, github = self._coordinator(directory)
            store.upsert_remote_run(
                TASK_ID,
                repository="owner/worker",
                workflow="worker.yml",
                run_id=99,
                attempt=1,
                remote_state="queued",
            )
            with self.assertRaisesRegex(
                GitHubRemoteError, "remote dispatch is disabled"
            ):
                coordinator.execute(
                    task_id=TASK_ID,
                    build_job=_build_job,
                    import_result=lambda _value: None,
                    cancel_requested=lambda: False,
                    progress=_noop_progress,
                    allow_dispatch=False,
                )
            # 守卫必须先于取消：不允许「取消了旧的、自己又没跑」。
            self.assertEqual(github.cancel_count, 0)
            row = store.get_remote_run(TASK_ID) or {}
            self.assertEqual(str(row.get("remote_state") or ""), "queued")

    def test_dispatch_failure_after_cancel_records_failed_row(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, _secrets, github = self._coordinator(directory)
            store.upsert_remote_run(
                TASK_ID,
                repository="owner/worker",
                workflow="worker.yml",
                run_id=99,
                attempt=1,
                remote_state="queued",
            )
            github.dispatch_error = GitHubRemoteError("dispatch rejected")
            with self.assertRaises(GitHubRemoteError):
                coordinator.execute(
                    task_id=TASK_ID,
                    build_job=_build_job,
                    import_result=lambda _value: None,
                    cancel_requested=lambda: False,
                    progress=_noop_progress,
                )
            # 取消后的派发失败必须落真相，不能留 queued 幻影。
            row = store.get_remote_run(TASK_ID) or {}
            self.assertEqual(str(row.get("remote_state") or ""), "failed")
            self.assertTrue(str(row.get("last_error") or "").strip())
            attempt = store.get_remote_attempt(TASK_ID, 2)
            self.assertIsNotNone(attempt)
            self.assertEqual(
                str(attempt.get("error_code") or ""), "githubremoteerror"
            )


class FailureCodePassthroughTests(SupervisorDedupTestBase):
    def test_signed_worker_code_survives_failure_rows_and_task_message(self):
        """worker 签名失败码必须活到 attempt 行与任务行，不得被泛化码覆写。"""
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, _secrets, github = self._coordinator(directory)
            github.conclusion = "failure"
            reads = {"controls": 0}

            def read_controls(*_args, **_kwargs):
                reads["controls"] += 1
                return [(1, {"sealed": True})] if reads["controls"] == 1 else []

            github.read_controls = read_controls
            control = {
                "sequence": 1,
                "control_kind": "progress",
                "payload": {
                    "stage": "remote_compute",
                    "status": "failed",
                    "error_code": "platform_challenge_required",
                },
            }
            with patch("src.remote.coordinator.open_control", return_value=control):
                with self.assertRaises(GitHubRemoteError) as captured:
                    coordinator.execute(
                        task_id=TASK_ID,
                        build_job=_build_job,
                        import_result=lambda _value: None,
                        cancel_requested=lambda: False,
                        progress=_noop_progress,
                    )
            self.assertEqual(
                str(captured.exception.code), "platform_challenge_required"
            )
            attempt = store.get_remote_attempt(TASK_ID, 1)
            self.assertEqual(
                str(attempt.get("error_code") or ""), "platform_challenge_required"
            )
            row = store.get_remote_run(TASK_ID) or {}
            self.assertEqual(str(row.get("remote_state") or ""), "failed")
            # 任务行失败消息（realtest #4）：优先可指引的闭集码。
            self.assertEqual(
                _task_failure_message(captured.exception),
                "platform_challenge_required",
            )
            self.assertIn(
                "platform_challenge_required", _REMOTE_WORKER_GUIDANCE_CODES
            )
            # 失败后租约必须已释放：同任务可再次被监督（复用/重试面）。
            github2 = FakeGitHub(github.worker_private, github.signing_private)
            coordinator.github = github2
            result = coordinator.execute(
                task_id=TASK_ID,
                build_job=_build_job,
                import_result=lambda _value: None,
                cancel_requested=lambda: False,
                progress=_noop_progress,
            )
            self.assertTrue(result["outputs"]["echo"]["ok"])


class MultiFlightGitHub:
    """按任务出 dispatch/信封/结果工件的假件：两个监督者可同时完整跑通。

    RUNLOCK-1 多飞审计（⑩）：工作流并发组改按任务分组后，客户端监督层
    必须证明异任务互不阻塞、取消不伤邻任务、租约在取消后零残留。
    """

    def __init__(self, worker_private, signing_private):
        self.worker_private = worker_private
        self.signing_private = signing_private
        self.envelopes = {}
        self.run_task = {}
        self.run_reads = {}
        self.stall_tasks = set()
        self.canceled_runs = set()
        self.dispatch_count = 0
        self.cancel_count = 0

    def dispatch_workflow(self, repo, *, task_id, **_kwargs):
        self.dispatch_count += 1
        run_id = 100 + self.dispatch_count
        self.run_task[run_id] = str(task_id)
        return DispatchResult(run_id=run_id, status="queued")

    def get_run(self, repo, run_id):
        run_id = int(run_id)
        self.run_reads[run_id] = self.run_reads.get(run_id, 0) + 1
        if run_id in self.canceled_runs:
            return {"status": "completed", "conclusion": "cancelled"}
        if self.run_task.get(run_id) in self.stall_tasks:
            # 卡在启动握手窗：取消确认与邻居推进都发生在此窗口。
            return {"status": "in_progress", "conclusion": ""}
        if self.run_reads[run_id] == 1:
            return {"status": "in_progress", "conclusion": ""}
        return {"status": "completed", "conclusion": "success"}

    def get_run_jobs(self, repo, run_id):
        if self.run_task.get(int(run_id)) in self.stall_tasks:
            return [{"steps": [{"name": "Install pinned runtime", "status": "in_progress"}]}]
        return [{"steps": [{"name": "Process encrypted job", "status": "in_progress"}]}]

    def publish_job(self, repo, task_id, envelope):
        self.envelopes[str(task_id)] = envelope
        return {"issue_number": 7, "comment_ids": [8], "part_count": 1}

    def cancel_run(self, repo, run_id):
        self.cancel_count += 1
        self.canceled_runs.add(int(run_id))

    def list_run_artifacts(self, repo, run_id):
        task_id = self.run_task[int(run_id)]
        return [{"id": int(run_id), "name": f"courselens-result-{task_id}", "expired": False}]

    def download_artifact_file(self, repo, artifact_id, filename):
        task_id = self.run_task[int(artifact_id)]
        job = open_job(self.envelopes[task_id], self.worker_private)
        result = {
            "schema": RESULT_SCHEMA,
            "protocol_version": PROTOCOL_VERSION,
            "task_id": task_id,
            "job_kind": "echo",
            "input_hash": job["input_hash"],
            "status": "completed",
            "outputs": {"echo": {"ok": True}},
            "metrics": {},
            "warnings": [],
        }
        return json_dumps_sealed(result, job["result_public_key"], self.signing_private)


def _job_for(task_id):
    def build(result_public_key):
        now = time.time()
        return {
            "task_id": task_id,
            "job_kind": "echo",
            "created_at": now,
            "expires_at": now + 300,
            "result_public_key": result_public_key,
            "pipeline": {"version": "test"},
            "payload": {},
        }
    return build


class SupervisorMultiFlightTests(unittest.TestCase):
    def _coordinator(self, directory):
        worker_private, worker_public = generate_box_keypair()
        signing_private, signing_public = generate_signing_keypair()
        github = MultiFlightGitHub(worker_private, signing_private)
        settings = RemoteSettings(
            enabled=True,
            github_token="token",
            worker_public_key=worker_public,
            worker_signing_public_key=signing_public,
            poll_seconds=0.001,
        )
        store = TaskStore(Path(directory) / "state.db")
        return (
            RemoteCoordinator(settings, store, MemorySecrets(), github=github),
            store,
            github,
        )

    def test_different_tasks_supervise_concurrently_without_busy(self):
        """异任务多飞：两个监督者必须同时在飞到首个轮询，且各自完整收口。"""
        with tempfile.TemporaryDirectory() as directory:
            coordinator, _store, github = self._coordinator(directory)
            barrier = threading.Barrier(2, timeout=15)
            original_get_run = github.get_run

            def barrier_get_run(repo, run_id):
                if github.run_reads.get(int(run_id), 0) == 0:
                    # 双方都必须先派发成功才能到首个轮询；全局单飞租约下
                    # 第二监督者根本到不了这里，栅栏必然破裂。
                    barrier.wait()
                return original_get_run(repo, run_id)

            github.get_run = barrier_get_run
            outcomes = {}

            def run(task_id):
                try:
                    coordinator.execute(
                        task_id=task_id,
                        build_job=_job_for(task_id),
                        import_result=lambda _value: None,
                        cancel_requested=lambda: False,
                        progress=_noop_progress,
                    )
                    outcomes[task_id] = "imported"
                except Exception as exc:  # pragma: no cover - diagnostics only
                    outcomes[task_id] = f"failed: {exc}"

            threads = [
                threading.Thread(target=run, args=(TASK_ID,), daemon=True),
                threading.Thread(target=run, args=(OTHER_TASK_ID,), daemon=True),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            self.assertEqual(
                outcomes, {TASK_ID: "imported", OTHER_TASK_ID: "imported"}
            )
            self.assertEqual(github.dispatch_count, 2)
            self.assertEqual(github.cancel_count, 0)

    def test_cancel_of_one_task_leaves_neighbor_and_leases_untouched(self):
        """取消 A 不伤 B：A 走取消确认并即时释放租约，B 完整收口、零残留。"""
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, github = self._coordinator(directory)
            github.stall_tasks.add(TASK_ID)
            cancel_a = threading.Event()
            outcomes = {}

            def run(task_id, cancel):
                try:
                    coordinator.execute(
                        task_id=task_id,
                        build_job=_job_for(task_id),
                        import_result=lambda _value: None,
                        cancel_requested=cancel.is_set,
                        progress=_noop_progress,
                    )
                    outcomes[task_id] = "imported"
                except Exception as exc:  # pragma: no cover - diagnostics only
                    outcomes[task_id] = exc

            thread_a = threading.Thread(
                target=run, args=(TASK_ID, cancel_a), daemon=True
            )
            thread_a.start()
            row = {}
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                row = store.get_remote_run(TASK_ID) or {}
                if int(row.get("run_id") or 0):
                    break
                time.sleep(0.01)
            self.assertTrue(int(row.get("run_id") or 0))

            cancel_a.set()
            thread_b = threading.Thread(
                target=run,
                args=(OTHER_TASK_ID, threading.Event()),
                daemon=True,
            )
            thread_b.start()
            thread_b.join(30)
            thread_a.join(30)

            self.assertIsInstance(outcomes.get(TASK_ID), RemoteTaskPaused)
            self.assertEqual(outcomes.get(OTHER_TASK_ID), "imported")
            # 只有 A 的 run 被取消，B 的 run 一次都不该碰。
            self.assertEqual(github.cancel_count, 1)
            # 第二十三案观察点钉：取消/失败路径的租约必须即时释放（finally），
            # 探测性重获取必须成功；TTL 自释另由 test_expired_lease_is_preempted 钉。
            self.assertTrue(store.acquire_remote_supervisor_lease(
                TASK_ID, owner="probe-a", ttl_seconds=5
            ))
            self.assertTrue(store.acquire_remote_supervisor_lease(
                OTHER_TASK_ID, owner="probe-b", ttl_seconds=5
            ))


if __name__ == "__main__":
    unittest.main()
