import json
import sys

import pytest

from scripts import sign_client_release
from scripts.sign_client_release import import_pfx, main, resolve_artifacts

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Authenticode is Windows-only")


def _launcher_tree(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "start_fudan_courselens.ps1").write_text("# launcher\n", encoding="utf-8")
    return root


def test_inventory_reports_unsigned_launcher(tmp_path, capsys):
    root = _launcher_tree(tmp_path)
    assert main(["inventory", "--root", str(root)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == "courselens.authenticode-inventory.v1"
    assert [item["file"] for item in report["artifacts"]] == ["start_fudan_courselens.ps1"]
    assert report["artifacts"][0]["kind"] == "ps1"
    assert report["artifacts"][0]["signed"] is False
    assert report["all_signed"] is False


def test_inventory_survives_incompatible_psmodulepath(tmp_path, capsys, monkeypatch):
    root = _launcher_tree(tmp_path)
    monkeypatch.setenv(
        "PSModulePath",
        r"C:\nonexistent\powershell7\Modules;Z:\broken-tool-runtime\Modules",
    )
    assert main(["inventory", "--root", str(root)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == "courselens.authenticode-inventory.v1"
    assert report["artifacts"][0]["kind"] == "ps1"
    assert report["artifacts"][0]["signed"] is False
    assert report["all_signed"] is False


def test_powershell_uses_windows_powershell_module_path(monkeypatch):
    captured = {}

    class Result:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(*args, **kwargs):
        captured.update(kwargs["env"])
        return Result()

    monkeypatch.setenv("SystemRoot", r"C:\hostile")
    monkeypatch.setenv("WINDIR", r"C:\also-hostile")
    monkeypatch.setenv("PSModulePath", r"C:\bad\PowerShell7\Modules")
    monkeypatch.setattr(sign_client_release, "_windows_system_directory", lambda: r"C:\Windows\System32")
    monkeypatch.setattr(sign_client_release.subprocess, "run", fake_run)
    assert sign_client_release._powershell("$null") == ""
    assert captured["PSMODULEPATH"].casefold() == (
        r"C:\Windows\System32\WindowsPowerShell\v1.0\Modules".casefold()
    )


def test_powershell_ignores_hostile_system_root_environment(monkeypatch):
    monkeypatch.setenv("SystemRoot", r"Z:\attacker")
    monkeypatch.setenv("WINDIR", r"Y:\attacker")
    monkeypatch.setattr(sign_client_release, "_windows_system_directory", lambda: r"C:\Windows\System32")
    captured = {}

    class Result:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(*args, **kwargs):
        captured.update(kwargs["env"])
        return Result()

    monkeypatch.setattr(sign_client_release.subprocess, "run", fake_run)
    sign_client_release._powershell("$null")
    assert r"attacker" not in captured["PSMODULEPATH"].casefold()


def test_verify_fails_closed_on_unsigned_launcher(tmp_path):
    root = _launcher_tree(tmp_path)
    assert main(["verify", "--root", str(root)]) == 2


def test_sign_requires_an_explicit_certificate_source(tmp_path):
    root = _launcher_tree(tmp_path)
    with pytest.raises(SystemExit, match="certificate source"):
        main(["sign", "--root", str(root)])


def test_missing_requested_artifact_fails_closed(tmp_path):
    with pytest.raises(SystemExit, match="artifact is missing"):
        resolve_artifacts(tmp_path, ["does-not-exist.ps1"])


def test_invalid_thumbprint_is_rejected_before_any_signing(tmp_path):
    root = _launcher_tree(tmp_path)
    with pytest.raises(SystemExit, match="thumbprint"):
        main(["sign", "--root", str(root), "--thumbprint", "not-a-thumbprint"])


def test_pfx_import_requires_password_environment(tmp_path):
    empty = tmp_path / "empty.pfx"
    empty.write_bytes(b"")
    with pytest.raises(SystemExit, match="WINDOWS_AUTHENTICODE_CERTIFICATE_PASSWORD"):
        import_pfx(empty)
