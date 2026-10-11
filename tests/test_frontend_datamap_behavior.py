from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "frontend_datamap_behavior.mjs"


class FrontendDataMapBehaviorTests(unittest.TestCase):
    def test_real_settings_family_drives_the_data_map_panel(self):
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
            f"frontend data map behavior failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("frontend data map behavior passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
