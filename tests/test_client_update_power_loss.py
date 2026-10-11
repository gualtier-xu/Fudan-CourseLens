"""Synthetic process-death matrix for every update persistence boundary."""

from __future__ import annotations

import json

import pytest

from tests.test_client_update import build


class PowerLoss(BaseException):
    pass


class Trip:
    def __init__(self, boundary: str):
        self.boundary = boundary
        self.seen: list[str] = []

    def __call__(self, boundary: str) -> None:
        self.seen.append(boundary)
        if boundary == self.boundary:
            raise PowerLoss(boundary)


def restart(service):
    return type(service)(
        current_version=service.current_version,
        trust_path=service.trust_path,
        state_root=service.state_root,
        install_root=service.install_root,
        transport=service.transport,
        now=service.now,
    )


def assert_bootable(service, old="1.0.0", candidate="1.1.0"):
    current_path = service.install_root / "state" / "current.json"
    if not current_path.exists():
        # Before the first pointer switch, the stable launcher retains its
        # compiled/configured old version.
        assert (service.install_root / "versions" / old).is_dir()
        return old
    current = json.loads(current_path.read_text(encoding="utf-8"))
    selected = str(current["version"])
    assert selected in {old, candidate}
    assert (service.install_root / "versions" / selected).is_dir()
    return selected


@pytest.mark.parametrize("boundary", [
    "download.after_replace",
    "verification.after_complete",
    "slot.after_replace",
    "accepted.replace.after_file_fsync",
    "accepted.replace.after_replace",
])
def test_download_verify_extract_slot_boundaries_restart_bootable(
    tmp_path, monkeypatch, boundary
):
    service, *_ = build(tmp_path, monkeypatch)
    service.check()
    trip = Trip(boundary)
    service.fault = trip
    with pytest.raises(PowerLoss, match=boundary):
        service.download()
    recovered = restart(service)
    assert boundary in trip.seen
    assert_bootable(recovered)
    # Repeating download is safe whether the slot rename happened or not.
    assert recovered.download()["state"] == "ready_to_restart"
    assert_bootable(recovered)


@pytest.mark.parametrize("boundary", [
    "pending.replace.before_write",
    "pending.replace.after_file_fsync",
    "pending.replace.after_replace",
])
def test_pending_boundaries_leave_old_or_pending_candidate(
    tmp_path, monkeypatch, boundary
):
    service, _, _, install, _ = build(tmp_path, monkeypatch)
    service.check()
    service.download()
    (install / "state").mkdir()
    (install / "state" / "install-layout.json").write_text(
        '{"schema":"courselens.managed-install.v1"}', encoding="utf-8"
    )
    (install / "state" / "current.json").write_text(
        '{"schema":"courselens.current-version.v1","version":"1.0.0"}',
        encoding="utf-8",
    )
    service.fault = Trip(boundary)
    with pytest.raises(PowerLoss, match=boundary):
        service.install()
    recovered = restart(service)
    assert assert_bootable(recovered) == "1.0.0"
    if (install / "state" / "pending.json").exists():
        recovered.apply_pending()
        assert assert_bootable(recovered) == "1.1.0"


@pytest.mark.parametrize("boundary", [
    "current.apply_replace.before_write",
    "current.apply_replace.after_file_fsync",
    "current.apply_replace.after_replace",
    "pending.after_remove",
])
def test_apply_pointer_boundaries_are_idempotent(tmp_path, monkeypatch, boundary):
    service, _, _, install, _ = build(tmp_path, monkeypatch)
    service.check()
    service.download()
    (install / "state").mkdir()
    (install / "state" / "install-layout.json").write_text(
        '{"schema":"courselens.managed-install.v1"}', encoding="utf-8"
    )
    (install / "state" / "current.json").write_text(
        '{"schema":"courselens.current-version.v1","version":"1.0.0"}',
        encoding="utf-8",
    )
    service.install()
    service.fault = Trip(boundary)
    with pytest.raises(PowerLoss, match=boundary):
        service.apply_pending()
    recovered = restart(service)
    selected = assert_bootable(recovered)
    assert selected in {"1.0.0", "1.1.0"}
    if selected == "1.0.0":
        recovered.apply_pending()
    else:
        recovered.apply_pending()
    assert assert_bootable(recovered) == "1.1.0"


