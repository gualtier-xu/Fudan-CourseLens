from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import socket
import stat
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from nacl.signing import SigningKey

from src.update.service import MANIFEST_SOURCE_ID, UpdateError, UpdateService


HOST = "github.com"
MANIFEST_URL = (
    "https://github.com/gualtier-xu/Fudan-CourseLens/releases"
    "/latest/download/courselens-windows-manifest.json"
)
PACKAGE_URL = (
    "https://github.com/gualtier-xu/Fudan-CourseLens/releases"
    "/download/client-v1.1.0/courselens-windows-x86_64.zip"
)
SOURCE = {
    "repository": MANIFEST_SOURCE_ID,
    "commit": "a" * 40,
    "tree": "b" * 40,
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def package_bytes(version="1.1.0", release_id="release-1", *, unsafe=None):
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        marker = b"trusted payload"
        names = ["src/marker.txt"]
        if unsafe == "reserved":
            names = ["src/CON.txt"]
        elif unsafe == "ads":
            names = ["src/marker.txt:stream"]
        elif unsafe == "trailing":
            names = ["src/marker.txt. "]
        elif unsafe == "unicode":
            names = ["src/cafe\u0301.txt"]
        elif unsafe == "case_collision":
            names = ["src/Marker.txt", "src/marker.txt"]
        elif unsafe == "unicode_collision":
            names = ["src/\u212b.txt", "src/\u00c5.txt"]
        files = {name: hashlib.sha256(marker).hexdigest() for name in names}
        if unsafe == "declared_missing":
            files["src/missing.txt"] = hashlib.sha256(b"missing").hexdigest()
        metadata = {
            "schema": "courselens.client-package.v1", "version": version,
            "release_id": release_id, "platform": "windows", "architecture": "x86_64",
            "source": SOURCE, "files": files,
        }
        archive.writestr("courselens-package.json", json.dumps(metadata))
        for name in names:
            archive.writestr(name, marker)
        if unsafe == "extra":
            archive.writestr("src/extra.txt", marker)
        if unsafe == "traversal":
            archive.writestr("../escape.txt", "no")
        if unsafe == "symlink":
            info = zipfile.ZipInfo("bad-link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "target")
    return target.getvalue()


def signed_envelope(key, payload, *, key_id="update-2026-01", epoch=1):
    return {
        "schema": "courselens.client-update.v1",
        "manifest": payload,
        "signature": {
            "key_id": key_id, "key_epoch": epoch,
            "value": base64.b64encode(key.sign(canonical(payload)).signature).decode(),
        },
    }


def build(tmp_path, monkeypatch, *, version="1.1.0", release_id="release-1", package=None,
          published_delta=-60, expires_delta=3600, current="1.0.0", minimum="1.0.0",
          authorization_status="active", authorization_expires_days=30, service_kwargs=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    root_signing = SigningKey.generate()
    signing = SigningKey.generate()
    package = package if package is not None else package_bytes(version, release_id)
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    manifest = {
        "release_id": release_id, "version": version, "channel": "stable",
        "platform": "windows", "architecture": "x86_64",
        "published_at": (now + timedelta(seconds=published_delta)).isoformat(),
        "expires_at": (now + timedelta(seconds=expires_delta)).isoformat(),
        "minimum_security_version": minimum,
        "source": SOURCE,
        "package": {
            "url": PACKAGE_URL, "size": len(package),
            "sha256": hashlib.sha256(package).hexdigest(), "format": "zip-v1",
        },
        "release_notes": "Signed synthetic release",
    }
    envelope = signed_envelope(signing, manifest)
    authorization = {
        "release_key_id": "update-2026-01",
        "public_key": base64.b64encode(bytes(signing.verify_key)).decode(),
        "key_epoch": 1,
        "valid_from": (now - timedelta(days=1)).isoformat(),
        "expires_at": (now + timedelta(days=authorization_expires_days)).isoformat(),
        "status": authorization_status, "channels": ["stable"], "platforms": ["windows"],
    }
    trust = {
        "schema": "courselens.client-update-trust.v2", "enabled": True,
        "channel": "stable", "platform": "windows", "architecture": "x86_64",
        "minimum_version": "1.0.0", "minimum_key_epoch": 1,
        "root_keys": {
            "update-root-2026-01": base64.b64encode(bytes(root_signing.verify_key)).decode(),
        },
        "release_key_authorizations": [{
            "schema": "courselens.update-key-authorization.v1",
            "authorization": authorization,
            "signature": {
                "root_key_id": "update-root-2026-01",
                "value": base64.b64encode(
                    root_signing.sign(canonical(authorization)).signature
                ).decode(),
            },
        }],
        "allowed_hosts": [HOST], "manifest_url": MANIFEST_URL,
        "distribution": {
            "repository": "gualtier-xu/Fudan-CourseLens",
            "visibility": "public",
            "auth_model": "none",
            "tag_namespace": "client-v",
            "manifest_asset": "courselens-windows-manifest.json",
            "source_repository_access": False,
        },
        "production_gates": {
            name: True for name in {
                "public_release_repository", "protected_monorepo_ci",
                "offline_update_root", "protected_update_release_key",
                "public_download_safety_implemented", "release_artifact_sha256_published",
                "clean_machine_acceptance", "real_restart_acceptance",
                "power_loss_recovery_acceptance",
            }
        },
    }
    trust_path = tmp_path / "trust.json"
    trust_path.write_text(json.dumps(trust), encoding="utf-8")
    responses = {MANIFEST_URL: json.dumps(envelope).encode(), PACKAGE_URL: package}

    def transport(url, destination, limit):
        content = responses[url]
        if len(content) > limit:
            raise UpdateError("download_too_large")
        if destination is None:
            return content
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return None

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
    ])
    monkeypatch.setattr(
        "src.update.service.host_platform", lambda: ("windows", "x86_64")
    )
    install = tmp_path / "install"
    (install / "versions" / current).mkdir(parents=True)
    state = tmp_path / "update-state"
    service = UpdateService(
        current_version=current, trust_path=trust_path, state_root=state,
        install_root=install, transport=transport, now=lambda: now.timestamp(),
        **(service_kwargs or {}),
    )
    return service, envelope, responses, install, state


def make_managed_install(service):
    """Turn a build() install root into a managed layout for install()."""
    state = service.install_root / "state"
    state.mkdir(exist_ok=True)
    (state / "install-layout.json").write_text(
        '{"schema":"courselens.managed-install.v1"}', encoding="utf-8"
    )
    (state / "current.json").write_text(
        '{"schema":"courselens.current-version.v1","version":"1.0.0"}', encoding="utf-8"
    )
    return state


def test_signed_check_download_apply_health_and_rollback(tmp_path, monkeypatch):
    service, _, _, install, _ = build(tmp_path, monkeypatch)
    assert service.check()["state"] == "available"
    assert service.download()["state"] == "ready_to_restart"
    (install / "state").mkdir()
    (install / "state" / "install-layout.json").write_text(
        '{"schema":"courselens.managed-install.v1"}', encoding="utf-8"
    )
    (install / "state" / "current.json").write_text(
        '{"version":"1.0.0"}', encoding="utf-8"
    )
    assert service.install()["actions"] == ["check", "update_now"]
    assert service.apply_pending()["state"] == "applying"
    assert service.confirm_health("1.1.0")["state"] == "healthy"

    # A later update switches atomically but automatically rolls back if health fails.
    current = json.loads((install / "state" / "current.json").read_text())
    current.update({"awaiting_health": True, "previous_version": "1.0.0"})
    (install / "state" / "current.json").write_text(json.dumps(current))
    rolled_back = service.rollback_if_unhealthy()
    assert rolled_back["state"] == "rolled_back"
    assert rolled_back["current_version"] == "1.0.0"
    assert rolled_back["rollback"] == {
        "available": True, "version": "1.1.0", "state": "rolled_back",
    }
    assert json.loads((install / "state" / "current.json").read_text())["version"] == "1.0.0"


def test_snapshot_exposes_download_progress_pair(tmp_path, monkeypatch):
    """UPDATE-UX-1 零呆等三律：快照闭集新增 download_bytes/download_total；
    未下载恒 0，前端只在 downloading 态消费；既有 (url, destination, limit)
    传输形状零改动照常工作。"""
    service, _, responses, _, _ = build(tmp_path, monkeypatch)
    snapshot = service.snapshot()
    assert snapshot["download_bytes"] == 0
    assert snapshot["download_total"] == 0
    assert service.check()["state"] == "available"
    # 既有三参传输（未声明 progress kwarg）：下载全链照常（零行为漂移负断言）
    assert service.download()["state"] == "ready_to_restart"
    after = service.snapshot()
    assert isinstance(after["download_bytes"], int)
    assert isinstance(after["download_total"], int)


def test_download_progress_events_reach_snapshot(tmp_path, monkeypatch):
    """UPDATE-UX-1 三律：声明了 progress kwarg 的注入传输把字节进度推进快照，
    顶栏/面板据此出百分比与 ETA。"""
    service, _, responses, install, state = build(tmp_path, monkeypatch)
    assert service.check()["state"] == "available"
    package = responses[PACKAGE_URL]
    half = len(package) // 2
    seen: list[int] = []

    def paced_transport(url, destination, limit, progress=None):
        content = responses[url]
        if len(content) > limit:
            raise UpdateError("download_too_large")
        if destination is None:
            return content
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        if progress is not None:
            progress(half)
            seen.append(paced.snapshot()["download_bytes"])
            progress(len(content))
            seen.append(paced.snapshot()["download_bytes"])
        return None

    paced = UpdateService(
        current_version="1.0.0", trust_path=tmp_path / "trust.json",
        state_root=state, install_root=install, transport=paced_transport,
        now=service.now,
    )
    snapshot = paced.download()
    assert seen == [half, len(package)], seen
    assert snapshot["state"] == "ready_to_restart"
    assert snapshot["download_total"] == len(package)
    assert snapshot["download_bytes"] == len(package)


@pytest.mark.parametrize("mutation,code", [
    (lambda e: e["signature"].update(value=base64.b64encode(b"x" * 64).decode()), "manifest_signature_invalid"),
    (lambda e: e["manifest"].update(platform="macos"), "manifest_signature_invalid"),
    (lambda e: e["signature"].update(key_epoch=0), "manifest_key_untrusted"),
])
def test_rejects_signature_platform_mix_and_epoch(tmp_path, monkeypatch, mutation, code):
    service, envelope, responses, _, _ = build(tmp_path, monkeypatch)
    mutation(envelope)
    responses[MANIFEST_URL] = json.dumps(envelope).encode()
    value = service.check()
    assert value["state"] == "policy_blocked"
    assert value["error_code"] == code


def test_rejects_future_and_expired_manifest(tmp_path, monkeypatch):
    future, *_ = build(tmp_path / "future", monkeypatch, published_delta=30)
    assert future.check()["error_code"] == "manifest_expired_or_future"
    expired, *_ = build(tmp_path / "expired", monkeypatch, expires_delta=-1)
    assert expired.check()["error_code"] == "manifest_expired_or_future"


def test_rejects_downgrade_replay_and_security_floor(tmp_path, monkeypatch):
    downgrade, *_ = build(tmp_path / "down", monkeypatch, version="0.9.0")
    assert downgrade.check()["state"] == "up_to_date"
    floor, *_ = build(tmp_path / "floor", monkeypatch, minimum="1.2.0")
    assert floor.check()["error_code"] == "current_version_below_security_floor"

    replay, _, _, _, state = build(tmp_path / "replay", monkeypatch)
    state.mkdir(exist_ok=True)
    (state / "accepted.json").write_text(
        '{"version":"1.2.0","release_id":"newer","key_epoch":1}', encoding="utf-8"
    )
    assert replay.check()["error_code"] == "manifest_replay_or_downgrade"


@pytest.mark.parametrize("unsafe", ["traversal", "symlink"])
def test_rejects_archive_path_and_link_attacks(tmp_path, monkeypatch, unsafe):
    package = package_bytes(unsafe=unsafe)
    service, *_ = build(tmp_path, monkeypatch, package=package)
    assert service.check()["state"] == "available"
    assert service.download()["error_code"] == "package_archive_unsafe"
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.parametrize("unsafe", [
    "reserved", "ads", "trailing", "unicode", "case_collision", "unicode_collision",
])
def test_rejects_windows_namespace_and_name_collisions(tmp_path, monkeypatch, unsafe):
    service, *_ = build(tmp_path, monkeypatch, package=package_bytes(unsafe=unsafe))
    assert service.check()["state"] == "available"
    assert service.download()["error_code"] == "package_archive_unsafe"


@pytest.mark.parametrize("unsafe", ["extra", "declared_missing"])
def test_archive_inventory_must_exactly_match_metadata(tmp_path, monkeypatch, unsafe):
    service, *_ = build(tmp_path, monkeypatch, package=package_bytes(unsafe=unsafe))
    assert service.check()["state"] == "available"
    assert service.download()["error_code"] == "package_inventory_mismatch"


def test_rejects_tampered_truncated_and_unmanaged_install(tmp_path, monkeypatch):
    service, _, responses, _, _ = build(tmp_path, monkeypatch)
    assert service.check()["state"] == "available"
    responses[PACKAGE_URL] = responses[PACKAGE_URL][:-5]
    assert service.download()["error_code"] == "package_size_mismatch"

    service, *_ = build(tmp_path / "unmanaged", monkeypatch)
    service.check()
    service.download()
    assert service.install()["error_code"] == "managed_install_required"


def test_concurrent_lock_and_partial_recovery(tmp_path, monkeypatch):
    service, *_ = build(tmp_path, monkeypatch)
    assert service._mutex.acquire(blocking=False)
    with pytest.raises(UpdateError, match="update_busy"):
        service.check()
    service._mutex.release()
    lock = service._acquire_process_lock()
    try:
        with pytest.raises(UpdateError, match="update_busy"):
            service._acquire_process_lock()
    finally:
        service._release_process_lock(lock)
    partial = service.state_root / "staging" / "package.partial"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"partial")
    service.recover()
    assert not partial.exists()


