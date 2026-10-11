"""Mailbox history reconciliation: primitive + action guards, all synthetic.

Covers the 2026-09 contract: preserving-history PATCH primitive (GET/PATCH
only), the guarded `reconcile-mailbox-history` action (zero DELETE, fresh
re-verification, bounded batch, idempotent operation replay), and the closed
failure modes.  Nothing here touches real GitHub state.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from path_utils import PROJECT_ROOT

from src.application import CourseLensApplication
from src.remote.connection import RemoteConnectionError
from src.remote.github_client import GitHubClient, GitHubRemoteError


TEST_CACHE = PROJECT_ROOT / "runtime" / "cache"
TEST_CACHE.mkdir(parents=True, exist_ok=True)

WORKER_REPO = "student/Fudan-CourseLens-Worker"
MAILBOX_REPO = "student/Fudan-CourseLens-Mailbox"
CONSUMED_BODY = '{"schema":"mailbox.v2","state":"consumed"}'


class StatusResponse:
    def __init__(self, payload, *, status_code=200, headers=None):
        self.payload = payload
        self.status_code = int(status_code)
        self.headers = headers or {}

    def json(self):
        return self.payload


def _issue(number, *, title="[courselens-job] task", body="sealed", state="closed",
           comments=2, labels=("courselens-job",)):
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "comments": comments,
        "labels": [{"name": name} for name in labels],
    }


class MarkConsumedPreservingHistoryTests(unittest.TestCase):
    """The write primitive: GET → verify → PATCH → readback, nothing else."""

    def _client(self, responses):
        client = GitHubClient("token")
        recorder = []
        def record(method, path, **kwargs):
            recorder.append((method, path, dict(kwargs)))
            return responses.pop(0)
        return client, record, recorder

    def test_seal_writes_consumed_marker_and_preserves_everything_else(self):
        issue = _issue(123, body="sealed")
        client, record, recorder = self._client([
            StatusResponse(issue),
            StatusResponse({}),
            StatusResponse(_issue(123, body=CONSUMED_BODY)),
        ])
        with patch.object(client, "_request", side_effect=record):
            result = client.mark_job_consumed_preserving_history(MAILBOX_REPO, 123)
        self.assertEqual(result, {"changed": True, "state": "closed", "consumed": True})
        self.assertEqual(
            [(method, path) for method, path, _ in recorder],
            [
                ("GET", f"/repos/{MAILBOX_REPO}/issues/123"),
                ("PATCH", f"/repos/{MAILBOX_REPO}/issues/123"),
                ("GET", f"/repos/{MAILBOX_REPO}/issues/123"),
            ],
        )
        patch_body = recorder[1][2]["json"]
        self.assertEqual(patch_body, {"state": "closed", "body": CONSUMED_BODY})
        # 零 DELETE、零评论端点、不改标题/标签（PATCH 载荷仅 state+body）。
        self.assertEqual(sorted({method for method, _, _ in recorder}), ["GET", "PATCH"])
        self.assertNotIn("title", patch_body)
        self.assertNotIn("labels", patch_body)

    def test_already_consumed_target_is_a_no_op_with_a_single_get(self):
        client = GitHubClient("token")
        calls = []
        def respond(method, path, **kwargs):
            calls.append((method, path))
            return StatusResponse(_issue(7, body=CONSUMED_BODY))
        with patch.object(client, "_request", side_effect=respond):
            result = client.mark_job_consumed_preserving_history(MAILBOX_REPO, 7)
        self.assertEqual(result, {"changed": False, "state": "closed", "consumed": True})
        self.assertEqual(calls, [("GET", f"/repos/{MAILBOX_REPO}/issues/7")])

    def test_open_or_unmanaged_or_missing_targets_are_rejected_without_patch(self):
        cases = (
            (_issue(1, state="open"), 1),        # 活动中的 open issue：不是历史
            (_issue(2, title="renamed"), 2),     # 标题前缀丢失
            (_issue(3, labels=()), 3),           # label 被移除
            (None, 9),                           # issue 不存在（404）
        )
        for issue, number in cases:
            client = GitHubClient("token")
            calls = []

            def respond(method, path, **kwargs):
                calls.append((method, path))
                return StatusResponse(issue) if issue else StatusResponse({}, status_code=404)

            with self.subTest(number=number):
                with patch.object(client, "_request", side_effect=respond):
                    with self.assertRaises(GitHubRemoteError):
                        client.mark_job_consumed_preserving_history(MAILBOX_REPO, number)
                self.assertEqual([method for method, _ in calls], ["GET"])

    def test_readback_mismatch_fails_closed(self):
        client = GitHubClient("token")
        responses = [
            StatusResponse(_issue(11, body="sealed")),
            StatusResponse({}),
            StatusResponse(_issue(11, body="still sealed")),  # 回读仍是载荷
        ]
        with patch.object(client, "_request", side_effect=lambda *a, **k: responses.pop(0)):
            with self.assertRaisesRegex(GitHubRemoteError, "readback"):
                client.mark_job_consumed_preserving_history(MAILBOX_REPO, 11)

    def test_readback_reopened_issue_fails_closed(self):
        client = GitHubClient("token")
        responses = [
            StatusResponse(_issue(12, body="sealed")),
            StatusResponse({}),
            StatusResponse(_issue(12, body=CONSUMED_BODY, state="open")),
        ]
        with patch.object(client, "_request", side_effect=lambda *a, **k: responses.pop(0)):
            with self.assertRaisesRegex(GitHubRemoteError, "readback"):
                client.mark_job_consumed_preserving_history(MAILBOX_REPO, 12)


class FakeReconcileGitHubApp:
    """GitHubApp stand-in serving every read the action performs."""

    def __init__(self, *, issues=(), active_runs=0, artifacts=0, flip_active_after=0,
                 job_token=False):
        self.issues = list(issues)
        self.active_runs = int(active_runs)
        self.artifacts = int(artifacts)
        self.flip_active_after = int(flip_active_after)
        self.job_token = job_token
        self.reads = {"runs": 0, "artifacts": 0}

    def access_token(self, **_kwargs):
        return "token"

    def check_worker_integrity(self, *, read_only=True):
        return {
            "trusted": True,
            "dispatch_mode": "personal-worker",
            "repository": WORKER_REPO,
        }

    def inspect_managed_resources(self, *, read_only=True):
        return {
            "identity": {"login": "student", "account_id": 1},
            "installation": {
                "installed": True,
                "repository_selection_exact": True,
            },
            "mailbox": {
                "exists": True, "private": True, "managed": True, "has_issues": True,
                "archived": False, "disabled": False, "owner": "student",
            },
            "secret_names": ["COURSELENS_JOB_TOKEN"] if self.job_token else [],
        }

    def _api(self, _method, path, *, token=None, expected=None, params=None):
        if "/actions/workflows/" in path:
            self.reads["runs"] += 1
            if self.flip_active_after and self.reads["runs"] > self.flip_active_after:
                self.active_runs = 1
            runs = [{"id": 1, "status": "in_progress"}] * self.active_runs
            return StatusResponse({"total_count": len(runs), "workflow_runs": runs})
        if path.endswith("/actions/artifacts"):
            self.reads["artifacts"] += 1
            artifacts = [{"id": 9, "name": "courselens-result-x"}] * self.artifacts
            return StatusResponse({"artifacts": artifacts})
        if path.endswith("/environments/courselens-worker/secrets"):
            names = ["COURSELENS_JOB_TOKEN"] if self.job_token else []
            return StatusResponse({"secrets": [{"name": name} for name in names]})
        if path.endswith("/issues"):
            return StatusResponse(self.issues)
        raise AssertionError(path)


class FakeReconcileStore:
    def __init__(self, *, leases=(), runs=None, cleanup_pending=0):
        self.leases = list(leases)
        self.runs = list(runs or [])
        self.cleanup_pending = int(cleanup_pending)

    def list_remote_token_leases(self):
        return self.leases

    def migration_cleanup_pending_count(self):
        return self.cleanup_pending

    def list_remote_runs(self, *, limit=10):
        return self.runs[: int(limit)]


class FakeCredentials:
    def __init__(self, values):
        self.values = dict(values)

    def load_secret(self, name):
        return self.values[name]

    def has_secret(self, name):
        return name in self.values

    def list_secret_names(self, *, prefix=""):
        return [name for name in self.values if name.startswith(prefix)]


class RecordingGitHubClient:
    """Captures the write primitive calls issued by the action."""

    latest = None

    def __init__(self, token, *, proxy_url=""):
        self.calls = []
        self.fail_numbers = set()
        self.outcomes = {}
        RecordingGitHubClient.latest = self

    def mark_job_consumed_preserving_history(self, repo, number):
        self.calls.append((repo, number))
        if number in self.fail_numbers:
            raise GitHubRemoteError("synthetic write failure")
        return self.outcomes.get(number, {"changed": True, "state": "closed", "consumed": True})


def _base_credentials(**extra):
    secrets = {
        "github_worker_repo": WORKER_REPO,
        "github_mailbox_repo": MAILBOX_REPO,
        "github_app_access_token": "token",
    }
    secrets.update(extra)
    return FakeCredentials(secrets)


class ReconcileHarness:
    """Binds the real action method onto a narrow synthetic application."""

    _reconcile_mailbox_history = CourseLensApplication._reconcile_mailbox_history

    def __init__(self, credentials, task_store, github_app, *, active=False):
        self.credentials = credentials
        self.task_store = task_store
        self.github_app = github_app
        self.remote_settings = SimpleNamespace(proxy_url="")
        self._active = bool(active)

    def has_active_work(self):
        return self._active

    def run(self):
        return self._reconcile_mailbox_history()


def _closed_history_issue(number, comments):
    return _issue(number, body="sealed", state="closed", comments=comments)


class ReconcileMailboxHistoryActionTests(unittest.TestCase):
    def setUp(self):
        RecordingGitHubClient.latest = None

    def _harness(self, issues, *, credentials=None, store=None, github=None, active=False):
        github = github or FakeReconcileGitHubApp(issues=issues)
        harness = ReconcileHarness(
            credentials or _base_credentials(),
            store or FakeReconcileStore(),
            github,
            active=active,
        )
        return harness

    def test_two_closed_history_records_are_sealed_in_order_without_deletes(self):
        # #123/#128 回归场景：closed/unconsumed（2 与 181 条评论）通过通用分类修复。
        issues = [
            _closed_history_issue(123, comments=2),
            _closed_history_issue(128, comments=181),
        ]
        harness = self._harness(issues)
        with patch("src.remote.github_client.GitHubClient", RecordingGitHubClient):
            result = harness.run()
        self.assertTrue(result["reconcile_complete"])
        self.assertEqual(result["processed_count"], 2)
        self.assertEqual(result["changed_count"], 2)
        self.assertEqual(result["remaining_count"], 0)
        client = RecordingGitHubClient.latest
        self.assertEqual(
            client.calls,
            [(MAILBOX_REPO, 123), (MAILBOX_REPO, 128)],
        )
        # 每条写入前都做了 fresh 零活动复核（首查 + 每条一次，每次 2 个 workflow 清单）。
        self.assertEqual(harness.github_app.reads["runs"], 6)

    def test_stale_remote_enabled_flag_no_longer_blocks_reconcile(self):
        """U1：开关退役后「必须先关闭远程计算」前置不复存在——残留
        remote_enabled=1 的装机也能修复历史记录（零活动前置照旧，见下）。"""
        harness = self._harness(
            [_closed_history_issue(123, comments=2)],
            credentials=_base_credentials(remote_enabled="1"),
        )
        with patch("src.remote.github_client.GitHubClient", RecordingGitHubClient):
            result = harness.run()
        self.assertTrue(result["reconcile_complete"])
        self.assertEqual(result["changed_count"], 1)

    def test_local_active_work_blocks_reconcile(self):
        harness = self._harness([_closed_history_issue(123, comments=0)], active=True)
        with self.assertRaises(RemoteConnectionError) as raised:
            harness.run()
        self.assertEqual(raised.exception.code, "active_local_work_present")

    def test_active_worker_run_or_artifact_blocks_reconcile(self):
        for kwargs, code in (
            ({"active_runs": 1}, "active_worker_run_present"),
            ({"artifacts": 1}, "worker_artifacts_present"),
            ({"job_token": True}, "remote_cleanup_pending"),
        ):
            with self.subTest(code=code):
                github = FakeReconcileGitHubApp(issues=[_closed_history_issue(5, comments=0)], **kwargs)
                harness = self._harness([], github=github)
                with self.assertRaises(RemoteConnectionError) as raised:
                    harness.run()
                self.assertEqual(raised.exception.code, code)

    def test_lease_result_key_or_cleanup_pending_blocks_reconcile(self):
        for store, credentials, code in (
            (FakeReconcileStore(leases=[{"task_id": "t"}]), None, "remote_cleanup_pending"),
            (
                FakeReconcileStore(),
                FakeCredentials(_base_credentials().values | {"remote_result_private:x": "k"}),
                "remote_cleanup_pending",
            ),
            (FakeReconcileStore(cleanup_pending=1), None, "remote_cleanup_pending"),
        ):
            with self.subTest(code=code):
                harness = self._harness(
                    [_closed_history_issue(6, comments=0)],
                    credentials=credentials, store=store,
                )
                with self.assertRaises(RemoteConnectionError) as raised:
                    harness.run()
                self.assertEqual(raised.exception.code, code)

    def test_drift_trust_and_installation_guards_block_reconcile(self):
        drift_issue = _issue(7, title="renamed", body="sealed", state="closed", comments=0)
        harness = self._harness([drift_issue, _closed_history_issue(8, comments=0)])
        with self.assertRaises(RemoteConnectionError) as raised:
            harness.run()
        self.assertEqual(raised.exception.code, "mailbox_metadata_drift")

        open_issue = _issue(9, body="sealed", state="open", comments=1)
        harness = self._harness([open_issue])
        with self.assertRaises(RemoteConnectionError) as raised:
            harness.run()
        self.assertEqual(raised.exception.code, "mailbox_open_unconsumed")

        class Distrusted(FakeReconcileGitHubApp):
            def check_worker_integrity(self, *, read_only=True):
                return {"trusted": False, "dispatch_mode": "personal-worker", "repository": WORKER_REPO}

        harness = self._harness([], github=Distrusted(issues=[_closed_history_issue(10, comments=0)]))
        with self.assertRaises(RemoteConnectionError) as raised:
            harness.run()
        self.assertEqual(raised.exception.code, "worker_trust_unavailable")

        class LooseInstallation(FakeReconcileGitHubApp):
            def inspect_managed_resources(self, *, read_only=True):
                return {
                    "identity": {"login": "student"},
                    "installation": {"repository_selection_exact": False},
                    "mailbox": {
                        "exists": True, "private": True, "managed": True, "has_issues": True,
                        "archived": False, "disabled": False, "owner": "student",
                    },
                    "secret_names": [],
                }

        harness = self._harness([], github=LooseInstallation(issues=[_closed_history_issue(11, comments=0)]))
        with self.assertRaises(RemoteConnectionError) as raised:
            harness.run()
        self.assertEqual(raised.exception.code, "installation_scope_not_exact")

    def test_batch_is_bounded_and_reports_remaining(self):
        issues = [_closed_history_issue(number, comments=0) for number in range(1, 26)]
        harness = self._harness(issues)
        with patch("src.remote.github_client.GitHubClient", RecordingGitHubClient):
            result = harness.run()
        self.assertFalse(result["reconcile_complete"])
        self.assertEqual(result["processed_count"], 20)
        self.assertEqual(result["remaining_count"], 5)
        self.assertEqual(len(RecordingGitHubClient.latest.calls), 20)

    def test_mid_batch_write_failure_keeps_completed_items_retryable(self):
        issues = [
            _closed_history_issue(21, comments=0),
            _closed_history_issue(22, comments=0),
            _closed_history_issue(23, comments=0),
        ]
        harness = self._harness(issues)

        def factory(token, *, proxy_url=""):
            client = RecordingGitHubClient(token, proxy_url=proxy_url)
            client.fail_numbers = {22}
            return client

        with patch("src.remote.github_client.GitHubClient", factory):
            result = harness.run()
        self.assertFalse(result["reconcile_complete"])
        self.assertEqual(result["processed_count"], 1)
        self.assertEqual(result["remaining_count"], 2)
        self.assertEqual(result["error_code"], "mailbox_reconcile_failed")

    def test_first_item_failure_raises_closed_error(self):
        harness = self._harness([_closed_history_issue(31, comments=0)])

        def factory(token, *, proxy_url=""):
            client = RecordingGitHubClient(token, proxy_url=proxy_url)
            client.fail_numbers = {31}
            return client

        with patch("src.remote.github_client.GitHubClient", factory):
            with self.assertRaises(RemoteConnectionError) as raised:
                harness.run()
        self.assertEqual(raised.exception.code, "mailbox_reconcile_failed")

    def test_mid_batch_activity_appearance_stops_the_batch(self):
        # 首查通过后、第 2 条写入前出现活动 run：立即停止。
        github = FakeReconcileGitHubApp(
            issues=[_closed_history_issue(41, comments=0), _closed_history_issue(42, comments=0)],
            # 首查（2）+ 第 1 条复核（2）通过后，第 2 条复核时出现活动 run。
            flip_active_after=4,
        )
        harness = self._harness([], github=github)
        with patch("src.remote.github_client.GitHubClient", RecordingGitHubClient):
            result = harness.run()
        self.assertEqual(result["processed_count"], 1)
        self.assertEqual(result["remaining_count"], 1)
        self.assertEqual(result["error_code"], "mailbox_reconcile_stopped_activity")

    def test_result_contains_no_issue_numbers_or_payloads(self):
        issues = [_closed_history_issue(51, comments=4)]
        harness = self._harness(issues)
        with patch("src.remote.github_client.GitHubClient", RecordingGitHubClient):
            result = harness.run()
        serialized = json.dumps(result, default=str)
        self.assertNotIn("51", serialized)
        self.assertNotIn("sealed", serialized)


class OperationReplayTests(unittest.TestCase):
    """operation_id replay must not issue a second batch of PATCHes."""

    def test_replayed_operation_id_returns_stored_result_without_new_writes(self):
        with tempfile.TemporaryDirectory(dir=TEST_CACHE) as temp:
            service = CourseLensApplication(Path(temp))
            try:
                for name, value in {
                    "github_worker_repo": WORKER_REPO,
                    "github_mailbox_repo": MAILBOX_REPO,
                    "github_app_access_token": "token",
                    "remote_enabled": "0",
                }.items():
                    service.credentials.save_secret(name, value)
                service.github_app = FakeReconcileGitHubApp(
                    issues=[_closed_history_issue(123, comments=2)]
                )
                service.remote_connection = SimpleNamespace(
                    request_probe=lambda: None, stop=lambda *a, **k: True
                )
                with patch("src.remote.github_client.GitHubClient", RecordingGitHubClient):
                    first = service.remote_connection_action(
                        "reconcile-mailbox-history", operation_id="replay-test-0001"
                    )
                    second = service.remote_connection_action(
                        "reconcile-mailbox-history", operation_id="replay-test-0001"
                    )
                self.assertEqual(first["state"], "accepted")
                self.assertEqual(second, first)
                self.assertEqual(
                    RecordingGitHubClient.latest.calls,
                    [(MAILBOX_REPO, 123)],
                )
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
