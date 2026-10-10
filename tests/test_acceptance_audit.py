from __future__ import annotations

import inspect
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.final_acceptance_audit import (
    EVIDENCE_SCHEMA,
    REQUIRED_EXTERNAL_GATES,
    ROOT,
    build_report,
)


class AcceptanceAuditTests(unittest.TestCase):
    def test_help_does_not_run_the_audit(self):
        path = Path(__file__).parents[1] / "scripts" / "final_acceptance_audit.py"
        result = subprocess.run(
            [sys.executable, str(path), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)
        self.assertNotIn("courselens.final-acceptance-audit", result.stdout)

    def test_audit_is_read_only_redacted_and_reports_release_gates(self):
        path = Path(__file__).parents[1] / "scripts" / "final_acceptance_audit.py"
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("state.db", "learning.db"):
                database = sqlite3.connect(Path(tmp) / name)
                database.execute("create table acceptance_fixture(id integer primary key)")
                if name == "state.db":
                    database.execute("create table catalog_courses(course_id text primary key)")
                    database.execute("create table catalog_lectures(sub_id text primary key)")
                database.commit()
                database.close()
            env = dict(os.environ)
            env["COURSELENS_DATA_DIR"] = tmp
            result = subprocess.run(
                [sys.executable, str(path), "--cache-root", str(Path(tmp) / "clean-cache")],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                env=env,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["schema"], "courselens.final-acceptance-audit.v2")
        self.assertIn("cleanup_gate", report)
        self.assertFalse(report["cleanup_gate"]["eligible"])
        self.assertFalse(report["release_ready"])
        self.assertEqual(
            report["databases"]["state.db"]["catalog_counts"],
            {"courses": 0, "lectures": 0},
        )
        self.assertIn(report["local_compute_probe"], {"ok", "not_applicable"})
        self.assertNotIn("course_title", result.stdout)
        self.assertNotIn("CommandLine", result.stdout)
        for root in report["active_media"]["roots"].values():
            for digest in root["sample_path_sha256"]:
                self.assertEqual(len(digest), 64)
            self.assertNotIn("items", root)

    def test_default_cache_root_stays_package_runtime_cache(self):
        # 普查决策点 3（A 方案）收编：cache 根 CLI 参数化，但默认保持 ROOT/runtime/cache
        # 向后兼容。默认路径行为由本钉静态固化；套件内审计调用一律指向干净临时根，
        # 不再对真实共享 cache 根做活体扫描（夜普查 soak 残留×审计误红交叉污染收编）。
        default = inspect.signature(build_report).parameters["cache_root"].default
        self.assertEqual(default, ROOT / "runtime" / "cache")

    def test_missing_databases_fail_closed(self):
        path = Path(__file__).parents[1] / "scripts" / "final_acceptance_audit.py"
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["COURSELENS_DATA_DIR"] = tmp
            result = subprocess.run(
                [sys.executable, str(path), "--cache-root", str(Path(tmp) / "clean-cache")],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                env=env,
            )
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["local_gates"]["database_integrity"])
        self.assertFalse(report["local_audit_passed"])

    def test_complete_hashed_evidence_enables_accelerated_release_gate(self):
        path = Path(__file__).parents[1] / "scripts" / "final_acceptance_audit.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("state.db", "learning.db"):
                database = sqlite3.connect(root / name)
                try:
                    database.execute("create table acceptance_fixture(id integer primary key)")
                    if name == "state.db":
                        database.execute("create table catalog_courses(course_id text primary key)")
                        database.execute("create table catalog_lectures(sub_id text primary key)")
                    database.commit()
                finally:
                    database.close()
            evidence = root / "evidence.json"
            evidence.write_text(json.dumps({
                "schema": EVIDENCE_SCHEMA,
                "gates": {
                    gate: {"status": "passed", "evidence_sha256": "a" * 64}
                    for gate in REQUIRED_EXTERNAL_GATES
                },
            }), encoding="utf-8")
            output = root / "report.json"
            env = dict(os.environ)
            env["COURSELENS_DATA_DIR"] = tmp
            result = subprocess.run(
                [
                    sys.executable,
                    str(path),
                    "--evidence",
                    str(evidence),
                    "--output",
                    str(output),
                    "--cache-root",
                    str(root / "clean-cache"),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                env=env,
            )
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(report["cleanup_gate"]["eligible"])
        self.assertTrue(report["release_ready"])
        self.assertEqual(report["cleanup_gate"]["required_days"], 0)

    def test_installed_package_gate_fails_when_runtime_is_missing(self):
        path = Path(__file__).parents[1] / "scripts" / "final_acceptance_audit.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("state.db", "learning.db"):
                database = sqlite3.connect(root / name)
                database.close()
            env = dict(os.environ)
            env["COURSELENS_DATA_DIR"] = tmp
            result = subprocess.run(
                [
                    sys.executable,
                    str(path),
                    "--package-root",
                    tmp,
                    "--cache-root",
                    str(root / "clean-cache"),
                    "--require-installed-package",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                env=env,
            )
            report = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(report["package"]["complete"])
        self.assertFalse(report["local_gates"]["installed_package_complete"])

    def test_installed_package_size_includes_generated_bytecode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            for name in ("state.db", "learning.db"):
                database = sqlite3.connect(data / name)
                try:
                    database.execute("create table acceptance_fixture(id integer primary key)")
                    if name == "state.db":
                        database.execute("create table catalog_courses(course_id text primary key)")
                        database.execute("create table catalog_lectures(sub_id text primary key)")
                    database.commit()
                finally:
                    database.close()

            bytecode = root / "tools" / "python312" / "lib" / "__pycache__" / "module.pyc"
            bytecode.parent.mkdir(parents=True)
            bytecode.write_bytes(b"12345")
            python = root / "tools" / "python312" / "python.exe"
            python.write_bytes(b"67")

            with mock.patch.dict(os.environ, {"COURSELENS_DATA_DIR": str(data)}):
                report = build_report(
                    package_root=root,
                    cache_root=root / "clean-cache",
                    require_installed_package=True,
                )

        self.assertTrue(report["package"]["complete"])
        self.assertEqual(report["package"]["bytes"], 7)


if __name__ == "__main__":
    unittest.main()
