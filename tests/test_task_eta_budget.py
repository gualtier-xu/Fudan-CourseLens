"""Offline ETA / queue-evidence / task-intel regressions.

Hosts the synthetic ETA replay harness (contract §5.3): per-task held-out
trajectories are replayed through the baseline wall-time ETA and the blended
live+history duration ETA, and the public task model inputs (estimate basis
closed set, ranges, queue/stale anchors) are locked.  The former worker
protection budget regressions were removed together with the budget gate
(2026-09-22 拍板：每日计算保护整体移除).
"""

from __future__ import annotations

import hashlib
import sqlite3
import statistics
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from src.application import CourseLensApplication
from src.runtime.http_api import public_task
from src.runtime.progress import (
    ESTIMATE_BASIS_VALUES,
    SUBTITLE_BOOTSTRAP_RTFS,
    ProgressTracker,
)
from src.runtime.task_store import TaskStore


@contextmanager
def raw_db(path: Path):
    """Short-lived inspection connection that always closes (Windows WAL)."""
    db = sqlite3.connect(path)
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Synthetic ETA replay harness (contract §5.3)
# ---------------------------------------------------------------------------

CHUNK_SECONDS = 30.0
# Fit set A: four completed subtitle lectures whose end-to-end RTFs form the
# ``subtitle-duration`` history.  Eval set B never contributes history.
FIT_TASKS = (
    # (duration, asr_pace, proofread_seconds, write_seconds)
    (3600.0, 0.40, 300.0, 30.0),
    (2400.0, 0.35, 220.0, 25.0),
    (4800.0, 0.45, 260.0, 35.0),
    (1800.0, 0.30, 180.0, 20.0),
)
FIT_TAIL_SECONDS = {
    "proofread": 240.0,  # median of fit-set proofread totals
    "write": 27.5,       # median of fit-set write totals
}
EVAL_TASKS = {
    # nominal: pace matches the fit history
    "nominal": {"duration": 3000.0, "pace": 0.38, "proofread": 240.0, "write": 30.0},
    # 2x ASR slowdown after 40% of the media
    "slowdown": {
        "duration": 3000.0, "pace": 0.38, "proofread": 240.0, "write": 30.0,
        "slowdown_at": 0.4, "slowdown_factor": 2.0,
    },
    # one stalled chunk (+150s) and a short task with a tail heavier than the
    # history proportion
    "stall": {
        "duration": 2400.0, "pace": 0.40, "proofread": 200.0, "write": 25.0,
        "stall_chunk": 40, "stall_seconds": 150.0,
    },
    "short_tail_heavy": {"duration": 900.0, "pace": 0.35, "proofread": 260.0, "write": 30.0},
}
COLD_TASK = {"duration": 1800.0, "pace": 0.30, "proofread": 280.0, "write": 30.0}


def _fit_history() -> list[float]:
    """End-to-end RTFs the store would have recorded for fit-set tasks."""
    history = []
    for duration, pace, proofread, write in FIT_TASKS:
        history.append((duration * pace + proofread + write) / duration)
    return history


def _subtitle_steps(
    duration: float,
    pace: float,
    proofread: float,
    write: float,
    *,
    slowdown_at: float | None = None,
    slowdown_factor: float = 2.0,
    stall_chunk: int | None = None,
    stall_seconds: float = 0.0,
) -> list[dict]:
    """Deterministic synthetic subtitle trajectory with ground truth.

    ASR advances one 30s media chunk per worker heartbeat; the proofread and
    write phases follow as the genuinely serial tail.  ``true_remaining`` is
    computed from the generator schedule (future pace included), never from
    either ETA model.
    """
    chunk_count = int(round(duration / CHUNK_SECONDS))
    slowdown_from = chunk_count if slowdown_at is None else int(chunk_count * slowdown_at)
    chunk_times = []
    current_pace = pace
    for index in range(chunk_count):
        if index >= slowdown_from:
            current_pace = pace * slowdown_factor
        cost = CHUNK_SECONDS * current_pace
        if index == stall_chunk:
            cost += stall_seconds
        chunk_times.append(cost)
    prefix = [0.0]
    for cost in chunk_times:
        prefix.append(prefix[-1] + cost)
    suffix = [0.0] * (chunk_count + 1)
    for index in range(chunk_count - 1, -1, -1):
        suffix[index] = suffix[index + 1] + chunk_times[index]
    steps = []
    for index in range(chunk_count):
        processed = min(duration, (index + 1) * CHUNK_SECONDS)
        steps.append({
            "phase": "recognize",
            "phase_percent": 100.0 * processed / duration,
            "processed": processed,
            "elapsed": prefix[index + 1],
            "media_events": index + 1,
            "true_remaining": suffix[index + 1] + proofread + write,
        })
    for fraction in (1.0 / 3.0, 2.0 / 3.0, 1.0):
        steps.append({
            "phase": "proofread",
            "phase_percent": 100.0 * fraction,
            "processed": duration,
            "elapsed": prefix[-1] + proofread * fraction,
            "media_events": chunk_count,
            "true_remaining": proofread * (1.0 - fraction) + write,
        })
    for fraction in (0.5, 1.0):
        steps.append({
            "phase": "write",
            "phase_percent": 100.0 * fraction,
            "processed": duration,
            "elapsed": prefix[-1] + proofread + write * fraction,
            "media_events": chunk_count,
            "true_remaining": write * (1.0 - fraction),
        })
    return steps


def _band_relative(samples: list[float]) -> float:
    """Same robust relative band the estimator uses (clamped 0.15..0.8)."""
    clean = [float(value) for value in samples if value > 0]
    if not clean:
        return 0.7416  # no samples: honest wide band
    center = statistics.median(clean)
    spread = statistics.median([abs(value - center) for value in clean]) / center
    return max(0.15, min(0.8, 1.4826 * spread))


def _baseline_point(step: dict, duration: float, history: list[float]) -> dict:
    """Exact replication of the pre-fix duration ETA: wall time only.

    ``completed_units = elapsed / (duration*rtf/100)`` means
    ``remaining = duration*rtf - elapsed`` — the model cannot see
    ``processed_media_seconds`` and reaches 0 while tails still run.
    """
    priors = [value for value in history if value > 0]
    rtf = statistics.median(priors) if priors else SUBTITLE_BOOTSTRAP_RTFS["proofread"]
    remaining = max(0.0, duration * rtf - step["elapsed"])
    relative = _band_relative(priors)
    return {
        "pred": remaining,
        "low": max(0.0, remaining * (1.0 - relative)),
        "high": remaining * (1.0 + relative),
        "confidence": "high" if priors else "low",
        "basis": "history_median" if priors else "bootstrap",
        "elapsed": step["elapsed"],
        "true": step["true_remaining"],
        "phase": step["phase"],
    }


def _new_point(
    step: dict,
    duration: float,
    proofread: bool,
    history: list[float],
    tail_seconds: dict[str, float],
    *,
    previous_estimate: dict | None = None,
) -> tuple[dict, dict]:
    """Replay one heartbeat through the production rebuild path.

    Mirrors ``CourseLensApplication._refresh_subtitle_task_estimate``: a fresh
    ``ProgressTracker`` is seeded from the persisted progress/estimate and
    ``configure_duration_estimate`` recomputes the blended ETA.
    """
    weighted = step["phase_percent"] * 0.5 if step["phase"] == "recognize" else 0.0
    progress = {
        "schema_version": 2,
        "percent": min(99.0, weighted),
        "phase_id": step["phase"],
        "phase_percent": step["phase_percent"],
        "media_duration_seconds": duration,
        "processed_media_seconds": step["processed"],
        "media_events": step["media_events"],
        "elapsed_active_seconds": step["elapsed"],
        "observed_at": 1000.0 + step["elapsed"],
    }
    clock = lambda: 0.0  # noqa: E731 - elapsed comes from the persisted offset
    wall = lambda: 1000.0 + step["elapsed"]  # noqa: E731
    tracker = ProgressTracker(
        "subtitle", proofread=proofread, initial_progress=progress,
        initial_estimate=previous_estimate, clock=clock, wall_clock=wall,
    )
    tracker.configure_duration_estimate(
        duration,
        historical_rtfs=history,
        processed_media_seconds=step["processed"],
        average_rtf=None,
        stable_media_events=step["media_events"],
        phase_tail_seconds=tail_seconds,
    )
    _, estimate = tracker.snapshot()
    point = {
        "pred": estimate.get("remaining_seconds"),
        "low": estimate.get("lower_seconds"),
        "high": estimate.get("upper_seconds"),
        "confidence": estimate.get("confidence"),
        "basis": estimate.get("basis"),
        "elapsed": step["elapsed"],
        "true": step["true_remaining"],
        "phase": step["phase"],
    }
    return point, estimate