def test_rejects_signing_epoch_rollback(tmp_path, monkeypatch):
    service, _, _, _, state = build(tmp_path, monkeypatch)
    state.mkdir(exist_ok=True)
    (state / "accepted.json").write_text(
        '{"version":"1.0.0","release_id":"old","key_epoch":2}', encoding="utf-8"
    )
    assert service.check()["error_code"] == "manifest_key_epoch_rollback"


def test_root_authorization_tamper_fails_closed(tmp_path, monkeypatch):
    tampered, *_ = build(tmp_path / "tampered", monkeypatch)
    raw = json.loads(tampered.trust_path.read_text(encoding="utf-8"))
    raw["release_key_authorizations"][0]["authorization"]["key_epoch"] = 2
    tampered.trust_path.write_text(json.dumps(raw), encoding="utf-8")
    assert tampered.check()["error_code"] == "release_key_authorization_invalid"


def test_manifest_epoch_must_match_root_authorization(tmp_path, monkeypatch):
    service, envelope, responses, _, _ = build(tmp_path, monkeypatch)
    envelope["signature"]["key_epoch"] = 2
    responses[MANIFEST_URL] = json.dumps(envelope).encode()
    assert service.check()["error_code"] == "manifest_key_untrusted"


def test_conflicting_active_authorizations_for_key_id_fail_closed(tmp_path, monkeypatch):
    service, *_ = build(tmp_path, monkeypatch)
    raw = json.loads(service.trust_path.read_text(encoding="utf-8"))
    second_root = SigningKey.generate()
    second_release = SigningKey.generate()
    authorization = dict(raw["release_key_authorizations"][0]["authorization"])
    authorization["public_key"] = base64.b64encode(
        bytes(second_release.verify_key)
    ).decode()
    raw["root_keys"]["update-root-test-02"] = base64.b64encode(
        bytes(second_root.verify_key)
    ).decode()
    raw["release_key_authorizations"].append({
        "schema": "courselens.update-key-authorization.v1",
        "authorization": authorization,
        "signature": {
            "root_key_id": "update-root-test-02",
            "value": base64.b64encode(
                second_root.sign(canonical(authorization)).signature
            ).decode(),
        },
    })
    service.trust_path.write_text(json.dumps(raw), encoding="utf-8")
    assert service.check()["error_code"] == "release_key_authorization_conflict"


