from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "frontend_settings_reset_behavior.mjs"


class FrontendSettingsResetBehaviorTests(unittest.TestCase):
    def test_real_settings_module_drives_the_client_reset_flow(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for frontend behavior tests")
        result = subprocess.run(
            [str(node), str(HARNESS)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"frontend settings reset behavior failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("frontend settings reset behavior passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