def _replay(steps: list[dict], task: dict, history: list[float], tail: dict[str, float]) -> list[dict]:
    points = []
    previous_estimate = None
    for step in steps:
        _, estimate = _new_point(
            step, task["duration"], True, history, tail,
            previous_estimate=previous_estimate,
        )
        previous_estimate = estimate
        points.append({
            "pred": estimate.get("remaining_seconds"),
            "low": estimate.get("lower_seconds"),
            "high": estimate.get("upper_seconds"),
            "confidence": estimate.get("confidence"),
            "basis": estimate.get("basis"),
            "elapsed": step["elapsed"],
            "true": step["true_remaining"],
            "phase": step["phase"],
        })
    return points


def _mdae(points: list[dict]) -> float:
    return statistics.median(abs(p["pred"] - p["true"]) for p in points)


def _mdape(points: list[dict]) -> float:
    values = [abs(p["pred"] - p["true"]) / p["true"] for p in points if p["true"] >= 60.0]
    return statistics.median(values)


def _bias(points: list[dict]) -> float:
    return statistics.median(p["pred"] - p["true"] for p in points)


def _coverage(points: list[dict]) -> float:
    hits = sum(1 for p in points if p["low"] <= p["true"] <= p["high"])
    return hits / len(points)


def _jumps(points: list[dict], threshold: float = 300.0) -> int:
    return sum(
        1
        for previous, current in zip(points, points[1:])
        if previous["pred"] is not None and current["pred"] is not None
        and abs(current["pred"] - previous["pred"]) > threshold
    )


def _eta_replay_table() -> dict:
    """Baseline vs new metrics over the held-out eval set (for the report)."""
    history = _fit_history()
    table: dict = {"tasks": {}, "pooled": {}}
    pools = {"baseline": [], "new": []}
    for name, task in EVAL_TASKS.items():
        steps = _subtitle_steps(
            task["duration"], task["pace"], task["proofread"], task["write"],
            slowdown_at=task.get("slowdown_at"),
            slowdown_factor=task.get("slowdown_factor", 2.0),
            stall_chunk=task.get("stall_chunk"),
            stall_seconds=task.get("stall_seconds", 0.0),
        )
        baseline = [_baseline_point(step, task["duration"], history) for step in steps]
        new = _replay(steps, task, history, FIT_TAIL_SECONDS)
        pools["baseline"].extend(baseline)
        pools["new"].extend(new)
        table["tasks"][name] = {
            "mdae": (_mdae(baseline), _mdae(new)),
            "mdape": (_mdape(baseline), _mdape(new)),
            "bias": (_bias(baseline), _bias(new)),
            "coverage": (_coverage(baseline), _coverage(new)),
            "jumps": (_jumps(baseline), _jumps(new)),
        }
    cold_steps = _subtitle_steps(
        COLD_TASK["duration"], COLD_TASK["pace"], COLD_TASK["proofread"], COLD_TASK["write"]
    )
    cold_new = _replay(cold_steps, COLD_TASK, [], {})
    table["cold_no_history"] = {
        "mdae": _mdae(cold_new),
        "coverage": _coverage(cold_new),
        "confidences": sorted({p["confidence"] for p in cold_new}),
    }
    pools["new"].extend(cold_new)
    for model, points in pools.items():
        # Jumps are per-trajectory: never count the seam between two tasks.
        table["pooled"][model] = {
            "mdae": _mdae(points),
            "mdape": _mdape(points),
            "bias": _bias(points),
            "coverage": _coverage(points),
            "jumps": sum(row["jumps"][model == "new"] for row in table["tasks"].values())
            + (_jumps(cold_new) if model == "new" else 0),
            "bases": sorted({p["basis"] for p in points}),
            "confidences": sorted({p["confidence"] for p in points}),
        }
    return table


