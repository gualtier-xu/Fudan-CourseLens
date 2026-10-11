from __future__ import annotations

import json

import pytest

from scripts.local_performance_baseline import main


def test_synthetic_baseline_has_required_metrics_and_cleans_up(tmp_path):
    output = tmp_path / "output"
    assert main([
        "--output-dir", str(output),
        "--repetitions", "5",
        "--synthetic-rows", "50",
    ]) == 0
    result = json.loads((output / "performance-baseline.json").read_text(encoding="utf-8"))
    assert set(result["metrics"]) == {
        "cold_start", "warm_start", "empty_database", "synthetic_database",
        "search", "course_switch", "media_first_byte", "exit",
    }
    assert result["isolation"]["synthetic_only"] is True
    assert all(metric["runs"] == 5 for metric in result["metrics"].values())
    assert not list(output.glob("courselens-perf-*"))


def test_requires_five_repetitions_and_explicit_output(tmp_path):
    with pytest.raises(SystemExit):
        main(["--output-dir", str(tmp_path), "--repetitions", "4"])
    with pytest.raises(SystemExit):
        main([])
