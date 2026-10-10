from __future__ import annotations

import base64
import copy
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from nacl.signing import SigningKey

from scripts.export_worker_mirror import ExportError, export_snapshot
from shared.protocol.mirror import (
    MANIFEST_SCHEMA,
    TRUST_SCHEMA,
    MirrorVerificationError,
    pretty_json,
    sign_document,
    verify_manifest_document,
    verify_snapshot_files,
)


def keypair() -> tuple[str, str]:
    key = SigningKey.generate()
    return (
        base64.b64encode(bytes(key)).decode("ascii"),
        base64.b64encode(bytes(key.verify_key)).decode("ascii"),
    )


class WorkerMirrorTests(unittest.TestCase):
    def test_repository_worker_sources_are_all_allowlisted(self):
        repo_root = Path(__file__).resolve().parents[1]
        allowlist = json.loads(
            (repo_root / "scripts" / "worker_mirror_allowlist.json").read_text(encoding="utf-8")
        )
        expected = {str(item["source"]).replace("\\", "/") for item in allowlist["files"]}
        # --others --exclude-standard：清单要覆盖「会随提交一起进仓的」worker 源文件。
        # 只列已跟踪文件时，新加入的模块在提交前不可见，缺口要到提交后才暴露——那时
        # 清单已经写错了一版；加上未跟踪（非 ignore）文件就能在提交前把缺口挡下来。
        completed = subprocess.run(
            [
                "git",
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                "worker",
                "shared/protocol",
                "shared/__init__.py",
                "shared/evidence_contract.py",
            ],
            cwd=repo_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        actual = {line for line in completed.stdout.splitlines() if line}
        self.assertEqual(actual, expected)
        self.assertIn("shared/evidence_contract.py", expected)

    def test_worker_shared_import_exports_evidence_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            values = self.fixture(
                Path(tmp), worker_source="from shared.evidence_contract import compute_id\n"
            )
            result = export_snapshot(
                values[0], values[1], signing_private_key=values[8], signing_key_id=values[7],
                source_commit="1" * 40, worker_tree="2" * 40,
                allowlist_path=values[2], release_root=values[3],
            )
            exported = {item["path"] for item in result["manifest"]["payload"]["files"]}
            self.assertIn("shared/evidence_contract.py", exported)

    def trust(self):
        root_private, root_public = keypair()
        release_private, release_public = keypair()
        root_id = "root-test-01"
        release_id = "release-test-01"
        root = {
            "schema": "courselens.worker-mirror.root.v1",
            "root_keys": [{"key_id": root_id, "public_key": root_public}],
        }
        trust = {
            "schema": TRUST_SCHEMA,
            "epoch": 1,
            "expires_at": int(time.time()) + 3600,
            "release_keys": [{
                "key_id": release_id,
                "public_key": release_public,
                "status": "active",
            }],
            "revoked_manifests": [],
        }
        trust_signature = sign_document(trust, key_id=root_id, private_key=root_private)
        return root, trust, trust_signature, release_id, release_private

    def fixture(self, directory: Path, *, worker_source: str = "print('ok')\n"):
        repo = directory / "repo"
        output = directory / "output"
        (repo / "worker").mkdir(parents=True)
        (repo / "shared" / "protocol").mkdir(parents=True)
        (repo / "shared" / "__init__.py").write_text('"""shared"""\n', encoding="utf-8")
        (repo / "shared" / "evidence_contract.py").write_text(
            "def compute_id(*parts: str) -> str:\n    return '/'.join(parts)\n", encoding="utf-8"
        )
        (repo / "shared" / "protocol" / "wire.py").write_text("PROTOCOL_VERSION = '2'\n", encoding="utf-8")
        (repo / "worker" / "main.py").write_text(worker_source, encoding="utf-8")
        allowlist = {
            "schema": "courselens.worker-mirror.allowlist.v1",
            "exporter_version": "1.0.0",
            "text_extensions": [".py"],
            "files": [
                {"source": "shared/__init__.py", "destination": "shared/__init__.py", "mode": "100644"},
                {"source": "shared/evidence_contract.py", "destination": "shared/evidence_contract.py", "mode": "100644"},
                {"source": "shared/protocol/wire.py", "destination": "shared/protocol/wire.py", "mode": "100644"},
                {"source": "worker/main.py", "destination": "main.py", "mode": "100644"},
            ],
        }
        allowlist_path = directory / "allowlist.json"
        allowlist_path.write_bytes(pretty_json(allowlist))
        release_root = directory / "release"
        release_root.mkdir()
        root, trust, trust_signature, key_id, release_private = self.trust()
        (release_root / "worker-mirror-root.json").write_bytes(pretty_json(root))
        (release_root / "worker-mirror-trust.json").write_bytes(pretty_json(trust))
        (release_root / "worker-mirror-trust.sig").write_bytes(pretty_json(trust_signature))
        return repo, output, allowlist_path, release_root, root, trust, trust_signature, key_id, release_private

    def export(self, directory: Path, **kwargs):
        values = self.fixture(directory, **kwargs)
        repo, output, allowlist, release_root, root, trust, trust_sig, key_id, private = values
        result = export_snapshot(
            repo,
            output,
            signing_private_key=private,
            signing_key_id=key_id,
            source_commit="1" * 40,
            worker_tree="2" * 40,
            allowlist_path=allowlist,
            release_root=release_root,
        )
        return result, values

    def test_export_is_reproducible_and_verifiable(self):
        with tempfile.TemporaryDirectory() as first_tmp, tempfile.TemporaryDirectory() as second_tmp:
            first, first_values = self.export(Path(first_tmp))
            second, second_values = self.export(Path(second_tmp))
            self.assertEqual(first["manifest"], second["manifest"])
            self.assertEqual(first["payload_git_tree"], second["payload_git_tree"])
            output = first_values[1]
            manifest = json.loads((output / "worker-mirror.manifest.json").read_text(encoding="utf-8"))
            signature = json.loads((output / "worker-mirror.manifest.sig").read_text(encoding="utf-8"))
            verified = verify_manifest_document(
                manifest,
                signature,
                first_values[5],
                first_values[6],
                root_keys={
                    item["key_id"]: item["public_key"]
                    for item in first_values[4]["root_keys"]
                },
            )
            self.assertEqual(verified["manifest_sha256"], first["manifest_sha256"])
            verify_snapshot_files(output, verified["files"])

    def test_verifier_ignores_only_root_git_checkout_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, values = self.export(Path(tmp))
            output = values[1]
            git_metadata = output / ".git"
            git_metadata.mkdir()
            (git_metadata / "description").write_text("public checkout metadata\n", encoding="utf-8")
            verify_snapshot_files(output, result["manifest"]["payload"]["files"])

            nested = output / "nested" / ".git"
            nested.mkdir(parents=True)
            (nested / "config").write_text("unexpected\n", encoding="utf-8")
            with self.assertRaisesRegex(MirrorVerificationError, "unknown file nested/.git/config"):
                verify_snapshot_files(output, result["manifest"]["payload"]["files"])

    def test_unknown_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            values = self.fixture(Path(tmp))
            (values[0] / "worker" / "unknown.py").write_text("pass\n", encoding="utf-8")
            with self.assertRaisesRegex(ExportError, "Unknown Worker source file"):
                export_snapshot(
                    values[0], values[1], signing_private_key=values[8], signing_key_id=values[7],
                    source_commit="1" * 40, worker_tree="2" * 40,
                    allowlist_path=values[2], release_root=values[3],
                )

    def test_private_import_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            values = self.fixture(Path(tmp), worker_source="from src.api import webvpn\n")
            with self.assertRaisesRegex(ExportError, "private module"):
                export_snapshot(
                    values[0], values[1], signing_private_key=values[8], signing_key_id=values[7],
                    source_commit="1" * 40, worker_tree="2" * 40,
                    allowlist_path=values[2], release_root=values[3],
                )

    def test_path_traversal_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            values = self.fixture(Path(tmp))
            allowlist = json.loads(values[2].read_text(encoding="utf-8"))
            allowlist["files"][0]["destination"] = "../escape.py"
            values[2].write_bytes(pretty_json(allowlist))
            with self.assertRaisesRegex(ExportError, "unsafe"):
                export_snapshot(
                    values[0], values[1], signing_private_key=values[8], signing_key_id=values[7],
                    source_commit="1" * 40, worker_tree="2" * 40,
                    allowlist_path=values[2], release_root=values[3],
                )

    def test_symlink_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            values = self.fixture(Path(tmp))
            target = values[0] / "worker" / "linked.py"
            try:
                os.symlink(values[0] / "worker" / "main.py", target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable")
            with self.assertRaisesRegex(ExportError, "link or reparse"):
                export_snapshot(
                    values[0], values[1], signing_private_key=values[8], signing_key_id=values[7],
                    source_commit="1" * 40, worker_tree="2" * 40,
                    allowlist_path=values[2], release_root=values[3],
                )

    def test_manifest_tamper_and_protocol_mismatch_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, values = self.export(Path(tmp))
            signature = json.loads((values[1] / "worker-mirror.manifest.sig").read_text(encoding="utf-8"))
            damaged = copy.deepcopy(result["manifest"])
            damaged["source"]["commit"] = "f" * 40
            roots = {item["key_id"]: item["public_key"] for item in values[4]["root_keys"]}
            with self.assertRaises(MirrorVerificationError):
                verify_manifest_document(damaged, signature, values[5], values[6], root_keys=roots)
            with self.assertRaisesRegex(MirrorVerificationError, "incompatible"):
                verify_manifest_document(
                    result["manifest"], signature, values[5], values[6], root_keys=roots,
                    supported_protocol_versions=("999",),
                )

    def test_revoked_release_key_is_rejected(self):
        root_private, root_public = keypair()
        release_private, release_public = keypair()
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "protocol": {"supported_versions": ["2"]},
            "payload": {
                "files": [{"path": "a.py", "mode": "100644", "size": 1, "sha256": "0" * 64}],
                "tree_sha256": "0" * 64,
                "git_tree": "0" * 40,
            },
        }
        trust = {
            "schema": TRUST_SCHEMA,
            "epoch": 2,
            "expires_at": int(time.time()) + 3600,
            "release_keys": [{"key_id": "release-revoked", "public_key": release_public, "status": "revoked"}],
            "revoked_manifests": [],
        }
        trust_sig = sign_document(trust, key_id="root-revoke", private_key=root_private)
        manifest_sig = sign_document(manifest, key_id="release-revoked", private_key=release_private)
        with self.assertRaisesRegex(MirrorVerificationError, "revoked"):
            verify_manifest_document(
                manifest, manifest_sig, trust, trust_sig,
                root_keys={"root-revoke": root_public},
            )


if __name__ == "__main__":
    unittest.main()