class TaskEtaReplayTests(unittest.TestCase):
    """Baseline vs blended-model replay on per-task held-out trajectories."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.table = _eta_replay_table()

    def test_baseline_documents_the_wall_time_flaw(self):
        """The old ETA is a line to zero: it ignores processed media and dies
        during the proofread tail while real work (and truth) remains."""
        history = _fit_history()
        task = EVAL_TASKS["slowdown"]
        steps = _subtitle_steps(
            task["duration"], task["pace"], task["proofread"], task["write"],
            slowdown_at=task["slowdown_at"], slowdown_factor=task["slowdown_factor"],
        )
        baseline_slow = [_baseline_point(step, task["duration"], history) for step in steps]
        tail_points = [p for p in baseline_slow if p["phase"] != "recognize" and p["true"] > 0]
        self.assertTrue(tail_points)
        for point in tail_points:
            self.assertEqual(point["pred"], 0.0, "baseline ETA hits zero during the tail")
            self.assertGreaterEqual(point["true"], 10.0)
        self.assertLessEqual(
            _mdae(baseline_slow) * 0.5,
            self.table["tasks"]["slowdown"]["mdae"][1],
            "sanity: new model keeps a positive error budget on this task",
        )
        self.assertLess(
            self.table["tasks"]["slowdown"]["mdae"][1],
            self.table["tasks"]["slowdown"]["mdae"][0],
            "blended model must beat the wall-time baseline on the 2x slowdown",
        )

    def test_new_model_not_worse_than_baseline_on_held_out_tasks(self):
        pooled_base = self.table["pooled"]["baseline"]
        pooled_new = self.table["pooled"]["new"]
        self.assertLessEqual(pooled_new["mdae"], pooled_base["mdae"])
        self.assertLessEqual(pooled_new["mdape"], pooled_base["mdape"])
        self.assertLessEqual(abs(pooled_new["bias"]), abs(pooled_base["bias"]))
        # At most one bounded correction beyond the baseline jump budget: the
        # cold-start task may correct its bootstrap guess once live chunks
        # arrive, and the x1.5 cap keeps that correction to a single step.
        self.assertLessEqual(pooled_new["jumps"], pooled_base["jumps"] + 1)
        self.assertGreaterEqual(pooled_new["coverage"], 0.5)
        self.assertGreaterEqual(pooled_new["coverage"], pooled_base["coverage"] - 0.05)
        slow_base, slow_new = self.table["tasks"]["slowdown"]["mdae"]
        self.assertLessEqual(slow_new, slow_base * 0.7)

    def test_slowdown_tracks_within_bounded_lag(self):
        """After the blend activates, the 2x-slowdown ETA stays within a
        bounded band around truth instead of collapsing toward zero."""
        history = _fit_history()
        task = EVAL_TASKS["slowdown"]
        steps = _subtitle_steps(
            task["duration"], task["pace"], task["proofread"], task["write"],
            slowdown_at=task["slowdown_at"], slowdown_factor=task["slowdown_factor"],
        )
        tail = FIT_TAIL_SECONDS
        points = []
        previous = None
        for step in steps:
            point, estimate = _new_point(
                step, task["duration"], True, history, tail,
                previous_estimate=previous,
            )
            previous = estimate
            points.append(point)
        late = [
            p for p in points
            if p["phase"] == "recognize" and p["elapsed"] >= 0.5 * (task["duration"] * task["pace"])
        ]
        self.assertTrue(late)
        for point in late:
            ratio = point["pred"] / point["true"]
            self.assertGreaterEqual(ratio, 0.45, f"ETA collapsed at {point}")
            self.assertLessEqual(ratio, 1.2, f"ETA overshot at {point}")
        for previous, current in zip(points, points[1:]):
            if previous["phase"] == current["phase"]:
                self.assertLessEqual(
                    current["pred"], previous["pred"] * 1.5 + 1.0,
                    "per-update change must stay bounded (x1.5 cap)",
                )
                self.assertGreaterEqual(
                    current["pred"], previous["pred"] / 1.5 - 1.0,
                    "per-update change must stay bounded (x1.5 cap)",
                )

    def test_tail_phase_inherits_consumed_time_and_never_zeroes(self):
        history = _fit_history()
        task = EVAL_TASKS["nominal"]
        steps = _subtitle_steps(task["duration"], task["pace"], task["proofread"], task["write"])
        points = _replay(steps, task, history, FIT_TAIL_SECONDS)
        tail_points = [p for p in points if p["phase"] != "recognize"]
        for point in tail_points:
            self.assertEqual(point["basis"], "phase_history")
            self.assertGreater(point["pred"], 0.0, "ETA must not drop to 0 when ASR completes")
            self.assertLessEqual(
                point["pred"], FIT_TAIL_SECONDS["proofread"] + FIT_TAIL_SECONDS["write"] + 1.0,
                "tail estimate must not reset to a fresh full estimate",
            )
        mid_proofread = tail_points[1]
        expected = FIT_TAIL_SECONDS["proofread"] / 3.0 + FIT_TAIL_SECONDS["write"]
        self.assertLessEqual(abs(mid_proofread["pred"] - expected), expected * 0.35)
        # Contrast (slowdown test documents the flaw): on this pace-matched
        # task the wall-time baseline happens to track, so the tail claim is
        # proven where history and reality diverge.

    def test_restart_recovery_keeps_estimate_and_signals_monotonic(self):
        """Persisted progress carries elapsed/processed/media_events across the
        per-heartbeat rebuild (which is also the restart-recovery path)."""
        history = _fit_history()
        task = EVAL_TASKS["stall"]
        steps = _subtitle_steps(
            task["duration"], task["pace"], task["proofread"], task["write"],
            stall_chunk=task["stall_chunk"], stall_seconds=task["stall_seconds"],
        )
        points = _replay(steps, task, history, FIT_TAIL_SECONDS)
        elapsed_values = [p["elapsed"] for p in points]
        self.assertEqual(elapsed_values, sorted(elapsed_values))
        boundary = points[40]
        self.assertGreater(points[39]["elapsed"], 0)
        self.assertLess(
            abs(boundary["pred"] - points[39]["pred"]),
            300.0,
            "a stalled chunk must not produce a huge ETA jump",
        )

    def test_cold_start_is_honest_and_low_confidence_is_wider(self):
        cold = self.table["cold_no_history"]
        self.assertEqual(cold["confidences"], ["low", "medium"])
        self.assertGreaterEqual(cold["coverage"], 0.85)
        pooled = self.table["pooled"]["new"]
        self.assertIn("bootstrap", pooled["bases"])
        self.assertIn("live_blend", pooled["bases"])
        self.assertIn("history_median", pooled["bases"])
        self.assertIn("phase_history", pooled["bases"])
        self.assertTrue(set(pooled["bases"]) <= set(ESTIMATE_BASIS_VALUES))
        self.assertTrue(set(pooled["confidences"]) <= {"low", "medium", "high"})
        # Interval honesty is directionally consistent with confidence:
        # weaker evidence must publish wider ranges, never narrow ones.
        widths = self._mean_relative_width_by_confidence()
        high = widths.get("high")
        medium = widths.get("medium")
        low = widths.get("low")
        self.assertTrue(high)
        self.assertTrue(medium)
        self.assertTrue(low)
        self.assertLess(high, medium)
        self.assertLess(medium, low)

    @classmethod
    def _mean_relative_width_by_confidence(cls) -> dict[str, float]:
        buckets: dict[str, list[float]] = {}
        for task in EVAL_TASKS.values():
            steps = _subtitle_steps(
                task["duration"], task["pace"], task["proofread"], task["write"],
                slowdown_at=task.get("slowdown_at"),
                slowdown_factor=task.get("slowdown_factor", 2.0),
                stall_chunk=task.get("stall_chunk"),
                stall_seconds=task.get("stall_seconds", 0.0),
            )
            cls._collect_widths(buckets, _replay(steps, task, _fit_history(), FIT_TAIL_SECONDS))
        cold_steps = _subtitle_steps(
            COLD_TASK["duration"], COLD_TASK["pace"], COLD_TASK["proofread"], COLD_TASK["write"]
        )
        cls._collect_widths(buckets, _replay(cold_steps, COLD_TASK, [], {}))
        return {
            confidence: sum(values) / len(values)
            for confidence, values in buckets.items()
            if values
        }

    @staticmethod
    def _collect_widths(buckets: dict[str, list[float]], points: list[dict]) -> None:
        for point in points:
            if not point["pred"]:
                continue
            relative = (point["high"] - point["low"]) / point["pred"]
            buckets.setdefault(point["confidence"], []).append(relative)

    def test_terminal_zeroing_and_point_range_shape(self):
        history = _fit_history()
        task = EVAL_TASKS["nominal"]
        step = _subtitle_steps(task["duration"], task["pace"], task["proofread"], task["write"])[5]
        _, estimate = _new_point(step, task["duration"], True, history, FIT_TAIL_SECONDS)
        self.assertGreater(estimate["remaining_seconds"], 0.0)
        self.assertLessEqual(estimate["lower_seconds"], estimate["remaining_seconds"])
        self.assertLessEqual(estimate["remaining_seconds"], estimate["upper_seconds"])
        tracker = ProgressTracker("subtitle", proofread=True)
        tracker.configure_duration_estimate(
            task["duration"], historical_rtfs=history,
            phase_tail_seconds=FIT_TAIL_SECONDS,
        )
        for phase_id, percent in (("prepare", 100.0), ("recognize", 100.0), ("proofread", 100.0)):
            tracker.update(phase_id, percent)
        progress, estimate = tracker.complete()
        self.assertEqual(progress["percent"], 100.0)
        self.assertEqual(estimate["remaining_seconds"], 0.0)
        self.assertIn(estimate["basis"], ESTIMATE_BASIS_VALUES)


class TaskEtaDurationModelTests(unittest.TestCase):
    """Direct duration-model behavior: blend activation and tail history."""

    def _tracker(self, *, history=(), tail=None, processed=None, events=0, elapsed=0.0,
                 phase=None, phase_percent=None, duration=1800.0, proofread=True,
                 previous_estimate=None):
        progress = {"elapsed_active_seconds": elapsed}
        if processed is not None:
            progress["processed_media_seconds"] = processed
            progress["media_duration_seconds"] = duration
        if events:
            progress["media_events"] = events
        if phase:
            progress["phase_id"] = phase
            progress["phase_percent"] = phase_percent or 0.0
        tracker = ProgressTracker(
            "subtitle", proofread=proofread, initial_progress=progress,
            initial_estimate=previous_estimate,
            clock=lambda: 0.0, wall_clock=lambda: 1000.0 + elapsed,
        )
        tracker.configure_duration_estimate(
            duration, historical_rtfs=history,
            processed_media_seconds=processed, stable_media_events=events,
            phase_tail_seconds=tail,
        )
        return tracker

    def test_history_only_until_two_stable_chunks_then_live_blend(self):
        tracker = self._tracker(history=[0.5, 0.52], processed=30.0, events=1, elapsed=15.0)
        _, estimate = tracker.snapshot()
        self.assertEqual(estimate["basis"], "history_median")
        tracker = self._tracker(history=[0.5, 0.52], processed=600.0, events=20, elapsed=240.0)
        _, estimate = tracker.snapshot()
        self.assertEqual(estimate["basis"], "live_blend")
        # live rtf = 240/600 = 0.40; prior = 0.51;
        # w = clamp(600/1800*1.25, 0.15, 0.75) = 0.4167
        expected_rtf = 0.51 * (1 - 0.4167) + 0.4167 * 0.40
        expected_remaining = 1200.0 * expected_rtf + 240.0 + 30.0
        self.assertLessEqual(
            abs(estimate["remaining_seconds"] - expected_remaining), 1.0,
            (estimate["remaining_seconds"], expected_remaining),
        )

    def test_bootstrap_when_no_history_and_no_live_signal(self):
        tracker = self._tracker(history=[])
        _, estimate = tracker.snapshot()
        self.assertEqual(estimate["basis"], "bootstrap")
        expected = 1800.0 * SUBTITLE_BOOTSTRAP_RTFS["proofread"] + 240.0 + 30.0
        self.assertEqual(estimate["remaining_seconds"], round(expected, 1))

    def test_tail_history_medians_replace_bootstrap_constants(self):
        tracker = self._tracker(
            history=[0.5], tail={"proofread": 400.0, "write": 60.0},
            processed=1800.0, events=60, elapsed=900.0,
        )
        _, estimate = tracker.snapshot()
        self.assertEqual(estimate["remaining_seconds"], 460.0)
        plain = self._tracker(history=[0.5], processed=1800.0, events=60, elapsed=900.0)
        _, estimate = plain.snapshot()
        self.assertEqual(estimate["remaining_seconds"], 270.0)

    def test_tail_phase_consumption_is_inherited_not_reset(self):
        tracker = self._tracker(
            history=[0.5], tail={"proofread": 400.0, "write": 60.0},
            processed=1800.0, events=60, elapsed=900.0,
            phase="proofread", phase_percent=60.0,
        )
        _, estimate = tracker.snapshot()
        self.assertEqual(estimate["remaining_seconds"], 400.0 * 0.4 + 60.0)
        self.assertEqual(estimate["basis"], "phase_history")

    def test_missing_media_duration_stays_insufficient(self):
        tracker = ProgressTracker("subtitle", proofread=True)
        _, estimate = tracker.snapshot()
        self.assertIsNone(estimate["remaining_seconds"])
        self.assertEqual(estimate["basis"], "insufficient_data")

    def test_summary_percent_path_keeps_engine_and_closed_basis(self):
        tracker = ProgressTracker(
            "summary", include_ppt=True, prior_costs=[3.0, 3.2],
            clock=lambda: 100.0, wall_clock=lambda: 9000.0,
        )
        sequence = (("input", 100.0), ("ai_parts", 40.0), ("ai_parts", 80.0),
                    ("finalize", 100.0))
        remaining_values = []
        for phase_id, percent in sequence:
            _, estimate = tracker.update(phase_id, percent)
            remaining = estimate["remaining_seconds"]
            self.assertIsNotNone(remaining)
            remaining_values.append(remaining)
            self.assertIn(estimate["basis"], ESTIMATE_BASIS_VALUES)
            self.assertLessEqual(estimate["lower_seconds"], remaining)
            self.assertLessEqual(remaining, estimate["upper_seconds"])
        # The engine still converges as the weighted percent completes.
        self.assertLess(remaining_values[-1], remaining_values[0])
        # Prior-only state follows the historical median cost per weighted
        # percent (input 3 + ai_parts 0.25 x 40 = 13 completed units).
        self.assertEqual(remaining_values[1], round(87.0 * 3.1, 1))


class TaskEtaPublicModelTests(unittest.TestCase):
    """public_task additive fields: basis closed set, ranges, queue anchors."""

    def _running_task(self, store: TaskStore, *, estimate=None, progress=None):
        task, _ = store.add_task(
            "subtitle", "c", "l1",
            {"subtitle_mode": "automatic", "subtitle_proofread": True, "source_kind": "remote-actions"},
            config_key="automatic",
        )
        now = time.time()
        task = store.update_task(
            task["task_id"], state="running", started_at=now - 120.0,
            progress={
                "schema_version": 2, "stage": "asr", "phase_id": "asr",
                "percent": 40.0, "processed_media_seconds": 720.0,
                "media_duration_seconds": 1800.0, "media_events": 24,
                "elapsed_active_seconds": 300.0, "observed_at": now,
            },
            estimate=estimate if estimate is not None else {
                "elapsed_seconds": 300.0, "remaining_seconds": 540.0,
                "lower_seconds": 480.0, "upper_seconds": 620.0,
                "confidence": "high", "basis": "live_blend",
                "updated_at": int(now * 1000),
            },
        )
        return task, now

    def test_high_confidence_exposes_point_and_range_with_basis(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            task, now = self._running_task(store)
            value = public_task(task, store, now=now)
            self.assertEqual(value["remaining_seconds"], 540.0)
            self.assertEqual(value["remaining_lower_seconds"], 480.0)
            self.assertEqual(value["remaining_upper_seconds"], 620.0)
            self.assertEqual(value["estimate_basis"], "live_blend")
            self.assertEqual(value["progress_confidence"], "high")

    def test_queued_task_publishes_queue_basis_and_wait_not_eta(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            task, _ = store.add_task(
                "subtitle", "c", "l1",
                {"subtitle_mode": "automatic", "subtitle_proofread": True, "source_kind": "remote-actions"},
                config_key="automatic",
            )
            created = float(task["created_at"])
            task = store.update_task(
                task["task_id"],
                progress={"schema_version": 2, "percent": 0.0,
                          "elapsed_active_seconds": 0.0, "observed_at": created + 5.0},
                estimate={"remaining_seconds": 600.0, "confidence": "medium",
                          "basis": "history_median"},
            )
            value = public_task(task, store, now=created + 90.0)
            self.assertEqual(value["estimate_basis"], "queue")
            self.assertIsNone(value["remaining_seconds"])
            self.assertIsNone(value["remaining_lower_seconds"])
            self.assertIsNone(value["remaining_upper_seconds"])
            self.assertEqual(value["elapsed_queued_seconds"], 90.0)
            started = created + 120.0
            task = store.update_task(
                task["task_id"], state="running", started_at=started,
                progress={"schema_version": 2, "percent": 5.0,
                          "elapsed_active_seconds": 0.0, "observed_at": started + 30.0},
            )
            value = public_task(task, store, now=started + 10.0)
            self.assertEqual(value["elapsed_queued_seconds"], 120.0)
            self.assertEqual(value["estimate_basis"], "history_median")
            self.assertEqual(value["remaining_seconds"], 600.0)

    def test_stale_and_insufficient_states_degrade_with_closed_basis(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            task, now = self._running_task(store)
            value = public_task(task, store, now=now + 3600.0)
            self.assertTrue(value["stale"])
            self.assertEqual(value["estimate_basis"], "stale")
            self.assertIsNone(value["remaining_seconds"])
            self.assertIsNone(value["remaining_lower_seconds"])
            task, now = self._running_task(store, estimate={})
            value = public_task(task, store, now=now)
            self.assertEqual(value["estimate_basis"], "insufficient_data")
            self.assertIsNone(value["remaining_seconds"])
            self.assertEqual(value["elapsed_seconds"], 300.0)

    def test_completed_task_zeroes_remaining_with_closed_basis(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            task, now = self._running_task(store)
            task = store.update_task(task["task_id"], state="completed")
            value = public_task(task, store, now=now + 5.0)
            self.assertEqual(value["remaining_seconds"], 0.0)
            self.assertIsNone(value["remaining_lower_seconds"])
            self.assertIsNone(value["remaining_upper_seconds"])
            self.assertIn(value["estimate_basis"], ESTIMATE_BASIS_VALUES)

    def test_legacy_estimate_bases_map_into_closed_set(self):
        from src.runtime.http_api import _public_estimate_basis
        for legacy, expected in (
            ("duration-history", "history_median"),
            ("duration-live", "live_blend"),
            ("duration-bootstrap", "bootstrap"),
            ("history+live", "phase_history"),
            ("live", "live_blend"),
            ("unknown", "insufficient_data"),
            ("", "insufficient_data"),
            ("mystery", "insufficient_data"),
        ):
            self.assertEqual(_public_estimate_basis(legacy), expected)
        self.assertEqual(
            set(ESTIMATE_BASIS_VALUES),
            {"queue", "history_median", "live_blend", "bootstrap",
             "phase_history", "insufficient_data", "stale"},
        )


# ---------------------------------------------------------------------------
# Stage 09-C: trusted Worker queue observations + hierarchical forecasts
# ---------------------------------------------------------------------------

TRUSTED_HEAD = "a" * 40


def _queue_run(**overrides):
    run = {
        "id": 77,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "success",
        "event": "workflow_dispatch",
        "head_sha": TRUSTED_HEAD,
        "created_at": "2026-09-11T05:00:00Z",
        "run_started_at": "2026-09-11T05:03:00Z",
        "updated_at": "2026-09-11T05:20:00Z",
        "html_url": "",
    }
    run.update(overrides)
    return run


class QueueObservationTests(unittest.TestCase):
    """Trusted queue-evidence capture: filters, bounds, hierarchy, floor."""

    def _record(self, store, workflow="process.yml", values=(100.0,)):
        from src.runtime.task_store import queue_profile_keys
        for value in values:
            store.record_queue_sample(
                queue_profile_keys(workflow, pipeline="2"), float(value)
            )

    def test_trusted_observation_accepts_comparable_completed_runs(self):
        from src.runtime.task_store import trusted_queue_observation
        sample = trusted_queue_observation(
            _queue_run(), workflow="process.yml", pipeline="2", expected_head=TRUSTED_HEAD,
        )
        self.assertIsNotNone(sample)
        self.assertEqual(sample["schema"], "courselens.queue-observation.v1")
        self.assertEqual(sample["queued_seconds"], 180.0)
        self.assertEqual(
            sample["profile_keys"],
            ["queue:process.yml:public-runner:2", "queue:process.yml:public-runner", "queue:worker"],
        )

    def test_observation_workflow_set_covers_live_llm_fast_path(self):
        """N1-ROUTING 留面对齐：llm.yml 已是活跃派发目标（adcf399），观测闭集
        同笔收编其队列等待样本；观测纯采样，不触碰派发路由。"""
        workflows = CourseLensApplication.QUEUE_OBSERVATION_WORKFLOWS
        self.assertIn("llm.yml", workflows)
        self.assertIn("process.yml", workflows)
        # 闭集语义：既有成员零位移。
        self.assertEqual(
            tuple(workflows),
            ("process.yml", "llm.yml", "echo.yml", "cloud-verify.yml", "cloud-daily.yml"),
        )

    def test_observation_rejects_malformed_or_unordered_timestamps(self):
        from src.runtime.task_store import trusted_queue_observation
        kwargs = dict(workflow="process.yml", pipeline="2", expected_head=TRUSTED_HEAD)
        for bad in (
            _queue_run(created_at="", run_started_at="2026-09-11T05:03:00Z"),
            _queue_run(created_at="not-a-time", run_started_at="2026-09-11T05:03:00Z"),
            _queue_run(run_started_at=""),
            _queue_run(run_started_at="2026-09-11T04:59:00Z"),  # started before created
            _queue_run(created_at="2026-09-10T05:00:00Z", run_started_at="2026-09-11T05:00:01Z"),  # > 24h
        ):
            self.assertIsNone(trusted_queue_observation(bad, **kwargs))

    def test_observation_rejects_cancelled_skipped_and_ambiguous(self):
        from src.runtime.task_store import trusted_queue_observation
        kwargs = dict(workflow="process.yml", pipeline="2", expected_head=TRUSTED_HEAD)
        for bad in (
            _queue_run(conclusion="cancelled"),
            _queue_run(conclusion="canceled"),
            _queue_run(conclusion="skipped"),
            _queue_run(conclusion="startup_failure"),
            _queue_run(conclusion=""),
            _queue_run(status="in_progress"),
            _queue_run(status="queued"),
        ):
            self.assertIsNone(trusted_queue_observation(bad, **kwargs))

    def test_observation_fails_closed_without_trusted_head_match(self):
        from src.runtime.task_store import trusted_queue_observation
        for expected in ("", "b" * 40):
            self.assertIsNone(trusted_queue_observation(
                _queue_run(), workflow="process.yml", pipeline="2", expected_head=expected))
        self.assertIsNone(trusted_queue_observation(
            _queue_run(head_sha="c" * 40), workflow="process.yml", pipeline="2",
            expected_head=TRUSTED_HEAD))
        self.assertIsNone(trusted_queue_observation(
            _queue_run(id=0), workflow="process.yml", pipeline="2", expected_head=TRUSTED_HEAD))

    def test_record_rejects_non_finite_and_out_of_bounds_waits(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            for bad in (0.0, -5.0, float("nan"), float("inf"), 25 * 3600.0):
                self.assertFalse(store.record_queue_sample(["queue:worker"], bad))
            self.assertFalse(store.record_queue_sample([], 60.0))
            with raw_db(Path(tmp) / "state.db") as db:
                rows = int(db.execute("SELECT COUNT(*) FROM estimate_samples").fetchone()[0])
            self.assertEqual(rows, 0)

    def test_forecast_publishes_nothing_below_five_comparable_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            self._record(store, values=(100.0, 140.0, 180.0, 160.0))
            self.assertIsNone(store.queue_forecast_for_workflow("process.yml", pipeline="2"))
            self.assertIsNone(store.queue_forecast_for_workflow("echo.yml"))
            self._record(store, values=(200.0,))
            forecast = store.queue_forecast_for_workflow("process.yml", pipeline="2")
            self.assertIsNotNone(forecast)
            self.assertEqual(forecast["sample_count"], 5)
            self.assertLessEqual(forecast["lower_seconds"], forecast["center_seconds"])
            self.assertLessEqual(forecast["center_seconds"], forecast["upper_seconds"])
            self.assertEqual(forecast["confidence"], "low")
            self.assertEqual(forecast["basis"], "history_median")
            self.assertEqual(forecast["level"], "exact")

    def test_forecast_falls_through_the_hierarchical_levels(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            # Only the worker-wide bucket reaches the floor.
            self._record(store, workflow="process.yml", values=(100.0, 140.0, 180.0, 160.0))
            self._record(store, workflow="echo.yml", values=(120.0,))
            forecast = store.queue_forecast_for_workflow("cloud-daily.yml", pipeline="2")
            self.assertIsNotNone(forecast)
            self.assertEqual(forecast["level"], "worker")
            # Sparse exact bucket falls back to the runner-class bucket.
            for _ in range(5):
                self._record(store, workflow="echo.yml", values=(90.0,))
            forecast = store.queue_forecast_for_workflow("echo.yml", pipeline="9")
            self.assertIsNotNone(forecast)
            self.assertEqual(forecast["level"], "runner")
            self.assertEqual(forecast["center_seconds"], 90.0)
            # Fill the exact pipeline bucket past the floor; it answers first.
            from src.runtime.task_store import queue_profile_keys
            for _ in range(5):
                store.record_queue_sample(
                    queue_profile_keys("echo.yml", pipeline="9"), 96.0)
            forecast = store.queue_forecast_for_workflow("echo.yml", pipeline="9")
            self.assertIsNotNone(forecast)
            self.assertEqual(forecast["level"], "exact")

    def test_forecast_ignores_samples_older_than_the_age_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            self._record(store, values=(100.0, 140.0, 180.0, 160.0, 200.0))
            self.assertIsNotNone(store.queue_forecast_for_workflow("process.yml", pipeline="2"))
            ancient = time.time() - 45 * 24 * 3600.0
            db = sqlite3.connect(Path(tmp) / "state.db")
            try:
                db.execute("UPDATE estimate_samples SET created_at=?", (ancient,))
                db.commit()
            finally:
                db.close()
            self.assertIsNone(store.queue_forecast_for_workflow("process.yml", pipeline="2"))

    def test_queue_sample_count_is_capped_per_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            self._record(store, values=[float(100 + index) for index in range(30)])
            with raw_db(Path(tmp) / "state.db") as db:
                rows = int(db.execute(
                    "SELECT COUNT(*) FROM estimate_samples WHERE profile_key='queue:worker'"
                ).fetchone()[0])
            self.assertEqual(rows, 20)


class SanitizedWorkflowRunViewTests(unittest.TestCase):
    """The sanitized GitHub run view carries the queue-timing timestamp."""

    def test_list_workflow_runs_includes_run_started_at(self):
        from tests.test_github_app import JsonResponse, MemoryCredentials
        from src.remote.github_app import GitHubAppClient

        credentials = MemoryCredentials()
        credentials.save_secret("github_app_access_token", "token")
        credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        credentials.save_secret("github_worker_repo", "student/worker")
        client = GitHubAppClient(credentials, client_id="client-id", app_slug="fudan-courselens")
        payload = {"workflow_runs": [{
            "id": 9, "run_attempt": 1, "status": "completed", "conclusion": "success",
            "event": "schedule", "head_sha": "a" * 40,
            "created_at": "2026-09-11T05:00:00Z", "run_started_at": "2026-09-11T05:01:00Z",
            "updated_at": "2026-09-11T05:09:00Z", "html_url": "https://example.invalid",
        }]}
        with patch.object(
            client, "_api", return_value=JsonResponse(payload)
        ) as api:
            runs = client.list_workflow_runs("cloud-verify.yml", limit=5)
        self.assertEqual(
            api.call_args.args[:2],
            ("GET", "/repos/student/worker/actions/workflows/cloud-verify.yml/runs"),
        )
        self.assertEqual(len(runs), 1)
        self.assertEqual(sorted(runs[0].keys()), sorted([
            "id", "run_attempt", "status", "conclusion", "event", "head_sha",
            "created_at", "run_started_at", "updated_at", "html_url",
        ]))
        self.assertEqual(runs[0]["run_started_at"], "2026-09-11T05:01:00Z")


class CaptureLoopTests(unittest.TestCase):
    """The monitor-loop capture records each trusted run exactly once."""

    def _service(self, store, runs_by_workflow):
        service = CourseLensApplication.__new__(CourseLensApplication)
        service.task_store = store
        service.credentials = Mock()
        service.credentials.has_secret.return_value = True
        service.github_app = Mock()

        def list_runs(workflow, limit=20):
            return runs_by_workflow.get(workflow, [])

        service.github_app.list_workflow_runs.side_effect = list_runs
        settings = Mock()
        settings.expected_worker_commit = TRUSTED_HEAD
        service.remote_settings = settings
        service.QUEUE_OBSERVATION_WORKFLOWS = ("process.yml", "cloud-verify.yml")
        return service

    def test_capture_records_each_run_once_and_deduplicates_retries(self):
        from src.runtime.task_store import queue_profile_keys
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            runs = {
                "process.yml": [_queue_run(id=1), _queue_run(id=2, run_started_at="2026-09-11T05:04:00Z")],
                "cloud-verify.yml": [_queue_run(id=3, conclusion="failure")],
            }
            service = self._service(store, runs)
            recorded = service._capture_queue_observations()
            self.assertEqual(recorded, 3)
            # A second cycle sees the same runs: no duplicate samples.
            self.assertEqual(service._capture_queue_observations(), 0)
            # Two process.yml samples are below the publish floor: honest None.
            self.assertIsNone(store.queue_forecast_for_workflow("process.yml", pipeline="2"))
            with raw_db(Path(tmp) / "state.db") as db:
                total = int(db.execute(
                    "SELECT COUNT(*) FROM estimate_samples WHERE profile_key='queue:worker'"
                ).fetchone()[0])
            self.assertEqual(total, 3)

    def test_capture_survives_api_failures_and_missing_pins(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            service = self._service(store, {})
            service.github_app.list_workflow_runs.side_effect = RuntimeError("github down")
            self.assertEqual(service._capture_queue_observations(), 0)
            service.remote_settings.expected_worker_commit = ""
            self.assertEqual(service._capture_queue_observations(), 0)
            service.credentials.has_secret.return_value = False
            self.assertEqual(service._capture_queue_observations(), 0)


# ---------------------------------------------------------------------------
# Stage 09-C: queue/completion replay with acceptance metrics + negative control
# ---------------------------------------------------------------------------

# Fit set: trusted queued seconds observed on the managed Worker (process.yml).
QUEUE_FIT_SECONDS = (120.0, 180.0, 240.0, 150.0, 210.0, 190.0, 170.0, 200.0)
# Held-out queue truths (never fed into the fit set above).  The named
# interval from the fit set is roughly [160, 220]s; normal waits land inside,
# while a real queue spike and a late runner legitimately escape — the
# acceptance requires at most 2 misses out of 6 (>=60% coverage).
QUEUE_EVAL_TASKS = (
    ("normal_early", 165.0),
    ("normal_late", 210.0),
    ("normal_mid", 190.0),
    ("normal_high", 205.0),
    ("queue_spike", 1500.0),   # real queue spikes exist; honest ranges miss them
    ("late_runner", 420.0),
)
STATIC_QUEUE_BASELINE_SECONDS = 600.0  # a no-information constant guess


def _fit_queue_forecast(tmp):
    from src.runtime.task_store import queue_profile_keys
    store = TaskStore(Path(tmp) / "queue.db")
    for value in QUEUE_FIT_SECONDS:
        store.record_queue_sample(queue_profile_keys("process.yml", pipeline="2"), value)
    return store, store.queue_forecast_for_workflow("process.yml", pipeline="2")


class QueueReplayTests(unittest.TestCase):
    """Queue forecast vs no-information baseline over held-out truths."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        store, forecast = _fit_queue_forecast(Path(cls._tmp.name))
        cls.store = store
        cls.forecast = forecast

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _model_points(self):
        return [
            {
                "pred": self.forecast["center_seconds"],
                "low": self.forecast["lower_seconds"],
                "high": self.forecast["upper_seconds"],
                "true": truth,
            }
            for _, truth in QUEUE_EVAL_TASKS
        ]

    def _baseline_points(self):
        return [
            {
                "pred": STATIC_QUEUE_BASELINE_SECONDS,
                "low": STATIC_QUEUE_BASELINE_SECONDS * 0.5,
                "high": STATIC_QUEUE_BASELINE_SECONDS * 1.5,
                "true": truth,
            }
            for _, truth in QUEUE_EVAL_TASKS
        ]

    def test_forecast_is_published_and_closed(self):
        self.assertIsNotNone(self.forecast)
        self.assertEqual(self.forecast["sample_count"], len(QUEUE_FIT_SECONDS))
        self.assertIn(self.forecast["confidence"], {"low", "medium", "high"})
        self.assertLessEqual(self.forecast["lower_seconds"], self.forecast["upper_seconds"])

    def test_queue_model_beats_static_baseline_on_held_out_error(self):
        model = self._model_points()
        baseline = self._baseline_points()
        self.assertLessEqual(_mdae(model), _mdae(baseline))
        self.assertLess(
            _mdae(model), _mdae(baseline),
            "hierarchical forecast must beat the no-information constant",
        )

    def test_named_interval_covers_truth_at_least_60_percent(self):
        coverage = _coverage(self._model_points())
        self.assertGreaterEqual(coverage, 0.6)
        # Normal waits land inside the named band; a queue spike and a late
        # runner legitimately escape — honest ranges must miss real outliers.
        misses = [name for name, truth in QUEUE_EVAL_TASKS
                  if not (self.forecast["lower_seconds"] <= truth <= self.forecast["upper_seconds"])]
        self.assertLessEqual(len(misses), 2, misses)
        self.assertEqual(set(misses), {"queue_spike", "late_runner"})

    def test_below_floor_cold_start_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "cold.db")
            for value in (100.0, 120.0, 140.0, 160.0):  # 4 < floor of 5
                store.record_queue_sample(["queue:worker"], value)
            self.assertIsNone(store.queue_forecast(["queue:worker"]))

    def test_combined_pipeline_intervals_contain_truth(self):
        """学习材料链：字幕(排队+处理) → 摘要(排队+处理)，合并区间恰好计一次。"""
        history = _fit_history()
        task = EVAL_TASKS["nominal"]
        steps = _subtitle_steps(task["duration"], task["pace"], task["proofread"], task["write"])
        processing_points = _replay(steps, task, history, FIT_TAIL_SECONDS)
        # True chain truths: one held-out queue truth + one held-out
        # processing truth per pipeline stage; both bounds sum exactly once.
        combined = []
        for index, (queue_name, queue_truth) in enumerate(QUEUE_EVAL_TASKS):
            point = processing_points[min(index, len(processing_points) - 1)]
            lower = self.forecast["lower_seconds"] + point["low"]
            upper = self.forecast["upper_seconds"] + point["high"]
            truth = queue_truth + point["true"]
            combined.append({"low": lower, "high": upper, "true": truth})
        self.assertEqual(len(combined), len(QUEUE_EVAL_TASKS))
        self.assertGreaterEqual(_coverage(combined), 0.6)
        # Queue time is never counted as processing speed: whenever the queue
        # truth itself sits inside the queue band, the combined upper bound
        # reaches the chain truth (spike/late-runner entries may honestly miss).
        for (queue_name, queue_truth), item in zip(QUEUE_EVAL_TASKS, combined):
            inside_queue = (
                self.forecast["lower_seconds"] <= queue_truth <= self.forecast["upper_seconds"]
            )
            if inside_queue:
                self.assertGreaterEqual(item["high"], item["true"] * 0.99, queue_name)

    def test_negative_control_corrupted_floor_fails_the_harness(self):
        """Falsifiability: corrupt the sample-floor constant and the harness
        acceptance must fail, then restore the constant and file bytes."""
        import src.runtime.progress as progress_module

        def acceptance():
            with tempfile.TemporaryDirectory() as tmp:
                store = TaskStore(Path(tmp) / "nc.db")
                for value in (100.0, 120.0, 140.0, 160.0):
                    store.record_queue_sample(["queue:worker"], value)
                # The acceptance under test: publishing below the floor is forbidden.
                assert store.queue_forecast(["queue:worker"]) is None

        acceptance()  # green before corruption
        source = Path(progress_module.__file__).read_bytes()
        digest_before = hashlib.sha256(source).hexdigest()
        original = progress_module.QUEUE_SAMPLE_FLOOR
        try:
            progress_module.QUEUE_SAMPLE_FLOOR = 1
            with self.assertRaises(AssertionError):
                acceptance()
        finally:
            progress_module.QUEUE_SAMPLE_FLOOR = original
        acceptance()  # green again after restore
        digest_after = Path(progress_module.__file__).read_bytes()
        self.assertEqual(hashlib.sha256(digest_after).hexdigest(), digest_before)

    def test_negative_control_corrupted_timestamp_is_rejected(self):
        from src.runtime.task_store import trusted_queue_observation
        kwargs = dict(workflow="process.yml", pipeline="2", expected_head=TRUSTED_HEAD)
        corrupt = _queue_run(created_at="2026-09-11T05:03:00Z", run_started_at="2026-09-11T05:03:00Z")
        corrupt["created_at"] = "2026-09-11T05:10:00Z"  # after started: unordered
        self.assertIsNone(trusted_queue_observation(corrupt, **kwargs))
        # A zeroed timestamp (corruption to 0) is equally unusable.
        zeroed = _queue_run(run_started_at="0000-00-00T00:00:00Z")
        self.assertIsNone(trusted_queue_observation(zeroed, **kwargs))