def test_current_revocation_removes_an_active_release_key(tmp_path, monkeypatch):
    service, *_ = build(tmp_path, monkeypatch)
    raw = json.loads(service.trust_path.read_text(encoding="utf-8"))
    revocation_root = SigningKey.generate()
    authorization = dict(raw["release_key_authorizations"][0]["authorization"])
    authorization["status"] = "revoked"
    raw["root_keys"]["update-root-revocation"] = base64.b64encode(
        bytes(revocation_root.verify_key)
    ).decode()
    raw["release_key_authorizations"].append({
        "schema": "courselens.update-key-authorization.v1",
        "authorization": authorization,
        "signature": {
            "root_key_id": "update-root-revocation",
            "value": base64.b64encode(
                revocation_root.sign(canonical(authorization)).signature
            ).decode(),
        },
    })
    service.trust_path.write_text(json.dumps(raw), encoding="utf-8")
    assert service.check()["error_code"] == "update_not_configured"


@pytest.mark.parametrize("content", ["{", "{}", '{"version":"1.1.0"}'])
def test_corrupt_accepted_history_fails_closed(tmp_path, monkeypatch, content):
    service, _, _, _, state = build(tmp_path, monkeypatch)
    state.mkdir(exist_ok=True)
    (state / "accepted.json").write_text(content, encoding="utf-8")
    assert service.check()["error_code"] == "accepted_history_invalid"


