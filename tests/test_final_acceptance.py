from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from scripts.final_acceptance import (
    EXTERNAL_GATES,
    GATE_EVIDENCE_SCHEMA,
    authorize_active_credentials,
    credential_inventory,
    gate_binding,
    record_gate,
    migrate_legacy_credentials,
    retire_legacy_credentials,
    summary,
    verify_active_credentials,
    write_external_evidence,
)


def context(source: str = "a" * 64, tree: str = "b" * 40) -> dict:
    return {
        "source_content_sha256": source,
        "browser_content_sha256": "9" * 64,
        "worker": {
            "repository": "owner/worker",
            "commit": "c" * 40,
            "tree": tree,
            "manifest_sha256": "d" * 64,
            "key_id": "release-key",
            "protocol_versions": [2],
        },
        "catalog": {"status": "ok", "courses": 2, "lectures": 3, "digest": "e" * 64},
    }


class FinalAcceptanceTests(unittest.TestCase):
    @mock.patch("src.remote.github_app.GitHubAppClient")
    @mock.patch("credentials.CredentialStore")
    def test_authorization_uses_explicit_github_proxy(self, store_type, client_type):
        client_type.return_value.start_device_authorization.side_effect = RuntimeError("stop")
        with self.assertRaisesRegex(RuntimeError, "stop"):
            authorize_active_credentials(
                Path("credentials.json"),
                open_browser=False,
                proxy_url="http://127.0.0.1:6268",
            )
        client_type.assert_called_once_with(
            store_type.return_value, proxy_url="http://127.0.0.1:6268"
        )

    @mock.patch("src.remote.github_app.GitHubAppClient")
    @mock.patch("credentials.CredentialStore")
    def test_credential_verification_uses_explicit_github_proxy(
        self, store_type, client_type
    ):
        store_type.return_value.has_secret.return_value = True
        client_type.return_value.verify_user_authorization.return_value = {
            "authorized": True,
            "installed": True,
        }
        client_type.return_value.check_worker_integrity.return_value = {
            "trusted": True,
            "actual_tree": "b" * 40,
        }
        state: dict = {}
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / "credentials.json"
            active.write_text('{"version":1}', encoding="utf-8")
            verify_active_credentials(
                state,
                active,
                context(),
                proxy_url="http://127.0.0.1:6268",
            )
        client_type.assert_called_once_with(
            store_type.return_value, proxy_url="http://127.0.0.1:6268"
        )
        self.assertEqual(state["credentials"]["status"], "verified")

    def test_gate_evidence_is_bound_to_current_context(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "evidence.json"
            current = context()
            artifact.write_text(json.dumps({
                "schema": GATE_EVIDENCE_SCHEMA,
                "gate": "encrypted_echo_cleanup",
                "status": "passed",
                "binding": gate_binding("encrypted_echo_cleanup", current),
            }), encoding="utf-8")
            state = {"gates": {}}
            record_gate(state, "encrypted_echo_cleanup", artifact, current)
            self.assertEqual(
                summary(state, current)["external"]["encrypted_echo_cleanup"], "passed"
            )
            self.assertEqual(
                summary(state, context(tree="f" * 40))["external"]["encrypted_echo_cleanup"],
                "pending",
            )
            value = summary(state, current)
            self.assertEqual(
                value["required_external"]["encrypted_echo_cleanup"], "passed"
            )
            self.assertEqual(
                set(value["optional_external"]),
                {
                    "complete_lecture",
                    "authorized_real_quality_set",
                    "clean_windows_onboarding",
                },
            )

    def test_browser_binding_ignores_unrelated_backend_change(self):
        self.assertEqual(
            gate_binding("browser_matrix", context(source="a" * 64)),
            gate_binding("browser_matrix", context(source="f" * 64)),
        )

    def test_incomplete_evidence_fails_closed(self):
        state = {"gates": {}}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence.json"
            self.assertFalse(write_external_evidence(state, context(), output))
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(set(payload["gates"]), set(EXTERNAL_GATES))
        self.assertTrue(all(row["status"] == "not_recorded" for row in payload["gates"].values()))

    def test_inventory_never_exposes_paths_or_secret_names(self):
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / "active.json"
            legacy = Path(directory) / "legacy.json"
            legacy.write_text(json.dumps({
                "accounts": {"private-account": {}},
                "secrets": {"github_app_access_token": {"value": "ciphertext"}},
            }), encoding="utf-8")
            rows = credential_inventory([legacy], active=active)
        rendered = json.dumps(rows)
        self.assertNotIn("private-account", rendered)
        self.assertNotIn("access_token", rendered)
        self.assertNotIn("legacy.json", rendered)
        self.assertTrue(rows[0]["has_oauth"])

    def test_retirement_requires_fresh_matching_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active = root / "active.json"
            legacy = root / "legacy.json"
            active.write_text('{"accounts":{},"secrets":{}}', encoding="utf-8")
            legacy.write_text('{"accounts":{},"secrets":{}}', encoding="utf-8")
            current = context()
            state = {"credentials": {}}
            with self.assertRaisesRegex(RuntimeError, "live verification"):
                retire_legacy_credentials(
                    state, active=active, candidates=[legacy], context=current
                )
            self.assertTrue(legacy.exists())
            state["credentials"] = {
                "status": "verified",
                "active_content_sha256": __import__("hashlib").sha256(active.read_bytes()).hexdigest(),
                "worker_binding": gate_binding("signed_worker_repair", current),
                "verified_at": time.time(),
            }
            retired = retire_legacy_credentials(
                state, active=active, candidates=[legacy], context=current,
                allowed_candidates=[legacy],
            )
            self.assertFalse(legacy.exists())
            self.assertEqual(retired[0]["status"], "retired")

    def test_retirement_refuses_changed_active_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active = root / "active.json"
            legacy = root / "legacy.json"
            active.write_text('{"accounts":{},"secrets":{}}', encoding="utf-8")
            legacy.write_text('{"accounts":{},"secrets":{}}', encoding="utf-8")
            state = {"credentials": {
                "status": "verified",
                "active_content_sha256": "0" * 64,
                "worker_binding": gate_binding("signed_worker_repair", context()),
                "verified_at": time.time(),
            }}
            with self.assertRaisesRegex(RuntimeError, "changed"):
                retire_legacy_credentials(
                    state, active=active, candidates=[legacy], context=context(),
                    allowed_candidates=[legacy],
                )
            self.assertTrue(legacy.exists())

    @mock.patch("scripts.final_acceptance._validate_migration_ciphertext")
    @mock.patch("credentials.CredentialStore.load_deepseek_key")
    @mock.patch("credentials.CredentialStore.load")
    @mock.patch("credentials.CredentialStore.load_secret")
    @mock.patch("credentials.CredentialStore.has_secret", return_value=False)
    @mock.patch("credentials.CredentialStore.list_accounts", return_value=[])
    def test_migration_copies_allowed_ciphertext_but_never_oauth(
        self, _list_accounts, _has_secret, _load_secret, _load,
        _load_deepseek_key, _validate_ciphertext,
    ):
        scratch = Path(__file__).resolve().parents[1] / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            root = Path(directory)
            active = root / "active.json"
            legacy = root / "legacy.json"
            active.write_text(json.dumps({
                "version": 1,
                "accounts": {},
                "secrets": {
                    "worker_mirror_root_signing_private:root": {"value": "keep"},
                    "network_github_proxy": {"value": "active-ciphertext"},
                },
            }), encoding="utf-8")
            legacy.write_text(json.dumps({
                "version": 1,
                "accounts": {"student": {"password": "ciphertext"}},
                "secrets": {
                    "deepseek_api_key": {"value": "ciphertext"},
                    "github_worker_repo": {"value": "ciphertext"},
                    "network_github_proxy": {"value": "legacy-ciphertext"},
                    "github_app_access_token": {"value": "stale"},
                    "github_app_refresh_token": {"value": "stale"},
                },
            }), encoding="utf-8")
            result = migrate_legacy_credentials(
                active=active, source=legacy, allowed_sources=[legacy]
            )
            payload = json.loads(active.read_text(encoding="utf-8"))
        self.assertIn("student", payload["accounts"])
        self.assertIn("deepseek_api_key", payload["secrets"])
        self.assertIn("github_worker_repo", payload["secrets"])
        self.assertNotIn("github_app_access_token", payload["secrets"])
        self.assertNotIn("github_app_refresh_token", payload["secrets"])
        self.assertEqual(
            payload["secrets"]["network_github_proxy"]["value"], "active-ciphertext"
        )
        self.assertEqual(result["preserved_active_secret_count"], 1)
        self.assertFalse(result["oauth_migrated"])


if __name__ == "__main__":
    unittest.main()
