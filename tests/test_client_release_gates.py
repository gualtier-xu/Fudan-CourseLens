import json

from scripts.check_client_release_gates import audit, main
from src.update.service import UpdateService


def test_committed_policy_is_production_go_and_the_disabled_shape_stays_fail_closed(tmp_path):
    # GO-FLIP-1: the committed policy is the reviewed production GO state.
    result = audit(
        __import__("pathlib").Path("config/client-update-trust.json")
    )
    assert result["policy_valid"] is True
    assert result["production_enabled"] is True
    assert result["missing_gates"] == []
    assert result["release_allowed"] is True
    assert main([]) == 0
    assert main(["--require-release-ready"]) == 0
    # The disabled shape keeps its fail-closed semantics: release stays
    # disallowed until the policy is enabled with every gate a true boolean.
    disabled = json.loads(
        __import__("pathlib").Path("config/client-update-trust.json").read_text(encoding="utf-8")
    )
    disabled["enabled"] = False
    disabled["root_keys"] = {}
    disabled["release_key_authorizations"] = []
    disabled["production_gates"] = {}
    disabled_path = tmp_path / "disabled-trust.json"
    disabled_path.write_text(json.dumps(disabled), encoding="utf-8")
    disabled_result = audit(disabled_path)
    assert disabled_result["policy_valid"] is True
    assert disabled_result["production_enabled"] is False
    assert disabled_result["release_allowed"] is False
    assert main(["--config", str(disabled_path)]) == 0
    assert main(["--config", str(disabled_path), "--require-release-ready"]) == 3


def test_release_workflow_uploads_the_policy_pinned_manifest_asset():
    config_path = __import__("pathlib").Path("config/client-update-trust.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    asset = config["distribution"]["manifest_asset"]
    assert asset == "courselens-windows-manifest.json"
    workflow = __import__("pathlib").Path(".github/workflows/client-release.yml").read_text(
        encoding="utf-8"
    )
    # The publish job validates the policy asset name, renames the built
    # manifest.json to it, and uploads the renamed asset — never the raw
    # build output name the updater does not pin.
    assert asset in workflow  # the workflow pins EXPECTED_MANIFEST_ASSET to it
    assert 'mv release-package/manifest.json "release-package/$manifest_asset"' in workflow
    assert 'release-package/$MANIFEST_ASSET' in workflow
    upload = workflow.split("gh release upload", 1)[1]
    upload = upload.split('cat >> "$GITHUB_STEP_SUMMARY"', 1)[0]
    assert "release-package/manifest.json" not in upload


def test_enabling_json_cannot_bypass_the_pinned_public_manifest_url(tmp_path, monkeypatch):
    # 本测试钉的是 Windows 信任链的 distribution 门语义；宿主平台钉为
    # windows/x64 使断言与跑套件的 OS 无关（Windows 宿主上=恒等零变化），
    # macOS 宿主不再被更早的 host_platform_mismatch 短路（产品 fail-closed
    # 语义零改动，见 src/update/service.py::_policy 的门序）。
    monkeypatch.setattr(
        "src.update.service.host_platform", lambda: ("windows", "x86_64")
    )
    raw = json.loads(
        __import__("pathlib").Path("config/client-update-trust.json").read_text(encoding="utf-8")
    )
    raw["enabled"] = True
    raw["manifest_url"] = "https://objects.githubusercontent.com/manifest.json"
    raw["root_keys"] = {"root-test": "unused"}
    raw["release_key_authorizations"] = []
    raw["production_gates"] = {name: True for name in raw["production_gates"]}
    policy = tmp_path / "trust.json"
    policy.write_text(json.dumps(raw), encoding="utf-8")
    service = UpdateService(
        current_version="0.1.0", trust_path=policy,
        state_root=tmp_path / "state", install_root=tmp_path / "install",
    )
    assert service.check()["error_code"] == "distribution_policy_invalid"


def test_string_false_gate_is_missing_not_whitewashed(tmp_path, monkeypatch):
    # SRC-CLEANUP-1 U3（N6CP#11 处方）：字符串 "false" 不是真布尔——
    # checker 审计与 TrustPolicy.load 同步 fail-closed，绝不允许洗绿。
    # 宿主平台钉 windows/x64：被测语义=production_gates 门序，与跑套件
    # 的 OS 无关（同上，Windows 宿主上=恒等零变化）。
    import pathlib

    monkeypatch.setattr(
        "src.update.service.host_platform", lambda: ("windows", "x86_64")
    )

    raw = json.loads(
        pathlib.Path("config/client-update-trust.json").read_text(encoding="utf-8")
    )
    raw["enabled"] = True  # 越过 update_not_configured 短路，直达门判定
    raw["production_gates"] = {
        name: ("false" if idx == 0 else True)
        for idx, name in enumerate(sorted(raw["production_gates"]))
    }
    policy = tmp_path / "trust.json"
    policy.write_text(json.dumps(raw), encoding="utf-8")

    result = audit(policy)
    assert result["missing_gates"], "string gate must be counted missing"
    assert result["release_allowed"] is False

    service = UpdateService(
        current_version="0.1.0", trust_path=policy,
        state_root=tmp_path / "state", install_root=tmp_path / "install",
    )
    assert service.check()["error_code"] == "production_gates_incomplete"