@pytest.mark.parametrize("actual", [("linux", "x86_64"), ("windows", "arm64")])
def test_actual_host_platform_must_match_policy(tmp_path, monkeypatch, actual):
    service, *_ = build(tmp_path, monkeypatch)
    monkeypatch.setattr("src.update.service.host_platform", lambda: actual)
    assert service.check()["error_code"] == "host_platform_mismatch"


def test_stable_channel_rejects_prerelease_manifest(tmp_path, monkeypatch):
    service, *_ = build(tmp_path, monkeypatch, version="1.1.0-rc.1")
    assert service.check()["error_code"] == "manifest_prerelease_blocked"


@pytest.mark.parametrize("kwargs", [
    {"authorization_status": "revoked"},
    {"authorization_expires_days": -1},
])
def test_revoked_or_expired_release_authorization_has_no_trusted_key(
    tmp_path, monkeypatch, kwargs
):
    service, *_ = build(tmp_path, monkeypatch, **kwargs)
    assert service.check()["error_code"] == "update_not_configured"


def test_transport_urls_and_paths_are_not_exposed(tmp_path, monkeypatch):
    service, *_ = build(tmp_path, monkeypatch)
    snapshot = service.check()
    text = json.dumps(snapshot)
    assert HOST not in text
    assert str(tmp_path) not in text


