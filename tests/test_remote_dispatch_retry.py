from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.remote.coordinator import RemoteCoordinator, RemoteSettings
from src.remote.github_client import DispatchResult, GitHubRemoteError
from src.remote.protocol import (
    RESULT_SCHEMA,
    PROTOCOL_VERSION,
    generate_box_keypair,
    generate_signing_keypair,
    open_job,
    seal_result,
)
from src.runtime.task_store import TaskStore


TASK_ID = "0123456789abcdef0123456789abcdef"

HTTP_403 = GitHubRemoteError("GitHub API POST returned HTTP 403 (request abc-123)")
HTTP_400 = GitHubRemoteError("GitHub API POST returned HTTP 400 (request abc-123)")
HTTP_429 = GitHubRemoteError("GitHub API POST returned HTTP 429 (request abc-123)")
HTTP_500 = GitHubRemoteError("GitHub API POST returned HTTP 500 (request abc-123)")
HTTP_503 = GitHubRemoteError("GitHub API POST returned HTTP 503 (request abc-123)")
NETWORK_BLIP = GitHubRemoteError("GitHub API POST failed: ConnectionError")
SEMANTIC_CONFLICT = GitHubRemoteError("encrypted job issue contains an unexpected comment")


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
    """Happy-path fake with scripted dispatch/publish failures and probes."""

    def __init__(self, worker_private, signing_private):
        self.worker_private = worker_private
        self.signing_private = signing_private
        self.run_reads = 0
        self.envelope = None
        self.deleted = []
        self.cleaned = False
        self.dispatch_count = 0
        self.dispatch_failures = []
        self.find_count = 0
        self.probe_script = []
        self.publish_count = 0
        self.publish_failures = []
        self.conclusion = "success"

    def dispatch_workflow(self, *args, **kwargs):
        self.dispatch_count += 1
        if self.dispatch_failures:
            failure = self.dispatch_failures.pop(0)
            if failure is not None:
                raise failure
        return DispatchResult(run_id=99, status="queued")

    def find_workflow_run(self, *args, **kwargs):
        self.find_count += 1
        if self.probe_script:
            item = self.probe_script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return None

    def publish_job(self, repo, task_id, envelope):
        self.publish_count += 1
        if self.publish_failures:
            failure = self.publish_failures.pop(0)
            if failure is not None:
                raise failure
        self.envelope = envelope
        return {"issue_number": 7, "comment_ids": [8], "part_count": 1}

    def retire_job_issues(self, repo, task_id):
        return 1

    def get_run(self, *args, **kwargs):
        self.run_reads += 1
        return {
            "status": "in_progress" if self.run_reads == 1 else "completed",
            "conclusion": self.conclusion,
        }

    def get_run_jobs(self, *args, **kwargs):
        return [{"steps": [{"name": "Process encrypted job", "status": "in_progress"}]}]

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
            "metrics": {},
            "warnings": [],
        }
        return json.dumps(
            seal_result(result, job["result_public_key"], self.signing_private)
        ).encode()

    def delete_artifact(self, repo, artifact_id):
        self.deleted.append(artifact_id)

    def cleanup_job(self, repo, issue_number, **kwargs):
        self.cleaned = True

    def cancel_run(self, *args, **kwargs):
        pass


