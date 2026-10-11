"""Validate and replace only the local GitHub orchestration token."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from credentials import CredentialStore
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
        "User-Agent": f"{SOURCE_REPOSITORY}/token-check",
    })
    checks = (
        ("GitHub identity", "https://api.github.com/user"),
        ("public Actions", f"https://api.github.com/repos/{DISTRIBUTION_REPOSITORY}/actions/workflows"),
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
    parser = argparse.ArgumentParser(description="Replace the local GitHub PAT after validation")
    parser.add_argument("--proxy", help="Optional HTTP/HTTPS proxy")
    args = parser.parse_args(argv)
    token = getpass.getpass("New local PAT: ").strip()
    if not token:
        raise RuntimeError("A local PAT is required")
    _validate(token, args.proxy)
    CredentialStore().save_secret("github_remote_token", token)
    print("LOCAL_TOKEN_OK: the validated PAT is stored with Windows DPAPI.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