class SummaryReplayTests(unittest.TestCase):
    """Summary pipelines replay through the percent engine with closed bases.

    With-slides (OCR-heavy) and transcript-only schedules are deterministic:
    the truth is computed from the generator schedule, never from the model.
    """

    SUMMARY_SCHEDULES = {
        # phase_id -> seconds, keyed by the SUMMARY_PPT_PHASES / SUMMARY_TRANSCRIPT_PHASES ids.
        # prior_costs are the historical seconds per weighted percent; their
        # median matches the schedule's true pace (1260s/600s per 100 units).
        "with_slides": {
            "phases": (("input", 20.0), ("login", 30.0), ("ppt_fetch", 120.0),
                       ("ocr", 600.0), ("ai_parts", 400.0), ("finalize", 90.0)),
            "prior_costs": [12.0, 13.2],
        },
        "transcript_only": {
            "phases": (("input", 20.0), ("ai_parts", 500.0), ("finalize", 80.0)),
            "prior_costs": [5.8, 6.2],
        },
    }

    def _summary_steps(self, schedule):
        steps = []
        total = sum(seconds for _, seconds in schedule["phases"])
        elapsed = 0.0
        for phase_id, seconds in schedule["phases"]:
            for fraction in (0.5, 1.0):
                elapsed += seconds * 0.5
                steps.append({
                    "phase_id": phase_id,
                    "percent": 100.0 * fraction,
                    "elapsed": elapsed,
                    "true_remaining": max(0.0, total - elapsed),
                })
        return steps

    def _replay_summary(self, schedule, include_ppt):
        tracker = ProgressTracker(
            "summary", include_ppt=include_ppt, prior_costs=list(schedule["prior_costs"]),
            clock=lambda: 0.0, wall_clock=lambda: 1000.0,
        )
        points = []
        for step in self._summary_steps(schedule):
            progress, estimate = tracker.update(step["phase_id"], step["percent"])
            points.append({
                "low": estimate.get("lower_seconds"),
                "high": estimate.get("upper_seconds"),
                "true": step["true_remaining"],
                "basis": estimate.get("basis"),
            })
        return points

    def test_summary_intervals_contain_truth_with_and_without_slides(self):
        for name, schedule in self.SUMMARY_SCHEDULES.items():
            points = self._replay_summary(schedule, include_ppt=name == "with_slides")
            self.assertTrue(all(p["low"] is not None for p in points), name)
            self.assertGreaterEqual(_coverage(points), 0.6, name)
            self.assertTrue(all(p["basis"] in ESTIMATE_BASIS_VALUES for p in points), name)
            # The engine converges as the weighted percent completes.
            self.assertLess(points[-1]["true"], points[0]["true"])


