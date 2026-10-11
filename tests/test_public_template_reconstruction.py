"""Black-box coverage for the offline reconstruction gate.

The production fixture is a real pair of Git object databases; the test never
mocks Git, signatures, or reconstruction primitives.
"""
from __future__ import annotations
import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROOT = ROOT.parents[1] / "public" / "worker-mirror"
PYTHON = ROOT.parents[0] / "main" / ".venv-client-py310" / "Scripts" / "python.exe"

class ReconstructionGateTests(unittest.TestCase):
    def command(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(PYTHON if PYTHON.is_file() else sys.executable), "scripts/verify_public_template_reconstruction.py", *args], cwd=ROOT, text=True, capture_output=True)

    def test_pinned_real_git_objects_pass_with_recomputable_evidence(self):
        if not PUBLIC_ROOT.is_dir():
            self.skipTest("production public Worker mirror is absent from this checkout layout; run from a drill layout with the pinned sibling template")
        result = self.command("--go", "WORKER-RECONSTRUCTION-GO")
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(result.stdout)
        self.assertEqual(evidence["status"], "passed")
        digest = evidence.pop("evidence_sha256")
        self.assertEqual(digest, hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        self.assertNotEqual(evidence["payload_git_tree"], evidence["public_tree"])

    def test_wrong_token_does_not_run_reconstruction(self):
        result = self.command("--go", "wrong")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exact verifier GO token", result.stderr)

if __name__ == "__main__": unittest.main()
