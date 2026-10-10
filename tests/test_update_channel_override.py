"""Update-channel parameterization pins.

``COURSELENS_UPDATE_CHANNEL`` overrides the distribution repository without
touching any trust logic: the default value stays verbatim, an override must
be pinned by the installed trust policy to have any effect, and a policy that
pins a different repository fails closed as ``distribution_policy_invalid``.
"""

from __future__ import annotations

import base64
import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from nacl.signing import SigningKey

from src.update import service as update_service
from src.update.service import (
    DISTRIBUTION_REPOSITORY,
    MANIFEST_SOURCE_ID,
    UPDATE_CHANNEL_ENV,
    UpdateError,
    UpdateService,
    _distribution_repository,
    _stable_manifest_url,
    _validate_package_url,
    host_platform,
)


DEFAULT_REPOSITORY = "gualtier-xu/Fudan-CourseLens"
DEFAULT_MANIFEST_URL = (
    "https://github.com/gualtier-xu/Fudan-CourseLens/releases"
    "/latest/download/courselens-windows-manifest.json"
)
OVERRIDE_REPOSITORY = "gualtier-xu/Fudan-CourseLens-Worker-Rehearsal"
GLOBAL_DNS = [
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
]


def _global_dns(*args, **kwargs):
    return GLOBAL_DNS


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _identity() -> dict:
    return {"repository": MANIFEST_SOURCE_ID, "commit": "a" * 40, "tree": "b" * 40}


def _package_url(repository: str, version: str = "0.1.0") -> str:
    return (
        f"https://github.com/{repository}/releases/download/"
        f"client-v{version}/courselens-{version}-windows-x86_64.zip"
    )


def _trust(tmp_path: Path, repository: str, signing: SigningKey) -> Path:
    """A fully enabled trust policy pinning exactly ``repository``."""
    platform_name, architecture = host_platform()
    now = datetime.now(timezone.utc)
    authorization = {
        "release_key_id": "update-rehearsal-01",
        "public_key": base64.b64encode(bytes(signing.verify_key)).decode(),
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
        "root_keys": {},
        "release_key_authorizations": [],
        "allowed_hosts": ["github.com"],
        "manifest_url": _stable_manifest_url_for(repository),
        "distribution": {
            "repository": repository,
            "visibility": "public",
            "auth_model": "none",
            "tag_namespace": "client-v",
            "manifest_asset": "courselens-windows-manifest.json",
            "source_repository_access": False,
        },
        "production_gates": {name: True for name in update_service.REQUIRED_PRODUCTION_GATES},
    }
    # The tests below exercise the manifest-verification path only; the
    # authorization envelope is a local self-signed formality kept valid so
    # TrustPolicy.load accepts the file.
    root = SigningKey.generate()
    trust["root_keys"] = {
        "update-rehearsal-root": base64.b64encode(bytes(root.verify_key)).decode()
    }
    trust["release_key_authorizations"] = [{
        "schema": "courselens.update-key-authorization.v1",
        "authorization": authorization,
        "signature": {
            "root_key_id": "update-rehearsal-root",
            "value": base64.b64encode(root.sign(canonical(authorization)).signature).decode(),
        },
    }]
    trust_path = tmp_path / "client-update-trust.json"
    trust_path.write_text(json.dumps(trust), encoding="utf-8")
    return trust_path


def _stable_manifest_url_for(repository: str) -> str:
    return (
        f"https://github.com/{repository}/releases"
        f"/latest/download/courselens-windows-manifest.json"
    )


def _envelope(signing: SigningKey, repository: str, version: str) -> dict:
    platform_name, architecture = host_platform()
    now = datetime.now(timezone.utc)
    manifest = {
        "release_id": "rehearsal-release-1",
        "version": version,
        "channel": "stable",
        "platform": platform_name,
        "architecture": architecture,
        "published_at": (now - timedelta(seconds=60)).isoformat(),
        "expires_at": (now + timedelta(hours=2)).isoformat(),
        "minimum_security_version": "0.0.9",
        "source": _identity(),
        "package": {
            "url": _package_url(repository, version),
            "size": 16,
            "sha256": "c" * 64,
            "format": "zip-v1",
        },
        "release_notes": "rehearsal",
    }
    return {
        "schema": "courselens.client-update.v1",
        "manifest": manifest,
        "signature": {
            "key_id": "update-rehearsal-01",
            "key_epoch": 1,
            "value": base64.b64encode(signing.sign(canonical(manifest)).signature).decode(),
        },
    }


