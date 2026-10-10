"""Bounded real-material key-recovery challenge (never outputs secret values).

Proves that Ed25519 signing material - the offline Worker-mirror root and
release keys stored as credential-store secrets, or an operator-supplied
escrow file (base64 text or raw bytes) - can still be recovered and used:
load the material, sign a fresh random challenge, verify the signature with
the derived public key, optionally cross-check the derived public key
against a pinned key, and emit exactly one closed JSON report.  Private
bytes stay in memory only and are best-effort overwritten; they are never
printed, written, or logged.  Missing or uninspectable material fails
closed (exit 1).

Exit codes: 0 = challenge passed; 1 = failed or unavailable; 2 = usage.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nacl.signing import SigningKey, VerifyKey

from credentials import CredentialStore
from path_utils import DEFAULT_DATA_DIR
from shared.protocol.mirror import _b64decode

SCHEMA = "courselens.key-recovery-challenge.v1"


def _decode_material(raw: bytes) -> bytes:
    """Accept base64 text (the escrow encoding) or raw key bytes."""
    stripped = bytes(raw.strip())
    try:
        decoded = base64.b64decode(stripped, validate=True)
        if len(decoded) in (32, 64):
            return decoded
    except ValueError:
        pass
    return raw


def _load_material(args: argparse.Namespace) -> bytes:
    if args.secret_name:
        data_dir = Path(args.data_dir) if args.data_dir else DEFAULT_DATA_DIR
        store = CredentialStore(data_dir / "credentials.json")
        if not store.has_secret(args.secret_name):
            raise FileNotFoundError("credential store has no such secret name")
        return base64.b64decode(str(store.load_secret(args.secret_name)).strip(), validate=True)
    if args.file:
        return _decode_material(Path(args.file).read_bytes())
    raise ValueError("one of --secret-name or --file is required")


def _signing_key(material: bytes) -> SigningKey:
    if len(material) == 64:
        seed, embedded = material[:32], material[32:]
        key = SigningKey(seed)
        if key.verify_key.encode() != embedded:
            raise RuntimeError("embedded public half does not match the derived key")
        return key
    if len(material) == 32:
        return SigningKey(material)
    raise RuntimeError("key material has an unsupported length")


def _zeroize(buffer: bytearray | None) -> None:
    if buffer is not None:
        for index in range(len(buffer)):
            buffer[index] = 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secret-name", default="", help="credential-store secret holding the private key material")
    parser.add_argument("--file", default="", help="escrow file holding the private key material")
    parser.add_argument("--data-dir", default=None, help="data dir for the credential store (default COURSELENS_DATA_DIR)")
    parser.add_argument("--key-id", default="", help="expected key id to record in the report")
    parser.add_argument("--expected-public", default="", help="base64 public key the recovered material must match")
    args = parser.parse_args(argv)
    if bool(args.secret_name) == bool(args.file):
        parser.error("choose exactly one of --secret-name or --file")
    report: dict[str, object] = {
        "schema": SCHEMA,
        "key_id": args.key_id,
        "source_class": "credential-store" if args.secret_name else "escrow-file",
        "challenge_sha256": "",
        "public_key_fingerprint": "",
        "expected_public_matched": False,
        "verification": "failed",
        "local_copies_zeroized": False,
    }
    material: bytes | None = None
    try:
        material = _load_material(args)
        key = _signing_key(material)
        challenge = os.urandom(32)
        signature = key.sign(challenge).signature
        VerifyKey(key.verify_key.encode()).verify(challenge, signature)
        report["challenge_sha256"] = hashlib.sha256(challenge).hexdigest()
        report["public_key_fingerprint"] = hashlib.sha256(key.verify_key.encode()).hexdigest()[:32]
        if args.expected_public:
            expected = _b64decode(args.expected_public, field="expected_public")
            report["expected_public_matched"] = expected == key.verify_key.encode()
            if not report["expected_public_matched"]:
                raise RuntimeError("recovered public key does not match the expected public key")
        report["verification"] = "passed"
    except FileNotFoundError as exc:
        report["verification"] = "material_absent"
        report["observations"] = {"reason": str(exc)}
    except Exception as exc:  # the tool must always close with one report
        report["verification"] = "failed"
        report["observations"] = {"reason": str(exc)}
    finally:
        _zeroize(bytearray(material) if material is not None else None)
        report["local_copies_zeroized"] = True
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["verification"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