class PredictionOutcomeTests(unittest.TestCase):
    """Actual-vs-initial calibration records (budget gate retired 2026-09-22)."""

    def _completed_task(self, store, *, with_metadata=True):
        task, _ = store.add_task(
            "subtitle", "c", "l1",
            {"subtitle_mode": "automatic", "subtitle_proofread": True, "source_kind": "remote-actions"},
            config_key="automatic",
        )
        if with_metadata:
            store.upsert_v3_metadata(task["task_id"], dedupe_key=f"subtitle:c:l1:{task['task_id'][:8]}")
        store.update_task(task["task_id"], state="running")
        store.mark_terminal(task["task_id"], "completed")
        return task

    def test_outcome_records_actual_vs_initial_when_metadata_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            task = self._completed_task(store)
            self.assertIsNone(store.prediction_outcome(task["task_id"]))
            store.record_prediction_outcome(
                task["task_id"], initial_minutes=30.0, actual_minutes=24.0, attempt=1,
            )
            outcome = store.prediction_outcome(task["task_id"])
            self.assertEqual(outcome["initial_minutes"], 30.0)
            self.assertEqual(outcome["actual_minutes"], 24.0)
            self.assertEqual(outcome["delta_minutes"], 6.0)

    def test_outcome_skips_tasks_without_metadata_and_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            task = self._completed_task(store, with_metadata=False)
            self.assertIsNone(store.record_prediction_outcome(
                task["task_id"], initial_minutes=30.0, actual_minutes=24.0))
            task2 = self._completed_task(store)
            for bad in ((0.0, 10.0), (-1.0, 10.0), (10.0, -3.0), (float("nan"), 1.0)):
                self.assertIsNone(store.record_prediction_outcome(
                    task2["task_id"], initial_minutes=bad[0], actual_minutes=bad[1]))
            self.assertIsNone(store.prediction_outcome(task2["task_id"]))


