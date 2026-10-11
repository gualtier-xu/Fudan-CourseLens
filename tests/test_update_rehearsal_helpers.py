"""Pins for the local update-chain rehearsal helpers.

Keeps ``scripts/rehearse_client_update_local.py`` from rotting: its URL
builders must stay byte-identical to the service's pinned canonical forms,
its re-sign/corrupt helpers must produce exactly what their names claim, and
the one-shot local channel must serve real HTTP on loopback only.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import urllib.request
from pathlib import Path

import pytest
from nacl.signing import SigningKey

from src.update import service as update_service
from src.update.service import (
    UPDATE_CHANNEL_ENV,
    _stable_manifest_url,
    _validate_package_url,
)

REHEARSAL_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rehearse_client_update_local.py"


@pytest.fixture(scope="module")
def rehearsal():
    spec = importlib.util.spec_from_file_location("rehearse_client_update_local", REHEARSAL_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_url_builder_matches_service_default(monkeypatch, rehearsal):
    monkeypatch.delenv(UPDATE_CHANNEL_ENV, raising=False)
    assert (
        rehearsal.manifest_url_for("gualtier-xu/Fudan-CourseLens")
        == _stable_manifest_url()
    )


def test_package_url_builder_passes_service_validation(monkeypatch, rehearsal):
    monkeypatch.delenv(UPDATE_CHANNEL_ENV, raising=False)
    url = rehearsal.package_url_for("gualtier-xu/Fudan-CourseLens", "0.1.0")
    _validate_package_url(url)


def test_resign_manifest_roundtrips_through_verifykey(rehearsal):
    release = SigningKey.generate()
    manifest = {
        "release_id": "r1", "version": "0.1.0", "channel": "stable",
        "platform": "windows", "architecture": "x86_64",
        "package": {"url": "https://github.com/gualtier-xu/Fudan-CourseLens/releases/download/client-v0.1.0/pkg.zip",
                    "size": 10, "sha256": "c" * 64, "format": "zip-v1"},
    }
    envelope = {
        "schema": "courselens.client-update.v1",
        "manifest": manifest,
        "signature": {
            "key_id": "k", "key_epoch": 1,
            "value": base64.b64encode(release.sign(update_service._canonical(manifest)).signature).decode(),
        },
    }
    other = SigningKey.generate()
    resigned = json.loads(rehearsal.resign_manifest(envelope, "0.2.0", other).decode("utf-8"))
    assert resigned["manifest"]["version"] == "0.2.0"
    assert resigned["signature"]["key_id"] == "k"
    verify = SigningKey.generate()  # wrong key must not verify
    with pytest.raises(Exception):
        verify.verify_key.verify(
            update_service._canonical(resigned["manifest"]),
            base64.b64decode(resigned["signature"]["value"]),
        )
    other.verify_key.verify(
        update_service._canonical(resigned["manifest"]),
        base64.b64decode(resigned["signature"]["value"]),
    )


def test_corrupt_signature_flips_exactly_one_byte(rehearsal):
    release = SigningKey.generate()
    manifest = {"release_id": "r1"}
    envelope = {
        "schema": "s", "manifest": manifest,
        "signature": {"key_id": "k", "key_epoch": 1,
                      "value": base64.b64encode(release.sign(b"{}").signature).decode()},
    }
    original = json.loads(json.dumps(envelope))
    corrupted = json.loads(rehearsal.corrupt_signature(json.dumps(envelope).encode("utf-8")).decode("utf-8"))
    assert corrupted["signature"]["key_id"] == original["signature"]["key_id"]
    assert corrupted["signature"]["value"] != original["signature"]["value"]


def test_local_channel_serves_bytes_and_statuses(tmp_path, rehearsal):
    payload = tmp_path / "asset.bin"
    payload.write_bytes(b"rehearsal-bytes")
    channel = rehearsal.LocalChannel(rehearsal.RehearsalLog())
    try:
        channel.routes = {
            "/gualtier-xu/Fudan-CourseLens/releases/latest/download/x.json": payload,
            "/gone": 404,
        }
        channel.serve_forever_background()
        with urllib.request.urlopen(f"{channel.base}/gualtier-xu/Fudan-CourseLens/releases/latest/download/x.json", timeout=5) as response:
            assert response.read() == b"rehearsal-bytes"
        raised = False
        try:
            urllib.request.urlopen(f"{channel.base}/gone", timeout=5)
        except Exception:
            raised = True
        assert raised
        try:
            urllib.request.urlopen(f"{channel.base}/never-routed", timeout=5)
        except Exception:
            raised = True
        assert raised
    finally:
        channel.shutdown()