def test_update_now_action_closed_set_keeps_labels_non_executable(tmp_path, monkeypatch):
    service, *_ = build(tmp_path, monkeypatch)
    with pytest.raises(UpdateError, match="confirmation_required"):
        service.action("update_now")
    with pytest.raises(UpdateError, match="update_action_invalid"):
        service.action("restart", confirmed=True)


def test_update_now_orchestrates_and_defers_shutdown_until_reply(tmp_path, monkeypatch):
    shutdowns = []
    service, *_ = build(tmp_path, monkeypatch, service_kwargs={
        "has_active_work": lambda: False,
        "request_shutdown": shutdowns.append,
    })
    state = make_managed_install(service)
    value = service.action("update_now", confirmed=True)
    assert value["state"] == "ready_to_restart"
    pending = json.loads((state / "pending.json").read_text(encoding="utf-8"))
    assert pending["target_version"] == "1.1.0"
    assert pending["previous_version"] == "1.0.0"
    # The shutdown request stays deferred until the HTTP 202 reply is flushed.
    assert shutdowns == []
    service.complete_restart()
    assert shutdowns == ["update_restart"]
    service.complete_restart()
    assert shutdowns == ["update_restart"]  # one-shot per orchestration


def test_update_now_rejects_active_work_without_state_change(tmp_path, monkeypatch):
    shutdowns = []
    service, *_ = build(tmp_path, monkeypatch, service_kwargs={
        "has_active_work": lambda: True,
        "request_shutdown": shutdowns.append,
    })
    with pytest.raises(UpdateError, match="update_restart_blocked"):
        service.action("update_now", confirmed=True)
    assert service.snapshot()["state"] == "idle"
    assert shutdowns == []


def test_update_now_blocked_fails_closed_when_active_work_is_unknown(tmp_path, monkeypatch):
    def broken():
        raise RuntimeError("status read failed")

    service, *_ = build(tmp_path, monkeypatch, service_kwargs={"has_active_work": broken})
    with pytest.raises(UpdateError, match="update_restart_blocked"):
        service.action("update_now", confirmed=True)


def test_update_now_busy_under_held_mutex(tmp_path, monkeypatch):
    service, *_ = build(tmp_path, monkeypatch)
    assert service._mutex.acquire(blocking=False)
    try:
        with pytest.raises(UpdateError, match="update_busy"):
            service.action("update_now", confirmed=True)
    finally:
        service._mutex.release()


