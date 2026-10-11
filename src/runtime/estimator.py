"""Robust evolving duration estimates for remote task stages."""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Iterable


def _median(values: Iterable[float]) -> float | None:
    clean = [float(v) for v in values if math.isfinite(float(v)) and float(v) > 0]
    return statistics.median(clean) if clean else None


def _mad(values: list[float], center: float) -> float:
    return statistics.median([abs(value - center) for value in values]) if values else 0.0


@dataclass(slots=True)
class EvolvingEstimate:
    profile_key: str
    total_units: float
    prior_costs: list[float] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    completed_units: float = 0.0
    elapsed_seconds: float = 0.0
    live_cost: float | None = None
    observations: int = 0

    def observe(self, completed_units: float, elapsed_seconds: float) -> None:
        completed_units = max(0.0, float(completed_units))
        elapsed_seconds = max(0.0, float(elapsed_seconds))
        delta_units = completed_units - self.completed_units
        delta_elapsed = elapsed_seconds - self.elapsed_seconds
        self.completed_units = max(self.completed_units, completed_units)
        self.elapsed_seconds = max(self.elapsed_seconds, elapsed_seconds)
        if delta_units <= 0 or delta_elapsed <= 0:
            return
        sample = delta_elapsed / delta_units
        prior = _median(self.prior_costs)
        if prior and (sample > prior * 8 or sample < prior / 8):
            sample = max(prior / 8, min(prior * 8, sample))
        if self.live_cost is not None:
            # Subtitle progress advances in stage and proofreading-window
            # jumps.  A single long window can therefore produce a very
            # large marginal cost even though the end-to-end pace is stable.
            # Bound each update before the EMA so estimates respond smoothly
            # instead of temporarily doubling late in a task.
            sample = max(self.live_cost * 0.5, min(self.live_cost * 1.5, sample))
        self.live_cost = sample if self.live_cost is None else 0.3 * sample + 0.7 * self.live_cost
        self.observations += 1

    def snapshot(self, now: float | None = None) -> dict:
        now = float(now or time.time())
        elapsed = max(self.elapsed_seconds, now - self.started_at)
        prior = _median(self.prior_costs)
        if self.live_cost is None and prior is None:
            return {
                "elapsed_seconds": round(elapsed, 1),
                "remaining_seconds": None,
                "total_seconds": None,
                "lower_seconds": None,
                "upper_seconds": None,
                "confidence": "low",
                "updated_at": int(now * 1000),
            }
        if prior is None:
            cost = float(self.live_cost or 0.0)
        elif self.live_cost is None:
            cost = prior
        else:
            weight = self.observations / (self.observations + 3.0)
            cost = (1.0 - weight) * prior + weight * self.live_cost
        remaining_units = max(0.0, float(self.total_units) - self.completed_units)
        remaining = max(0.0, remaining_units * cost)
        total = elapsed + remaining
        samples = [v for v in self.prior_costs if v > 0]
        if self.live_cost:
            samples.append(self.live_cost)
        center = _median(samples) or cost
        spread = (_mad(samples, center) / center) if center > 0 else 0.5
        relative = max(0.15, min(0.8, 1.4826 * spread))
        confidence = "low"
        if self.observations >= 3 and relative <= 0.3:
            confidence = "high"
        elif self.observations >= 1 or len(self.prior_costs) >= 2:
            confidence = "medium"
        return {
            "elapsed_seconds": round(elapsed, 1),
            "remaining_seconds": round(remaining, 1),
            "total_seconds": round(total, 1),
            "lower_seconds": round(max(0.0, remaining * (1.0 - relative)), 1),
            "upper_seconds": round(remaining * (1.0 + relative), 1),
            "confidence": confidence,
            "updated_at": int(now * 1000),
        }


def estimate_from_progress(progress: dict, prior_seconds: float | None = None) -> dict:
    """Fallback ETA for restored/queued jobs that lack a live estimator."""
    percent = max(0.0, min(100.0, float(progress.get("percent") or 0.0)))
    elapsed = max(0.0, float(progress.get("elapsed_seconds") or 0.0))
    total = None
    if percent >= 1 and elapsed > 0:
        total = elapsed / (percent / 100.0)
    elif prior_seconds and prior_seconds > 0:
        total = float(prior_seconds)
    remaining = max(0.0, total - elapsed) if total is not None else None
    return {
        "elapsed_seconds": round(elapsed, 1),
        "remaining_seconds": round(remaining, 1) if remaining is not None else None,
        "total_seconds": round(total, 1) if total is not None else None,
        "lower_seconds": round(remaining * 0.7, 1) if remaining is not None else None,
        "upper_seconds": round(remaining * 1.4, 1) if remaining is not None else None,
        "confidence": "medium" if percent >= 10 else "low",
        "updated_at": int(time.time() * 1000),
    }
