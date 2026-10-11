"""Validate and replace only the runner's read-only private-Issue token."""

from __future__ import annotations

import argparse
import getpass
import subprocess
import sys
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.distribution import DISTRIBUTION_REPOSITORY, SOURCE_REPOSITORY


def _validate(token: str, proxy: str | None) -> None:
    session = requests.Session()
    session.trust_env = False
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"{SOURCE_REPOSITORY}/runner-token-check",
    })
    checks = (
        ("GitHub identity", "https://api.github.com/user"),
        ("private Issues", f"https://api.github.com/repos/{SOURCE_REPOSITORY}/issues?state=open&per_page=1"),
    )
    for label, url in checks:
        try:
            response = session.get(url, timeout=20)
        except requests.RequestException as exc:
            raise RuntimeError(f"{label} connection failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise RuntimeError(f"{label} validation returned HTTP {response.status_code}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replace the runner mailbox PAT after validation")
    parser.add_argument("--proxy", help="Optional HTTP/HTTPS proxy")
    args = parser.parse_args(argv)
    token = getpass.getpass("New runner PAT: ").strip()
    if not token:
        raise RuntimeError("A runner PAT is required")
    _validate(token, args.proxy)
    completed = subprocess.run(
        [
            "gh", "secret", "set", "PRIVATE_JOB_REPO_TOKEN",
            "--repo", DISTRIBUTION_REPOSITORY,
            "--env", "courselens-worker",
        ],
        input=token,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "gh secret set failed").strip()
        raise RuntimeError(message.splitlines()[-1][:300])
    print("RUNNER_TOKEN_OK: the validated PAT replaced the environment secret.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
