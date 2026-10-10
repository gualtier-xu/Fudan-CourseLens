"""Read-only audit proving the retired signed-template-direct mode left no residue."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from credentials import CredentialStore
from path_utils import DEFAULT_DATA_DIR
from src.remote.github_app import GitHubAppClient
from src.remote.worker_migration import build_signed_template_retirement_audit
from src.runtime.task_store import TaskStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args(argv)
    data_dir = args.data_dir.resolve()
    credentials = CredentialStore(data_dir / "credentials.json")
    try:
        state_db = data_dir / "state.db"
        task_store = TaskStore(state_db, read_only=True) if state_db.is_file() else None
        github_app = GitHubAppClient(credentials)
        report = build_signed_template_retirement_audit(
            credentials=credentials, task_store=task_store, github_app=github_app
        )
    except (RuntimeError, FileNotFoundError, AttributeError) as exc:
        # The preflight must always emit exactly one closed report; a missing,
        # schema-mismatched, or otherwise uninspectable task store is a NO-GO
        # state, never a crash.  Raised messages are closed product codes.
        report = {
            "schema": "courselens.signed-template-retirement-audit.v1",
            "status": "task_store_unavailable",
            "ready": False,
            "legacy_direct_state_detected": False,
            "worker_repository": "",
            "mailbox_repository": "",
            "checks": {},
            "observations": {"preflight_error": str(exc)},
        }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
