from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.remote.coordinator import RemoteCoordinator, RemoteSettings
from src.remote.github_client import DispatchResult, GitHubRemoteError
from src.remote.protocol import (
    PROTOCOL_VERSION,
    RESULT_SCHEMA,
    generate_box_keypair,
    generate_signing_keypair,
    open_job,
    seal_result,
)
from src.runtime.task_store import TaskStore


TASK_ID = "0123456789abcdef0123456789abcdef"


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


class RemoteSettingsTests(unittest.TestCase):
    def test_plan_toggle_name_overrides_saved_disabled_state(self):
        secrets = MemorySecrets()
        secrets.save_secret("remote_enabled", "0")
        with patch.dict(os.environ, {"REMOTE_COMPUTE_ENABLED": "1"}, clear=False):
            settings = RemoteSettings.load(secrets)
        self.assertTrue(settings.enabled)


class FakeGitHub:
    def __init__(self, worker_private, signing_private):
        self.worker_private = worker_private
        self.signing_private = signing_private
        self.run_reads = 0
        self.envelope = None
        self.deleted = []
        self.cleaned = False
        self.dispatch_count = 0
        self.conclusion = "success"
        self.events = []
        self.cancel_count = 0
        self.retired = []
        self.run_read_failures = 0
        self.result_warnings = []
        self.result_metrics = {}

    def retire_job_issues(self, repo, task_id):
        self.events.append("retire")
        self.retired.append((repo, task_id))
        return len(self.retired)

    def dispatch_workflow(self, *args, **kwargs):
        self.dispatch_count += 1
        return DispatchResult(run_id=99, status="queued")

    def get_run(self, *args, **kwargs):
        if self.run_read_failures > 0:
            self.run_read_failures -= 1
            raise GitHubRemoteError("GitHub API GET failed: ConnectionError")
        self.run_reads += 1
        self.events.append("run")
        return {"status": "in_progress" if self.run_reads == 1 else "completed", "conclusion": self.conclusion}

    def get_run_jobs(self, *args, **kwargs):
        self.events.append("ready")
        return [{"steps": [{"name": "Process encrypted job", "status": "in_progress"}]}]

    def publish_job(self, repo, task_id, envelope):
        self.events.append("publish")
        self.envelope = envelope
        return {"issue_number": 7, "comment_ids": [8], "part_count": 1}

    def list_run_artifacts(self, *args, **kwargs):
        return [{"id": 12, "name": f"courselens-result-{TASK_ID}", "expired": False}]

    def download_artifact_file(self, repo, artifact_id, filename):
        job = open_job(self.envelope, self.worker_private)
        result = {
            "schema": RESULT_SCHEMA,
            "protocol_version": PROTOCOL_VERSION,
            "task_id": TASK_ID,
            "job_kind": "echo",
            "input_hash": job["input_hash"],
            "status": "completed",
            "outputs": {"echo": {"ok": True}},
            "metrics": dict(self.result_metrics),
            "warnings": list(self.result_warnings),
        }
        return json.dumps(
            seal_result(result, job["result_public_key"], self.signing_private)
        ).encode()

    def delete_artifact(self, repo, artifact_id):
        self.deleted.append(artifact_id)

    def cleanup_job(self, repo, issue_number, **kwargs):
        self.cleaned = True

    def cancel_run(self, *args, **kwargs):
        self.cancel_count += 1