def test_update_now_repeat_is_idempotent_and_reuses_the_staged_slot(tmp_path, monkeypatch):
    shutdowns = []
    service, *_ = build(tmp_path, monkeypatch, service_kwargs={
        "request_shutdown": shutdowns.append,
    })
    state = make_managed_install(service)
    first = service.action("update_now", confirmed=True)
    service.complete_restart()
    second = service.action("update_now", confirmed=True)
    service.complete_restart()
    assert first["state"] == second["state"] == "ready_to_restart"
    pending = json.loads((state / "pending.json").read_text(encoding="utf-8"))
    assert pending["target_version"] == "1.1.0"
    assert (service.install_root / "versions" / "1.1.0").is_dir()
    assert shutdowns == ["update_restart", "update_restart"]


def test_update_now_up_to_date_or_failed_never_restarts(tmp_path, monkeypatch):
    shutdowns = []
    current, *_ = build(tmp_path / "current", monkeypatch, current="1.1.0",
                        service_kwargs={"request_shutdown": shutdowns.append})
    assert current.action("update_now", confirmed=True)["state"] == "up_to_date"
    current.complete_restart()

    failed, envelope, responses, *_ = build(tmp_path / "failed", monkeypatch,
                                            service_kwargs={"request_shutdown": shutdowns.append})
    envelope["signature"].update(value=base64.b64encode(b"x" * 64).decode())
    responses[MANIFEST_URL] = json.dumps(envelope).encode()
    assert failed.action("update_now", confirmed=True)["state"] == "policy_blocked"
    failed.complete_restart()
    assert shutdowns == []


def test_background_checks_start_only_for_managed_installs(tmp_path, monkeypatch):
    service, *_ = build(tmp_path, monkeypatch, service_kwargs={
        "background_checks_enabled": lambda: True,
        "background_first_delay": (0.01, 0.01),
        "background_period": 0.2,
    })
    service.start_background_checks()
    assert service._background_thread is None  # source checkout: never background-checks
    make_managed_install(service)
    service.start_background_checks()
    try:
        assert service._background_thread is not None
    finally:
        service.stop_background_checks()
    assert service._background_thread is None


def test_background_timer_respects_preference_and_throttle(tmp_path, monkeypatch):
    hits = {"count": 0}
    enabled = {"value": False}
    service, *_ = build(tmp_path, monkeypatch, service_kwargs={
        "background_checks_enabled": lambda: enabled["value"],
        "background_first_delay": (0.01, 0.01),
        "background_period": 0.15,
    })
    inner = service.transport

    def counting_transport(url, destination, limit):
        hits["count"] += 1
        return inner(url, destination, limit)

    service.transport = counting_transport
    make_managed_install(service)
    service.start_background_checks()
    try:
        time.sleep(0.5)
        assert hits["count"] == 0  # preference off: no checks at all
        enabled["value"] = True
        time.sleep(0.5)
        assert 1 <= hits["count"] <= 3  # checks resumed
        resumed = hits["count"]
        time.sleep(0.5)
        # last_checked_at throttles the fixed-clock loop to a single success.
        assert hits["count"] == resumed
    finally:
        service.stop_background_checks()


def test_background_check_never_gated_by_active_work_and_backs_off_offline(tmp_path, monkeypatch):
    hits = {"count": 0}
    service, *_ = build(tmp_path, monkeypatch, service_kwargs={
        "has_active_work": lambda: True,
        "background_checks_enabled": lambda: True,
        "background_first_delay": (0.01, 0.01),
        "background_period": 1.0,
    })

    def failing_transport(url, destination, limit):
        hits["count"] += 1
        raise UpdateError("network_unavailable")

    service.transport = failing_transport
    make_managed_install(service)
    service.start_background_checks()
    try:
        time.sleep(1.5)
    finally:
        service.stop_background_checks()
    # Backoff re-checks sooner than one full period instead of waiting 24h,
    # while active work keeps gating only the orchestration action.
    assert hits["count"] >= 3
    assert hits["count"] <= 8


def test_update_now_rechecks_active_work_before_install(tmp_path, monkeypatch):
    shutdowns = []
    gate = {"busy": False, "downloaded": False}
    service, _, _, install, _ = build(tmp_path, monkeypatch, service_kwargs={
        "has_active_work": lambda: gate["busy"],
        "request_shutdown": shutdowns.append,
    })
    state = make_managed_install(service)
    inner = service.transport

    def transport(url, destination, limit):
        if url == PACKAGE_URL:
            gate["busy"] = True  # active work starts inside the download window
            gate["downloaded"] = True
        return inner(url, destination, limit)

    service.transport = transport
    with pytest.raises(UpdateError, match="update_restart_blocked"):
        service.action("update_now", confirmed=True)
    # The front gate passed and the download ran; the in-lock re-check then
    # rejected before install.  No restart is owed, nothing is pending for
    # the launcher, and the rejection itself changed no state.
    assert gate["downloaded"] is True
    assert service._restart_owed is False
    assert not (state / "pending.json").exists()
    service.complete_restart()
    assert shutdowns == []


