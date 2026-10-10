from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "frontend_focus_policy_behavior.mjs"


class FrontendFocusPolicyBehaviorTests(unittest.TestCase):
    def test_focus_policy_chokepoint_matrix_and_wiring(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for frontend behavior tests")
        result = subprocess.run(
            [str(node), str(HARNESS)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"frontend focus policy behavior failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("frontend focus policy behavior passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
