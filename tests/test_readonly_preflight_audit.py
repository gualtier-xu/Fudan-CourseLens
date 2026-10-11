from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from credentials import CredentialStore
from src.remote.github_app import GitHubAppClient, GitHubAppError
from src.runtime.task_store import TaskStore
from scripts import verify_migration_synthetic


class Credentials:
    """A byte-addressable credential envelope with mutation accounting."""

    def __init__(self, values=()):
        self.values = dict(values)
        self.calls = []

    def load_secret(self, name):
        return self.values[name]

    def has_secret(self, name):
        return name in self.values

    def save_secret(self, name, value):
        self.calls.append(("save", name))
        self.values[name] = str(value)

    def delete_secret(self, name):
        self.calls.append(("delete", name))
        return self.values.pop(name, None) is not None

    def update_secrets(self, updates, *, deletes=()):
        self.calls.append(("update", tuple(sorted(updates))))
        replacement = dict(self.values)
        replacement.update({name: str(value) for name, value in updates.items()})
        for name in deletes:
            replacement.pop(name, None)
        self.values = replacement

    def list_secret_names(self, *, prefix=""):
        return sorted(name for name in self.values if not prefix or name.startswith(prefix))

    def envelope(self):
        return repr(sorted(self.values.items())).encode()


class Response:
    def __init__(self, value):
        self.value = value
        self.status_code = 200

    def json(self):
        return self.value


