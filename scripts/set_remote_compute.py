"""Explicitly enable or disable the configured remote-compute path."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from credentials import CredentialStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Switch GitHub Actions remote compute")
    parser.add_argument("state", choices=("enable", "disable"))
    args = parser.parse_args(argv)
    credentials = CredentialStore()
    if args.state == "enable":
        missing = [
            name for name in (
                "github_remote_token",
                "worker_box_public_key",
                "worker_signing_public_key",
            ) if not credentials.has_secret(name)
        ]
        if missing:
            raise RuntimeError("Remote compute is not configured: " + ", ".join(missing))
    credentials.save_secret("remote_enabled", "1" if args.state == "enable" else "0")
    print(f"Remote compute {args.state}d. Restart the local CourseLens service.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