class RemoteCoordinatorTests(unittest.TestCase):
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
        task, _ = store.add_task("subtitle", "course", "lecture", {})
        with store._connect() as db:
            db.execute("UPDATE tasks SET task_id=? WHERE task_id=?", (TASK_ID, task["task_id"]))
        return RemoteCoordinator(settings, store, secrets, github=github), store, secrets, github

    def test_personal_worker_rejects_run_from_unpinned_head(self):
        worker_private, worker_public = generate_box_keypair()
        signing_private, signing_public = generate_signing_keypair()
        github = FakeGitHub(worker_private, signing_private)
        github.get_run = lambda *_args, **_kwargs: {
            "status": "queued", "head_sha": "b" * 40,
        }
        settings = RemoteSettings(
            enabled=True,
            github_token="token",
            worker_public_key=worker_public,
            worker_signing_public_key=signing_public,
            expected_worker_commit="a" * 40,
        )
        with tempfile.TemporaryDirectory() as directory:
            coordinator = RemoteCoordinator(
                settings, TaskStore(Path(directory) / "state.db"), MemorySecrets(), github=github
            )
            with self.assertRaisesRegex(Exception, "worker_run_head_sha_mismatch"):
                coordinator._get_verified_run(99)

    @staticmethod
    def _job(result_public_key):
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

    def test_echo_round_trip_imports_before_cleanup(self):
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
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.db")
            task, _ = store.add_task("subtitle", "course", "lecture", {})
            # Coordinator task IDs are deliberately the app-generated UUID.
            with store._connect() as db:
                db.execute("UPDATE tasks SET task_id=? WHERE task_id=?", (TASK_ID, task["task_id"]))
            coordinator = RemoteCoordinator(settings, store, secrets, github=github)
            imported = []

            def build_job(result_public_key):
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

            result = coordinator.execute(
                task_id=TASK_ID,
                build_job=build_job,
                import_result=lambda value: imported.append(value),
                cancel_requested=lambda: False,
                progress=lambda *_args: None,
            )
            self.assertTrue(result["outputs"]["echo"]["ok"])
            self.assertEqual(len(imported), 1)
            self.assertTrue(github.cleaned)
            self.assertIn(12, github.deleted)
            self.assertEqual(store.get_remote_run(TASK_ID)["remote_state"], "imported")
            self.assertLess(github.events.index("ready"), github.events.index("publish"))

    def test_resume_after_user_pause_dispatches_fresh_with_new_evidence(self):
        """U1/第廿三案：暂停落账 remote_state=paused 后，恢复必须全新派发
        （attempt+1、新 run 身份、退场旧 mailbox issue、新证据封套），
        绝不附着旧 run 复用旧证据。"""
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, secrets, github = self._coordinator(directory)
            store.upsert_remote_run(
                TASK_ID, repository="owner/worker", workflow="worker.yml",
                run_id=41, attempt=1, issue_number=3, remote_state="paused",
                input_hash="oldhash", checkpoint={"completed_chunks": 6},
            )
            imported = []

            def build_job(result_public_key):
                now = time.time()
                return {
                    "task_id": TASK_ID,
                    "job_kind": "echo",
                    "created_at": now,
                    "expires_at": now + 300,
                    "result_public_key": result_public_key,
                    "pipeline": {"version": "test"},
                    "payload": {"checkpoint": {"completed_chunks": 6}},
                }

            result = coordinator.execute(
                task_id=TASK_ID,
                build_job=build_job,
                import_result=lambda value: imported.append(value),
                cancel_requested=lambda: False,
                progress=lambda *_args: None,
            )

            self.assertTrue(result["outputs"]["echo"]["ok"])
            self.assertEqual(len(imported), 1)
            # 全新派发：一次 dispatch、新 run 身份、attempt+1
            self.assertEqual(github.dispatch_count, 1)
            remote = store.get_remote_run(TASK_ID)
            self.assertEqual(int(remote["run_id"]), 99)
            self.assertNotEqual(int(remote["run_id"]), 41)
            self.assertEqual(int(remote["attempt"]), 2)
            self.assertEqual(remote["remote_state"], "imported")
            # 旧 mailbox issue 必须退场，不与新信封共存
            self.assertIn("retire", github.events)
            # 新证据封套：input_hash 与暂停前的旧值不同
            opened = open_job(github.envelope, github.worker_private)
            self.assertNotEqual(str(opened["input_hash"]), "oldhash")
            # 已完成检查点随新载荷续跑，不丢进度
            self.assertEqual(
                opened["payload"].get("checkpoint", {}).get("completed_chunks"), 6
            )

    def test_result_warnings_are_recorded_for_the_task_center(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, _secrets, github = self._coordinator(directory)
            store.upsert_v3_metadata(TASK_ID, dedupe_key=f"dedupe:{TASK_ID}")
            github.result_warnings = ["slides_skipped"]
            github.result_metrics = {
                "elapsed_seconds": 12.0,
                "slides_skipped": {"unidentified_image": 1},
            }
            imported = []
            result = coordinator.execute(
                task_id=TASK_ID,
                build_job=self._job,
                import_result=lambda value: imported.append(value),
                cancel_requested=lambda: False,
                progress=lambda *_args: None,
            )
            self.assertTrue(result["outputs"]["echo"]["ok"])
            self.assertEqual(len(imported), 1)
            self.assertEqual(
                store.get_v3_metadata(TASK_ID)["result_notices"],
                {"warnings": ["slides_skipped"], "slides_skipped": {"unidentified_image": 1}},
            )

    def test_clean_result_writes_no_result_notices(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, _secrets, github = self._coordinator(directory)
            store.upsert_v3_metadata(TASK_ID, dedupe_key=f"dedupe:{TASK_ID}")
            coordinator.execute(
                task_id=TASK_ID,
                build_job=self._job,
                import_result=lambda _value: None,
                cancel_requested=lambda: False,
                progress=lambda *_args: None,
            )
            self.assertEqual(github.result_warnings, [])
            self.assertEqual(store.get_v3_metadata(TASK_ID)["result_notices"], {})

    def test_transient_run_read_failures_are_retried_not_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, secrets, github = self._coordinator(directory)
            github.run_read_failures = 2
            with patch("src.remote.coordinator.time.sleep"):
                result = coordinator.execute(
                    task_id=TASK_ID,
                    build_job=lambda result_public_key: {
                        "task_id": TASK_ID,
                        "job_kind": "echo",
                        "created_at": time.time(),
                        "expires_at": time.time() + 300,
                        "result_public_key": result_public_key,
                        "pipeline": {"version": "test"},
                        "payload": {},
                    },
                    import_result=lambda value: None,
                    cancel_requested=lambda: False,
                    progress=lambda *_args: None,
                )
            self.assertTrue(result["outputs"]["echo"]["ok"])
            self.assertEqual(github.dispatch_count, 1)
            self.assertEqual(store.get_remote_run(TASK_ID)["remote_state"], "imported")

    def test_retry_after_failure_retires_stale_mailbox_issue_before_republish(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, secrets, github = self._coordinator(directory)
            store.upsert_remote_run(
                TASK_ID,
                repository=coordinator.settings.public_repo,
                workflow=coordinator.settings.workflow,
                run_id=98,
                attempt=1,
                issue_number=6,
                remote_state="failed",
                input_hash="prior",
            )
            coordinator.execute(
                task_id=TASK_ID,
                build_job=lambda result_public_key: {
                    "task_id": TASK_ID,
                    "job_kind": "echo",
                    "created_at": time.time(),
                    "expires_at": time.time() + 300,
                    "result_public_key": result_public_key,
                    "pipeline": {"version": "test"},
                    "payload": {},
                },
                import_result=lambda value: None,
                cancel_requested=lambda: False,
                progress=lambda *_args: None,
            )
            self.assertEqual(github.retired, [(coordinator.settings.private_repo, TASK_ID)])
            self.assertIn("retire", github.events)
            self.assertLess(github.events.index("retire"), github.events.index("publish"))
            self.assertEqual(store.get_remote_run(TASK_ID)["attempt"], 2)

    def test_reattach_to_remotely_failed_run_converges_state_and_key(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, secrets, github = self._coordinator(directory)
            github.conclusion = "failure"
            store.upsert_remote_run(
                TASK_ID,
                repository=coordinator.settings.public_repo,
                workflow=coordinator.settings.workflow,
                run_id=98,
                attempt=1,
                issue_number=6,
                remote_state="running",
                input_hash="prior",
            )
            secrets.save_secret(f"remote_result_private:{TASK_ID}", "one-time-key")
            with self.assertRaises(GitHubRemoteError):
                coordinator.execute(
                    task_id=TASK_ID,
                    build_job=lambda result_public_key: {
                        "task_id": TASK_ID,
                        "job_kind": "echo",
                        "created_at": time.time(),
                        "expires_at": time.time() + 300,
                        "result_public_key": result_public_key,
                        "pipeline": {"version": "test"},
                        "payload": {},
                    },
                    import_result=lambda value: None,
                    cancel_requested=lambda: False,
                    progress=lambda *_args: None,
                )
            remote = store.get_remote_run(TASK_ID)
            self.assertEqual(remote["remote_state"], "failed")
            self.assertIn("concluded with failure", str(remote.get("last_error") or ""))
            self.assertFalse(secrets.has_secret(f"remote_result_private:{TASK_ID}"))
            self.assertEqual(store.active_remote_run_count(), 0)

    def test_reattach_converges_even_when_the_run_precedes_the_current_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, secrets, github = self._coordinator(directory)
            # Simulate a run dispatched on the previous signed commit: the
            # pinned-head verification raises, the raw read shows a failure.
            coordinator.settings = type(coordinator.settings)(
                **{**coordinator.settings.__dict__,
                   "expected_worker_commit": "f" * 40}
            ) if hasattr(coordinator.settings, "__dict__") else coordinator.settings
            with patch.object(
                coordinator, "_get_verified_run",
                side_effect=GitHubRemoteError("worker_run_head_sha_mismatch"),
            ), patch.object(
                coordinator.github, "get_run",
                return_value={"status": "completed", "conclusion": "failure",
                              "run_attempt": 1, "head_sha": "a" * 40},
            ):
                store.upsert_remote_run(
                    TASK_ID,
                    repository=coordinator.settings.public_repo,
                    workflow=coordinator.settings.workflow,
                    run_id=98,
                    attempt=1,
                    issue_number=6,
                    remote_state="running",
                    input_hash="prior",
                )
                secrets.save_secret(f"remote_result_private:{TASK_ID}", "one-time-key")
                with self.assertRaises(GitHubRemoteError):
                    coordinator.execute(
                        task_id=TASK_ID,
                        build_job=lambda result_public_key: {
                            "task_id": TASK_ID,
                            "job_kind": "echo",
                            "created_at": time.time(),
                            "expires_at": time.time() + 300,
                            "result_public_key": result_public_key,
                            "pipeline": {"version": "test"},
                            "payload": {},
                        },
                        import_result=lambda value: None,
                        cancel_requested=lambda: False,
                        progress=lambda *_args: None,
                    )
            remote = store.get_remote_run(TASK_ID)
            self.assertEqual(remote["remote_state"], "failed")
            self.assertFalse(secrets.has_secret(f"remote_result_private:{TASK_ID}"))
            self.assertEqual(store.active_remote_run_count(), 0)

    def test_explicit_crash_recovery_reuses_queued_run_without_second_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, _secrets, github = self._coordinator(directory)
            store.upsert_remote_run(
                TASK_ID, repository=coordinator.settings.public_repo,
                workflow="process.yml", run_id=99, attempt=1,
                remote_state="queued", dispatched_at=time.time(),
            )
            reads = iter((
                {"status": "in_progress", "conclusion": None},
                {"status": "in_progress", "conclusion": None},
                {"status": "completed", "conclusion": "success"},
            ))
            github.get_run = lambda *_args, **_kwargs: next(reads)
            imported = []
            coordinator.execute(
                task_id=TASK_ID,
                build_job=self._job,
                import_result=lambda value: imported.append(value),
                cancel_requested=lambda: False,
                progress=lambda *_args: None,
                reuse_queued_run=True,
            )
            self.assertEqual(github.dispatch_count, 0)
            self.assertEqual(len(imported), 1)
            self.assertTrue(github.cleaned)

    def test_imported_cleanup_retry_is_idempotent_and_does_not_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, secrets, github = self._coordinator(directory)
            store.upsert_remote_run(
                TASK_ID, repository=coordinator.settings.public_repo,
                workflow="process.yml", run_id=99, attempt=1,
                issue_number=7, artifact_id=12, remote_state="imported",
            )
            store.upsert_remote_attempt(
                TASK_ID, 1, repository=coordinator.settings.public_repo,
                workflow="process.yml", run_id=99, artifact_id=12,
                import_state="imported", cleanup_state="pending",
            )
            secrets.save_secret(f"remote_result_private:{TASK_ID}", "temporary")
            store.upsert_remote_run(
                TASK_ID, repository=coordinator.settings.public_repo,
                workflow="process.yml", run_id=99, attempt=1,
                issue_number=7, artifact_id=12, remote_state="imported",
                last_error="remote_cleanup_pending",
            )
            result = coordinator.retry_imported_cleanup(TASK_ID)
            self.assertEqual(result["cleanup_state"], "complete")
            self.assertEqual(github.dispatch_count, 0)
            self.assertTrue(github.cleaned)
            self.assertIn(12, github.deleted)
            self.assertEqual(store.get_remote_run(TASK_ID)["last_error"], "")
            self.assertFalse(secrets.has_secret(f"remote_result_private:{TASK_ID}"))
            self.assertEqual(store.migration_cleanup_pending_count(), 0)

    def test_runner_failure_cleans_unrecoverable_mailbox_and_result_key(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, secrets, github = self._coordinator(directory)
            github.conclusion = "failure"
            with self.assertRaisesRegex(Exception, "concluded with failure"):
                coordinator.execute(
                    task_id=TASK_ID,
                    build_job=self._job,
                    import_result=lambda _value: None,
                    cancel_requested=lambda: False,
                    progress=lambda *_args: None,
                )
            self.assertTrue(github.cleaned)
            self.assertNotIn(f"remote_result_private:{TASK_ID}", secrets.values)
            self.assertEqual(store.get_remote_run(TASK_ID)["remote_state"], "failed")

    def test_failure_before_payload_preserves_prior_verified_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, _secrets, _github = self._coordinator(directory)
            checkpoint = {
                "mode": "automatic", "completed_chunks": 2, "total_chunks": 3,
                "raw_sensevoice": [{"start_ms": 0, "end_ms": 1, "text": "kept"}],
            }
            store.upsert_remote_run(
                TASK_ID, repository="owner/worker", workflow="worker.yml",
                run_id=98, attempt=1, remote_state="failed", checkpoint=checkpoint,
            )

            def fail_before_payload(_result_public_key):
                raise RuntimeError("credentials unavailable")

            with self.assertRaisesRegex(RuntimeError, "credentials unavailable"):
                coordinator.execute(
                    task_id=TASK_ID,
                    build_job=fail_before_payload,
                    import_result=lambda _value: None,
                    cancel_requested=lambda: False,
                    progress=lambda *_args: None,
                )
            self.assertEqual(store.get_remote_run(TASK_ID)["checkpoint"], checkpoint)

    def test_failed_import_resumes_same_artifact_without_new_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, secrets, github = self._coordinator(directory)

            def fail_import(_value):
                raise RuntimeError("database unavailable")

            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                coordinator.execute(
                    task_id=TASK_ID,
                    build_job=self._job,
                    import_result=fail_import,
                    cancel_requested=lambda: False,
                    progress=lambda *_args: None,
                )
            self.assertEqual(github.dispatch_count, 1)
            self.assertFalse(github.cleaned)
            self.assertIn(f"remote_result_private:{TASK_ID}", secrets.values)
            self.assertEqual(store.get_remote_run(TASK_ID)["remote_state"], "failed")

            imported = []
            result = coordinator.execute(
                task_id=TASK_ID,
                build_job=self._job,
                import_result=lambda value: imported.append(value),
                cancel_requested=lambda: False,
                progress=lambda *_args: None,
            )
            self.assertTrue(result["outputs"]["echo"]["ok"])
            self.assertEqual(len(imported), 1)
            self.assertEqual(github.dispatch_count, 1)
            self.assertTrue(github.cleaned)
            self.assertNotIn(f"remote_result_private:{TASK_ID}", secrets.values)
            self.assertEqual(store.get_remote_run(TASK_ID)["remote_state"], "imported")

    def test_signed_progress_is_not_overwritten_by_generic_runner_state(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, secrets, github = self._coordinator(directory)
            store.upsert_remote_run(
                TASK_ID, repository="owner/worker", workflow="worker.yml",
                run_id=99, attempt=1, issue_number=7, remote_state="running",
            )
            secrets.save_secret(f"remote_result_private:{TASK_ID}", "test-key")
            reads = {"controls": 0, "runs": 0}

            def read_controls(*_args, **_kwargs):
                reads["controls"] += 1
                return [(1, {"sealed": True})] if reads["controls"] == 1 else []

            def get_run(*_args, **_kwargs):
                reads["runs"] += 1
                return {
                    "status": "in_progress" if reads["runs"] == 1 else "completed",
                    "conclusion": None if reads["runs"] == 1 else "success",
                    "run_attempt": 1,
                }

            github.read_controls = read_controls
            github.get_run = get_run
            events = []
            control = {
                "sequence": 1,
                "control_kind": "progress",
                "payload": {
                    "stage": "asr", "status": "running",
                    "completed": 3, "total": 10, "error_code": "",
                },
            }
            with patch("src.remote.coordinator.open_control", return_value=control):
                coordinator._wait_until_complete(
                    TASK_ID, 99, 7, f"remote_result_private:{TASK_ID}", "input-hash",
                    lambda: False, lambda *args: events.append(args),
                )
            self.assertEqual(events, [("asr", 30.0, "GitHub runner reported signed progress")])
            attempt = store.get_remote_attempt(TASK_ID, 1)
            self.assertEqual(attempt["completed"], 3)
            self.assertEqual(attempt["total"], 10)

    def test_runner_failure_carries_worker_closed_set_code(self):
        """B3 词汇对齐：worker 最后一次签名进度里的闭集码随失败上抛（code 属性），
        任务失败面才能给出可指引的细粒度原因；无签名证据时保持为空。"""
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, secrets, github = self._coordinator(directory)
            store.upsert_remote_run(
                TASK_ID, repository="owner/worker", workflow="worker.yml",
                run_id=99, attempt=1, issue_number=7, remote_state="running",
            )
            secrets.save_secret(f"remote_result_private:{TASK_ID}", "test-key")
            reads = {"runs": 0, "controls": 0}

            def get_run(*_args, **_kwargs):
                reads["runs"] += 1
                return {
                    "status": "in_progress" if reads["runs"] == 1 else "completed",
                    "conclusion": None if reads["runs"] == 1 else "failure",
                    "run_attempt": 1,
                }

            def read_controls(*_args, **_kwargs):
                reads["controls"] += 1
                return [(1, {"sealed": True})] if reads["controls"] == 1 else []

            github.read_controls = read_controls
            github.get_run = get_run
            control = {
                "sequence": 1,
                "control_kind": "progress",
                "payload": {
                    "stage": "remote_compute", "status": "failed",
                    "error_code": "platform_challenge_required",
                },
            }
            with patch("src.remote.coordinator.open_control", return_value=control):
                with self.assertRaises(GitHubRemoteError) as captured:
                    coordinator._wait_until_complete(
                        TASK_ID, 99, 7, f"remote_result_private:{TASK_ID}", "input-hash",
                        lambda: False, lambda *args: None,
                    )
            self.assertEqual(
                str(captured.exception.code), "platform_challenge_required",
            )
            # 已消费的码保持脱敏形态：消息本体仍是闭集句式，不含原始细节。
            self.assertIn("concluded with failure", str(captured.exception))

    def test_runner_failure_without_signed_progress_has_empty_code(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, _store, _secrets, github = self._coordinator(directory)
            github.read_controls = lambda *_args, **_kwargs: []
            github.get_run = lambda *_args, **_kwargs: {
                "status": "completed", "conclusion": "failure", "run_attempt": 1,
            }
            with self.assertRaises(GitHubRemoteError) as captured:
                coordinator._wait_until_complete(
                    TASK_ID, 99, 7, f"remote_result_private:{TASK_ID}", "input-hash",
                    lambda: False, lambda *args: None,
                )
            self.assertEqual(captured.exception.code, "")

    def test_runner_killed_midrun_maps_to_remote_runner_lost(self):
        """⑫（LOG1 定案签名）：run 级 failure + 步级 cancelled 且无 worker
        签名码 = 托管 runner 被 infra 杀掉 → 专属闭集码 remote_runner_lost。"""
        with tempfile.TemporaryDirectory() as directory:
            coordinator, _store, _secrets, github = self._coordinator(directory)
            github.read_controls = lambda *_args, **_kwargs: []
            github.get_run = lambda *_args, **_kwargs: {
                "status": "completed", "conclusion": "failure", "run_attempt": 1,
            }
            github.get_run_jobs = lambda *_args, **_kwargs: [{
                "conclusion": "failure",
                "steps": [
                    {"name": "Set up job", "conclusion": "success"},
                    {"name": "Process encrypted job", "conclusion": "cancelled"},
                ],
            }]
            with self.assertRaises(GitHubRemoteError) as captured:
                coordinator._wait_until_complete(
                    TASK_ID, 99, 7, f"remote_result_private:{TASK_ID}", "input-hash",
                    lambda: False, lambda *args: None,
                )
            self.assertEqual(captured.exception.code, "remote_runner_lost")

    def test_worker_failure_with_signature_keeps_worker_code_not_runner_lost(self):
        """worker 签名码在场时优先；步级 cancelled 不抢码（防误判主动取消）。"""
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, secrets, github = self._coordinator(directory)
            secrets.save_secret(f"remote_result_private:{TASK_ID}", "control-secret")
            store.upsert_remote_run(
                TASK_ID, repository="owner/worker", workflow="worker.yml",
                run_id=99, attempt=1, remote_state="running",
            )
            github.read_controls = lambda *_args, **_kwargs: [(1, {"sealed": True})]
            github.get_run = lambda *_args, **_kwargs: {
                "status": "completed", "conclusion": "failure", "run_attempt": 1,
            }
            github.get_run_jobs = lambda *_args, **_kwargs: [{
                "conclusion": "failure",
                "steps": [{"name": "Process encrypted job", "conclusion": "cancelled"}],
            }]
            control = {
                "sequence": 1, "control_kind": "progress",
                "payload": {"stage": "remote_compute", "status": "failed",
                            "error_code": "platform_challenge_required"},
            }
            with patch("src.remote.coordinator.open_control", return_value=control):
                with self.assertRaises(GitHubRemoteError) as captured:
                    coordinator._wait_until_complete(
                        TASK_ID, 99, 7, f"remote_result_private:{TASK_ID}", "input-hash",
                        lambda: False, lambda *args: None,
                    )
            self.assertEqual(captured.exception.code, "platform_challenge_required")

    def test_cancel_waits_for_github_conclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, _secrets, github = self._coordinator(directory)
            store.upsert_remote_run(
                TASK_ID, repository="owner/worker", workflow="worker.yml",
                run_id=99, attempt=1, remote_state="running",
            )
            reads = iter([
                {"status": "in_progress", "conclusion": None, "run_attempt": 1},
                {"status": "completed", "conclusion": "cancelled", "run_attempt": 1},
            ])
            github.get_run = lambda *_args, **_kwargs: next(reads)
            self.assertTrue(coordinator._wait_for_cancel_confirmation(TASK_ID, 99))
            self.assertEqual(github.cancel_count, 1)
            attempt = store.get_remote_attempt(TASK_ID, 1)
            self.assertEqual(attempt["conclusion"], "cancelled")

    def test_successful_run_wins_cancel_race_and_remains_importable(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, _secrets, github = self._coordinator(directory)
            store.upsert_remote_run(
                TASK_ID, repository="owner/worker", workflow="worker.yml",
                run_id=99, attempt=1, remote_state="running",
            )
            github.get_run = lambda *_args, **_kwargs: {
                "status": "completed", "conclusion": "success", "run_attempt": 1,
            }
            self.assertFalse(coordinator._wait_for_cancel_confirmation(TASK_ID, 99))
            self.assertEqual(github.cancel_count, 1)

    def test_confirmed_canceled_run_cleanup_is_complete_and_removes_key(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, secrets, github = self._coordinator(directory)
            store.upsert_remote_run(
                TASK_ID, repository="owner/worker", workflow="worker.yml",
                run_id=99, attempt=1, issue_number=7, remote_state="running",
            )
            secrets.save_secret(f"remote_result_private:{TASK_ID}", "test-key")
            github.get_run = lambda *_args, **_kwargs: {
                "status": "completed", "conclusion": "cancelled", "run_attempt": 1,
            }

            result = coordinator.cleanup_canceled_run(TASK_ID)

            self.assertEqual(result["cleanup_state"], "complete")
            self.assertTrue(github.cleaned)
            self.assertEqual(github.deleted, [12])
            self.assertNotIn(f"remote_result_private:{TASK_ID}", secrets.values)
            self.assertEqual(store.get_remote_run(TASK_ID)["remote_state"], "canceled")
            attempt = store.get_remote_attempt(TASK_ID, 1)
            self.assertEqual(attempt["import_state"], "canceled")
            self.assertEqual(attempt["cleanup_state"], "complete")


class _FakeCoordinatorTime:
    """替代 coordinator 模块内的 time：sleep 只记账并把单调钟推进同量，
    使退避梯断言零真实等待、且 _backoff_sleep 每次等待恰好记一笔总额。"""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def sleep(self, seconds):
        self.sleeps.append(float(seconds))
        self.now += float(seconds)

    def monotonic(self):
        return self.now

    def time(self):
        return self.now


class PollBackoffTests(unittest.TestCase):
    """N11（夜14-R1 T29）：运行态长等待轮询退避。

    语义：排队/启动窗（_wait_until_started）保持基线 poll_seconds 恒定；
    _wait_until_complete 每次进入从基线重新爬梯（成功收口即回落基线），
    基线×2 指数退避、帽 15s；用户显式调大 poll_seconds 时帽跟随不收紧；
    退避睡眠分片，本地取消最迟一个基线间隔内被接住（取消延迟不回退）。
    """

    def _coordinator(self, directory, *, poll_seconds):
        worker_private, worker_public = generate_box_keypair()
        signing_private, signing_public = generate_signing_keypair()
        github = FakeGitHub(worker_private, signing_private)
        settings = RemoteSettings(
            enabled=True,
            github_token="token",
            worker_public_key=worker_public,
            worker_signing_public_key=signing_public,
            poll_seconds=poll_seconds,
        )
        store = TaskStore(Path(directory) / "state.db")
        task, _ = store.add_task("subtitle", "course", "lecture", {})
        with store._connect() as db:
            db.execute("UPDATE tasks SET task_id=? WHERE task_id=?", (TASK_ID, task["task_id"]))
        return RemoteCoordinator(settings, store, MemorySecrets(), github=github), store, github

    def test_poll_seconds_default_baseline_is_three_seconds(self):
        self.assertEqual(RemoteSettings(enabled=True).poll_seconds, 3.0)

    def test_long_wait_poll_delay_follows_exponential_ladder_with_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, _store, _github = self._coordinator(directory, poll_seconds=3.0)
            delays = [coordinator._long_wait_poll_delay(rounds) for rounds in range(1, 7)]
            self.assertEqual(delays, [3.0, 6.0, 12.0, 15.0, 15.0, 15.0])

    def test_long_wait_poll_delay_never_shrinks_user_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, _store, _github = self._coordinator(directory, poll_seconds=20.0)
            delays = [coordinator._long_wait_poll_delay(rounds) for rounds in range(1, 5)]
            self.assertEqual(delays, [20.0, 20.0, 20.0, 20.0])

    def test_long_wait_poll_delay_stays_tiny_for_fast_test_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, _store, _github = self._coordinator(directory, poll_seconds=0.001)
            delays = [coordinator._long_wait_poll_delay(rounds) for rounds in range(1, 5)]
            self.assertEqual(delays, [0.001, 0.002, 0.004, 0.008])

    def test_wait_until_complete_polls_with_backoff_from_baseline(self):
        """运行态等待的轮询间隔（相邻 status GET 间距）=3→6→12→帽15，而非恒定 3s。"""
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, github = self._coordinator(directory, poll_seconds=3.0)
            store.upsert_remote_run(
                TASK_ID, repository="owner/worker", workflow="worker.yml",
                run_id=99, attempt=1, remote_state="running",
            )
            reads = iter([
                {"status": "in_progress", "conclusion": None, "run_attempt": 1},
                {"status": "in_progress", "conclusion": None, "run_attempt": 1},
                {"status": "in_progress", "conclusion": None, "run_attempt": 1},
                {"status": "in_progress", "conclusion": None, "run_attempt": 1},
                {"status": "completed", "conclusion": "success", "run_attempt": 1},
            ])
            fake_time = _FakeCoordinatorTime()
            observed = []

            def fake_get_run(*_args, **_kwargs):
                observed.append(fake_time.now)
                return next(reads)

            github.get_run = fake_get_run
            with patch("src.remote.coordinator.time", fake_time):
                coordinator._wait_until_complete(
                    TASK_ID, 99, 7, f"remote_result_private:{TASK_ID}", "hash",
                    cancel=lambda: False, progress=lambda *_args: None,
                )
            deltas = [observed[i + 1] - observed[i] for i in range(len(observed) - 1)]
            self.assertEqual(deltas, [3.0, 6.0, 12.0, 15.0])

    def test_each_new_wait_restarts_ladder_at_baseline(self):
        """成功收口即回落基线：下一次等待第一轮仍从 3s 起步。"""
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, github = self._coordinator(directory, poll_seconds=3.0)
            store.upsert_remote_run(
                TASK_ID, repository="owner/worker", workflow="worker.yml",
                run_id=99, attempt=1, remote_state="running",
            )
            for _ in range(2):
                reads = iter([
                    {"status": "in_progress", "conclusion": None, "run_attempt": 1},
                    {"status": "in_progress", "conclusion": None, "run_attempt": 1},
                    {"status": "completed", "conclusion": "success", "run_attempt": 1},
                ])
                fake_time = _FakeCoordinatorTime()
                observed = []

                def fake_get_run(*_args, **_kwargs):
                    observed.append(fake_time.now)
                    return next(reads)

                github.get_run = fake_get_run
                with patch("src.remote.coordinator.time", fake_time):
                    coordinator._wait_until_complete(
                        TASK_ID, 99, 7, f"remote_result_private:{TASK_ID}", "hash",
                        cancel=lambda: False, progress=lambda *_args: None,
                    )
                deltas = [observed[i + 1] - observed[i] for i in range(len(observed) - 1)]
                self.assertEqual(deltas, [3.0, 6.0])

    def test_backoff_sleep_keeps_local_cancel_responsive(self):
        """退避睡眠分片：cancel 置位后最迟一个基线间隔内返回，不再睡满梯值。"""
        with tempfile.TemporaryDirectory() as directory:
            coordinator, _store, _github = self._coordinator(directory, poll_seconds=3.0)
            fake_time = _FakeCoordinatorTime()
            flags = {"checked": 0}

            def cancel():
                flags["checked"] += 1
                return flags["checked"] > 1

            with patch("src.remote.coordinator.time", fake_time):
                coordinator._backoff_sleep(15.0, cancel)
            self.assertEqual(fake_time.sleeps, [3.0])

    def test_queued_startup_window_keeps_constant_baseline_poll(self):
        """排队/启动窗不退避（夜14-R1 定谳）：恒定基线直到授权步就绪。"""
        with tempfile.TemporaryDirectory() as directory:
            coordinator, _store, github = self._coordinator(directory, poll_seconds=3.0)
            reads = iter([
                {"status": "queued", "conclusion": None, "run_attempt": 1},
                {"status": "queued", "conclusion": None, "run_attempt": 1},
                {"status": "queued", "conclusion": None, "run_attempt": 1},
                {"status": "in_progress", "conclusion": None, "run_attempt": 1},
            ])
            fake_time = _FakeCoordinatorTime()
            observed = []

            def fake_get_run(*_args, **_kwargs):
                observed.append(fake_time.now)
                return next(reads)

            github.get_run = fake_get_run
            with patch("src.remote.coordinator.time", fake_time):
                coordinator._wait_until_started(
                    TASK_ID, 99, cancel=lambda: False, progress=lambda *_args: None,
                )
            deltas = [observed[i + 1] - observed[i] for i in range(len(observed) - 1)]
            self.assertEqual(deltas, [3.0, 3.0, 3.0])


if __name__ == "__main__":
    unittest.main()