@pytest.mark.parametrize("operation,boundary", [
    ("health", "current.health_replace.before_write"),
    ("health", "current.health_replace.after_file_fsync"),
    ("health", "current.health_replace.after_replace"),
    ("rollback", "current.rollback_replace.before_write"),
    ("rollback", "current.rollback_replace.after_file_fsync"),
    ("rollback", "current.rollback_replace.after_replace"),
])
def test_health_and_rollback_boundaries_remain_bootable_and_repeatable(
    tmp_path, monkeypatch, operation, boundary
):
    service, _, _, install, _ = build(tmp_path, monkeypatch)
    service.check()
    service.download()
    (install / "state").mkdir()
    (install / "state" / "install-layout.json").write_text(
        '{"schema":"courselens.managed-install.v1"}', encoding="utf-8"
    )
    (install / "state" / "current.json").write_text(
        '{"schema":"courselens.current-version.v1","version":"1.0.0"}',
        encoding="utf-8",
    )
    service.install()
    service.apply_pending()
    service.fault = Trip(boundary)
    call = (
        lambda: service.confirm_health("1.1.0")
        if operation == "health"
        else service.rollback_if_unhealthy()
    )
    with pytest.raises(PowerLoss, match=boundary):
        call()
    recovered = restart(service)
    selected = assert_bootable(recovered)
    if operation == "health":
        assert recovered.confirm_health("1.1.0")["state"] == "healthy"
        assert recovered.confirm_health("1.1.0")["state"] == "healthy"
    elif selected == "1.1.0":
        assert recovered.rollback_if_unhealthy()["state"] == "rolled_back"
        assert recovered.rollback_if_unhealthy()["state"] == "rolled_back"
    else:
        assert recovered.rollback_if_unhealthy()["state"] == "rolled_back"
    assert_bootable(recovered)


def test_half_flight_states_reset_to_idle_on_restart(tmp_path, monkeypatch):
    """UPDATE-UX-1 摩擦修复：半程编排态（checking/downloading/verifying）无法跨
    进程存活——带着旧进程快照重启会让顶栏/面板永久卡 busy（actions 空、无恢复
    入口）。重启后干净回到待检查，且更新链照常可用。"""
    service, _, _, install, state = build(tmp_path, monkeypatch)
    state.mkdir(parents=True, exist_ok=True)
    for stuck in ("checking", "downloading", "verifying"):
        (state / "state.json").write_text(json.dumps({
            "schema": "courselens.client-update-state.v1", "state": stuck,
            "error_code": "", "current_version": "1.0.0", "channel": "stable",
            "available_version": "1.1.0", "package_size": 10, "release_notes": "",
            "last_checked_at": 0, "observed_at": 0,
            "download_bytes": 5, "download_total": 10,
            "rollback": {"available": False, "version": "", "state": "none"},
            "actions": [],
        }), encoding="utf-8")
        recovered = restart(service)
        snapshot = recovered.snapshot()
        assert snapshot["state"] == "idle", stuck
        assert snapshot["actions"] == ["check"], stuck
        assert snapshot["error_code"] == "", stuck
        assert snapshot["download_bytes"] == 0 and snapshot["download_total"] == 0, stuck
        # 自愈后全链照常：重新检查→下载→就绪（既有语义零弱化）
        assert recovered.check()["state"] == "available"
        assert recovered.download()["state"] == "ready_to_restart"


def test_terminal_states_are_not_reset_by_recovery(tmp_path, monkeypatch):
    """负断言：带真实待办语义的终态不被自愈触碰（ready_to_restart 有 pending
    义务，failed/policy_blocked 有用户可读指导）。"""
    service, _, _, install, state = build(tmp_path, monkeypatch)
    state.mkdir(parents=True, exist_ok=True)
    for kept, actions in (("ready_to_restart", ["check", "install"]), ("failed", ["check"])):
        (state / "state.json").write_text(json.dumps({
            "schema": "courselens.client-update-state.v1", "state": kept,
            "error_code": "" if kept == "ready_to_restart" else "download_http_error",
            "current_version": "1.0.0", "channel": "stable",
            "available_version": "1.1.0", "package_size": 10, "release_notes": "",
            "last_checked_at": 0, "observed_at": 0,
            "download_bytes": 0, "download_total": 0,
            "rollback": {"available": False, "version": "", "state": "none"},
            "actions": actions,
        }), encoding="utf-8")
        recovered = restart(service)
        snapshot = recovered.snapshot()
        assert snapshot["state"] == kept, kept
        assert snapshot["actions"] == actions, kept


def test_recovery_removes_atomic_and_partial_residue(tmp_path, monkeypatch):
    service, _, _, install, state = build(tmp_path, monkeypatch)
    staging = state / "staging"
    staging.mkdir(parents=True)
    (staging / "package.partial").write_bytes(b"partial")
    (state / ".state.json.1.dead.tmp").write_text("{", encoding="utf-8")
    install_state = install / "state"
    install_state.mkdir()
    (install_state / ".current.json.1.dead.tmp").write_text("{", encoding="utf-8")
    recovered = restart(service)
    assert not list(state.rglob("*.partial"))
    assert not list(state.rglob(".*.tmp"))
    assert not list(install.rglob(".*.tmp"))
    assert_bootable(recovered)
