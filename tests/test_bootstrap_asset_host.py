import base64
import json
from pathlib import Path

import pytest
from nacl.signing import VerifyKey

from scripts import bootstrap_asset_host as host
from scripts.bootstrap_asset_host import main

REPO_CONFIG = Path("config/client-update-trust.json")


def disabled_policy(tmp_path):
    """A disabled-shape policy: since GO-FLIP-1 the committed config is the
    production GO state, and the bootstrap tool refuses enabled policies."""
    raw = json.loads(REPO_CONFIG.read_text(encoding="utf-8"))
    raw["enabled"] = False
    path = tmp_path / "disabled-trust.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_plan_lists_public_release_portal_and_never_secret_values(capsys):
    assert main(["plan"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["schema"] == "courselens.asset-host-bootstrap-plan.v1"
    assert plan["repository"]["name"] == "gualtier-xu/Fudan-CourseLens"
    assert plan["repository"]["visibility"] == "public"
    # The public portal repository already exists; the plan must never create it.
    assert not any("repo create" in command for command in plan["gh_commands"])
    trust = json.loads(REPO_CONFIG.read_text(encoding="utf-8"))
    expected = [str(name) for name in trust["required_secret_names"]]
    assert plan["secret_names"] == expected
    assert "COURSELENS_RELEASE_PUBLISHER_TOKEN" in plan["secret_names"]
    assert not any(
        name.startswith("COURSELENS_RELEASE_REPOSITORY_APP")
        for name in plan["secret_names"]
    )
    publisher_commands = [
        command for command in plan["secret_commands"]
        if "COURSELENS_RELEASE_PUBLISHER_TOKEN" in command
    ]
    assert len(publisher_commands) == 1
    assert "--env client-release-production" in publisher_commands[0]
    assert "--repo gualtier-xu-co/Fudan-CourseLens-Private" in publisher_commands[0]
    assert len(plan["secret_commands"]) == len(expected)
    for command in plan["secret_commands"]:
        assert command.startswith("gh secret set ")
        assert "--env client-release-production" in command
    assert any("contents:write" in step for step in plan["manual_steps"])
    assert any(
        "COURSELENS_RELEASE_PUBLISHER_TOKEN" in step for step in plan["manual_steps"]
    )
    assert any(
        "never the student App" in step for step in plan["manual_steps"]
    )
    # the plan is generated offline and must not carry secret-looking material
    rendered = json.dumps(plan)
    assert "BEGIN PRIVATE KEY" not in rendered
    assert "ghp_" not in rendered


def test_check_is_offline_and_reports_gate_audit(tmp_path, capsys):
    config = disabled_policy(tmp_path)
    assert main(["check", "--config", str(config)]) == 0
    check = json.loads(capsys.readouterr().out)
    assert check["schema"] == "courselens.asset-host-bootstrap-check.v1"
    assert check["gh_available"] is False
    assert check["trust_policy_ok"] is True
    assert check["production_enabled"] is False
    assert check["release_gate_audit"]["policy_valid"] is True
    # The committed production GO policy is refused by design: the bootstrap
    # phase is over once updates are enabled.
    with pytest.raises(SystemExit, match="refusing to operate"):
        main(["check"])


def test_apply_is_refused_without_explicit_yes():
    with pytest.raises(SystemExit, match="--yes"):
        main(["apply"])


def test_apply_without_gh_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(host.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit, match="gh CLI is required"):
        main(["apply", "--yes", "--config", str(disabled_policy(tmp_path))])


def test_apply_only_verifies_the_public_repository_and_creates_nothing(monkeypatch, capsys, tmp_path):
    calls = []

    def fake_gh(*args, check=True):
        calls.append(list(args))
        return json.dumps({
            "name": host.ASSET_REPOSITORY, "isPrivate": False, "description": "x",
        })

    monkeypatch.setattr(host.shutil, "which", lambda name: "gh")
    monkeypatch.setattr(host, "_gh", fake_gh)
    assert main(["apply", "--yes", "--config", str(disabled_policy(tmp_path))]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["repository"] == "gualtier-xu/Fudan-CourseLens"
    assert any("nothing was created or edited" in action for action in result["actions"])
    assert any("COURSELENS_RELEASE_PUBLISHER_TOKEN" in action for action in result["actions"])
    for arguments in calls:
        assert "create" not in arguments
        assert "edit" not in arguments


def test_apply_refuses_a_non_public_repository(monkeypatch, tmp_path):
    def fake_gh(*args, check=True):
        return json.dumps({
            "name": host.ASSET_REPOSITORY, "isPrivate": True, "description": "",
        })

    monkeypatch.setattr(host.shutil, "which", lambda name: "gh")
    monkeypatch.setattr(host, "_gh", fake_gh)
    with pytest.raises(SystemExit, match="not public"):
        main(["apply", "--yes", "--config", str(disabled_policy(tmp_path))])


def test_apply_fails_closed_cleanly_when_the_repository_is_missing(monkeypatch, tmp_path):
    def fake_gh(*args, check=True):
        return ""

    monkeypatch.setattr(host.shutil, "which", lambda name: "gh")
    monkeypatch.setattr(host, "_gh", fake_gh)
    with pytest.raises(SystemExit, match="not accessible"):
        main(["apply", "--yes", "--config", str(disabled_policy(tmp_path))])


def test_generate_root_key_writes_outside_repo_and_refuses_inside(tmp_path, capsys):
    out = tmp_path / "keys" / "update-root.b64"
    assert main(["generate-root-key", "--out", str(out)]) == 0
    payload = json.loads(capsys.readouterr().out)
    seed = base64.b64decode(out.read_text(encoding="ascii"))
    assert len(seed) == 32
    public = base64.b64decode(payload["public_key"])
    VerifyKey(public)  # a valid Ed25519 public key
    assert payload["written_to"] == str(out.resolve())
    with pytest.raises(SystemExit, match="overwrite"):
        main(["generate-root-key", "--out", str(out)])
    inside_repo = Path(host.PROJECT_ROOT) / "release-keys" / "forbidden.b64"
    with pytest.raises(SystemExit, match="inside the repository"):
        main(["generate-root-key", "--out", str(inside_repo)])
    assert not inside_repo.exists()
