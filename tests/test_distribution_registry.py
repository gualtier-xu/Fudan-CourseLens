"""Pins for the distribution registry and its drift checker.

The registry (``config/distribution.json`` via ``src/distribution.py``) is
the single source of truth for repository identities on rebuild day; the
checker must stay green on a compliant tree and fail with file:line reports
when a literal tries to come back.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CHECKER = REPO / "scripts" / "check_distribution_references.py"


def test_registry_file_matches_packaging_defaults():
    sys.path.insert(0, str(REPO))
    from src.distribution import DEFAULT_REGISTRY, REGISTRY_PATH

    file_registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    assert file_registry == DEFAULT_REGISTRY


def test_load_registry_missing_file_falls_back_to_defaults(tmp_path):
    sys.path.insert(0, str(REPO))
    from src.distribution import DEFAULT_REGISTRY, load_registry

    assert load_registry(tmp_path / "absent.json") == DEFAULT_REGISTRY


def test_load_registry_fails_closed_on_unknown_keys(tmp_path):
    sys.path.insert(0, str(REPO))
    from src.distribution import DEFAULT_REGISTRY, load_registry

    bad = tmp_path / "distribution.json"
    bad.write_text(json.dumps({**{k: v for k, v in DEFAULT_REGISTRY.items()},
                               "extra": 1}), encoding="utf-8")
    with pytest.raises(RuntimeError):
        load_registry(bad)


def test_checker_passes_on_current_tree():
    result = subprocess.run(
        [sys.executable, str(CHECKER)],
        check=False, capture_output=True, text=True, cwd=str(REPO),
    )
    assert result.returncode == 0, f"checker reported drift:\n{result.stdout}"


def test_checker_reports_drift_with_file_and_line(tmp_path):
    scratch = tmp_path / "probe.py"
    scratch.write_text(
        'REPO = "gualtier-xu-co/Fudan-CourseLens-Worker"\n', encoding="utf-8"
    )
    # The checker scans the repository tree; simulate drift by pointing the
    # scan at a scratch copy of src/ via the same code path is intrusive, so
    # assert the reporting contract instead: a synthetic drift line formats
    # as file:line and exits non-zero.
    import shutil

    sandbox = tmp_path / "repo"
    sandbox.mkdir()
    for item in ("scripts", "src", "config", "frontend"):
        shutil.copytree(REPO / item, sandbox / item,
                        ignore=shutil.ignore_patterns("__pycache__"))
    (sandbox / "src" / "remote" / "drift_probe.py").write_text(
        'PRIVATE = "gualtier-xu-co/Fudan-CourseLens-Private"\n', encoding="utf-8"
    )
    result = subprocess.run(
        [sys.executable, str(sandbox / "scripts" / "check_distribution_references.py")],
        check=False, capture_output=True, text=True, cwd=str(sandbox),
    )
    assert result.returncode == 1
    assert "src/remote/drift_probe.py:1:" in result.stdout
    assert "DRIFT:" in result.stdout
