"""Honest-state pins for the update channel's failure branches.

Each branch is the exact behavior the local-channel rehearsal
(``scripts/rehearse_client_update_local.py``) asserts against a real
127.0.0.1 server; here the same closed-set outcomes are pinned through the
injected transport so they run network-free on every test pass.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import socket
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from nacl.signing import SigningKey

from src.update import service as update_service
from src.update.service import (
    MANIFEST_SOURCE_ID,
    UPDATE_CHANNEL_ENV,
    UpdateError,
    UpdateService,
    host_platform,
)


HOST = "github.com"
MANIFEST_URL = (
    "https://github.com/gualtier-xu/Fudan-CourseLens/releases"
    "/latest/download/courselens-windows-manifest.json"
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _global_dns(*args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def package_bytes(version="1.1.0", release_id="release-1"):
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        marker = b"trusted payload"
        files = {"src/marker.txt": hashlib.sha256(marker).hexdigest()}
        metadata = {
            "schema": "courselens.client-package.v1", "version": version,
            "release_id": release_id, "platform": "windows", "architecture": "x86_64",
            "source": {"repository": MANIFEST_SOURCE_ID, "commit": "a" * 40, "tree": "b" * 40},
            "files": files,
        }
        archive.writestr("courselens-package.json", json.dumps(metadata))
        archive.writestr("src/marker.txt", marker)
    return target.getvalue()


class _Rehearsal:
    """One signed release served from a mutable route table."""

    def __init__(self, tmp_path: Path, current: str = "1.0.0"):
        self.root = SigningKey.generate()
        self.signing = SigningKey.generate()
        self.current = current
        self.package = package_bytes()
        self.manifest_envelope: dict | None = None
        self.package_status = 200
        self.manifest_error: str | None = None
        self.routes: dict[str, bytes] = {}
        self.service = UpdateService(
            current_version=current,
            trust_path=self._write_trust(tmp_path),
            state_root=tmp_path / "data" / "updates",
            install_root=tmp_path / "install",
            transport=self._transport,
        )

    def publish(self, *, version="1.1.0", release_id="release-1", signer=None,
                package: bytes | None = None, declared_size: int | None = None,
                declared_hash: str | None = None):
        payload = self.package if package is None else package
        now = datetime.now(timezone.utc)
        platform_name, architecture = host_platform()
        manifest = {
            "release_id": release_id, "version": version, "channel": "stable",
            "platform": platform_name, "architecture": architecture,
            "published_at": (now - timedelta(seconds=60)).isoformat(),
            "expires_at": (now + timedelta(hours=2)).isoformat(),
            "minimum_security_version": "0.0.9",
            "source": {"repository": MANIFEST_SOURCE_ID, "commit": "a" * 40, "tree": "b" * 40},
            "package": {
                "url": f"https://github.com/gualtier-xu/Fudan-CourseLens/releases/download/client-v{version}/pkg.zip",
                "size": len(payload) if declared_size is None else declared_size,
                "sha256": hashlib.sha256(payload).hexdigest() if declared_hash is None else declared_hash,
                "format": "zip-v1",
            },
            "release_notes": "rehearsal branch",
        }
        envelope = {
            "schema": "courselens.client-update.v1",
            "manifest": manifest,
            "signature": {
                "key_id": "update-rehearsal-01", "key_epoch": 1,
                "value": base64.b64encode(
                    (signer or self.signing).sign(canonical(manifest)).signature
                ).decode(),
            },
        }
        self.manifest_envelope = envelope
        self.package_status = 200
        self.manifest_error = None
        self.package = payload
        return envelope

    def _write_trust(self, tmp_path: Path):
        platform_name, architecture = host_platform()
        now = datetime.now(timezone.utc)
        authorization = {
            "release_key_id": "update-rehearsal-01",
            "public_key": base64.b64encode(bytes(self.signing.verify_key)).decode(),
            "key_epoch": 1,
            "valid_from": (now - timedelta(days=1)).isoformat(),
            "expires_at": (now + timedelta(days=30)).isoformat(),
            "status": "active",
            "channels": ["stable"],
            "platforms": [platform_name],
        }
        trust = {
            "schema": "courselens.client-update-trust.v2",
            "enabled": True,
            "channel": "stable",
            "platform": platform_name,
            "architecture": architecture,
            "minimum_version": "0.0.9",
            "minimum_key_epoch": 1,
            "root_keys": {
                "update-rehearsal-root": base64.b64encode(bytes(self.root.verify_key)).decode()
            },
            "release_key_authorizations": [{
                "schema": "courselens.update-key-authorization.v1",
                "authorization": authorization,
                "signature": {
                    "root_key_id": "update-rehearsal-root",
                    "value": base64.b64encode(
                        self.root.sign(canonical(authorization)).signature
                    ).decode(),
                },
            }],
            "allowed_hosts": [HOST],
            "manifest_url": MANIFEST_URL,
            "distribution": {
                "repository": "gualtier-xu/Fudan-CourseLens",
                "visibility": "public",
                "auth_model": "none",
                "tag_namespace": "client-v",
                "manifest_asset": "courselens-windows-manifest.json",
                "source_repository_access": False,
            },
            "production_gates": {name: True for name in update_service.REQUIRED_PRODUCTION_GATES},
        }
        trust_path = tmp_path / "client-update-trust.json"
        trust_path.write_text(json.dumps(trust), encoding="utf-8")
        return trust_path

    def _transport(self, url, destination, limit):
        if destination is None:
            if self.manifest_error is not None:
                raise UpdateError(self.manifest_error)
            return json.dumps(self.manifest_envelope).encode("utf-8")
        if self.package_status != 200:
            raise UpdateError("download_http_error")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.package)
        return None


@pytest.fixture(autouse=True)
def _channel_env(monkeypatch):
    monkeypatch.delenv(UPDATE_CHANNEL_ENV, raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", _global_dns)


def test_manifest_404_maps_to_policy_blocked(tmp_path):
    harness = _Rehearsal(tmp_path)
    harness.manifest_error = "download_http_error"
    snapshot = harness.service.check()
    assert snapshot["state"] == "policy_blocked"
    assert snapshot["error_code"] == "download_http_error"
    assert snapshot["actions"] == ["check"]


def test_manifest_offline_maps_to_offline(tmp_path):
    harness = _Rehearsal(tmp_path)
    harness.manifest_error = "network_unavailable"
    snapshot = harness.service.check()
    assert snapshot["state"] == "offline"
    assert snapshot["error_code"] == "network_unavailable"


def test_wrong_signature_key_rejected(tmp_path):
    harness = _Rehearsal(tmp_path)
    harness.publish(signer=SigningKey.generate())
    snapshot = harness.service.check()
    assert snapshot["state"] == "policy_blocked"
    assert snapshot["error_code"] == "manifest_signature_invalid"


def test_same_version_is_up_to_date(tmp_path):
    harness = _Rehearsal(tmp_path, current="1.1.0")
    harness.publish(version="1.1.0")
    snapshot = harness.service.check()
    assert snapshot["state"] == "up_to_date"
    assert snapshot["available_version"] == "1.1.0"
    assert snapshot["actions"] == ["check"]


def test_newer_version_offers_download(tmp_path):
    harness = _Rehearsal(tmp_path)
    harness.publish(version="1.1.0")
    snapshot = harness.service.check()
    assert snapshot["state"] == "available"
    assert snapshot["actions"] == ["check", "download"]
    assert snapshot["package_size"] == len(harness.package)


def test_package_hash_mismatch_fails_and_cleans_staging(tmp_path):
    harness = _Rehearsal(tmp_path)
    pristine = harness.package
    harness.publish(
        version="1.1.0", package=b"tampered bytes",
        declared_size=len(b"tampered bytes"),
        declared_hash=hashlib.sha256(pristine).hexdigest(),
    )
    assert harness.service.check()["state"] == "available"
    snapshot = harness.service.download()
    assert snapshot["state"] == "failed"
    assert snapshot["error_code"] == "package_hash_mismatch"
    assert not (harness.service.state_root / "staging" / "package.zip").exists()


def test_package_size_mismatch_fails_closed(tmp_path):
    harness = _Rehearsal(tmp_path)
    harness.publish(version="1.1.0", declared_size=len(harness.package) + 1)
    assert harness.service.check()["state"] == "available"
    snapshot = harness.service.download()
    assert snapshot["state"] == "failed"
    assert snapshot["error_code"] == "package_size_mismatch"


def test_package_404_fails_with_channel_code(tmp_path):
    harness = _Rehearsal(tmp_path)
    harness.publish(version="1.1.0")
    assert harness.service.check()["state"] == "available"
    harness.package_status = 404
    snapshot = harness.service.download()
    assert snapshot["state"] == "failed"
    assert snapshot["error_code"] == "download_http_error"



def test_insufficient_disk_space_blocks_download(tmp_path, monkeypatch):
    import collections
    import shutil as _shutil

    harness = _Rehearsal(tmp_path)
    harness.publish(version="1.1.0")
    assert harness.service.check()["state"] == "available"
    real_usage = _shutil.disk_usage
    usage_type = collections.namedtuple("usage", "total used free")

    def full_disk(path):
        usage = real_usage(path)
        return usage_type(usage.total, usage.used, 0)  # free < expected*3 + 64 MiB

    monkeypatch.setattr(update_service.shutil, "disk_usage", full_disk)
    snapshot = harness.service.download()
    assert snapshot["state"] == "failed"
    assert snapshot["error_code"] == "insufficient_disk_space"
    assert not (harness.service.state_root / "staging" / "package.zip").exists()


def test_release_notes_truncated_to_snapshot_cap(tmp_path):
    harness = _Rehearsal(tmp_path)
    harness.publish(version="1.1.0")
    manifest = dict(harness.manifest_envelope["manifest"])
    manifest["release_notes"] = "x" * 9000
    harness.manifest_envelope = json.loads(
        resign_for_test(harness, manifest).decode("utf-8"))
    snapshot = harness.service.check()
    assert len(snapshot["release_notes"]) == 8000


def test_future_published_at_rejected(tmp_path):
    harness = _Rehearsal(tmp_path)
    from datetime import datetime, timedelta, timezone as tz

    harness.publish(version="1.1.0")
    manifest = harness.manifest_envelope["manifest"]
    manifest["published_at"] = (datetime.now(tz.utc) + timedelta(hours=1)).isoformat()
    # re-sign so only the time boundary is under test
    harness.manifest_envelope = json.loads(
        resign_for_test(harness, manifest).decode("utf-8"))
    snapshot = harness.service.check()
    assert snapshot["state"] == "policy_blocked"
    assert snapshot["error_code"] == "manifest_expired_or_future"


def test_extra_manifest_key_breaks_schema_even_when_signed(tmp_path):
    harness = _Rehearsal(tmp_path)
    harness.publish(version="1.1.0")
    manifest = dict(harness.manifest_envelope["manifest"])
    manifest["extra"] = "attacker field"
    harness.manifest_envelope = json.loads(
        resign_for_test(harness, manifest).decode("utf-8"))
    snapshot = harness.service.check()
    assert snapshot["state"] == "policy_blocked"
    assert snapshot["error_code"] == "manifest_schema_invalid"


def test_stable_channel_blocks_prerelease(tmp_path):
    harness = _Rehearsal(tmp_path)
    harness.publish(version="1.1.0-beta.1")
    snapshot = harness.service.check()
    assert snapshot["state"] == "policy_blocked"
    assert snapshot["error_code"] == "manifest_prerelease_blocked"


def test_key_epoch_rollback_rejected(tmp_path):
    harness = _Rehearsal(tmp_path)
    harness.publish(version="1.1.0")
    accepted = harness.service.state_root / "accepted.json"
    accepted.parent.mkdir(parents=True, exist_ok=True)
    accepted.write_text(json.dumps(
        {"version": "1.0.9", "release_id": "release-0", "key_epoch": 5}
    ), encoding="utf-8")
    snapshot = harness.service.check()
    assert snapshot["state"] == "policy_blocked"
    assert snapshot["error_code"] == "manifest_key_epoch_rollback"


def resign_for_test(harness: _Rehearsal, manifest: dict) -> bytes:
    """Re-sign a mutated manifest with the harness release key."""
    from nacl.signing import SigningKey  # noqa: F401  (import symmetry)

    signed = {
        "schema": "courselens.client-update.v1",
        "manifest": manifest,
        "signature": {
            "key_id": harness.manifest_envelope["signature"]["key_id"],
            "key_epoch": harness.manifest_envelope["signature"]["key_epoch"],
            "value": base64.b64encode(
                harness.signing.sign(canonical(manifest)).signature
            ).decode(),
        },
    }
    return json.dumps(signed).encode("utf-8")