class ReadOnlyPreflightAuditTests(unittest.TestCase):
    def _client(self, expires_at):
        credentials = Credentials((
            ("github_app_access_token", "token"),
            ("github_app_access_expires_at", expires_at),
            ("github_worker_repo", "student/Fudan-CourseLens-Worker"),
        ))
        return GitHubAppClient(credentials, client_id="id", app_slug="fudan-courselens"), credentials

    def test_access_token_read_only_rejects_every_nonfresh_expiry_without_refresh(self):
        for value in (None, "0", "NaN", "+Inf", "-Inf", "not-a-number"):
            with self.subTest(value=value):
                client, credentials = self._client(value)
                before = credentials.envelope()
                with patch.object(client, "refresh_access_token") as refresh:
                    with self.assertRaises(GitHubAppError) as raised:
                        client.access_token(minimum_lifetime_seconds=900, no_refresh=True)
                self.assertEqual(raised.exception.code, "authorization_not_fresh")
                refresh.assert_not_called()
                self.assertEqual(credentials.calls, [])
                self.assertEqual(credentials.envelope(), before)

    def test_read_only_integrity_success_and_failures_never_mutate_credential_envelope(self):
        client, credentials = self._client(str(time.time() + 3600))
        before = credentials.envelope()
        expected = {"commit": "a" * 40, "tree": "b" * 40, "manifest_sha256": "c" * 64}
        commit = Response({"sha": "a" * 40, "commit": {"tree": {"sha": "b" * 40}}})
        for failure in (None, GitHubAppError("bad release"), "tree-drift"):
            with self.subTest(failure=failure):
                credentials.calls.clear()
                with (
                    patch.object(client, "_validated_worker_release", side_effect=failure if isinstance(failure, Exception) else None, return_value=None if isinstance(failure, Exception) else expected),
                    patch.object(client, "_api", return_value=commit if failure != "tree-drift" else Response({"sha": "x" * 40, "commit": {"tree": {"sha": "y" * 40}}})),
                    patch("src.remote.github_app._bundled_worker_config", return_value={"tree": "b" * 40}),
                ):
                    if failure is None:
                        self.assertTrue(client.check_worker_integrity(read_only=True)["trusted"])
                    elif failure == "tree-drift":
                        self.assertFalse(client.check_worker_integrity(read_only=True)["trusted"])
                    else:
                        with self.assertRaises(GitHubAppError):
                            client.check_worker_integrity(read_only=True)
                self.assertEqual(credentials.calls, [])
                self.assertEqual(credentials.envelope(), before)

    def test_fresh_scope_rejects_slug_permission_and_discovered_id_drift_using_get_only(self):
        client, credentials = self._client(str(time.time() + 3600))
        before = credentials.envelope()
        base = {"id": 42, "app_slug": "fudan-courselens", "target_type": "User", "account": {"login": "student", "type": "User"}, "permissions": {"actions": "write"}}
        cases = (
            ({**base, "app_slug": "other"}, 42),
            ({**base, "permissions": {"actions": "read"}}, 42),
            (base, 43),
        )
        for installation, repository_id in cases:
            with self.subTest(installation=installation):
                calls = []
                def api(method, path, **_kwargs):
                    calls.append((method, path))
                    if path == "/user/installations":
                        return Response({"total_count": 1, "installations": [installation]})
                    return Response({"total_count": 2, "repositories": [
                        {"id": repository_id, "name": "Fudan-CourseLens-Worker", "owner": {"login": "student"}},
                        {"id": 3, "name": "Fudan-CourseLens-Mailbox", "owner": {"login": "student"}},
                    ]})
                with patch.object(client, "_api", side_effect=api):
                    if repository_id == 43:
                        # Fresh evidence is not compared with (or written to) a cached id.
                        self.assertEqual(client._inspect_fresh_user_installation("student", token="token", require_exact_repository_selection=True)["id"], 42)
                    else:
                        with self.assertRaises(GitHubAppError):
                            client._inspect_fresh_user_installation("student", token="token", require_exact_repository_selection=True)
                self.assertTrue(calls)
                self.assertTrue(all(method == "GET" for method, _ in calls))
                self.assertEqual(credentials.calls, [])
                self.assertEqual(credentials.envelope(), before)

    def test_read_only_taskstore_preserves_database_and_preexisting_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            TaskStore(path)
            for suffix, contents in (("", b""), ("-wal", b"wal"), ("-shm", b"shm")):
                candidate = Path(str(path) + suffix)
                if suffix:
                    candidate.write_bytes(contents)
            before = {candidate: (hashlib.sha256(candidate.read_bytes()).hexdigest(), candidate.stat().st_mtime_ns) for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"))}
            with self.assertRaisesRegex(RuntimeError, "active SQLite sidecars"):
                TaskStore(path, read_only=True)
            after = {candidate: (hashlib.sha256(candidate.read_bytes()).hexdigest(), candidate.stat().st_mtime_ns) for candidate in before}
            self.assertEqual(after, before)
            self.assertFalse(Path(str(path) + "-journal").exists())

    def test_read_only_taskstore_fails_closed_without_creating_or_migrating(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "new" / "missing.db"
            with self.assertRaises(FileNotFoundError):
                TaskStore(missing, read_only=True)
            self.assertFalse(missing.parent.exists())
            for name, sql in (("null.db", "CREATE TABLE tasks (task_id TEXT)"), ("old.db", "CREATE TABLE tasks (task_id TEXT)")):
                path = Path(directory) / name
                db = sqlite3.connect(path); db.execute(sql); db.commit(); db.close()
                before = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                with self.assertRaises(RuntimeError):
                    TaskStore(path, read_only=True)
                self.assertEqual((hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns), before)

    def test_device_token_envelope_is_atomic_when_update_fails(self):
        class FailingCredentials(Credentials):
            def update_secrets(self, updates, *, deletes=()):
                self.calls.append(("update", tuple(sorted(updates))))
                raise OSError("synthetic persistence failure")
        credentials = FailingCredentials((("existing", "unchanged"),))
        client = GitHubAppClient(credentials, client_id="id", app_slug="fudan-courselens")
        before = credentials.envelope()
        with self.assertRaises(OSError):
            client._save_tokens({"access_token": "secret", "expires_in": 900}, installation_id=42)
        self.assertEqual(credentials.envelope(), before)

    def test_fresh_installation_pagination_is_complete_and_rejects_count_or_duplicate_drift(self):
        client, credentials = self._client(str(time.time() + 3600))
        base = {"app_slug": "fudan-courselens", "target_type": "User", "account": {"login": "student", "type": "User"}, "permissions": {"actions": "write"}}

        def page(items, page):
            return items[(page - 1) * 100:page * 100]

        installations = [
            {**base, "id": index, "app_slug": "other"}
            for index in range(1, 102)
        ]
        installations[42] = {**base, "id": 43}
        repositories = [
            {"name": "Fudan-CourseLens-Worker", "owner": {"login": "student"}},
            {"name": "Fudan-CourseLens-Mailbox", "owner": {"login": "student"}},
        ]

        def api(method, path, *, params, **_kwargs):
            self.assertEqual(method, "GET")
            if path == "/user/installations":
                return Response({"total_count": len(installations), "installations": page(installations, params["page"])})
            return Response({"total_count": 2, "repositories": page(repositories, params["page"])})

        with patch.object(client, "_api", side_effect=api):
            self.assertEqual(client._inspect_fresh_user_installation("student", token="token", require_exact_repository_selection=True)["id"], 43)

        variants = (
            ("installation total", [{**base, "id": 43}], repositories, 2, 2),
            ("installation duplicate", [{**base, "id": 43}] * 101, repositories, 101, 2),
            ("repository total", [{**base, "id": 43}], repositories, 1, 3),
            ("repository duplicate", [{**base, "id": 43}], repositories * 34, 1, 68),
        )
        before = credentials.envelope()
        for _name, install_values, repo_values, install_total, repo_total in variants:
            with self.subTest(case=_name):
                def bad_api(method, path, *, params, **_kwargs):
                    values = install_values if path == "/user/installations" else repo_values
                    total = install_total if path == "/user/installations" else repo_total
                    return Response({"total_count": total, "installations" if path == "/user/installations" else "repositories": page(values, params["page"])})
                with patch.object(client, "_api", side_effect=bad_api):
                    with self.assertRaises(GitHubAppError):
                        client._inspect_fresh_user_installation("student", token="token", require_exact_repository_selection=True)
                self.assertEqual(credentials.calls, [])
                self.assertEqual(credentials.envelope(), before)

    def test_template_binding_integrity_read_only_fails_closed_without_updates(self):
        client, credentials = self._client(str(time.time() + 3600))
        credentials.values["github_worker_repo"] = "gualtier-xu-co/Fudan-CourseLens-Worker"
        before = credentials.envelope()
        with (
            patch("src.remote.github_app._bundled_worker_config", return_value={"repository": "gualtier-xu-co/Fudan-CourseLens-Worker", "tree": "b" * 40}),
            patch.object(client, "_validated_worker_release") as release,
            patch.object(client, "_api") as api,
            patch.object(client, "refresh_access_token") as refresh,
        ):
            with self.assertRaises(GitHubAppError) as raised:
                client.check_worker_integrity(read_only=True)
        self.assertEqual(raised.exception.code, "personal_worker_migration_required")
        release.assert_not_called()
        api.assert_not_called()
        refresh.assert_not_called()
        self.assertEqual(credentials.calls, [])
        self.assertEqual(credentials.envelope(), before)

    def test_real_credential_store_token_update_is_one_atomic_envelope_and_cleans_failed_replace(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=root) as directory:
            path = Path(directory) / "credentials.json"
            store = CredentialStore(path)
            with patch("credentials._protect", side_effect=lambda value: value.decode("utf-8")):
                client = GitHubAppClient(store, client_id="id", app_slug="fudan-courselens")
                with patch.object(store, "update_secrets", wraps=store.update_secrets) as update:
                    client._save_tokens({"access_token": "access", "expires_in": 900, "refresh_token": "refresh", "refresh_token_expires_in": 1800}, installation_id=42)
                self.assertEqual(update.call_count, 1)
                self.assertEqual(set(update.call_args.args[0]), {
                    "github_app_access_token", "github_app_access_expires_at", "github_app_refresh_token", "github_app_refresh_expires_at", "github_app_installation_id",
                })
                durable = path.read_bytes()
                with patch("credentials.os.replace", side_effect=OSError("replace failed")):
                    with self.assertRaises(OSError):
                        store.update_secrets({"github_app_access_token": "new"})
                self.assertEqual(path.read_bytes(), durable)
                self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_canonical_cancellation_preflight_rejects_discovered_installation_id_drift_without_go_or_writes(self):
        credentials = Credentials((
            ("process_canary_latch", json.dumps({"schema": "courselens.process-canary-latch.v2", "task_id": "a" * 32, "worker_commit": "a" * 40, "state": "complete", "run_id": 7, "attempt_cap": 2, "rerun_budget": 0})),
            ("remote_enabled", "0"), ("github_worker_repo", "student/target"),
            ("github_mailbox_repo", "student/mailbox"), ("github_app_installation_id", "42"),
        ))
        store = Mock()
        store.active_task_count.return_value = 0; store.active_remote_run_count.return_value = 0
        store.active_remote_attempt_count.return_value = 0; store.list_remote_token_leases.return_value = []
        store.active_automation_import_count.return_value = 0
        app = Mock(); app.access_token.return_value = "token"
        app.inspect_managed_resources.return_value = {
            "installation": {"installed": True, "installation_id": 43, "repository_selection_exact": True},
        }
        before = credentials.envelope()
        scope = (
            patch("scripts.verify_github_process_canary.build_preflight", return_value={"ready": True, "expected_worker_commit": "a" * 40, "zero_state": {"active_task_run_count": 0, "task_artifact_count": 0, "mailbox_managed_comment_count": 0, "mailbox_temporary_content_count": 0, "cleanup_pending_count": 0}}),
            patch("scripts.verify_migration_synthetic.replay_rejection", return_value={"status": "passed"}),
            patch("scripts.verify_migration_synthetic.tamper_rejection", return_value={"status": "passed"}),
            patch("scripts.verify_public_template_reconstruction.reconstruct", return_value={"status": "passed"}),
        )
        with scope[0], scope[1], scope[2], scope[3]:
            report = verify_migration_synthetic.read_only_cancellation_preflight(credentials=credentials, task_store=store, github_app=app)
        self.assertEqual(report["status"], "not_ready")
        self.assertFalse(report["checks"]["installation_id_matches_persisted"])
        self.assertNotIn("required_verifier_go", report)
        self.assertEqual(credentials.calls, [])
        self.assertEqual(credentials.envelope(), before)

    def test_canonical_cancellation_preflight_rejects_non_exact_installation_scope(self):
        credentials = Credentials((
            ("process_canary_latch", json.dumps({"schema": "courselens.process-canary-latch.v2", "task_id": "a" * 32, "worker_commit": "a" * 40, "state": "complete", "run_id": 7, "attempt_cap": 2, "rerun_budget": 0})),
            ("remote_enabled", "0"), ("github_worker_repo", "student/target"),
            ("github_mailbox_repo", "student/mailbox"), ("github_app_installation_id", "42"),
        ))
        store = Mock()
        store.active_task_count.return_value = 0; store.active_remote_run_count.return_value = 0
        store.active_remote_attempt_count.return_value = 0; store.list_remote_token_leases.return_value = []
        store.active_automation_import_count.return_value = 0
        app = Mock(); app.access_token.return_value = "token"
        app.inspect_managed_resources.return_value = {
            "installation": {"installed": True, "installation_id": 42, "repository_selection_exact": False},
        }
        scope = (
            patch("scripts.verify_github_process_canary.build_preflight", return_value={"ready": True, "expected_worker_commit": "a" * 40, "zero_state": {"active_task_run_count": 0, "task_artifact_count": 0, "mailbox_managed_comment_count": 0, "mailbox_temporary_content_count": 0, "cleanup_pending_count": 0}}),
            patch("scripts.verify_migration_synthetic.replay_rejection", return_value={"status": "passed"}),
            patch("scripts.verify_migration_synthetic.tamper_rejection", return_value={"status": "passed"}),
            patch("scripts.verify_public_template_reconstruction.reconstruct", return_value={"status": "passed"}),
        )
        with scope[0], scope[1], scope[2], scope[3]:
            report = verify_migration_synthetic.read_only_cancellation_preflight(credentials=credentials, task_store=store, github_app=app)
        self.assertEqual(report["status"], "not_ready")
        self.assertFalse(report["checks"]["installation_scope_exact"])
        self.assertNotIn("required_verifier_go", report)

    def test_canonical_preflight_fails_closed_when_snapshot_changes_or_second_read_fails(self):
        for after in (("changed",), RuntimeError("snapshot failed")):
            with self.subTest(after=after):
                credentials = Credentials((
                    ("process_canary_latch", json.dumps({"schema": "courselens.process-canary-latch.v2", "task_id": "a" * 32, "worker_commit": "a" * 40, "state": "complete", "run_id": 7, "attempt_cap": 2, "rerun_budget": 0})),
                    ("remote_enabled", "0"), ("github_worker_repo", "student/target"), ("github_mailbox_repo", "student/mailbox"), ("github_app_installation_id", "42"),
                ))
                store = Mock()
                store.readonly_snapshot.side_effect = [("stable",), after]
                store.active_task_count.return_value = 0; store.active_remote_run_count.return_value = 0
                store.active_remote_attempt_count.return_value = 0; store.list_remote_token_leases.return_value = []
                store.active_automation_import_count.return_value = 0
                app = Mock(); app.access_token.return_value = "token"
                app.inspect_managed_resources.return_value = {
                    "installation": {"installed": True, "installation_id": 42, "repository_selection_exact": True},
                }
                patches = (
                    patch("scripts.verify_github_process_canary.build_preflight", return_value={"ready": True, "expected_worker_commit": "a" * 40, "zero_state": {"active_task_run_count": 0, "task_artifact_count": 0, "mailbox_managed_comment_count": 0, "mailbox_temporary_content_count": 0, "cleanup_pending_count": 0}}),
                    patch("scripts.verify_migration_synthetic.replay_rejection", return_value={"status": "passed"}), patch("scripts.verify_migration_synthetic.tamper_rejection", return_value={"status": "passed"}), patch("scripts.verify_public_template_reconstruction.reconstruct", return_value={"status": "passed"}),
                )
                with patches[0], patches[1], patches[2], patches[3]:
                    report = verify_migration_synthetic.read_only_cancellation_preflight(credentials=credentials, task_store=store, github_app=app)
                self.assertEqual(report["status"], "not_ready")
                self.assertFalse(report["checks"]["local_db_snapshot_stable"])
                self.assertNotIn("required_verifier_go", report)

    def test_canonical_cancellation_preflight_real_rerun_history_is_ready_and_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            writable = TaskStore(database)
            writable.upsert_remote_run("canary", repository="student/target", workflow="process.yml", run_id=7, attempt=2, remote_state="imported")
            writable.upsert_remote_attempt("canary", 1, run_id=7, github_status="completed", conclusion="failure", worker_status="failed", import_state="failed", cleanup_state="best_effort")
            writable.upsert_remote_attempt("canary", 2, run_id=7, github_status="completed", conclusion="success", import_state="imported", cleanup_state="complete")
            writable.close()
            store = TaskStore(database, read_only=True)
            credentials = Credentials((
                ("process_canary_latch", json.dumps({"schema": "courselens.process-canary-latch.v2", "task_id": "a" * 32, "worker_commit": "a" * 40, "state": "complete", "run_id": 7, "attempt_cap": 2, "rerun_budget": 0})), ("remote_enabled", "0"),
                ("github_worker_repo", "student/target"), ("github_mailbox_repo", "student/mailbox"), ("github_app_installation_id", "42"),
            ))
            app = Mock(); app.access_token.return_value = "token"
            app.inspect_managed_resources.return_value = {
                "installation": {"installed": True, "installation_id": 42, "repository_selection_exact": True},
            }
            before = (hashlib.sha256(database.read_bytes()).hexdigest(), database.stat().st_mtime_ns, credentials.envelope())
            patches = (
                patch("scripts.verify_github_process_canary.build_preflight", return_value={"ready": True, "expected_worker_commit": "a" * 40, "zero_state": {"active_task_run_count": 0, "task_artifact_count": 0, "mailbox_managed_comment_count": 0, "mailbox_temporary_content_count": 0, "cleanup_pending_count": 0}}),
                patch("scripts.verify_migration_synthetic.replay_rejection", return_value={"status": "passed"}), patch("scripts.verify_migration_synthetic.tamper_rejection", return_value={"status": "passed"}), patch("scripts.verify_public_template_reconstruction.reconstruct", return_value={"status": "passed"}),
            )
            with patches[0], patches[1], patches[2], patches[3]:
                report = verify_migration_synthetic.read_only_cancellation_preflight(credentials=credentials, task_store=store, github_app=app)
            self.assertEqual(report["status"], "ready", report)
            self.assertEqual(report["required_verifier_go"], verify_migration_synthetic.GO["cancellation"])
            self.assertEqual((hashlib.sha256(database.read_bytes()).hexdigest(), database.stat().st_mtime_ns, credentials.envelope()), before)
            self.assertEqual(credentials.calls, [])
            self.assertFalse(Path(str(database) + "-wal").exists())
            self.assertFalse(Path(str(database) + "-shm").exists())


class SignedTemplateDirectPreflightScriptTests(unittest.TestCase):
    """The retirement-audit script must wire an immutable TaskStore and preserve the store."""

    def test_script_reports_closed_status_when_task_store_is_uninspectable(self):
        import contextlib
        import io
        import scripts.signed_template_direct_preflight as preflight
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            buffer = io.StringIO()
            with patch.object(preflight, "DEFAULT_DATA_DIR", data_dir), \
                 patch.object(preflight, "CredentialStore") as credential_store, \
                 patch.object(preflight, "GitHubAppClient"), \
                 patch.object(preflight, "build_signed_template_retirement_audit", side_effect=AttributeError("'NoneType' object has no attribute 'list_remote_token_leases'")), \
                 contextlib.redirect_stdout(buffer):
                exit_code = preflight.main([])
            credential_store.assert_called_once_with(data_dir.resolve() / "credentials.json")
            report = json.loads(buffer.getvalue())
            self.assertEqual(report["status"], "task_store_unavailable")
            self.assertFalse(report["ready"])
            self.assertEqual(exit_code, 1)

    def test_script_wires_read_only_taskstore(self):
        import contextlib
        import io
        import scripts.signed_template_direct_preflight as preflight
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "state.db").write_bytes(b"")
            buffer = io.StringIO()
            with patch.object(preflight, "DEFAULT_DATA_DIR", data_dir), \
                 patch.object(preflight, "CredentialStore") as credential_store, \
                 patch.object(preflight, "TaskStore") as task_store_factory, \
                 patch.object(preflight, "GitHubAppClient"), \
                 patch.object(preflight, "build_signed_template_retirement_audit", return_value={"ready": True}) as audit, \
                 contextlib.redirect_stdout(buffer):
                exit_code = preflight.main([])
            task_store_factory.assert_called_once_with(data_dir.resolve() / "state.db", read_only=True)
            audit.assert_called_once()
            self.assertEqual(exit_code, 0)

    def test_script_preserves_store_bytes_and_creates_no_sidecars(self):
        import os
        import subprocess
        import sys
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "state.db"
            TaskStore(database).close()
            before = database.read_bytes()
            environment = dict(os.environ, COURSELENS_DATA_DIR=str(tmp), PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run(
                [sys.executable, "-m", "scripts.signed_template_direct_preflight"],
                cwd=root, env=environment, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("authorization_missing", result.stdout)
            self.assertEqual(database.read_bytes(), before)
            self.assertFalse(Path(str(database) + "-wal").exists())
            self.assertFalse(Path(str(database) + "-shm").exists())


class VerifyKeyRecoveryTests(unittest.TestCase):
    """The recovery challenge proves real material signs and verifies, leaking nothing."""

    def _run_tool(self, argv, data_dir, secret=None):
        import os
        import subprocess
        import sys
        root = Path(__file__).resolve().parents[1]
        environment = dict(os.environ, COURSELENS_DATA_DIR=str(data_dir), PYTHONDONTWRITEBYTECODE="1")
        if secret is not None:
            name, material = secret
            seed_code = (
                "import os, sys\n"
                "from credentials import CredentialStore\n"
                "store = CredentialStore(os.path.join(os.environ['COURSELENS_DATA_DIR'], 'credentials.json'))\n"
                "store.save_secret(sys.argv[1], sys.stdin.read().strip())\n"
            )
            seeded = subprocess.run(
                [sys.executable, "-c", seed_code, name],
                input=material, capture_output=True, text=True, env=environment, cwd=root,
            )
            assert seeded.returncode == 0, seeded.stderr
        tool_run = subprocess.run(
            [sys.executable, "-m", "scripts.verify_key_recovery", *argv],
            capture_output=True, text=True, env=environment, cwd=root,
        )
        return tool_run.returncode, tool_run.stdout

    def test_roundtrip_signs_and_verifies_without_leaking_material(self):
        import base64
        from nacl.signing import SigningKey
        key = SigningKey.generate()
        material = base64.b64encode(bytes(key)).decode("ascii")
        expected_public = base64.b64encode(bytes(key.verify_key)).decode("ascii")
        with tempfile.TemporaryDirectory() as tmp:
            exit_code, output = self._run_tool(
                ["--secret-name", "worker_mirror_root_signing_private:test", "--expected-public", expected_public],
                tmp, secret=("worker_mirror_root_signing_private:test", material),
            )
        report = json.loads(output)
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["schema"], "courselens.key-recovery-challenge.v1")
        self.assertEqual(report["verification"], "passed")
        self.assertTrue(report["expected_public_matched"])
        self.assertTrue(report["local_copies_zeroized"])
        self.assertRegex(report["challenge_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(report["public_key_fingerprint"], r"^[0-9a-f]{32}$")
        self.assertNotIn(material, output)

    def test_invalid_base64_expected_public_fails_closed(self):
        import base64
        from nacl.signing import SigningKey
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "escrow.b64").write_text(
                base64.b64encode(bytes(SigningKey.generate())).decode("ascii"), encoding="ascii",
            )
            exit_code, output = self._run_tool(
                ["--file", str(Path(tmp) / "escrow.b64"), "--expected-public", "not+valid@base64!!"], tmp,
            )
        report = json.loads(output)
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["verification"], "failed")
        self.assertFalse(report["expected_public_matched"])

    def test_wrong_expected_public_fails_closed(self):
        import base64
        from nacl.signing import SigningKey
        material = base64.b64encode(bytes(SigningKey.generate())).decode("ascii")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "escrow.b64").write_text(material, encoding="ascii")
            exit_code, output = self._run_tool(
                ["--file", str(Path(tmp) / "escrow.b64"), "--expected-public", base64.b64encode(bytes(SigningKey.generate().verify_key)).decode("ascii")],
                tmp,
            )
        report = json.loads(output)
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["verification"], "failed")
        self.assertFalse(report["expected_public_matched"])

    def test_tampered_embedded_public_fails_closed(self):
        import base64
        from nacl.signing import SigningKey
        blob = bytes(SigningKey.generate()) + bytes(SigningKey.generate().verify_key)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "escrow.raw").write_bytes(blob)
            exit_code, output = self._run_tool(["--file", str(Path(tmp) / "escrow.raw")], tmp)
        report = json.loads(output)
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["verification"], "failed")

    def test_material_absent_is_reported_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            exit_code, output = self._run_tool(
                ["--secret-name", "worker_mirror_root_signing_private:absent"], tmp,
            )
        report = json.loads(output)
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["verification"], "material_absent")
        self.assertTrue(report["local_copies_zeroized"])

    def test_uninspectable_store_material_reports_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "credentials.json").write_text("not-json", encoding="ascii")
            exit_code, output = self._run_tool(
                ["--secret-name", "worker_mirror_root_signing_private:test"], tmp,
            )
        report = json.loads(output)
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["verification"], "failed")
        self.assertIn("not valid JSON", output)


if __name__ == "__main__":
    unittest.main()