def _service(tmp_path: Path, repository: str, version: str = "0.0.9"):
    """A service whose policy, envelope and signing key are mutually consistent."""
    signing = SigningKey.generate()
    envelope = _envelope(signing, repository, version)
    responses = {_stable_manifest_url_for(repository): json.dumps(envelope).encode("utf-8")}

    def transport(url, destination, limit):
        payload = responses[url]
        if destination is None:
            return payload
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return None

    service = UpdateService(
        current_version="0.0.9",
        trust_path=_trust(tmp_path, repository, signing),
        state_root=tmp_path / "data" / "updates",
        install_root=tmp_path / "install",
        transport=transport,
    )
    return service, envelope


def test_default_repository_and_manifest_url_stay_verbatim(monkeypatch):
    monkeypatch.delenv(UPDATE_CHANNEL_ENV, raising=False)
    assert DISTRIBUTION_REPOSITORY == DEFAULT_REPOSITORY
    assert _distribution_repository() == DEFAULT_REPOSITORY
    assert _stable_manifest_url() == DEFAULT_MANIFEST_URL


def test_blank_override_falls_back_to_default(monkeypatch):
    for value in ("", "   "):
        monkeypatch.setenv(UPDATE_CHANNEL_ENV, value)
        assert _distribution_repository() == DEFAULT_REPOSITORY
    assert _stable_manifest_url() == DEFAULT_MANIFEST_URL


def test_override_changes_effective_urls_but_not_the_constant(monkeypatch):
    monkeypatch.setenv(UPDATE_CHANNEL_ENV, OVERRIDE_REPOSITORY)
    assert _distribution_repository() == OVERRIDE_REPOSITORY
    assert DISTRIBUTION_REPOSITORY == DEFAULT_REPOSITORY
    assert _stable_manifest_url() == _stable_manifest_url_for(OVERRIDE_REPOSITORY)
    _validate_package_url(_package_url(OVERRIDE_REPOSITORY))
    with pytest.raises(UpdateError) as exc_info:
        _validate_package_url(_package_url(DEFAULT_REPOSITORY))
    assert exc_info.value.code == "source_url_blocked"


def test_default_package_url_shapes_unchanged(monkeypatch):
    monkeypatch.delenv(UPDATE_CHANNEL_ENV, raising=False)
    _validate_package_url(_package_url(DEFAULT_REPOSITORY, "0.1.0"))
    for blocked in (
        _package_url("other-org/other-repo"),
        "https://github.com/gualtier-xu/Fudan-CourseLens/releases/download/v0.1.0/pkg.zip",
        "https://github.com/gualtier-xu/Fudan-CourseLens/releases/download/client-v0.1.0/a/b.zip",
        "http://github.com/gualtier-xu/Fudan-CourseLens/releases/download/client-v0.1.0/pkg.zip",
    ):
        with pytest.raises(UpdateError) as exc_info:
            _validate_package_url(blocked)
        assert exc_info.value.code == "source_url_blocked"


def test_check_with_overridden_pinned_policy_succeeds(monkeypatch, tmp_path):
    monkeypatch.setenv(UPDATE_CHANNEL_ENV, OVERRIDE_REPOSITORY)
    monkeypatch.setattr(socket, "getaddrinfo", _global_dns)
    service, _envelope_unused = _service(tmp_path, OVERRIDE_REPOSITORY)
    snapshot = service.check()
    assert snapshot["state"] == "up_to_date"
    assert snapshot["available_version"] == "0.0.9"


def test_override_without_matching_policy_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv(UPDATE_CHANNEL_ENV, OVERRIDE_REPOSITORY)
    monkeypatch.setattr(socket, "getaddrinfo", _global_dns)
    service, _envelope_unused = _service(tmp_path, DEFAULT_REPOSITORY)
    snapshot = service.check()
    assert snapshot["state"] == "policy_blocked"
    assert snapshot["error_code"] == "distribution_policy_invalid"


def test_policy_pin_without_override_env_fails_closed(monkeypatch, tmp_path):
    monkeypatch.delenv(UPDATE_CHANNEL_ENV, raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", _global_dns)
    service, _envelope_unused = _service(tmp_path, OVERRIDE_REPOSITORY)
    snapshot = service.check()
    assert snapshot["state"] == "policy_blocked"
    assert snapshot["error_code"] == "distribution_policy_invalid"
