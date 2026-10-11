from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "frontend_a11y_shell_behavior.mjs"


class FrontendA11yShellBehaviorTests(unittest.TestCase):
    def test_real_shell_markup_and_a11y_css_honor_skip_link_and_group_roles(self):
        """A11Y-IMPL-2（D14 审计 P2-4/P3-3）：skip link 首停显形落焦 main、
        批量操作分组 span 全带 role=group——读真件行为钉见 mjs。"""
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
            f"frontend a11y shell behavior failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("frontend a11y shell behavior passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