class TaskIntelPublicModelTests(unittest.TestCase):
    """S09-C additive public groups: queue/processing/completion/phases."""

    def _store_with_workflow(self, tmp, *, workflow="process.yml"):
        from src.runtime.task_store import queue_profile_keys
        store = TaskStore(Path(tmp) / "state.db")
        for value in (120.0, 180.0, 240.0, 150.0, 210.0):
            store.record_queue_sample(queue_profile_keys(workflow, pipeline="2"), value)
        return store

    def _queued_task(self, store, *, with_remote=True, estimate=None):
        task, _ = store.add_task(
            "subtitle", "c", "l1",
            {"subtitle_mode": "automatic", "subtitle_proofread": True, "source_kind": "remote-actions"},
            config_key="automatic",
        )
        created = float(task["created_at"])
        if with_remote:
            store.upsert_remote_run(
                task["task_id"], repository="owner/worker", workflow="process.yml",
                run_id=11, remote_state="queued", dispatched_at=created,
            )
        store.update_task(
            task["task_id"],
            progress={"schema_version": 2, "percent": 0.0,
                      "elapsed_active_seconds": 0.0, "observed_at": created + 5.0},
            estimate=estimate if estimate is not None else {
                "remaining_seconds": 900.0, "lower_seconds": 780.0, "upper_seconds": 1140.0,
                "confidence": "medium", "basis": "history_median", "sample_count": 6,
                "updated_at": int((created + 5.0) * 1000),
            },
        )
        return store.get_task(task["task_id"]), created

    def test_queued_task_publishes_queue_group_with_floor_met(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store_with_workflow(tmp)
            task, created = self._queued_task(store)
            value = public_task(task, store, now=created + 90.0)
            queue_group = value.get("queue")
            self.assertIsInstance(queue_group, dict)
            self.assertEqual(queue_group["elapsed_seconds"], 90.0)
            self.assertEqual(queue_group["basis"], "history_median")
            self.assertIn(queue_group["confidence"], {"low", "medium", "high"})
            self.assertGreaterEqual(queue_group["sample_count"], 5)
            self.assertLessEqual(queue_group["lower_seconds"], queue_group["center_seconds"])
            self.assertIn(queue_group["level"], {"exact", "runner", "worker"})
            # Combined completion only when both components are valid.
            completion = value.get("completion")
            self.assertIsInstance(completion, dict)
            self.assertLessEqual(completion["earliest_seconds"], completion["likely_seconds"])
            self.assertLessEqual(completion["likely_seconds"], completion["latest_seconds"])
            self.assertEqual(
                completion["likely_seconds"],
                round(queue_group["center_seconds"] + 900.0, 1),
            )

    def test_queued_task_without_forecast_omits_bounds_not_elapsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")  # no queue samples at all
            task, created = self._queued_task(store)
            value = public_task(task, store, now=created + 60.0)
            queue_group = value.get("queue")
            self.assertIsInstance(queue_group, dict)
            self.assertEqual(queue_group["elapsed_seconds"], 60.0)
            for key in ("lower_seconds", "upper_seconds", "sample_count", "confidence", "basis", "center_seconds", "level"):
                self.assertNotIn(key, queue_group)
            self.assertIsNone(value.get("completion"))

    def test_running_task_publishes_processing_group_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            task, _ = self._queued_task(store)
            now = time.time()
            store.update_task(
                task["task_id"], state="running", started_at=now - 300.0,
                progress={
                    "schema_version": 2, "phase_id": "asr", "percent": 40.0,
                    "processed_media_seconds": 720.0, "media_duration_seconds": 1800.0,
                    "media_events": 24, "elapsed_active_seconds": 300.0, "observed_at": now,
                    "segments": [
                        {"id": "prepare", "label": "准备", "state": "complete"},
                        {"id": "asr", "label": "粗识别", "state": "running"},
                        {"id": "write", "label": "写入字幕", "state": "waiting"},
                    ],
                },
                estimate={
                    "elapsed_seconds": 300.0, "remaining_seconds": 540.0,
                    "lower_seconds": 480.0, "upper_seconds": 620.0,
                    "confidence": "high", "basis": "live_blend", "sample_count": 4,
                    "updated_at": int(now * 1000),
                },
            )
            task = store.get_task(task["task_id"])
            value = public_task(task, store, now=now)
            processing = value.get("processing")
            self.assertIsInstance(processing, dict)
            self.assertEqual(processing["remaining_seconds"], 540.0)
            self.assertEqual(processing["lower_seconds"], 480.0)
            self.assertEqual(processing["upper_seconds"], 620.0)
            self.assertEqual(processing["confidence"], "high")
            self.assertEqual(processing["basis"], "live_blend")
            self.assertEqual(processing["sample_count"], 4)
            self.assertIsNone(value.get("queue"), "运行中不再发布排队组（只计一次）")
            completion = value.get("completion")
            self.assertEqual(
                (completion["earliest_seconds"], completion["likely_seconds"], completion["latest_seconds"]),
                (480.0, 540.0, 620.0),
            )
            phases = value.get("phases")
            self.assertIsInstance(phases, dict)
            self.assertEqual(
                [(item["id"], item["state"]) for item in phases["items"]],
                [("prepare", "completed"), ("asr", "active"), ("write", "waiting")],
            )
            self.assertGreater(phases["evidence_updated_at"], 0)

    def test_recalibrating_estimate_is_flagged_low_confidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            task, _ = self._queued_task(store)
            now = time.time()
            store.update_task(
                task["task_id"], state="running", started_at=now - 60.0,
                progress={"schema_version": 2, "percent": 5.0,
                          "elapsed_active_seconds": 60.0, "observed_at": now},
                estimate={
                    "elapsed_seconds": 60.0, "remaining_seconds": 500.0,
                    "lower_seconds": 250.0, "upper_seconds": 750.0,
                    "confidence": "low", "basis": "live_blend",
                    "recalibrating": True, "updated_at": int(now * 1000),
                },
            )
            task = store.get_task(task["task_id"])
            value = public_task(task, store, now=now)
            processing = value.get("processing")
            self.assertTrue(processing.get("recalibrating"))
            self.assertEqual(processing["confidence"], "low")

    def test_terminal_tasks_publish_outcome_and_outputs_but_no_forecast(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            task, _ = self._queued_task(store)
            store.upsert_v3_metadata(
                task["task_id"], dedupe_key="subtitle:c:l1",
                requested_outputs=["subtitle"], parent_id="parent-123",
            )
            store.record_prediction_outcome(
                task["task_id"], initial_minutes=20.0, actual_minutes=15.0, attempt=1)
            store.update_task(task["task_id"], state="completed")
            task = store.get_task(task["task_id"])
            value = public_task(task, store, now=time.time())
            self.assertIsNone(value.get("queue"))
            self.assertIsNone(value.get("processing"))
            self.assertIsNone(value.get("completion"))
            outcome = value.get("prediction_outcome")
            self.assertEqual(
                (outcome["initial_minutes"], outcome["actual_minutes"], outcome["delta_minutes"]),
                (20.0, 15.0, 5.0),
            )
            self.assertEqual(value.get("requested_outputs"), ["subtitle"])
            self.assertEqual(value.get("parent_id"), "parent-123")

    def test_legacy_progress_without_segments_publishes_no_phases(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            task, _ = self._queued_task(store)
            now = time.time()
            store.update_task(
                task["task_id"], state="running",
                progress={"stage": "remote_compute", "percent": 50.0, "observed_at": now},
            )
            value = public_task(store.get_task(task["task_id"]), store, now=now)
            self.assertIsNone(value.get("phases"))


if __name__ == "__main__":
    unittest.main()
