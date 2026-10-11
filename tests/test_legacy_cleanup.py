import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from scripts.cleanup_legacy import (
    ACCEPTANCE_GATES,
    ACCEPTANCE_REPORT_SCHEMA,
    _acceptance_summary,
    _catalog_evidence,
    _create_safety_backup,
    _processes,
    _remove_allowlisted_path,
    WORKSPACE_TARGETS,
    scan,
    validate,
)


class LegacyCleanupTests(unittest.TestCase):
    def test_scan_is_audit_only_and_names_are_hashed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "downloads" / "nested").mkdir(parents=True)
            (root / "downloads" / "nested" / "old.mp4").write_bytes(b"old")
            report = scan(root)
            self.assertEqual(report["file_count"], 1)
            self.assertEqual(report["bytes"], 3)
            self.assertEqual(len(report["relative_name_sha256"]), 64)
            self.assertNotIn("files", report)
            self.assertNotIn("old.mp4", json.dumps(report))

    def test_scan_includes_only_allowlisted_workspace_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / "private" / "online-finalization"
            root.mkdir(parents=True)
            target = workspace / "archive" / "rollback-media-20260720"
            target.mkdir(parents=True)
            (target / "old.bin").write_bytes(b"retired")
            protected = workspace / "managed" / "personal-worker"
            protected.mkdir(parents=True)
            (protected / "keep.bin").write_bytes(b"keep")

            report = scan(root)

            self.assertEqual(tuple(report["workspace_targets"]), WORKSPACE_TARGETS)
            self.assertEqual(report["workspace_entries"][0]["bytes"], 7)
            self.assertNotIn("keep.bin", json.dumps(report))

    def test_protected_paths_fail_closed(self):
        report = {"targets": ["runtime/data"], "processes": [], "state_db_integrity": "missing"}
        self.assertTrue(validate(report))

    def test_cleanup_refuses_without_release_ready_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            (root / "src").mkdir()
            (root / "runtime-assets.json").write_text("{}\n", encoding="utf-8")
            report = scan(root)
        self.assertIn(
            "final real acceptance report is missing or not release-ready",
            validate(report),
        )

    @patch("scripts.cleanup_legacy.subprocess.check_output")
    def test_process_probe_failure_blocks_cleanup_without_command_details(self, check_output):
        check_output.side_effect = subprocess.TimeoutExpired("process-probe", 10)
        rows = _processes(Path.cwd())
        self.assertEqual(rows, [{"pid": "", "kind": "process_probe_failed"}])
        self.assertNotIn("command", rows[0])

    def test_catalog_evidence_reads_state_database_without_legacy_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "runtime" / "data"
            data.mkdir(parents=True)
            database = sqlite3.connect(data / "state.db")
            try:
                database.execute("create table catalog_courses(course_id text primary key)")
                database.execute("create table catalog_lectures(sub_id text primary key)")
                database.executemany(
                    "insert into catalog_courses values(?)", [("1",), ("2",)]
                )
                database.execute("insert into catalog_lectures values('lecture')")
                database.commit()
            finally:
                database.close()
            evidence = _catalog_evidence(root)

        self.assertTrue(evidence["database_readable"])
        self.assertTrue(evidence["matches"])
        self.assertEqual(evidence["database_courses"], 2)
        self.assertEqual(evidence["database_lectures"], 1)

    def test_acceptance_without_bound_catalog_counts_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "acceptance.json"
            path.write_text(json.dumps({
                "schema": ACCEPTANCE_REPORT_SCHEMA,
                "external_gates": {
                    gate: {"status": "passed", "evidence_sha256": "a" * 64}
                    for gate in ACCEPTANCE_GATES
                },
                "local_audit_passed": True,
                "release_ready": True,
                "databases": {"state.db": {}},
            }), encoding="utf-8")
            summary = _acceptance_summary(path)

        self.assertFalse(summary["catalog_counts_recorded"])
        self.assertFalse(summary["release_ready"])

    @patch("scripts.cleanup_legacy._restrict_backup_access")
    def test_safety_backup_copies_only_state_and_ciphertext(self, restrict_access):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "runtime" / "data"
            data.mkdir(parents=True)
            with closing(sqlite3.connect(data / "state.db")) as database:
                database.execute("create table catalog_courses(course_id text primary key)")
                database.commit()
            ciphertext = b'{"schema":"encrypted-test-only"}\n'
            (data / "credentials.json").write_bytes(ciphertext)

            evidence = _create_safety_backup(root)

            backup = data / "online-only-pre-cleanup-backup"
            self.assertEqual((backup / "credentials.json").read_bytes(), ciphertext)
            self.assertEqual(_catalog_evidence(root)["database_courses"], 0)
            self.assertEqual(evidence["schema"], "courselens.pre-cleanup-backup.v1")
            self.assertEqual(set(evidence["files"]), {"state", "credentials"})
            self.assertEqual(len(evidence["files"]["credentials"]["sha256"]), 64)
            restrict_access.assert_called_once_with(backup)

    def test_allowlisted_removal_handles_readonly_files(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "retired"
            target.mkdir()
            readonly = target / "packed.idx"
            readonly.write_bytes(b"retired")
            readonly.chmod(0o444)

            _remove_allowlisted_path(target)

            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
