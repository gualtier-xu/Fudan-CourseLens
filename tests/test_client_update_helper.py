import json
from pathlib import Path

import pytest

from scripts.client_update_helper import apply, confirm, rollback


def test_stable_helper_switches_confirms_and_rolls_back(tmp_path):
    root = tmp_path.resolve()
    (root / "state").mkdir()
    for version in ("1.0.0", "1.1.0"):
        slot = root / "versions" / version
        slot.mkdir(parents=True)
        (slot / "marker.txt").write_text("trusted", encoding="utf-8")
        import hashlib
        (slot / "courselens-package.json").write_text(json.dumps({
            "schema": "courselens.client-package.v1", "version": version,
            "files": {"marker.txt": hashlib.sha256(b"trusted").hexdigest()},
        }), encoding="utf-8")
    (root / "state" / "install-layout.json").write_text(
        '{"schema":"courselens.managed-install.v1"}', encoding="utf-8"
    )
    (root / "state" / "current.json").write_text('{"version":"1.0.0"}', encoding="utf-8")
    (root / "state" / "pending.json").write_text(json.dumps({
        "schema": "courselens.update-pending.v1", "target_version": "1.1.0",
        "previous_version": "1.0.0",
    }), encoding="utf-8")
    assert Path(apply(root)).name == "1.1.0"
    assert Path(apply(root)).name == "1.1.0"
    assert confirm(root, "1.1.0") == "1.1.0"
    assert confirm(root, "1.1.0") == "1.1.0"

    current = json.loads((root / "state" / "current.json").read_text())
    current["awaiting_health"] = True
    (root / "state" / "current.json").write_text(json.dumps(current))
    assert Path(rollback(root)).name == "1.0.0"
    assert Path(rollback(root)).name == "1.0.0"


def test_stable_helper_rejects_files_outside_exact_slot_inventory(tmp_path):
    root = tmp_path.resolve()
    (root / "state").mkdir()
    slot = root / "versions" / "1.1.0"
    slot.mkdir(parents=True)
    marker = b"trusted"
    (slot / "marker.txt").write_bytes(marker)
    (slot / "unexpected.txt").write_text("not inventoried", encoding="utf-8")
    import hashlib
    (slot / "courselens-package.json").write_text(json.dumps({
        "schema": "courselens.client-package.v1", "version": "1.1.0",
        "files": {"marker.txt": hashlib.sha256(marker).hexdigest()},
    }), encoding="utf-8")
    (root / "state" / "install-layout.json").write_text(
        '{"schema":"courselens.managed-install.v1"}', encoding="utf-8"
    )
    (root / "state" / "current.json").write_text(
        '{"version":"1.0.0"}', encoding="utf-8"
    )
    (root / "state" / "pending.json").write_text(json.dumps({
        "schema": "courselens.update-pending.v1", "target_version": "1.1.0",
        "previous_version": "1.0.0",
    }), encoding="utf-8")
    with pytest.raises(RuntimeError, match="staged_update_inventory_invalid"):
        apply(root)


def test_bom_written_state_files_apply_clean(tmp_path):
    """SRC-SYNDROME-1 U4（N6CP EX1 处方验收①）：PS5.1 写侧产出的 BOM 态
    state 文件族（utf-8-sig 写=带 BOM）必须被 helper 读侧透明消化。"""
    root = tmp_path.resolve()
    (root / "state").mkdir()
    for version in ("1.0.0", "1.1.0"):
        slot = root / "versions" / version
        slot.mkdir(parents=True)
        (slot / "marker.txt").write_text("trusted", encoding="utf-8")
        import hashlib
        (slot / "courselens-package.json").write_text(json.dumps({
            "schema": "courselens.client-package.v1", "version": version,
            "files": {"marker.txt": hashlib.sha256(b"trusted").hexdigest()},
        }), encoding="utf-8-sig")
    (root / "state" / "install-layout.json").write_text(
        '{"schema":"courselens.managed-install.v1"}', encoding="utf-8-sig"
    )
    (root / "state" / "current.json").write_text('{"version":"1.0.0"}', encoding="utf-8-sig")
    (root / "state" / "pending.json").write_text(json.dumps({
        "schema": "courselens.update-pending.v1", "target_version": "1.1.0",
        "previous_version": "1.0.0",
    }), encoding="utf-8-sig")
    assert Path(apply(root)).name == "1.1.0", "BOM 态安装必须照常通过"
    assert confirm(root, "1.1.0") == "1.1.0"


def test_utf16_state_files_fail_closed(tmp_path):
    """SRC-SYNDROME-1 U4 三态配方 D 态：UTF-16 形态必须闭集报错，绝不静默错读。"""
    root = tmp_path.resolve()
    (root / "state").mkdir()
    (root / "state" / "install-layout.json").write_bytes(
        '{"schema":"courselens.managed-install.v1"}'.encode("utf-16")
    )
    with pytest.raises((RuntimeError, OSError, ValueError)):
        apply(root)
