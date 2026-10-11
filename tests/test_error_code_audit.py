"""U11 码表前后端等集对账：审计脚本钉（学生可见码必须有人话文案）。"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


class ErrorCodeAuditTests(unittest.TestCase):
    def test_audit_passes_with_zero_missing_copy(self):
        repo_root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [sys.executable, str(repo_root / "scripts" / "error_code_audit.py")],
            cwd=repo_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("missing copy: 0", completed.stdout)


if __name__ == "__main__":
    unittest.main()
