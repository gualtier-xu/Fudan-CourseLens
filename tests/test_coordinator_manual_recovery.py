"""T6（夜14-R7 台账）：coordinator 三个手动恢复入口直测。

``resume_existing_run_only`` / ``resume_result_import_only`` /
``cleanup_failed_run`` 是 RUN45282 真实事故夜的运维恢复路径，唯一调用方
是 ``scripts/verify_github_process_canary.py``——自动恢复链
（test_lost_result_recovery.py，经 _FakeCoordinator 编排层）盖不到真
coordinator 的绑定校验/fail-closed 分支。本件以真 RemoteCoordinator +
真 TaskStore（临时 sqlite）+ 合成签名结果信封逐入口钉：happy 一遍 +
状态不符 fail-closed 一遍，任何绑定/结论漂移都红。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.remote.coordinator import RemoteCoordinator, RemoteSettings
from src.remote.github_client import GitHubRemoteError
from src.remote.protocol import (
    PROTOCOL_VERSION,
    RESULT_SCHEMA,
    generate_box_keypair,
    generate_signing_keypair,
    seal_result,
)
from src.runtime.task_store import TaskStore


TASK_ID = "0123456789abcdef0123456789abcdef"
INPUT_HASH = "a" * 64


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
    """闭集合成 GitHub 面：run 字典/工件清单/下载信封/清理动作全录制造。"""

    def __init__(self, result_envelope: bytes):
        self.result_envelope = result_envelope
        self.run = {
            "id": 99, "status": "completed", "conclusion": "success",
            "run_attempt": 2, "event": "workflow_dispatch",
        }
        self.artifacts = [
            {"id": 12, "name": f"courselens-result-{TASK_ID}", "expired": False},
            {"id": 13, "name": f"courselens-checkpoint-{TASK_ID}-1", "expired": False},
            {"id": 14, "name": "unrelated-artifact", "expired": False},
        ]
        self.downloads: list[int] = []
        self.deleted_artifacts: list[int] = []
        self.cleaned_jobs: list[tuple[str, int]] = []
        self.artifact_error: Exception | None = None

    def get_run(self, *args, **kwargs):
        return dict(self.run)

    def list_run_artifacts(self, *args, **kwargs):
        if self.artifact_error is not None:
            raise self.artifact_error
        return [dict(item) for item in self.artifacts]

    def download_artifact_file(self, repo, artifact_id, filename):
        self.downloads.append(int(artifact_id))
        return self.result_envelope

    def delete_artifact(self, repo, artifact_id):
        self.deleted_artifacts.append(int(artifact_id))

    def cleanup_job(self, repo, issue_number, **kwargs):
        self.cleaned_jobs.append((repo, int(issue_number)))


class ManualRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        worker_private, worker_public = generate_box_keypair()
        signing_private, signing_public = generate_signing_keypair()
        self.box_private = worker_private
        result = {
            "schema": RESULT_SCHEMA,
            "protocol_version": PROTOCOL_VERSION,
            "task_id": TASK_ID,
            "job_kind": "echo",
            "input_hash": INPUT_HASH,
            "status": "completed",
            "outputs": {"echo": {"ok": True}},
            "metrics": {},
            "warnings": [],
        }
        envelope = seal_result(result, worker_public, signing_private)
        self.github = FakeGitHub(json.dumps(envelope).encode("utf-8"))
        self.secrets = MemorySecrets()
        self.settings = RemoteSettings(
            enabled=True,
            github_token="token",
            worker_public_key=worker_public,
            worker_signing_public_key=signing_public,
            poll_seconds=0.001,
        )
        self.store = TaskStore(Path(tmp.name) / "state.db")
        self.store.add_task("subtitle", "course", "lecture", {})
        self.coordinator = RemoteCoordinator(
            self.settings, self.store, self.secrets, github=self.github,
        )
        self.result_secret = f"remote_result_private:{TASK_ID}"

    def _seed_run(self, **overrides):
        row = {
            "repository": self.settings.public_repo,
            "workflow": self.settings.workflow,
            "run_id": 99,
            "attempt": 2,
            "issue_number": 7,
            "artifact_id": 12,
            "input_hash": INPUT_HASH,
            "remote_state": "downloading_result",
        }
        row.update(overrides)
        self.store.upsert_remote_run(TASK_ID, **row)
        self.secrets.save_secret(self.result_secret, self.box_private)

    # --- resume_result_import_only --------------------------------------

    def test_result_import_resume_round_trip_without_new_dispatch(self):
        self._seed_run()
        imported = []
        result = self.coordinator.resume_result_import_only(
            task_id=TASK_ID,
            expected_run_id=99,
            expected_attempt=2,
            import_result=imported.append,
            progress=lambda *args: None,
        )
        self.assertEqual(imported, [result])
        self.assertEqual(result["outputs"]["echo"], {"ok": True})
        row = self.store.get_remote_run(TASK_ID) or {}
        self.assertEqual(row["remote_state"], "imported")
        self.assertFalse(self.secrets.has_secret(self.result_secret), "默认消费一次性密钥")
        self.assertEqual(self.github.downloads, [12])
        self.assertEqual(self.github.deleted_artifacts, [12, 13], "结果工件+检查点工件同清")
        self.assertEqual(self.github.cleaned_jobs, [(self.settings.private_repo, 7)])

    def test_result_import_resume_rejects_incomplete_binding_without_side_effects(self):
        self._seed_run(attempt=3)
        with self.assertRaisesRegex(GitHubRemoteError, "rerun result import binding is invalid"):
            self.coordinator.resume_result_import_only(
                task_id=TASK_ID,
                expected_run_id=99,
                expected_attempt=2,
                import_result=lambda value: None,
                progress=lambda *args: None,
            )
        self.assertEqual(self.github.downloads, [], "绑定不符零下载零派发")
        self.assertTrue(self.secrets.has_secret(self.result_secret), "fail-closed 不消费密钥")

    def test_result_import_resume_rejects_unsuccessful_source_run(self):
        self._seed_run()
        self.github.run = dict(self.github.run, conclusion="failure")
        with self.assertRaisesRegex(GitHubRemoteError, "not a signed success"):
            self.coordinator.resume_result_import_only(
                task_id=TASK_ID,
                expected_run_id=99,
                expected_attempt=2,
                import_result=lambda value: None,
                progress=lambda *args: None,
            )
        self.assertEqual(self.github.downloads, [])

    def test_result_import_resume_can_retain_the_result_secret(self):
        self._seed_run()
        self.coordinator.resume_result_import_only(
            task_id=TASK_ID,
            expected_run_id=99,
            expected_attempt=2,
            import_result=lambda value: None,
            progress=lambda *args: None,
            retain_result_secret=True,
        )
        self.assertTrue(self.secrets.has_secret(self.result_secret))

    # --- resume_existing_run_only ----------------------------------------

    def test_existing_run_resume_reattaches_and_imports(self):
        self._seed_run(remote_state="rerun_running")
        imported = []
        result = self.coordinator.resume_existing_run_only(
            task_id=TASK_ID,
            expected_run_id=99,
            expected_attempt=2,
            import_result=imported.append,
            cancel_requested=lambda: False,
            progress=lambda *args: None,
        )
        self.assertEqual(imported, [result])
        row = self.store.get_remote_run(TASK_ID) or {}
        self.assertEqual(row["remote_state"], "imported")
        self.assertEqual(row["artifact_id"], 12)

    def test_existing_run_resume_rejects_wrong_state_binding(self):
        self._seed_run(remote_state="failed")
        with self.assertRaisesRegex(GitHubRemoteError, "existing rerun binding is invalid"):
            self.coordinator.resume_existing_run_only(
                task_id=TASK_ID,
                expected_run_id=99,
                expected_attempt=2,
                import_result=lambda value: None,
                cancel_requested=lambda: False,
                progress=lambda *args: None,
            )
        self.assertEqual(self.github.downloads, [])

    def test_existing_run_resume_rejects_attempt_drift(self):
        self._seed_run(remote_state="rerun_running")
        self.github.run = dict(self.github.run, run_attempt=3)
        with self.assertRaisesRegex(GitHubRemoteError, "not exactly two"):
            self.coordinator.resume_existing_run_only(
                task_id=TASK_ID,
                expected_run_id=99,
                expected_attempt=2,
                import_result=lambda value: None,
                cancel_requested=lambda: False,
                progress=lambda *args: None,
            )

    # --- cleanup_failed_run -----------------------------------------------

    def test_cleanup_failed_run_removes_result_checkpoint_and_mailbox(self):
        self._seed_run(remote_state="failed")
        self.github.run = dict(self.github.run, conclusion="failure")
        outcome = self.coordinator.cleanup_failed_run(TASK_ID, expected_attempt=2)
        self.assertEqual(outcome, {"run_id": 99, "attempt": 2, "cleanup_state": "complete"})
        self.assertEqual(self.github.deleted_artifacts, [12, 13], "只删本任务结果/检查点工件")
        self.assertEqual(self.github.cleaned_jobs, [(self.settings.private_repo, 7)])
        row = self.store.get_remote_run(TASK_ID) or {}
        self.assertEqual(row["remote_state"], "failed")
        self.assertEqual(row["last_error"], "", "收口后失败账目清空")

    def test_cleanup_failed_run_rejects_non_terminal_bindings(self):
        self._seed_run(remote_state="failed", attempt=1)
        with self.assertRaisesRegex(GitHubRemoteError, "failed rerun binding is invalid"):
            self.coordinator.cleanup_failed_run(TASK_ID, expected_attempt=2)
        self.github.run = dict(self.github.run, conclusion="success")
        self._seed_run(remote_state="failed")
        with self.assertRaisesRegex(GitHubRemoteError, "not a terminal failed attempt"):
            self.coordinator.cleanup_failed_run(TASK_ID, expected_attempt=2)
        self.assertEqual(self.github.deleted_artifacts, [])

    def test_cleanup_failed_run_records_pending_state_on_remote_fault(self):
        self._seed_run(remote_state="failed")
        self.github.run = dict(self.github.run, conclusion="failure")
        self.github.artifact_error = GitHubRemoteError("GitHub API GET failed: boom")
        with self.assertRaisesRegex(GitHubRemoteError, "cleanup remains pending"):
            self.coordinator.cleanup_failed_run(TASK_ID, expected_attempt=2)
        row = self.store.get_remote_run(TASK_ID) or {}
        self.assertEqual(row["last_error"], "remote_cleanup_pending", "pending 落账可重入")
        self.assertTrue(
            self.secrets.has_secret(self.result_secret),
            "cleanup 面不消费一次性密钥（只有导入/取消路径删）",
        )
        attempt = self.store.get_remote_attempt(TASK_ID, 2) or {}
        self.assertEqual(attempt.get("cleanup_state"), "cleanup_pending")


if __name__ == "__main__":
    unittest.main()
