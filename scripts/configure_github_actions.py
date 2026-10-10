"""Interactively configure the two-repository encrypted Actions channel."""

from __future__ import annotations

import argparse
import getpass
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from credentials import CredentialStore
from src.distribution import DISTRIBUTION_REPOSITORY, SOURCE_REPOSITORY
from src.remote.protocol import generate_box_keypair, generate_signing_keypair


def _run(command: list[str], *, stdin: str | None = None) -> None:
    completed = subprocess.run(
        command,
        input=stdin,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "command failed").strip().splitlines()[-1]
        raise RuntimeError(message[:300])


def _set_secret(repo: str, environment: str, name: str, value: str) -> None:
    _run(
        ["gh", "secret", "set", name, "--repo", repo, "--env", environment],
        stdin=value,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Configure encrypted GitHub Actions remote compute")
    parser.add_argument("--public-repo", default=DISTRIBUTION_REPOSITORY)
    parser.add_argument("--private-repo", default=SOURCE_REPOSITORY)
    parser.add_argument("--environment", default="courselens-worker")
    parser.add_argument("--rotate", action="store_true", help="Replace worker keys already configured locally")
    args = parser.parse_args(argv)
    if not shutil.which("gh"):
        raise RuntimeError("GitHub CLI is required")
    _run(["gh", "auth", "status"])
    credentials = CredentialStore()
    if credentials.has_secret("worker_box_public_key") and not args.rotate:
        raise RuntimeError("Worker keys are already configured; use --rotate for an intentional rotation")
    print("Enter the local fine-grained PAT (public Actions read/write; private Issues read/write).")
    local_token = getpass.getpass("Local PAT: ").strip()
    print("Enter the separate runner PAT (private repository Issues read-only).")
    runner_token = getpass.getpass("Runner PAT: ").strip()
    if not local_token or not runner_token:
        raise RuntimeError("Both fine-grained PAT values are required")
    worker_private, worker_public = generate_box_keypair()
    signing_private, signing_public = generate_signing_keypair()
    _run([
        "gh", "api", "--method", "PUT",
        f"repos/{args.public_repo}/environments/{args.environment}",
    ])
    _set_secret(args.public_repo, args.environment, "PRIVATE_JOB_REPO_TOKEN", runner_token)
    _set_secret(args.public_repo, args.environment, "WORKER_INPUT_PRIVATE_KEY", worker_private)
    _set_secret(args.public_repo, args.environment, "WORKER_SIGNING_PRIVATE_KEY", signing_private)
    credentials.save_secret("github_remote_token", local_token)
    credentials.save_secret("worker_box_public_key", worker_public)
    credentials.save_secret("worker_signing_public_key", signing_public)
    credentials.save_secret("remote_enabled", "0")
    print("Remote compute is configured but remains disabled until its validation gates pass.")
    print("Run scripts/set_remote_compute.py enable after the echo and five-minute checks.")
    print("Worker private keys were sent directly to GitHub and were not written to disk.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
