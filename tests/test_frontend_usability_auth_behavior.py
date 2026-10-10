from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "frontend_usability_auth_behavior.mjs"


class FrontendUsabilityAuthBehaviorTests(unittest.TestCase):
    def test_real_modules_handle_drawer_wordmark_and_login_dialog(self):
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
            f"frontend usability/auth behavior failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("frontend usability/auth behavior passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
