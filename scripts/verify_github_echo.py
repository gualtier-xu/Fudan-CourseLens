"""Run one real encrypted echo through the public GitHub Actions worker."""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from credentials import CredentialStore
from path_utils import DEFAULT_DATA_DIR
from src.remote.coordinator import RemoteCoordinator, RemoteSettings
from src.remote.github_app import GitHubAppClient
from src.remote.protocol import JOB_SCHEMA, PROTOCOL_VERSION
from src.runtime.task_store import TaskStore


def build_echo_job(task_id: str, result_public_key: str) -> dict[str, Any]:
    now = time.time()
    return {
        "schema": JOB_SCHEMA,
        "protocol_version": PROTOCOL_VERSION,
        "task_id": task_id,
        "job_kind": "echo",
        "created_at": now,
        "expires_at": now + 600,
        "result_public_key": result_public_key,
        "pipeline": {"version": "actions-echo-v1"},
        "payload": {"nonce": uuid.uuid4().hex},
        "secrets": {},
    }


def render_progress(stage: str, percent: float | None, label: str) -> str:
    amount = "progress=unknown" if percent is None else f"progress={percent:.0f}%"
    return f"[{stage}] {amount} {label}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the encrypted GitHub Actions channel")
    parser.add_argument("--proxy", help="Optional HTTP/HTTPS proxy, for example http://127.0.0.1:6268")
    args = parser.parse_args(argv)
    if args.proxy:
        os.environ["HTTP_PROXY"] = args.proxy
        os.environ["HTTPS_PROXY"] = args.proxy

    credentials = CredentialStore()
    github_app = GitHubAppClient(credentials, proxy_url=str(args.proxy or ""))
    task_store = TaskStore(DEFAULT_DATA_DIR / "state.db")
    task_id = uuid.uuid4().hex
    imported: dict[str, Any] = {}

    def import_result(result: dict[str, Any]) -> None:
        echo = dict(dict(result.get("outputs") or {}).get("echo") or {})
        if echo.get("ok") is not True:
            raise RuntimeError("GitHub echo result is incomplete")
        imported.update(result)

    def progress(stage: str, percent: float | None, label: str) -> None:
        print(render_progress(stage, percent, label), flush=True)

    print(f"Starting encrypted echo task {task_id}", flush=True)
    with github_app.job_token_lease():
        settings = replace(
            RemoteSettings.load(credentials),
            enabled=True,
            workflow="echo.yml",
            ref="main",
        )
        coordinator = RemoteCoordinator(settings, task_store, credentials)
        coordinator.execute(
            task_id=task_id,
            build_job=lambda result_public_key: build_echo_job(task_id, result_public_key),
            import_result=import_result,
            cancel_requested=lambda: False,
            progress=progress,
        )
    remote_run = task_store.get_remote_run(task_id) or {}
    print(
        "ECHO_OK "
        f"task_id={task_id} run_id={int(remote_run.get('run_id') or 0)} "
        f"protocol={imported.get('protocol_version')}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
