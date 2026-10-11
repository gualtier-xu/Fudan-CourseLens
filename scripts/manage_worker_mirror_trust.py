"""Create and maintain root-signed Worker mirror trust metadata.

Private keys are stored only through the existing Windows DPAPI CredentialStore.
The command never prints a private key.
"""

from __future__ import annotations

import argparse
import base64
import subprocess
import sys
import time
from pathlib import Path

from nacl.signing import SigningKey

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from credentials import CredentialStore
from shared.protocol.mirror import TRUST_SCHEMA, pretty_json, sign_document


RELEASE_ROOT = ROOT / "release" / "worker-mirror"
ROOT_SCHEMA = "courselens.worker-mirror.root.v1"
ROOT_SECRET_PREFIX = "worker_mirror_root_signing_private:"
RELEASE_SECRET_PREFIX = "worker_mirror_release_signing_private:"


def _keypair() -> tuple[str, str]:
    key = SigningKey.generate()
    return (
        base64.b64encode(bytes(key)).decode("ascii"),
        base64.b64encode(bytes(key.verify_key)).decode("ascii"),
    )


def initialize(*, root_key_id: str, release_key_id: str, expires_days: int) -> None:
    existing = [] if not RELEASE_ROOT.exists() else [
        path for path in RELEASE_ROOT.iterdir() if path.name != "README.md"
    ]
    if existing:
        raise RuntimeError("Worker mirror trust directory already exists and is not empty")
    RELEASE_ROOT.mkdir(parents=True, exist_ok=True)
    credentials = CredentialStore()
    root_private, root_public = _keypair()
    release_private, release_public = _keypair()
    credentials.save_secret(ROOT_SECRET_PREFIX + root_key_id, root_private)
    credentials.save_secret(RELEASE_SECRET_PREFIX + release_key_id, release_private)
    root = {
        "schema": ROOT_SCHEMA,
        "root_keys": [{"key_id": root_key_id, "public_key": root_public}],
    }
    trust = {
        "schema": TRUST_SCHEMA,
        "epoch": 1,
        "expires_at": int(time.time()) + int(expires_days) * 86400,
        "release_keys": [{
            "key_id": release_key_id,
            "public_key": release_public,
            "status": "active",
        }],
        "revoked_manifests": [],
    }
    signature = sign_document(trust, key_id=root_key_id, private_key=root_private)
    (RELEASE_ROOT / "worker-mirror-root.json").write_bytes(pretty_json(root))
    (RELEASE_ROOT / "worker-mirror-trust.json").write_bytes(pretty_json(trust))
    (RELEASE_ROOT / "worker-mirror-trust.sig").write_bytes(pretty_json(signature))


def upload_release_key(*, key_id: str, repository: str, environment: str) -> None:
    credentials = CredentialStore()
    private_key = credentials.load_secret(RELEASE_SECRET_PREFIX + key_id)
    completed = subprocess.run(
        [
            "gh", "secret", "set", "WORKER_MIRROR_SIGNING_PRIVATE_KEY",
            "--repo", repository, "--env", environment,
        ],
        input=private_key,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise RuntimeError("GitHub rejected the protected release signing secret")
    subprocess.run(
        [
            "gh", "variable", "set", "WORKER_MIRROR_SIGNING_KEY_ID",
            "--repo", repository, "--env", environment, "--body", key_id,
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--root-key-id", required=True)
    init.add_argument("--release-key-id", required=True)
    init.add_argument("--expires-days", type=int, default=365)
    upload = commands.add_parser("upload-release-key")
    upload.add_argument("--key-id", required=True)
    upload.add_argument("--repository", required=True)
    upload.add_argument("--environment", default="worker-mirror-release")
    args = parser.parse_args()
    if args.command == "init":
        initialize(
            root_key_id=args.root_key_id,
            release_key_id=args.release_key_id,
            expires_days=args.expires_days,
        )
        print("Worker mirror public trust metadata created; private keys remain DPAPI-protected.")
    else:
        upload_release_key(
            key_id=args.key_id, repository=args.repository, environment=args.environment
        )
        print("Release signing key uploaded to the protected Environment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