class DispatchRetryTests(unittest.TestCase):
    def _coordinator(self, directory, **setting_overrides):
        worker_private, worker_public = generate_box_keypair()
        signing_private, signing_public = generate_signing_keypair()
        github = FakeGitHub(worker_private, signing_private)
        settings = RemoteSettings(
            enabled=True,
            github_token="token",
            worker_public_key=worker_public,
            worker_signing_public_key=signing_public,
            poll_seconds=0.001,
            **setting_overrides,
        )
        store = TaskStore(Path(directory) / "state.db")
        task, _ = store.add_task("subtitle", "course", "lecture", {})
        with store._connect() as db:
            db.execute("UPDATE tasks SET task_id=? WHERE task_id=?", (TASK_ID, task["task_id"]))
        coordinator = RemoteCoordinator(settings, store, MemorySecrets(), github=github)
        return coordinator, store, github

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

    def _execute(self, coordinator, *, progress=lambda *_args: None, **overrides):
        kwargs = dict(
            task_id=TASK_ID,
            build_job=self._job,
            import_result=lambda value: None,
            cancel_requested=lambda: False,
            progress=progress,
        )
        kwargs.update(overrides)
        return coordinator.execute(**kwargs)

    @staticmethod
    def _dispatch_events(store):
        return [row["payload"] for row in store.list_remote_events(topics=["remote-dispatch"])]

    def test_transient_http_403_dispatch_retries_and_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, github = self._coordinator(directory)
            github.dispatch_failures = [HTTP_403]
            sleeps = []
            with patch("src.remote.coordinator.time.sleep", side_effect=sleeps.append):
                result = self._execute(coordinator)
            self.assertTrue(result["outputs"]["echo"]["ok"])
            self.assertEqual(github.dispatch_count, 2)
            # An explicit rejection was never enqueued: re-POST without probing.
            self.assertEqual(github.find_count, 0)
            self.assertEqual(store.get_remote_run(TASK_ID)["run_id"], 99)
            self.assertEqual(store.get_remote_run(TASK_ID)["attempt"], 1)
            events = self._dispatch_events(store)
            self.assertEqual([item["outcome"] for item in events], ["retry"])
            self.assertEqual(events[0]["phase"], "dispatch_workflow")
            self.assertEqual(events[0]["failure_class"], "http_status")
            self.assertEqual(events[0]["http_status"], 403)
            self.assertTrue(0.75 <= sleeps[0] <= 1.25)

    def test_transient_http_403_dispatch_reports_retry_to_the_student(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, _store, github = self._coordinator(directory)
            github.dispatch_failures = [HTTP_403]
            seen = []
            with patch("src.remote.coordinator.time.sleep"):
                self._execute(
                    coordinator,
                    progress=lambda stage, percent, message: seen.append((stage, message)),
                )
            self.assertTrue(
                any(
                    stage == "remote_queue" and "retrying automatically" in message
                    for stage, message in seen
                )
            )

    def test_semantic_http_400_dispatch_never_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, github = self._coordinator(directory)
            github.dispatch_failures = [HTTP_400]
            with patch("src.remote.coordinator.time.sleep"):
                with self.assertRaises(GitHubRemoteError):
                    self._execute(coordinator)
            self.assertEqual(github.dispatch_count, 1)
            self.assertEqual(github.find_count, 0)
            events = self._dispatch_events(store)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["outcome"], "exhausted")
            self.assertEqual(events[0]["http_status"], 400)
            # 既有降级漏斗不变：派发面失败不落 run 行（非取消在途路径）。
            self.assertIsNone(store.get_remote_run(TASK_ID))

    def test_network_blip_probes_then_redispatches(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, github = self._coordinator(directory)
            github.dispatch_failures = [NETWORK_BLIP, NETWORK_BLIP]
            sleeps = []
            with patch("src.remote.coordinator.time.sleep", side_effect=sleeps.append):
                result = self._execute(coordinator)
            self.assertTrue(result["outputs"]["echo"]["ok"])
            self.assertEqual(github.dispatch_count, 3)
            # Each ambiguous retry probes twice before concluding "not landed".
            self.assertEqual(github.find_count, 4)
            self.assertEqual([item["outcome"] for item in self._dispatch_events(store)], ["retry", "retry"])
            self.assertTrue(any(0.75 <= value <= 1.25 for value in sleeps))
            self.assertTrue(any(1.5 <= value <= 2.5 for value in sleeps))
            self.assertEqual(sleeps.count(1.5), 2)

    def test_network_blip_adopts_landed_run_without_second_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, github = self._coordinator(directory)
            github.dispatch_failures = [NETWORK_BLIP]
            github.probe_script = [DispatchResult(run_id=77, status="queued")]
            with patch("src.remote.coordinator.time.sleep"):
                result = self._execute(coordinator)
            self.assertTrue(result["outputs"]["echo"]["ok"])
            self.assertEqual(github.dispatch_count, 1)
            self.assertEqual(github.find_count, 1)
            self.assertEqual(store.get_remote_run(TASK_ID)["run_id"], 77)
            events = self._dispatch_events(store)
            self.assertEqual([item["outcome"] for item in events], ["retry", "adopted"])

    def test_network_blip_with_ambiguous_probe_never_redispatches(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, github = self._coordinator(directory)
            github.dispatch_failures = [NETWORK_BLIP]
            github.probe_script = [
                GitHubRemoteError("multiple workflow runs or invalid inventory exist for one process canary task")
            ]
            with patch("src.remote.coordinator.time.sleep"):
                with self.assertRaises(GitHubRemoteError) as captured:
                    self._execute(coordinator)
            self.assertIn("ConnectionError", str(captured.exception))
            self.assertEqual(github.dispatch_count, 1)
            self.assertEqual(github.find_count, 1)
            events = self._dispatch_events(store)
            self.assertEqual([item["outcome"] for item in events], ["retry", "exhausted"])
            self.assertTrue(events[-1]["probe_inconclusive"])
            self.assertIsNone(store.get_remote_run(TASK_ID))

    def test_http_500_with_ambiguous_probe_never_redispatches(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, github = self._coordinator(directory)
            github.dispatch_failures = [HTTP_500]
            github.probe_script = [
                GitHubRemoteError("multiple workflow runs or invalid inventory exist for one process canary task")
            ]
            with patch("src.remote.coordinator.time.sleep"):
                with self.assertRaises(GitHubRemoteError):
                    self._execute(coordinator)
            self.assertEqual(github.dispatch_count, 1)
            self.assertEqual(github.find_count, 1)
            events = self._dispatch_events(store)
            self.assertEqual([item["outcome"] for item in events], ["retry", "exhausted"])
            self.assertTrue(events[-1]["probe_inconclusive"])

    def test_http_503_with_clean_probe_redispatches(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, _store, github = self._coordinator(directory)
            github.dispatch_failures = [HTTP_503]
            github.probe_script = [None]
            with patch("src.remote.coordinator.time.sleep"):
                result = self._execute(coordinator)
            self.assertTrue(result["outputs"]["echo"]["ok"])
            self.assertEqual(github.dispatch_count, 2)
            self.assertEqual(github.find_count, 1)

    def test_retry_budget_gate_disables_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, github = self._coordinator(
                directory, dispatch_retry_seconds=0.0
            )
            github.dispatch_failures = [HTTP_403]
            with patch("src.remote.coordinator.time.sleep"):
                with self.assertRaises(GitHubRemoteError):
                    self._execute(coordinator)
            self.assertEqual(github.dispatch_count, 1)
            self.assertEqual(self._dispatch_events(store)[0]["outcome"], "exhausted")

    def test_attempt_cap_bounds_total_tries(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, _store, github = self._coordinator(
                directory, dispatch_retry_attempts=2
            )
            github.dispatch_failures = [HTTP_403, HTTP_429]
            with patch("src.remote.coordinator.time.sleep"):
                with self.assertRaises(GitHubRemoteError):
                    self._execute(coordinator)
            self.assertEqual(github.dispatch_count, 2)
            outcomes = [item["outcome"] for item in self._dispatch_events(_store)]
            self.assertEqual(outcomes, ["retry", "exhausted"])

    def test_publish_transient_429_retries_and_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, github = self._coordinator(directory)
            github.publish_failures = [HTTP_429]
            sleeps = []
            with patch("src.remote.coordinator.time.sleep", side_effect=sleeps.append):
                result = self._execute(coordinator)
            self.assertTrue(result["outputs"]["echo"]["ok"])
            self.assertEqual(github.publish_count, 2)
            self.assertEqual(store.get_remote_run(TASK_ID)["remote_state"], "imported")
            events = self._dispatch_events(store)
            self.assertEqual(events[0]["phase"], "publish_job")
            self.assertEqual(events[0]["outcome"], "retry")
            self.assertEqual(events[0]["http_status"], 429)
            self.assertTrue(0.75 <= sleeps[0] <= 1.25)

    def test_publish_semantic_conflict_never_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, github = self._coordinator(directory)
            github.publish_failures = [SEMANTIC_CONFLICT]
            with patch("src.remote.coordinator.time.sleep"):
                with self.assertRaises(GitHubRemoteError):
                    self._execute(coordinator)
            self.assertEqual(github.publish_count, 1)
            # 降级不杀任务：信箱语义冲突沿既有错误卡闭集漏斗落账，不静默吞。
            self.assertEqual(store.get_remote_run(TASK_ID)["remote_state"], "failed")
            attempt = store.get_remote_attempt(TASK_ID, 1)
            self.assertEqual(attempt["error_code"], "githubremoteerror")
            self.assertEqual(self._dispatch_events(store)[0]["outcome"], "exhausted")

    def test_clean_dispatch_writes_no_retry_events(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, store, github = self._coordinator(directory)
            with patch("src.remote.coordinator.time.sleep"):
                result = self._execute(coordinator)
            self.assertTrue(result["outputs"]["echo"]["ok"])
            self.assertEqual(github.dispatch_count, 1)
            self.assertEqual(github.find_count, 0)
            self.assertEqual(self._dispatch_events(store), [])

    def test_failure_classifier_is_a_closed_set(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator, _store, _github = self._coordinator(directory)
            classify = coordinator._classify_dispatch_failure
            self.assertEqual(classify(HTTP_403), ("http_status", 403))
            self.assertEqual(classify(HTTP_429), ("http_status", 429))
            self.assertEqual(classify(HTTP_500), ("http_status", 500))
            self.assertEqual(classify(HTTP_400), ("http_status", 400))
            self.assertEqual(classify(NETWORK_BLIP), ("network", 0))
            self.assertEqual(classify(SEMANTIC_CONFLICT), ("", 0))
            self.assertEqual(classify(GitHubRemoteError("encrypted job readback does not match payload")), ("", 0))
            self.assertEqual(classify(GitHubRemoteError("worker_run_head_sha_mismatch")), ("", 0))
            # R3-43：结构化 status 通道优先——同措辞但 status 缺席时正则仍兜底，
            # status 在位时直读（措辞漂移免疫）。
            self.assertEqual(
                classify(GitHubRemoteError("措辞漂移也不影响分类", status=502)),
                ("http_status", 502),
            )
            self.assertEqual(
                classify(GitHubRemoteError("no status channel 404 in text")),
                ("", 0),
                "无 status 通道且无『returned HTTP』措辞=语义不重试",
            )
            self.assertEqual(RemoteSettings(enabled=False).dispatch_retry_attempts, 3)
            self.assertEqual(RemoteSettings(enabled=False).dispatch_retry_seconds, 45.0)

    def test_backoff_waits_grow_exponentially_inside_jitter(self):
        for try_number, low, high in ((1, 0.75, 1.25), (2, 1.5, 2.5), (3, 1.5, 2.5)):
            for _ in range(20):
                value = RemoteCoordinator._retry_wait_seconds(try_number)
                self.assertGreaterEqual(value, low)
                self.assertLessEqual(value, high)


if __name__ == "__main__":
    unittest.main()
