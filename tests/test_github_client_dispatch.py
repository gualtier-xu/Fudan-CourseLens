from __future__ import annotations

import io
import unittest
import zipfile
from unittest.mock import patch

from src.remote.github_client import GitHubClient, GitHubRemoteError


class Response:
    def __init__(self, payload, *, content=b"x"):
        self.payload = payload
        self.content = content

    def json(self):
        return self.payload


class GitHubClientDispatchPinTests(unittest.TestCase):
    def test_immediate_dispatch_response_fetches_and_verifies_run_head(self):
        client = GitHubClient("token")
        expected = "a" * 40
        responses = [
            Response({"workflow_run_id": 17, "status": "queued"}),
            Response({"id": 17, "status": "queued", "head_sha": expected}),
        ]
        with patch.object(client, "_request", side_effect=responses) as request:
            result = client.dispatch_workflow(
                "owner/template", workflow="echo.yml", ref="main",
                task_id="0" * 32, protocol_version="2", expected_head_sha=expected,
            )
        self.assertEqual(result.head_sha, expected)
        self.assertEqual(request.call_args_list[1].args[:2], (
            "GET", "/repos/owner/template/actions/runs/17",
        ))

    def test_dispatch_rejects_run_from_different_or_missing_head(self):
        client = GitHubClient("token")
        responses = [
            Response({"workflow_run_id": 17, "status": "queued"}),
            Response({"id": 17, "status": "queued", "head_sha": "b" * 40}),
        ]
        with patch.object(client, "_request", side_effect=responses):
            with self.assertRaises(GitHubRemoteError) as raised:
                client.dispatch_workflow(
                    "owner/template", workflow="process.yml", ref="main",
                    task_id="0" * 32, protocol_version="2", expected_head_sha="a" * 40,
                )
        self.assertEqual(str(raised.exception), "worker_run_head_sha_mismatch")

    def test_find_workflow_run_recovers_unique_same_task_and_signed_head(self):
        client = GitHubClient("token")
        expected = "a" * 40
        response = Response({"workflow_runs": [{
            "id": 17,
            "display_title": "CourseLens job " + "0" * 32,
            "status": "in_progress",
            "head_sha": expected,
        }]})
        with patch.object(client, "_request", return_value=response):
            found = client.find_workflow_run(
                "owner/template", workflow="process.yml", ref="main",
                task_id="0" * 32, expected_head_sha=expected,
            )
        self.assertEqual(found.run_id, 17)
        self.assertEqual(found.head_sha, expected)

    def test_find_workflow_run_rejects_duplicate_task_runs(self):
        client = GitHubClient("token")
        task_id = "0" * 32
        response = Response({"workflow_runs": [
            {"id": 17, "display_title": "CourseLens job " + task_id},
            {"id": 18, "display_title": "CourseLens job " + task_id},
        ]})
        with patch.object(client, "_request", return_value=response):
            with self.assertRaisesRegex(GitHubRemoteError, "multiple workflow runs"):
                client.find_workflow_run(
                    "owner/template", workflow="process.yml", ref="main",
                    task_id=task_id, expected_head_sha="a" * 40,
                )

    @staticmethod
    def log_zip(entries):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in entries:
                archive.writestr(name, content)
        return output.getvalue()

    def test_download_run_logs_returns_bounded_log_content(self):
        client = GitHubClient("token")
        response = Response({}, content=self.log_zip([
            ("job/1_step.txt", b"stage=one"), ("job/2_step.txt", b"stage=two"),
        ]))
        with patch.object(client, "_request", return_value=response):
            self.assertEqual(client.download_run_logs("owner/template", 17), b"stage=onestage=two")

    def test_download_run_logs_rejects_bad_zip_and_oversized_entries(self):
        client = GitHubClient("token")
        cases = (
            b"not-a-zip",
            self.log_zip([("large.txt", b"x" * (8 * 1024 * 1024 + 1))]),
            self.log_zip([
                (f"{index}.txt", b"x" * (7 * 1024 * 1024)) for index in range(5)
            ]),
        )
        for content in cases:
            with self.subTest(size=len(content)), patch.object(
                client, "_request", return_value=Response({}, content=content)
            ):
                with self.assertRaises(GitHubRemoteError):
                    client.download_run_logs("owner/template", 17)

    def test_download_run_logs_rejects_oversized_archive_before_opening(self):
        client = GitHubClient("token")
        response = Response({}, content=b"x" * (32 * 1024 * 1024 + 1))
        with patch.object(client, "_request", return_value=response):
            with self.assertRaisesRegex(GitHubRemoteError, "archive is unexpectedly large"):
                client.download_run_logs("owner/template", 17)

    def test_publish_readback_retries_through_read_after_write_lag(self):
        client = GitHubClient("token", read_after_write_seconds=10.0)
        envelope = {"schema": "job.v2", "payload": "x"}
        responses = [
            Response({"number": 5, "state": "open"}),   # create the issue
            Response([]),                                # comments listing: empty
            Response({"id": 9}),                         # post the single part
        ]
        with patch.object(client, "ensure_job_label"), \
             patch.object(client, "_matching_job_issues", return_value=[]), \
             patch.object(client, "_request", side_effect=responses), \
             patch.object(
                 client, "read_job",
                 side_effect=[
                     GitHubRemoteError("encrypted job is not available yet"),
                     (envelope, 5),
                 ],
             ), \
             patch("src.remote.github_client.time.sleep") as slept:
            result = client.publish_job("owner/mailbox", "0123456789abcdef0123456789abcdef", envelope)
        self.assertEqual(result["issue_number"], 5)
        self.assertEqual(result["part_count"], 1)
        self.assertEqual(result["comment_ids"], [9])
        self.assertTrue(slept.called)

    def test_publish_readback_still_fails_closed_after_retry_budget(self):
        client = GitHubClient("token", read_after_write_seconds=0.0)
        envelope = {"schema": "job.v2", "payload": "x"}
        responses = [
            Response({"number": 5, "state": "open"}),
            Response([]),
            Response({"id": 9}),
        ]
        with patch.object(client, "ensure_job_label"), \
             patch.object(client, "_matching_job_issues", return_value=[]), \
             patch.object(client, "_request", side_effect=responses), \
             patch.object(
                 client, "read_job",
                 side_effect=GitHubRemoteError("encrypted job is not available yet"),
             ):
            with self.assertRaisesRegex(GitHubRemoteError, "not available yet"):
                client.publish_job("owner/mailbox", "0123456789abcdef0123456789abcdef", envelope)


if __name__ == "__main__":
    unittest.main()
