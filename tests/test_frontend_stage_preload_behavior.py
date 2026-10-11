from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "frontend_stage_preload_behavior.mjs"


class FrontendStagePreloadBehaviorTests(unittest.TestCase):
    """PERF-C23 舞台级预载提示行为钉（夜批15 R2 C2-3 定谳落地）。"""

    def test_real_module_drives_stage_preload_hint(self):
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
            f"frontend stage preload behavior failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("frontend stage preload behavior passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
