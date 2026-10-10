from __future__ import annotations

import base64
import json
import shutil
import socket
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from nacl.signing import SigningKey

from scripts.build_client_update import canonical, main as build_update
from src.app import _client_install_root, _client_update_trust_path
from src.update.service import REQUIRED_PRODUCTION_GATES, UpdateError, UpdateService


HOST = "github.com"
MANIFEST_URL = (
    "https://github.com/gualtier-xu/Fudan-CourseLens/releases"
    "/latest/download/courselens-windows-manifest.json"
)
PACKAGE_URL = (
    "https://github.com/gualtier-xu/Fudan-CourseLens/releases"
    "/download/client-v1.1.0/courselens-windows-x86_64.zip"
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _release_source(root: Path) -> None:
    (root / "config").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "courselens-version.json").write_text(json.dumps({
        "schema": "courselens.client-version.v1",
        "version": "1.1.0",
        "channel": "stable",
    }), encoding="utf-8")
    (root / "config" / "client-update-trust.json").write_text(json.dumps({
        "schema": "courselens.client-update-trust.v2",
        "minimum_version": "1.0.0",
    }), encoding="utf-8")
    (root / "src" / "marker.py").write_text("CANDIDATE = True\n", encoding="utf-8")
    _git("init", "--quiet", cwd=root)
    _git("config", "user.email", "tests@example.invalid", cwd=root)
    _git("config", "user.name", "CourseLens Tests", cwd=root)
    _git(
        "remote", "add", "origin",
        "https://github.com/gualtier-xu-co/Fudan-CourseLens-Private.git", cwd=root,
    )
    _git("add", "--all", cwd=root)
    _git("commit", "--quiet", "-m", "synthetic release source", cwd=root)


