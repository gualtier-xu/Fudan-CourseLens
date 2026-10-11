from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "frontend_idle_loop_behavior.mjs"


class FrontendIdleLoopBehaviorTests(unittest.TestCase):
    """PF1 稳态空闲循环降载：指纹短路负例钉 + store 去重语义钉（IDLE-LOOP）。"""

    def test_idle_ticks_skip_rebuild_but_real_changes_land(self):
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
            f"frontend idle loop behavior failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("frontend idle loop behavior passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