def test_stop_background_checks_is_idempotent_and_joins_the_timer(tmp_path, monkeypatch):
    service, *_ = build(tmp_path, monkeypatch, service_kwargs={
        "background_checks_enabled": lambda: True,
        "background_first_delay": (60.0, 60.0),
        "background_period": 3600.0,
    })
    service.stop_background_checks()  # never started: harmless no-op
    make_managed_install(service)
    service.start_background_checks()
    assert service._background_thread is not None
    service.stop_background_checks()  # shutdown drain: stops and joins the timer
    assert service._background_thread is None
    assert service._background_stop is None
    service.stop_background_checks()  # already stopped: harmless re-entry
    assert service._background_thread is None


def test_app_shutdown_drain_stops_background_checks_once():
    import src.app as app_module

    stops = {"count": 0}

    class FakeUpdateService:
        def stop_background_checks(self):
            stops["count"] += 1

    app_module._active_client_update = FakeUpdateService()
    app_module._stop_client_update_background_checks()
    assert stops["count"] == 1
    assert app_module._active_client_update is None
    app_module._stop_client_update_background_checks()  # already drained: harmless
    assert stops["count"] == 1


def test_ready_to_restart_advertises_only_closed_set_actions(tmp_path, monkeypatch):
    service, *_ = build(tmp_path, monkeypatch)
    make_managed_install(service)
    assert service.check()["state"] == "available"
    assert service.download()["state"] == "ready_to_restart"
    value = service.install()
    assert value["state"] == "ready_to_restart"
    assert value["actions"] == ["check", "update_now"]
    # Every advertised action must be inside the server's own action closed set.
    for name in value["actions"]:
        try:
            service.action(name, confirmed=True)
        except UpdateError as exc:
            assert exc.code != "update_action_invalid", name


def test_snapshot_never_advertises_null_available_version(tmp_path, monkeypatch):
    service, *_ = build(tmp_path, monkeypatch)
    assert service.check()["state"] == "available"
    raw = json.loads(service._state_path.read_text(encoding="utf-8"))
    raw["available_version"] = None
    service._state_path.write_text(json.dumps(raw), encoding="utf-8")
    reloaded = UpdateService(
        current_version="1.0.0", trust_path=service.trust_path,
        state_root=service.state_root, install_root=service.install_root,
        transport=service.transport, now=service.now,
    )
    snapshot = reloaded.snapshot()
    assert snapshot["state"] == "available"
    assert snapshot["available_version"] == ""


def test_bom_state_files_keep_managed_update_available(tmp_path, monkeypatch):
    """SRC-SYNDROME-1 U4（N6CP EX1 处方验收②）：PS5.1 写侧的 BOM 态 state 件
    不得把 check 打成 managed_install_required。"""
    service, _, _, install, _ = build(tmp_path, monkeypatch)
    (install / "state").mkdir(parents=True, exist_ok=True)
    (install / "state" / "install-layout.json").write_text(
        '{"schema":"courselens.managed-install.v1"}', encoding="utf-8-sig"
    )
    (install / "state" / "current.json").write_text('{"version":"1.0.0"}', encoding="utf-8-sig")
    value = service.check()
    assert value["state"] == "available", value


def test_managed_install_root_accepts_bom_layout(tmp_path, monkeypatch):
    """SRC-SYNDROME-1 U4（N6CP EX1 处方验收③）：组合根判定 BOM 态布局返回非 None。"""
    from src import application as app_module

    install = tmp_path / "managed"
    (install / "versions" / "cur").mkdir(parents=True)
    (install / "state").mkdir()
    (install / "state" / "install-layout.json").write_text(
        '{"schema":"courselens.managed-install.v1"}', encoding="utf-8-sig"
    )
    monkeypatch.setenv("COURSELENS_INSTALL_ROOT", str(install))
    monkeypatch.setattr(app_module, "PROJECT_ROOT", install / "versions" / "cur")
    assert app_module._client_reset_managed_install_root() == install.resolve()
