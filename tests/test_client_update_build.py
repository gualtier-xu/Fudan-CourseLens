import base64
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from nacl.signing import SigningKey

from scripts.build_client_update import main
from src.distribution import MANIFEST_SOURCE_ID


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def _release_source(root: Path, marker: str = "print('runtime')\n") -> Path:
    (root / "src").mkdir(parents=True)
    (root / "frontend").mkdir()
    (root / "config").mkdir()
    (root / "tests").mkdir()
    (root / "src" / "marker.py").write_text(marker, encoding="utf-8")
    (root / "frontend" / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (root / "tests" / "source-only.txt").write_text("must not ship", encoding="utf-8")
    (root / "courselens-version.json").write_text(json.dumps({
        "schema": "courselens.client-version.v1", "version": "1.1.0", "channel": "stable",
    }), encoding="utf-8")
    (root / "config" / "client-update-trust.json").write_text(json.dumps({
        "schema": "courselens.client-update-trust.v2", "minimum_version": "1.0.0",
    }), encoding="utf-8")
    _git(root, "init", "--quiet")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "CourseLens Tests")
    _git(root, "remote", "add", "origin", "https://github.com/gualtier-xu-co/Fudan-CourseLens-Private.git")
    _git(root, "add", "--all")
    _git(root, "commit", "--quiet", "-m", "synthetic release source")
    return root


def _build_args(tmp_path: Path, root: Path) -> tuple[list[str], Path]:
    key_file = tmp_path / "signing-key"
    key_file.write_text(
        base64.b64encode(bytes(SigningKey.generate())).decode("ascii"),
        encoding="ascii",
    )
    notes = tmp_path / "notes.md"
    notes.write_text("Synthetic signed build.", encoding="utf-8")
    output = tmp_path / "output"
    return ([
        "--root", str(root),
        "--output", str(output),
        "--package-url", "https://updates.example.test/stable/client.zip",
        "--release-id", "synthetic-build-check",
        "--key-id", "update-test-01",
        "--key-epoch", "1",
        "--signing-key-file", str(key_file),
        "--notes-file", str(notes),
        "--minimum-security-version", "0.0.1",
    ], output)


def test_builds_signed_private_package_from_clean_provenanced_runtime_allowlist(tmp_path):
    root = _release_source(tmp_path / "source")
    args, output = _build_args(tmp_path, root)
    assert main(args) == 0
    package = next(output.glob("*.zip"))
    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()
        metadata = json.loads(archive.read("courselens-package.json"))
    assert set(names) == {
        "courselens-package.json", "courselens-version.json",
        "frontend/index.html", "src/marker.py",
    }
    assert metadata["schema"] == "courselens.client-package.v1"
    assert set(metadata["files"]) == set(names) - {"courselens-package.json"}
    assert metadata["source"] == {
        "repository": MANIFEST_SOURCE_ID,
        "commit": _git(root, "rev-parse", "HEAD"),
        "tree": _git(root, "rev-parse", "HEAD^{tree}"),
    }
    envelope = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert envelope["schema"] == "courselens.client-update.v1"
    assert envelope["manifest"]["source"] == metadata["source"]
    assert envelope["manifest"]["package"]["size"] == package.stat().st_size
    assert envelope["manifest"]["minimum_security_version"] == "0.0.1"


def test_builder_rejects_dirty_release_source(tmp_path):
    root = _release_source(tmp_path / "source")
    (root / "src" / "marker.py").write_text("dirty\n", encoding="utf-8")
    args, _ = _build_args(tmp_path, root)
    with pytest.raises(SystemExit, match="clean commit"):
        main(args)


def test_builder_rejects_untrusted_origin_and_output_inside_source(tmp_path):
    root = _release_source(tmp_path / "source")
    args, _ = _build_args(tmp_path, root)
    _git(root, "remote", "set-url", "origin", "https://github.com/example/not-authorized.git")
    with pytest.raises(SystemExit, match="not authorized"):
        main(args)

    _git(
        root, "remote", "set-url", "origin",
        "https://github.com/gualtier-xu-co/Fudan-CourseLens-Private.git",
    )
    output_index = args.index("--output") + 1
    args[output_index] = str(root / "release-output")
    with pytest.raises(SystemExit, match="outside the source worktree"):
        main(args)


def _commit_launcher(root: Path, content: str) -> None:
    (root / "start_fudan_courselens.ps1").write_text(content, encoding="utf-8")
    _git(root, "add", "start_fudan_courselens.ps1")
    _git(root, "commit", "--quiet", "-m", "add packaged launcher")


def test_builder_allows_recorded_authenticode_signed_launcher(tmp_path):
    root = _release_source(tmp_path / "source")
    _commit_launcher(root, "# launcher\nWrite-Host 'CourseLens'\n")
    (root / "start_fudan_courselens.ps1").write_text(
        "# launcher\nWrite-Host 'CourseLens'\nSIG # embedded authenticode blob\n",
        encoding="utf-8",
    )
    args, output = _build_args(tmp_path, root)
    args += ["--authenticode-signed-file", "start_fudan_courselens.ps1"]
    assert main(args) == 0
    with zipfile.ZipFile(next(output.glob("*.zip"))) as archive:
        metadata = json.loads(archive.read("courselens-package.json"))
        signed = archive.read("start_fudan_courselens.ps1")
    assert metadata["authenticode_signed_files"] == ["start_fudan_courselens.ps1"]
    assert b"SIG # embedded authenticode blob" in signed


def test_builder_rejects_unrecorded_modification_and_out_of_allowlist_allowance(tmp_path):
    root = _release_source(tmp_path / "source")
    _commit_launcher(root, "# launcher\n")
    (root / "start_fudan_courselens.ps1").write_text("# launcher\nSIG\n", encoding="utf-8")
    (root / "src" / "marker.py").write_text("dirty\n", encoding="utf-8")
    args, _ = _build_args(tmp_path, root)
    args += ["--authenticode-signed-file", "start_fudan_courselens.ps1"]
    with pytest.raises(SystemExit, match="unauthorized modification"):
        main(args)

    (root / "src" / "marker.py").write_text("print('runtime')\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="not a packaged runtime file"):
        main(args + ["--authenticode-signed-file", "tests/source-only.txt"])


@pytest.mark.parametrize("marker,reason", [
    ("token = 'github_pat_" + "A" * 40 + "'\n", "secret scan"),
    ('sample = {"course_id": "123456"}\n', "course-data scan"),
])
def test_builder_fails_closed_on_outgoing_secret_or_course_data(tmp_path, marker, reason):
    root = _release_source(tmp_path / "source", marker=marker)
    args, _ = _build_args(tmp_path, root)
    with pytest.raises(SystemExit, match=reason):
        main(args)
