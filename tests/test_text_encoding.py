import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TextEncodingTests(unittest.TestCase):
    def test_repository_encoding_policy(self):
        result = subprocess.run(
            [sys.executable, "scripts/check_text_encoding.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
