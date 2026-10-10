from __future__ import annotations

import unittest
from unittest import mock

from scripts.audit_github_echo_cleanup import build_cleanup_evidence
from src.remote.github_client import GitHubClient


TASK_ID = "0123456789abcdef0123456789abcdef"


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _Credentials:
    def __init__(self, temporary=()):
        self.temporary = set(temporary)

    def load_secret(self, name):
        if name == "github_mailbox_repo":
            return "student/mailbox"
        raise KeyError(name)

    def has_secret(self, name):
        return name in self.temporary


class _Store:
    def get_remote_run(self, _task_id):
        return {
            "attempt": 1,
            "run_id": 42,
            "issue_number": 7,
            "repository": "student/worker",
            "remote_state": "imported",
        }

    def get_remote_attempt(self, _task_id, _attempt):
        return {"import_state": "imported", "cleanup_state": "complete"}

    def list_remote_token_leases(self):
        return []


class _GitHub:
    def __init__(self, artifacts=()):
        self.artifacts = list(artifacts)

    def list_run_artifacts(self, _repo, _run_id):
        return self.artifacts

    def job_cleanup_summary(self, _repo, _issue_number):
        return {"state": "closed", "consumed": True, "comment_count": 0}


class _GitHubApp:
    def __init__(self, secrets=()):
        self.secrets = list(secrets)

    def list_worker_secrets(self):
        return [{"name": name} for name in self.secrets]


def _context():
    return {
        "source_content_sha256": "a" * 64,
        "browser_content_sha256": "b" * 64,
        "worker": {
            "repository": "student/worker",
            "commit": "c" * 40,
            "tree": "d" * 40,
            "manifest_sha256": "e" * 64,
            "key_id": "release-key",
            "protocol_versions": [2],
        },
        "catalog": {"status": "ok", "courses": 1, "lectures": 1, "digest": "f" * 64},
    }


class EchoCleanupAuditTests(unittest.TestCase):
    def test_passing_evidence_contains_counts_but_no_repository_or_issue(self):
        evidence = build_cleanup_evidence(
            TASK_ID,
            credentials=_Credentials(),
            task_store=_Store(),
            github_app=_GitHubApp(),
            github=_GitHub(),
            context=_context(),
        )
        self.assertEqual(evidence["status"], "passed")
        self.assertEqual(evidence["observations"]["artifact_count"], 0)
        rendered = str(evidence)
        self.assertNotIn("student/worker", rendered)
        self.assertNotIn("student/mailbox", rendered)
        self.assertNotIn("issue_number", rendered)

    def test_any_remote_or_local_residue_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "artifacts_removed"):
            build_cleanup_evidence(
                TASK_ID,
                credentials=_Credentials(),
                task_store=_Store(),
                github_app=_GitHubApp(),
                github=_GitHub(artifacts=[{"id": 1}]),
                context=_context(),
            )

    def test_mailbox_cleanup_summary_never_returns_body(self):
        client = GitHubClient("token")
        with mock.patch.object(
            client,
            "_request",
            side_effect=[
                _Response({"state": "closed", "body": '{"schema":"mailbox.v2","state":"consumed"}'}),
                _Response([]),
            ],
        ):
            summary = client.job_cleanup_summary("student/mailbox", 7)
        self.assertEqual(summary, {"state": "closed", "consumed": True, "comment_count": 0})
        self.assertNotIn("body", summary)


if __name__ == "__main__":
    unittest.main()
