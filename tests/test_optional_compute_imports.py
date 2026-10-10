import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IMPORTABLE_CLIENT_MODULES = (
    "src.app",
    "src.application",
    "src.runtime.http_api",
    "src.runtime.media_source",
    "src.remote.coordinator",
)

ISOLATED_IMPORT = r"""
import builtins
import importlib
import sys

blocked = {"imagehash", "numpy", "openai", "rapidocr_onnxruntime", "sherpa_onnx"}
original_import = builtins.__import__

def reject_optional(name, *args, **kwargs):
    if str(name).split(".", 1)[0] in blocked:
        raise ModuleNotFoundError(f"optional compute package blocked: {name}")
    return original_import(name, *args, **kwargs)

builtins.__import__ = reject_optional
importlib.import_module(sys.argv[1])
"""


class OptionalComputeImportTests(unittest.TestCase):
    def test_client_modules_do_not_import_optional_compute_packages_at_startup(self):
        for name in IMPORTABLE_CLIENT_MODULES:
            with self.subTest(module=name):
                result = subprocess.run(
                    [sys.executable, "-c", ISOLATED_IMPORT, name],
                    cwd=ROOT,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