def _trust(path: Path, release_key: SigningKey) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    root_key = SigningKey.generate()
    now = datetime.now(timezone.utc)
    authorization = {
        "release_key_id": "update-test-01",
        "public_key": base64.b64encode(bytes(release_key.verify_key)).decode("ascii"),
        "key_epoch": 1,
        "valid_from": (now - timedelta(days=1)).isoformat(),
        "expires_at": (now + timedelta(days=1)).isoformat(),
        "status": "active",
        "channels": ["stable"],
        "platforms": ["windows"],
    }
    value = {
        "schema": "courselens.client-update-trust.v2",
        "enabled": True,
        "channel": "stable",
        "platform": "windows",
        "architecture": "x86_64",
        "minimum_version": "1.0.0",
        "minimum_key_epoch": 1,
        "root_keys": {
            "update-root-test": base64.b64encode(bytes(root_key.verify_key)).decode("ascii"),
        },
        "release_key_authorizations": [{
            "schema": "courselens.update-key-authorization.v1",
            "authorization": authorization,
            "signature": {
                "root_key_id": "update-root-test",
                "value": base64.b64encode(
                    root_key.sign(canonical(authorization)).signature
                ).decode("ascii"),
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
        "production_gates": {name: True for name in REQUIRED_PRODUCTION_GATES},
    }
    path.write_text(json.dumps(value), encoding="utf-8")


def test_built_update_upgrades_old_client_in_outer_managed_layout(tmp_path, monkeypatch):
    source = tmp_path / "release-source"
    source.mkdir()
    _release_source(source)
    assert _client_update_trust_path(source, source) == (
        source / "config" / "client-update-trust.json"
    )
    release_key = SigningKey.generate()
    key_file = tmp_path / "release-key"
    key_file.write_text(
        base64.b64encode(bytes(release_key)).decode("ascii"), encoding="ascii"
    )
    notes = tmp_path / "notes.md"
    notes.write_text("Synthetic managed update.", encoding="utf-8")
    output = tmp_path / "output"
    assert build_update([
        "--root", str(source),
        "--output", str(output),
        "--package-url", PACKAGE_URL,
        "--release-id", "managed-integration-test",
        "--key-id", "update-test-01",
        "--key-epoch", "1",
        "--signing-key-file", str(key_file),
        "--notes-file", str(notes),
    ]) == 0

    envelope = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert envelope["manifest"]["version"] == "1.1.0"
    assert envelope["manifest"]["minimum_security_version"] == "1.0.0"
    package = next(output.glob("*.zip"))
    with zipfile.ZipFile(package) as archive:
        assert "config/client-update-trust.json" not in archive.namelist()
    responses = {
        MANIFEST_URL: (output / "manifest.json").read_bytes(),
        PACKAGE_URL: package.read_bytes(),
    }

    def transport(url, destination, limit):
        content = responses[url]
        assert len(content) <= limit
        if destination is None:
            return content
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return None

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
    ])
    # 被测语义=Windows 托管更新链全流程；宿主平台钉 windows/x64 使其与
    # 跑套件的 OS 无关（Windows 宿主上=恒等零变化）。
    monkeypatch.setattr(
        "src.update.service.host_platform", lambda: ("windows", "x86_64")
    )
    install_root = tmp_path / "managed"
    old_slot = install_root / "versions" / "1.0.0"
    old_slot.mkdir(parents=True)
    state = install_root / "state"
    state.mkdir()
    (state / "install-layout.json").write_text(
        '{"schema":"courselens.managed-install.v1"}', encoding="utf-8"
    )
    (state / "current.json").write_text(json.dumps({
        "schema": "courselens.current-version.v1",
        "version": "1.0.0",
        "awaiting_health": False,
    }), encoding="utf-8")
    trust_path = install_root / "trust" / "client-update-trust.json"
    _trust(trust_path, release_key)
    external_trust = trust_path.read_bytes()

    monkeypatch.delenv("COURSELENS_INSTALL_ROOT", raising=False)
    assert _client_install_root(old_slot) == old_slot.resolve()
    monkeypatch.setenv("COURSELENS_INSTALL_ROOT", str(install_root))
    assert _client_install_root(old_slot) == install_root.resolve()
    assert _client_update_trust_path(old_slot, install_root) == trust_path.resolve()
    outside_slot = tmp_path / "outside-slot"
    outside_slot.mkdir()
    with pytest.raises(RuntimeError, match="managed_update_trust_invalid"):
        _client_update_trust_path(outside_slot, install_root)
    service = UpdateService(
        current_version="1.0.0",
        trust_path=_client_update_trust_path(old_slot, install_root),
        state_root=install_root / "data" / "updates",
        install_root=_client_install_root(old_slot),
        transport=transport,
    )
    assert service.check()["state"] == "available"
    assert service.download()["state"] == "ready_to_restart"
    assert trust_path.read_bytes() == external_trust
    assert (install_root / "versions" / "1.1.0").is_dir()
    assert not (old_slot / "versions").exists()
    assert service.install()["actions"] == ["check", "update_now"]

    launcher = install_root / "launcher"
    launcher.mkdir()
    helper = launcher / "client_update_helper.py"
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "scripts" / "client_update_helper.py",
        helper,
    )
    isolated_cwd = tmp_path / "isolated-cwd"
    isolated_cwd.mkdir()
    result = subprocess.run(
        [sys.executable, "-I", str(helper), "apply", "--install-root", str(install_root)],
        cwd=isolated_cwd, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    current = json.loads((state / "current.json").read_text(encoding="utf-8"))
    assert current["version"] == "1.1.0"
    assert current["previous_version"] == "1.0.0"
    assert current["awaiting_health"] is True


def test_applied_switch_survives_next_process_recover_window(tmp_path, monkeypatch):
    """UPDATE-UX-1（2026-10-09 真实用户模拟实锤）：launcher helper apply 落盘
    switched_at —— 否则新槽位进程 UpdateService.recover() 读不到 switched_at
    （=0），把刚完成的换装判成 >120s 健康超窗而立即自动回滚，launcher confirm
    随后必然 health_confirmation_invalid（修前=每次受管更新装上即滚回）。
    全链钉：check → download → helper apply（真子进程）→ 新服务实例（recover
    在构造时运行）→ confirm_health 全部成立。"""
    source = tmp_path / "release-source"
    source.mkdir()
    _release_source(source)
    release_key = SigningKey.generate()
    key_file = tmp_path / "release-key"
    key_file.write_text(
        base64.b64encode(bytes(release_key)).decode("ascii"), encoding="ascii"
    )
    notes = tmp_path / "notes.md"
    notes.write_text("Synthetic managed update.", encoding="utf-8")
    output = tmp_path / "output"
    assert build_update([
        "--root", str(source),
        "--output", str(output),
        "--package-url", PACKAGE_URL,
        "--release-id", "recover-window-test",
        "--key-id", "update-test-01",
        "--key-epoch", "1",
        "--signing-key-file", str(key_file),
        "--notes-file", str(notes),
    ]) == 0
    responses = {
        MANIFEST_URL: (output / "manifest.json").read_bytes(),
        PACKAGE_URL: next(output.glob("*.zip")).read_bytes(),
    }

    def transport(url, destination, limit):
        content = responses[url]
        assert len(content) <= limit
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
    install_root = tmp_path / "managed"
    old_slot = install_root / "versions" / "1.0.0"
    old_slot.mkdir(parents=True)
    state = install_root / "state"
    state.mkdir()
    (state / "install-layout.json").write_text(
        '{"schema":"courselens.managed-install.v1"}', encoding="utf-8"
    )
    (state / "current.json").write_text(json.dumps({
        "schema": "courselens.current-version.v1",
        "version": "1.0.0",
        "awaiting_health": False,
    }), encoding="utf-8")
    trust_path = install_root / "trust" / "client-update-trust.json"
    _trust(trust_path, release_key)
    service = UpdateService(
        current_version="1.0.0",
        trust_path=trust_path,
        state_root=install_root / "data" / "updates",
        install_root=install_root,
        transport=transport,
    )
    assert service.check()["state"] == "available"
    assert service.download()["state"] == "ready_to_restart"
    assert service.install()["actions"] == ["check", "update_now"]

    # The trusted launcher applies the switch outside the dying process.
    launcher = install_root / "launcher"
    launcher.mkdir()
    helper = launcher / "client_update_helper.py"
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "scripts" / "client_update_helper.py",
        helper,
    )
    isolated_cwd = tmp_path / "isolated-cwd"
    isolated_cwd.mkdir()
    result = subprocess.run(
        [sys.executable, "-I", str(helper), "apply", "--install-root", str(install_root)],
        cwd=isolated_cwd, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    applied = json.loads((state / "current.json").read_text(encoding="utf-8"))
    assert applied["version"] == "1.1.0" and applied["awaiting_health"] is True
    # 修前此断言即红：apply 未写 switched_at，switched_at 缺失=超窗判定。
    assert isinstance(applied.get("switched_at"), (int, float)), applied

    # A fresh process (new service instance → recover() runs at construction)
    # must keep the fresh switch intact while awaiting the launcher confirm.
    successor = UpdateService(
        current_version="1.1.0",
        trust_path=trust_path,
        state_root=install_root / "data" / "updates",
        install_root=install_root,
        transport=transport,
    )
    survived = json.loads((state / "current.json").read_text(encoding="utf-8"))
    assert survived["version"] == "1.1.0", survived
    assert survived["awaiting_health"] is True, survived
    assert successor.confirm_health("1.1.0")["state"] == "healthy"

    # W-2（真实用户模拟）：launcher 已 confirm 的完成换装，下一进程启动即落
    # healthy 终态——不再让学生看着「等待重启」旧脸与旧版本号。
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "scripts" / "client_update_helper.py",
        helper,
    )
    result = subprocess.run(
        [sys.executable, "-I", str(helper), "confirm", "--install-root",
         str(install_root), "--version", "1.1.0"],
        cwd=isolated_cwd, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    # simulate the stale ready_to_restart face a dying process leaves behind
    (install_root / "data" / "updates" / "state.json").write_text(json.dumps({
        "schema": "courselens.client-update-state.v1", "state": "ready_to_restart",
        "error_code": "", "current_version": "1.0.0", "channel": "stable",
        "available_version": "1.1.0", "package_size": 10, "release_notes": "",
        "last_checked_at": 0, "observed_at": 0,
        "download_bytes": 0, "download_total": 0,
        "rollback": {"available": False, "version": "", "state": "none"},
        "actions": ["check", "update_now"],
    }), encoding="utf-8")
    settled = UpdateService(
        current_version="1.1.0",
        trust_path=trust_path,
        state_root=install_root / "data" / "updates",
        install_root=install_root,
        transport=transport,
    )
    final = settled.snapshot()
    assert final["state"] == "healthy", final
    assert final["current_version"] == "1.1.0", final
    assert final["actions"] == ["check"], final


def test_managed_layout_without_external_trust_fails_closed(tmp_path, monkeypatch):
    install_root = tmp_path / "managed"
    active_root = install_root / "versions" / "1.0.0"
    active_root.mkdir(parents=True)
    state = install_root / "state"
    state.mkdir()
    (state / "install-layout.json").write_text(
        '{"schema":"courselens.managed-install.v1"}', encoding="utf-8"
    )
    monkeypatch.setenv("COURSELENS_INSTALL_ROOT", str(install_root))
    with pytest.raises(FileNotFoundError):
        _client_update_trust_path(active_root, install_root)


def test_update_now_single_action_stages_pending_and_defers_shutdown(
    tmp_path, monkeypatch
):
    source = tmp_path / "release-source"
    source.mkdir()
    _release_source(source)
    release_key = SigningKey.generate()
    key_file = tmp_path / "release-key"
    key_file.write_text(
        base64.b64encode(bytes(release_key)).decode("ascii"), encoding="ascii"
    )
    notes = tmp_path / "notes.md"
    notes.write_text("Synthetic managed update.", encoding="utf-8")
    output = tmp_path / "output"
    assert build_update([
        "--root", str(source),
        "--output", str(output),
        "--package-url", PACKAGE_URL,
        "--release-id", "update-now-orchestration-test",
        "--key-id", "update-test-01",
        "--key-epoch", "1",
        "--signing-key-file", str(key_file),
        "--notes-file", str(notes),
    ]) == 0
    responses = {
        MANIFEST_URL: (output / "manifest.json").read_bytes(),
        PACKAGE_URL: next(output.glob("*.zip")).read_bytes(),
    }

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
    # 同上：Windows 托管更新链语义，宿主平台钉 windows/x64（Windows 上恒等）。
    monkeypatch.setattr(
        "src.update.service.host_platform", lambda: ("windows", "x86_64")
    )
    install_root = tmp_path / "managed"
    old_slot = install_root / "versions" / "1.0.0"
    old_slot.mkdir(parents=True)
    state = install_root / "state"
    state.mkdir()
    (state / "install-layout.json").write_text(
        '{"schema":"courselens.managed-install.v1"}', encoding="utf-8"
    )
    (state / "current.json").write_text(json.dumps({
        "schema": "courselens.current-version.v1",
        "version": "1.0.0",
        "awaiting_health": False,
    }), encoding="utf-8")
    trust_path = install_root / "trust" / "client-update-trust.json"
    _trust(trust_path, release_key)

    shutdowns = []
    active_work = {"blocked": False}
    service = UpdateService(
        current_version="1.0.0",
        trust_path=trust_path,
        state_root=install_root / "data" / "updates",
        install_root=install_root,
        transport=transport,
        has_active_work=lambda: active_work["blocked"],
        request_shutdown=shutdowns.append,
        background_checks_enabled=lambda: True,
    )
    service.start_background_checks()  # managed layout: the timer may start

    # Active work blocks the restart without staging anything (D1).
    active_work["blocked"] = True
    with pytest.raises(UpdateError, match="update_restart_blocked"):
        service.action("update_now", confirmed=True)
    assert shutdowns == []
    assert not (state / "pending.json").exists()

    active_work["blocked"] = False
    value = service.action("update_now", confirmed=True)
    assert value["state"] == "ready_to_restart"
    pending = json.loads((state / "pending.json").read_text(encoding="utf-8"))
    assert pending["target_version"] == "1.1.0"
    assert pending["previous_version"] == "1.0.0"
    assert shutdowns == []  # the HTTP 202 reply flushes before shutdown
    service.stop_background_checks()
    service.complete_restart()
    assert shutdowns == ["update_restart"]

    # The trusted launcher applies the pending switch outside the dying process.
    launcher = install_root / "launcher"
    launcher.mkdir()
    helper = launcher / "client_update_helper.py"
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "scripts" / "client_update_helper.py",
        helper,
    )
    isolated_cwd = tmp_path / "isolated-cwd"
    isolated_cwd.mkdir()
    result = subprocess.run(
        [sys.executable, "-I", str(helper), "apply", "--install-root", str(install_root)],
        cwd=isolated_cwd, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    current = json.loads((state / "current.json").read_text(encoding="utf-8"))
    assert current["version"] == "1.1.0"
    assert current["previous_version"] == "1.0.0"
    assert current["awaiting_health"] is True
