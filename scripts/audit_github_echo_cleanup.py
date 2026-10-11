"""Audit one imported encrypted echo and emit privacy-safe gate evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from credentials import CredentialStore
from path_utils import DEFAULT_DATA_DIR
from scripts.final_acceptance import (
    GATE_EVIDENCE_SCHEMA,
    build_context,
    gate_binding,
)
from src.remote.coordinator import RemoteSettings
from src.remote.github_app import GitHubAppClient
from src.remote.github_client import GitHubClient
from src.remote.protocol import PROTOCOL_VERSION, validate_task_id
from src.runtime.task_store import TaskStore


GATE = "encrypted_echo_cleanup"
JOB_TOKEN_SECRET = "COURSELENS_JOB_TOKEN"


def build_cleanup_evidence(
    task_id: str,
    *,
    credentials: Any,
    task_store: Any,
    github_app: Any,
    github: Any,
    context: dict[str, Any],
) -> dict[str, Any]:
    task_id = validate_task_id(task_id)
    remote = dict(task_store.get_remote_run(task_id) or {})
    attempt_number = max(1, int(remote.get("attempt") or 1))
    attempt = dict(task_store.get_remote_attempt(task_id, attempt_number) or {})
    run_id = int(remote.get("run_id") or 0)
    issue_number = int(remote.get("issue_number") or 0)
    if not run_id or not issue_number:
        raise RuntimeError("echo cleanup audit is missing remote run metadata")

    artifacts = list(github.list_run_artifacts(str(remote.get("repository") or ""), run_id))
    mailbox = dict(
        github.job_cleanup_summary(
            str(credentials.load_secret("github_mailbox_repo")), issue_number
        )
    )
    leases = list(task_store.list_remote_token_leases())
    worker_secrets = {
        str(item.get("name") or "") for item in github_app.list_worker_secrets()
    }
    temporary_local_secrets = sum(
        credentials.has_secret(name)
        for name in (
            f"remote_result_private:{task_id}",
            "github_remote_token",
            "github_job_token_cleanup_pending",
        )
    )
    checks = {
        "remote_imported": str(remote.get("remote_state") or "") == "imported",
        "attempt_imported": str(attempt.get("import_state") or "") == "imported",
        "attempt_cleanup_complete": str(attempt.get("cleanup_state") or "") == "complete",
        "artifacts_removed": len(artifacts) == 0,
        "mailbox_closed": str(mailbox.get("state") or "") == "closed",
        "mailbox_consumed": mailbox.get("consumed") is True,
        "mailbox_comments_removed": int(mailbox.get("comment_count") or 0) == 0,
        "token_leases_removed": len(leases) == 0,
        "temporary_local_secrets_removed": temporary_local_secrets == 0,
        "environment_job_token_removed": JOB_TOKEN_SECRET not in worker_secrets,
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise RuntimeError("echo cleanup audit failed: " + ",".join(failed))

    return {
        "schema": GATE_EVIDENCE_SCHEMA,
        "gate": GATE,
        "status": "passed",
        "binding": gate_binding(GATE, context),
        "task_sha256": hashlib.sha256(task_id.encode("ascii")).hexdigest(),
        "run_id": run_id,
        "protocol_version": PROTOCOL_VERSION,
        "observations": {
            "artifact_count": len(artifacts),
            "mailbox_state": str(mailbox.get("state") or ""),
            "mailbox_consumed": bool(mailbox.get("consumed")),
            "mailbox_comment_count": int(mailbox.get("comment_count") or 0),
            "token_lease_count": len(leases),
            "temporary_local_secret_count": temporary_local_secrets,
            "environment_job_token_count": int(JOB_TOKEN_SECRET in worker_secrets),
            "remote_state": str(remote.get("remote_state") or ""),
            "attempt_import_state": str(attempt.get("import_state") or ""),
            "attempt_cleanup_state": str(attempt.get("cleanup_state") or ""),
        },
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--proxy", default="")
    args = parser.parse_args(argv)

    credentials = CredentialStore()
    settings = RemoteSettings.load(credentials)
    proxy = str(args.proxy or settings.proxy_url or "")
    evidence = build_cleanup_evidence(
        args.task_id,
        credentials=credentials,
        task_store=TaskStore(DEFAULT_DATA_DIR / "state.db"),
        github_app=GitHubAppClient(credentials, proxy_url=proxy),
        github=GitHubClient(settings.github_token, proxy_url=proxy),
        context=build_context(ROOT),
    )
    _atomic_json(args.output.resolve(), evidence)
    print(json.dumps({
        "status": evidence["status"],
        "gate": evidence["gate"],
        "run_id": evidence["run_id"],
        "evidence_sha256": hashlib.sha256(args.output.resolve().read_bytes()).hexdigest(),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
