"""Minimal GitHub REST client that never includes response bodies in errors."""

from __future__ import annotations

import io
import json
import re
import time
import zipfile
from dataclasses import dataclass
from typing import Any, Iterable

import requests

from .protocol import chunk_envelope, join_envelope, validate_task_id
from src.runtime.test_mode import EgressBlockedError, ensure_egress_allowed


API_ROOT = "https://api.github.com"
ISSUE_TITLE_PREFIX = "[courselens-job]"
ISSUE_LABEL = "courselens-job"
CONSUMED_MARKER_BODY = '{"schema":"mailbox.v2","state":"consumed"}'
_JOB_PART_RE = re.compile(r"^(?:job )?part (\d+)/(\d+)\n([A-Za-z0-9+/=]+)$")
_CONTROL_PART_RE = re.compile(r"^control (\d+) part (\d+)/(\d+)\n([A-Za-z0-9+/=]+)$")
_STATUS_RE = re.compile(r"^status (\d+)\n([A-Za-z0-9+/=]+)$")


class GitHubRemoteError(RuntimeError):
    """Remote execution failure.

    ``code`` optionally carries the worker's closed-set failure reason as
    reported by its last signed progress control; it is redacted metadata
    only (never a raw message) and may stay empty for client-side failures.
    """

    def __init__(self, message: str, *, code: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.code = str(code or "")
        # R3-43：结构化 HTTP 状态通道（worker llm.py R3-07 同形）；消费面
        # coordinator._classify_dispatch_failure 优先读它，消息正则保留一个
        # 版本作兼容回退。
        self.status = int(status) if status is not None else None


@dataclass(frozen=True)
class DispatchResult:
    run_id: int
    status: str
    head_sha: str = ""


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        api_root: str = API_ROOT,
        timeout: int = 30,
        proxy_url: str = "",
        read_after_write_seconds: float = 30.0,
    ):
        token = str(token or "").strip()
        if not token:
            raise ValueError("GitHub token is required")
        self.api_root = api_root.rstrip("/")
        self.timeout = max(5, int(timeout))
        self.read_after_write_seconds = max(0.0, float(read_after_write_seconds))
        self.session = requests.Session()
        self.session.trust_env = False
        if proxy_url:
            self.session.proxies.update({"http": proxy_url, "https": proxy_url})
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "Fudan-CourseLens/remote-v2",
        })

    def _request(self, method: str, path: str, *, expected: Iterable[int] = (200,), **kwargs):
        # 测试模式出站门（src/runtime/test_mode.py）：拒绝 = egress_blocked
        # 审计行 + GitHubRemoteError(code="egress_blocked")，先于任何连接。
        url = f"{self.api_root}{path}"
        try:
            ensure_egress_allowed(url, purpose="github_remote")
        except EgressBlockedError as exc:
            raise GitHubRemoteError(
                f"GitHub API {method} failed: {exc.code}", code=exc.code
            ) from exc
        try:
            response = self.session.request(
                method,
                url,
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise GitHubRemoteError(f"GitHub API {method} failed: {type(exc).__name__}") from exc
        if response.status_code not in set(expected):
            request_id = response.headers.get("X-GitHub-Request-Id", "unknown")
            raise GitHubRemoteError(
                f"GitHub API {method} returned HTTP {response.status_code} (request {request_id})",
                status=response.status_code,
            )
        return response

    def dispatch_workflow(
        self,
        repo: str,
        *,
        workflow: str,
        ref: str,
        task_id: str,
        protocol_version: str,
        expected_head_sha: str = "",
    ) -> DispatchResult:
        task_id = validate_task_id(task_id)
        expected_head_sha = str(expected_head_sha or "").strip().lower()
        started = time.time()
        response = self._request(
            "POST",
            f"/repos/{repo}/actions/workflows/{workflow}/dispatches",
            expected=(200, 201, 204),
            json={
                "ref": ref,
                "inputs": {"task_id": task_id, "protocol_version": str(protocol_version)},
            },
        )
        if response.content:
            try:
                payload = response.json()
                run_id = int(payload.get("workflow_run_id") or payload.get("id") or 0)
                if run_id:
                    run = self.get_run(repo, run_id) if expected_head_sha else payload
                    head_sha = self._require_run_head_sha(run, expected_head_sha)
                    return DispatchResult(
                        run_id=run_id,
                        status=str(run.get("status") or payload.get("status") or "queued"),
                        head_sha=head_sha,
                    )
            except (ValueError, TypeError):
                pass
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            runs = self._request(
                "GET",
                f"/repos/{repo}/actions/workflows/{workflow}/runs",
                params={"event": "workflow_dispatch", "branch": ref, "per_page": 20},
            ).json().get("workflow_runs", [])
            candidates = [
                run for run in runs
                if task_id in str(run.get("display_title") or "")
                and _parse_github_time(str(run.get("created_at") or "")) >= started - 5
            ]
            if candidates:
                run = max(candidates, key=lambda item: int(item.get("id") or 0))
                head_sha = self._require_run_head_sha(run, expected_head_sha)
                return DispatchResult(
                    run_id=int(run["id"]),
                    status=str(run.get("status") or "queued"),
                    head_sha=head_sha,
                )
            time.sleep(1)
        raise GitHubRemoteError("GitHub accepted the workflow dispatch but its run id was not found")

    def get_run(self, repo: str, run_id: int) -> dict[str, Any]:
        payload = self._request("GET", f"/repos/{repo}/actions/runs/{int(run_id)}").json()
        return {
            key: payload.get(key)
            for key in (
                "id", "status", "conclusion", "run_attempt", "head_sha",
                "event", "workflow_id", "created_at", "run_started_at", "updated_at",
            )
        }

    def rerun_workflow_once(self, repo: str, run_id: int) -> None:
        """Request one rerun attempt without retries or fallback dispatch."""
        self._request(
            "POST",
            f"/repos/{repo}/actions/runs/{int(run_id)}/rerun",
            expected=(201,),
        )

    def find_workflow_run(
        self,
        repo: str,
        *,
        workflow: str,
        ref: str,
        task_id: str,
        expected_head_sha: str,
    ) -> DispatchResult | None:
        """Recover a unique workflow-dispatch run after a local crash gap."""
        task_id = validate_task_id(task_id)
        payload = self._request(
            "GET",
            f"/repos/{repo}/actions/workflows/{workflow}/runs",
            params={"event": "workflow_dispatch", "branch": ref, "per_page": 100},
        ).json()
        matches = [
            run for run in list(payload.get("workflow_runs") or [])
            if task_id in str(run.get("display_title") or "")
        ]
        self.require_unique_workflow_inventory(matches, task_id=task_id)
        if not matches:
            return None
        run = matches[0]
        head_sha = self._require_run_head_sha(run, expected_head_sha)
        return DispatchResult(
            run_id=int(run.get("id") or 0),
            status=str(run.get("status") or "queued"),
            head_sha=head_sha,
        )

    @staticmethod
    def require_unique_workflow_inventory(inventory: list[dict[str, Any]], *, task_id: str) -> None:
        """Require exactly zero or one opaque-task workflow run, never a guess."""
        validate_task_id(task_id)
        matches = [run for run in inventory if task_id in str(run.get("display_title") or run.get("task_id") or "")]
        ids = [int(run.get("id") or 0) for run in matches]
        if any(value <= 0 for value in ids) or len(ids) != len(set(ids)) or len(matches) > 1:
            raise GitHubRemoteError("multiple workflow runs or invalid inventory exist for one process canary task")

    @staticmethod
    def _require_run_head_sha(run: dict[str, Any], expected_head_sha: str) -> str:
        """Fail closed when a direct-template dispatch did not use its signed pin."""
        actual = str(run.get("head_sha") or "").strip().lower()
        expected = str(expected_head_sha or "").strip().lower()
        if expected and actual != expected:
            raise GitHubRemoteError("worker_run_head_sha_mismatch")
        return actual

    def get_run_jobs(self, repo: str, run_id: int) -> list[dict[str, Any]]:
        """Return only the step state needed for the authorization handshake."""
        payload = self._request(
            "GET",
            f"/repos/{repo}/actions/runs/{int(run_id)}/jobs",
            params={"filter": "latest", "per_page": 100},
        ).json()
        output: list[dict[str, Any]] = []
        for job in payload.get("jobs", []):
            output.append({
                "name": str(job.get("name") or ""),
                "status": str(job.get("status") or ""),
                "conclusion": job.get("conclusion"),
                "steps": [
                    {
                        "name": str(step.get("name") or ""),
                        "status": str(step.get("status") or ""),
                        "conclusion": step.get("conclusion"),
                    }
                    for step in (job.get("steps") or [])
                ],
            })
        return output

    def cancel_run(self, repo: str, run_id: int) -> None:
        self._request(
            "POST",
            f"/repos/{repo}/actions/runs/{int(run_id)}/cancel",
            expected=(202, 409),
        )

    def ensure_job_label(self, repo: str) -> None:
        response = self._request(
            "GET", f"/repos/{repo}/labels/{ISSUE_LABEL}", expected=(200, 404)
        )
        if response.status_code == 404:
            self._request(
                "POST",
                f"/repos/{repo}/labels",
                expected=(201,),
                json={"name": ISSUE_LABEL, "color": "1d76db", "description": "Encrypted CourseLens machine job"},
            )

    def _matching_job_issues(self, repo: str, task_id: str) -> list[dict[str, Any]]:
        title = f"{ISSUE_TITLE_PREFIX} {validate_task_id(task_id)}"
        issues = self._request(
            "GET",
            f"/repos/{repo}/issues",
            params={"state": "all", "labels": ISSUE_LABEL, "per_page": 100},
        ).json()
        matches = [
            dict(item) for item in list(issues or [])
            if str(item.get("title") or "") == title
        ]
        if len(matches) > 1:
            raise GitHubRemoteError("multiple encrypted job issues exist for one task")
        return matches

    @staticmethod
    def _issue_body_matches(issue: dict[str, Any], *, task_id: str, parts: int) -> bool:
        try:
            body = json.loads(str(issue.get("body") or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        return body == {"schema": "mailbox.v2", "task_id": task_id, "parts": parts}

    def publish_job_once(
        self, repo: str, task_id: str, envelope: dict[str, Any]
    ) -> dict[str, Any]:
        """Create or resume one exact encrypted mailbox payload.

        A process crash can occur after the Issue or only some comments reach
        GitHub.  This method adopts exactly one open Issue, validates every
        existing part byte-for-byte, fills only missing indices, then performs
        a complete readback.  Conflicts, duplicates, extra parts, or a closed
        Issue fail closed.
        """
        task_id = validate_task_id(task_id)
        self.ensure_job_label(repo)
        chunks = chunk_envelope(envelope)
        matches = self._matching_job_issues(repo, task_id)
        if matches:
            issue = matches[0]
            if str(issue.get("state") or "") != "open":
                raise GitHubRemoteError("encrypted job issue is already closed")
            if not self._issue_body_matches(issue, task_id=task_id, parts=len(chunks)):
                raise GitHubRemoteError("encrypted job issue metadata conflicts with payload")
        else:
            issue = self._request(
                "POST",
                f"/repos/{repo}/issues",
                expected=(201,),
                json={
                    "title": f"{ISSUE_TITLE_PREFIX} {task_id}",
                    "body": json.dumps(
                        {"schema": "mailbox.v2", "task_id": task_id, "parts": len(chunks)}
                    ),
                    "labels": [ISSUE_LABEL],
                },
            ).json()
        number = int(issue["number"])
        existing = self._request(
            "GET", f"/repos/{repo}/issues/{number}/comments", params={"per_page": 100}
        ).json()
        found: dict[int, tuple[int, str]] = {}
        for item in list(existing or []):
            parsed = _JOB_PART_RE.fullmatch(str(item.get("body") or "").strip())
            if parsed is None:
                raise GitHubRemoteError("encrypted job issue contains an unexpected comment")
            index, total, value = int(parsed.group(1)), int(parsed.group(2)), parsed.group(3)
            if total != len(chunks) or index < 1 or index > len(chunks):
                raise GitHubRemoteError("encrypted job issue contains an extra part")
            if index in found:
                raise GitHubRemoteError("encrypted job issue contains a duplicate part")
            if value != chunks[index - 1]:
                raise GitHubRemoteError("encrypted job issue part conflicts with payload")
            found[index] = (int(item.get("id") or 0), value)

        for index, chunk in enumerate(chunks, start=1):
            if index in found:
                continue
            comment = self._request(
                "POST",
                f"/repos/{repo}/issues/{number}/comments",
                expected=(201,),
                json={"body": f"job part {index}/{len(chunks)}\n{chunk}"},
            ).json()
            found[index] = (int(comment.get("id") or 0), chunk)

        deadline = time.monotonic() + self.read_after_write_seconds
        last_error: GitHubRemoteError | None = None
        while True:
            try:
                readback, readback_number = self.read_job(repo, task_id)
                if readback_number == number and readback == envelope:
                    break
                last_error = GitHubRemoteError(
                    "encrypted job readback does not match payload"
                )
            except GitHubRemoteError as exc:
                last_error = exc
            # The issue and its parts were just written; the listing behind a
            # user proxy can lag behind the write for a short while.
            if time.monotonic() >= deadline:
                assert last_error is not None
                raise last_error
            time.sleep(2.0)
        return {
            "issue_number": number,
            "comment_ids": [found[index][0] for index in sorted(found)],
            "part_count": len(chunks),
        }

    def publish_job(self, repo: str, task_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
        return self.publish_job_once(repo, task_id, envelope)

    def read_job(self, repo: str, task_id: str) -> tuple[dict[str, Any], int]:
        task_id = validate_task_id(task_id)
        matches = self._matching_job_issues(repo, task_id)
        if not matches:
            raise GitHubRemoteError("encrypted job is not available yet")
        match = matches[0]
        if str(match.get("state") or "") != "open":
            raise GitHubRemoteError("encrypted job issue is already closed")
        number = int(match["number"])
        comments = self._request(
            "GET", f"/repos/{repo}/issues/{number}/comments", params={"per_page": 100}
        ).json()
        parts: dict[int, str] = {}
        expected_total = 0
        for item in comments:
            parsed = _JOB_PART_RE.fullmatch(str(item.get("body") or "").strip())
            if not parsed:
                continue
            index, total, value = int(parsed.group(1)), int(parsed.group(2)), parsed.group(3)
            if expected_total and total != expected_total:
                raise GitHubRemoteError("encrypted job comments disagree on part count")
            expected_total = total
            parts[index] = value
        if expected_total <= 0 or sorted(parts) != list(range(1, expected_total + 1)):
            raise GitHubRemoteError("encrypted job is incomplete")
        return join_envelope(parts[index] for index in range(1, expected_total + 1)), number

    def read_controls(
        self,
        repo: str,
        issue_number: int,
        *,
        after_sequence: int = 0,
    ) -> list[tuple[int, dict[str, Any]]]:
        """Return complete encrypted runner control messages in sequence order."""
        comments = self._request(
            "GET", f"/repos/{repo}/issues/{int(issue_number)}/comments", params={"per_page": 100}
        ).json()
        messages: dict[int, dict[int, str]] = {}
        totals: dict[int, int] = {}
        for item in comments:
            body = str(item.get("body") or "").strip()
            status = _STATUS_RE.fullmatch(body)
            if status:
                sequence = int(status.group(1))
                if sequence > int(after_sequence):
                    totals[sequence] = 1
                    messages.setdefault(sequence, {})[1] = status.group(2)
                continue
            parsed = _CONTROL_PART_RE.fullmatch(body)
            if not parsed:
                continue
            sequence, index, total = map(int, parsed.group(1, 2, 3))
            if sequence <= int(after_sequence):
                continue
            if sequence in totals and totals[sequence] != total:
                raise GitHubRemoteError("encrypted control comments disagree on part count")
            totals[sequence] = total
            messages.setdefault(sequence, {})[index] = parsed.group(4)
        output: list[tuple[int, dict[str, Any]]] = []
        for sequence in sorted(messages):
            parts = messages[sequence]
            total = totals[sequence]
            if sorted(parts) == list(range(1, total + 1)):
                output.append((sequence, join_envelope(parts[index] for index in range(1, total + 1))))
        return output

    def retire_job_issues(self, repo: str, task_id: str) -> int:
        """Consume and rename stale mailbox issues so a retry can republish.

        A failed attempt leaves its issue behind, and the retry seals a fresh
        envelope whose parts can never match it.  Deleting the comments,
        closing, and renaming keeps the audit trail while removing the exact
        title match that ``publish_job_once`` requires.
        """
        retired = 0
        for issue in self._matching_job_issues(repo, task_id):
            number = int(issue["number"])
            self.cleanup_job(repo, number)
            self._request(
                "PATCH",
                f"/repos/{repo}/issues/{number}",
                json={"title": f"{ISSUE_TITLE_PREFIX} retired {validate_task_id(task_id)}"},
            )
            retired += 1
        return retired

    def cleanup_job(self, repo: str, issue_number: int, *, comment_ids: Iterable[int] | None = None) -> None:
        """Delete every comment on the job issue (all pages), then seal it.

        Comment IDs are collected across every ``comments`` page before any
        DELETE issues, so shifting pages cannot skip a comment; explicit
        ``comment_ids`` bypass pagination.  DELETE tolerates 404 so rerunning
        on an already-cleaned issue stays idempotent.
        """
        ids = list(comment_ids or [])
        if not ids:
            page = 1
            while True:
                comments = self._request(
                    "GET",
                    f"/repos/{repo}/issues/{int(issue_number)}/comments",
                    params={"per_page": 100, "page": page},
                ).json()
                page_ids = [int(item["id"]) for item in comments]
                ids.extend(page_ids)
                if len(page_ids) < 100:
                    break
                page += 1
        for comment_id in ids:
            self._request(
                "DELETE", f"/repos/{repo}/issues/comments/{comment_id}", expected=(204, 404)
            )
        self._request(
            "PATCH",
            f"/repos/{repo}/issues/{int(issue_number)}",
            json={"state": "closed", "body": CONSUMED_MARKER_BODY},
        )

    @staticmethod
    def _is_consumed_body(body: Any) -> bool:
        try:
            return json.loads(str(body or "")) == {"schema": "mailbox.v2", "state": "consumed"}
        except (TypeError, ValueError, json.JSONDecodeError):
            return False

    def mark_job_consumed_preserving_history(self, repo: str, issue_number: int) -> dict[str, Any]:
        """Seal one closed managed Mailbox issue while preserving its history.

        Exact precondition GET → consumed-marker PATCH (state stays closed) →
        readback GET.  The method never deletes a comment or issue, never
        touches the title, labels, assignee, or milestone, and never returns
        or logs body content.  A target that no longer satisfies every
        precondition is rejected instead of best-effort repaired.
        """
        number = int(issue_number)
        if number <= 0:
            raise GitHubRemoteError("mailbox reconcile target number is invalid")
        issue = self._request(
            "GET", f"/repos/{repo}/issues/{number}", expected=(200, 404)
        )
        if issue.status_code == 404:
            raise GitHubRemoteError("mailbox reconcile target is missing")
        payload = issue.json()
        label_names = {
            str((label or {}).get("name") or "")
            for label in list(payload.get("labels") or [])
        }
        if (
            not str(payload.get("title") or "").startswith(ISSUE_TITLE_PREFIX)
            or ISSUE_LABEL not in label_names
        ):
            raise GitHubRemoteError("mailbox reconcile target is not a managed job issue")
        if str(payload.get("state") or "") != "closed":
            raise GitHubRemoteError("mailbox reconcile target is not closed")
        if self._is_consumed_body(payload.get("body")):
            return {"changed": False, "state": "closed", "consumed": True}
        self._request(
            "PATCH",
            f"/repos/{repo}/issues/{number}",
            json={"state": "closed", "body": CONSUMED_MARKER_BODY},
        )
        readback = self._request("GET", f"/repos/{repo}/issues/{number}").json()
        if (
            str(readback.get("state") or "") != "closed"
            or not self._is_consumed_body(readback.get("body"))
        ):
            raise GitHubRemoteError("mailbox reconcile readback does not confirm the consumed marker")
        return {"changed": True, "state": "closed", "consumed": True}

    def job_cleanup_summary(self, repo: str, issue_number: int) -> dict[str, Any]:
        """Return cleanup state without exposing encrypted issue content."""
        issue = self._request(
            "GET", f"/repos/{repo}/issues/{int(issue_number)}"
        ).json()
        comments = self._request(
            "GET",
            f"/repos/{repo}/issues/{int(issue_number)}/comments",
            params={"per_page": 100},
        ).json()
        consumed = False
        try:
            marker = json.loads(str(issue.get("body") or ""))
            consumed = marker == {"schema": "mailbox.v2", "state": "consumed"}
        except (TypeError, ValueError, json.JSONDecodeError):
            consumed = False
        return {
            "state": str(issue.get("state") or ""),
            "consumed": consumed,
            "comment_count": len(list(comments or [])),
        }

    def list_run_artifacts(self, repo: str, run_id: int) -> list[dict[str, Any]]:
        return list(self._request(
            "GET", f"/repos/{repo}/actions/runs/{int(run_id)}/artifacts", params={"per_page": 100}
        ).json().get("artifacts", []))

    def download_artifact_file(self, repo: str, artifact_id: int, filename: str) -> bytes:
        response = self._request(
            "GET", f"/repos/{repo}/actions/artifacts/{int(artifact_id)}/zip", expected=(200,)
        )
        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                info = archive.getinfo(filename)
                if info.file_size > 64 * 1024 * 1024:
                    raise GitHubRemoteError("result artifact is unexpectedly large")
                return archive.read(info)
        except (zipfile.BadZipFile, KeyError) as exc:
            raise GitHubRemoteError("result artifact is invalid") from exc

    def download_artifact_files(self, repo: str, artifact_id: int) -> dict[str, bytes]:
        response = self._request(
            "GET", f"/repos/{repo}/actions/artifacts/{int(artifact_id)}/zip", expected=(200,)
        )
        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                files: dict[str, bytes] = {}
                total = 0
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    total += int(info.file_size)
                    if total > 64 * 1024 * 1024:
                        raise GitHubRemoteError("checkpoint artifact is unexpectedly large")
                    files[info.filename] = archive.read(info)
                return files
        except zipfile.BadZipFile as exc:
            raise GitHubRemoteError("checkpoint artifact is invalid") from exc

    def delete_artifact(self, repo: str, artifact_id: int) -> None:
        self._request(
            "DELETE", f"/repos/{repo}/actions/artifacts/{int(artifact_id)}", expected=(204, 404)
        )

    def _download_bounded_logs(self, path: str) -> bytes:
        response = self._request("GET", path, expected=(200,))
        if len(response.content) > 32 * 1024 * 1024:
            raise GitHubRemoteError("workflow log archive is unexpectedly large")
        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                output = bytearray()
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    if info.file_size > 8 * 1024 * 1024:
                        raise GitHubRemoteError("workflow log file is unexpectedly large")
                    output.extend(archive.read(info))
                    if len(output) > 32 * 1024 * 1024:
                        raise GitHubRemoteError("workflow logs are unexpectedly large")
                return bytes(output)
        except zipfile.BadZipFile as exc:
            raise GitHubRemoteError("workflow log archive is invalid") from exc

    def download_run_logs(self, repo: str, run_id: int) -> bytes:
        """Return a bounded concatenation of public run log files for redaction audit."""
        return self._download_bounded_logs(
            f"/repos/{repo}/actions/runs/{int(run_id)}/logs"
        )

    def download_run_attempt_logs(self, repo: str, run_id: int, attempt: int) -> bytes:
        """Return bounded logs for exactly one workflow attempt."""
        attempt = int(attempt)
        if attempt not in {1, 2}:
            raise GitHubRemoteError("process canary log attempt is out of bounds")
        return self._download_bounded_logs(
            f"/repos/{repo}/actions/runs/{int(run_id)}/attempts/{attempt}/logs"
        )


def _parse_github_time(value: str) -> float:
    try:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
