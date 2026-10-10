"""Unified, JSON-safe progress models for durable lecture tasks."""

from __future__ import annotations

import math
import time
import statistics
from dataclasses import dataclass
from typing import Callable, Iterable

from .estimator import EvolvingEstimate


@dataclass(frozen=True, slots=True)
class PhaseDefinition:
    id: str
    label: str
    category: str
    weight: float


# 唯一 automatic 字幕策略的两种运行形态：配置了 DeepSeek Key 时走 AI 校对
# 链；未配置时走非 AI 回退（单次精识别）。形态按行为命名，不是模式标签。
PROOFREAD_SUBTITLE_PHASES = (
    PhaseDefinition("prepare", "准备", "prepare", 0.03),
    PhaseDefinition("draft", "粗识别", "remote", 0.15),
    PhaseDefinition("context", "上下文", "remote", 0.07),
    PhaseDefinition("recognize", "精识别", "remote", 0.50),
    PhaseDefinition("proofread", "字幕校对", "remote", 0.21),
    PhaseDefinition("write", "写入字幕", "finalize", 0.04),
)

FALLBACK_SUBTITLE_PHASES = (
    PhaseDefinition("prepare", "准备", "prepare", 0.03),
    PhaseDefinition("recognize", "精识别", "remote", 0.92),
    PhaseDefinition("write", "写入字幕", "finalize", 0.05),
)

SUMMARY_PPT_PHASES = (
    PhaseDefinition("input", "载入字幕", "prepare", 0.03),
    PhaseDefinition("login", "PPT 清单", "network", 0.05),
    PhaseDefinition("ppt_fetch", "图片获取与去重", "network", 0.17),
    PhaseDefinition("ocr", "PPT OCR", "remote", 0.40),
    PhaseDefinition("ai_parts", "AI 分段总结", "remote", 0.25),
    PhaseDefinition("finalize", "合并与保存", "finalize", 0.10),
)

SUMMARY_TRANSCRIPT_PHASES = (
    PhaseDefinition("input", "载入字幕", "prepare", 0.05),
    PhaseDefinition("ai_parts", "AI 分段总结", "remote", 0.85),
    PhaseDefinition("finalize", "合并与保存", "finalize", 0.10),
)


SUBTITLE_STAGE_IDS = {
    "Preparing": "prepare",
    "input": "prepare",
    "asr": "recognize",
    "context": "context",
    "proofread": "proofread",
    "write": "write",
}


# Cold-start fallbacks are based on measured GitHub Worker execution.
# Real per-machine duration history immediately takes precedence once present.
SUBTITLE_BOOTSTRAP_RTFS = {
    "proofread": 0.60,
    "fallback": 0.55,
}


# Serial tail phases that run strictly AFTER the subtitle media (ASR) phase.
# prepare/draft/context/recognize overlap inside the remote ASR engine and are
# already covered by the end-to-end/live RTF, so only these genuinely serial
# phases are added on top of the media-pace ETA (critical path, never a
# mechanical sum of every stage).
SUBTITLE_SERIAL_TAIL_PHASES = {
    "proofread": ("proofread", "write"),
    "fallback": ("write",),
}

# Cold-start tail constants when no per-phase history exists yet (measured on
# sample GitHub Worker runs: LLM proofreading a full lecture takes minutes,
# the final write is short local serialization).  Per-phase history medians
# from ``estimate_samples`` replace them as soon as they exist.
SUBTITLE_TAIL_BOOTSTRAP_SECONDS = {
    "proofread": 240.0,
    "write": 30.0,
}

# Live ASR pace is blended into the historical RTF only after at least this
# many distinct monotonic processed-media advances (stable chunk heartbeats).
MIN_STABLE_MEDIA_EVENTS = 2
# Live blend weight: w = clamp(processed_fraction * LIVE_BLEND_K,
# LIVE_BLEND_WEIGHT_MIN, LIVE_BLEND_WEIGHT_MAX).  k=1.25 means the live pace
# dominates (w=0.75 cap) once ~60% of the media is processed, while early
# chunks stay anchored to history (w>=0.15).
LIVE_BLEND_K = 1.25
LIVE_BLEND_WEIGHT_MIN = 0.15
LIVE_BLEND_WEIGHT_MAX = 0.75
# One estimate may move at most this factor per progress event unless the
# phase changed (bounded smoothing; real slowdowns still raise the ETA, but
# single heartbeats cannot produce huge jumps).
MAX_ETA_STEP_FACTOR = 1.5

# Closed set of public estimate bases exposed by ``public_task``.
ESTIMATE_BASIS_VALUES = (
    "queue",
    "history_median",
    "live_blend",
    "bootstrap",
    "phase_history",
    "insufficient_data",
    "stale",
)

# --- Worker queue timing model (Stage 09-C) ---------------------------------
# Queue forecasts are published only after enough comparable trusted runs have
# been observed, so cold-start users never see an invented queue range.
QUEUE_SAMPLE_FLOOR = 5
QUEUE_SAMPLE_CONFIDENCE_MEDIUM = 10
QUEUE_SAMPLE_CONFIDENCE_HIGH = 15
# Robust band: median ± 1.4826·MAD widened to include the empirical 25%/90%
# quantiles of the fresh samples, so a single long outlier cannot create a
# narrow optimistic floor but real tail waits stay inside the named range.
QUEUE_BAND_K = 1.4826

# Closed basis set for the public ``queue`` object.  Distinct from
# ESTIMATE_BASIS_VALUES because queue forecasts never blend live pace.
QUEUE_BASIS_VALUES = ("history_median", "insufficient_data", "stale")


def queue_forecast_from_samples(samples: Iterable[float]) -> dict | None:
    """Robust queue-wait forecast from fresh comparable queued seconds.

    Returns ``None`` below :data:`QUEUE_SAMPLE_FLOOR` — publishing is
    forbidden until the floor is met.  Center is the median; bounds are the
    robust 1.4826·MAD band widened to the empirical q25/q90 so both the model
    and the observed spread agree before a range is named.
    """
    values = sorted(float(value) for value in samples if math.isfinite(float(value)) and float(value) > 0)
    count = len(values)
    if count < QUEUE_SAMPLE_FLOOR:
        return None
    center = statistics.median(values)
    mad = statistics.median(abs(value - center) for value in values)
    q25 = values[min(count - 1, int(round(0.25 * (count - 1))))]
    q90 = values[min(count - 1, int(round(0.90 * (count - 1))))]
    lower = max(0.0, min(center - QUEUE_BAND_K * mad, q25))
    upper = max(center + QUEUE_BAND_K * mad, q90)
    if upper < lower:
        upper = lower
    if count >= QUEUE_SAMPLE_CONFIDENCE_HIGH:
        confidence = "high"
    elif count >= QUEUE_SAMPLE_CONFIDENCE_MEDIUM:
        confidence = "medium"
    else:
        confidence = "low"
    return {
        "center_seconds": round(center, 1),
        "lower_seconds": round(lower, 1),
        "upper_seconds": round(upper, 1),
        "sample_count": count,
        "confidence": confidence,
        "basis": "history_median",
    }


def phase_plan(kind: str, *, proofread: bool = False, include_ppt: bool = True) -> tuple[PhaseDefinition, ...]:
    if kind == "subtitle":
        return PROOFREAD_SUBTITLE_PHASES if proofread else FALLBACK_SUBTITLE_PHASES
    if kind == "summary":
        return SUMMARY_PPT_PHASES if include_ppt else SUMMARY_TRANSCRIPT_PHASES
    raise ValueError(f"unsupported progress kind: {kind}")


def queued_progress(kind: str, *, proofread: bool = False, include_ppt: bool = True, label: str = "等待执行") -> dict:
    phases = phase_plan(kind, proofread=proofread, include_ppt=include_ppt)
    return {
        "schema_version": 2,
        "percent": 0.0,
        "label": label,
        "phase": None,
        "segments": [
            {
                "id": phase.id,
                "label": phase.label,
                "category": phase.category,
                "weight": phase.weight,
                "percent": 0.0,
                "state": "waiting",
                "completed_units": None,
                "total_units": None,
            }
            for phase in phases
        ],
        "elapsed_active_seconds": 0.0,
    }


def upgrade_progress(
    kind: str,
    value: dict | None,
    *,
    proofread: bool = False,
    include_ppt: bool = True,
) -> dict:
    """Lift legacy scalar progress into v2 without inventing completed work."""
    current = dict(value or {})
    if int(current.get("schema_version") or 0) >= 2:
        return current
    result = queued_progress(
        kind,
        proofread=proofread,
        include_ppt=include_ppt,
        label=str(current.get("label") or "等待执行"),
    )
    result["percent"] = max(0.0, min(100.0, float(current.get("percent") or 0.0)))
    result["elapsed_active_seconds"] = max(
        0.0,
        float(current.get("elapsed_active_seconds") or current.get("elapsed_seconds") or 0.0),
    )
    stage = str(current.get("stage") or "")
    phase_id = SUBTITLE_STAGE_IDS.get(stage, stage)
    phase_percent = current.get("stage_percent")
    for segment in result["segments"]:
        if segment["id"] != phase_id:
            continue
        segment["percent"] = max(0.0, min(100.0, float(phase_percent or 0.0)))
        segment["state"] = "running"
        segment["completed_units"] = current.get("chunk_index")
        segment["total_units"] = current.get("chunk_count")
        result["phase"] = {
            "id": segment["id"],
            "label": segment["label"],
            "category": segment["category"],
            "percent": segment["percent"],
        }
        break
    for key in ("stage", "stage_percent", "chunk_index", "chunk_count"):
        if key in current:
            result[key] = current[key]
    return result


class ProgressTracker:
    """Monotonic phase progress with a robust end-to-end ETA."""

    def __init__(
        self,
        kind: str,
        *,
        proofread: bool = False,
        include_ppt: bool = True,
        prior_costs: Iterable[float] = (),
        initial_progress: dict | None = None,
        initial_estimate: dict | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ):
        self.kind = kind
        self.proofread = proofread
        self.include_ppt = include_ppt
        self.phases = phase_plan(kind, proofread=proofread, include_ppt=include_ppt)
        self.clock = clock
        self.wall_clock = wall_clock
        self.started_at = clock()
        self._elapsed_offset = 0.0
        self._phase_percent = {phase.id: 0.0 for phase in self.phases}
        self._last_percent = 0.0
        self._current_phase = ""
        self._label = "准备任务"
        self._units: dict[str, tuple[float | None, float | None]] = {}
        self._sample_points: dict[str, tuple[float, float]] = {}
        self._pending_samples: list[tuple[str, str, float, float, float]] = []
        self._media_duration_seconds: float | None = None
        self._processed_media_seconds: float | None = None
        self._average_rtf: float | None = None
        self._estimate_basis = ""
        self._compat_fields: dict[str, object] = {}
        self._prior_rtfs: list[float] = []
        self._phase_tail_seconds: dict[str, float] = {}
        self._stable_media_events = 0
        self._announced_remaining: float | None = None
        self._announced_phase = ""
        self._eta_signature: tuple[float, str] | None = None
        behavior = "proofread" if proofread else "fallback"
        self.estimator = EvolvingEstimate(
            f"{kind}:{behavior}", 100.0, list(prior_costs), started_at=wall_clock()
        )
        if isinstance(initial_progress, dict):
            self._elapsed_offset = max(
                0.0,
                float(
                    initial_progress.get("elapsed_active_seconds")
                    or initial_progress.get("elapsed_seconds")
                    or 0.0
                ),
            )
            for segment in initial_progress.get("segments") or []:
                phase_id = str(segment.get("id") or "")
                if phase_id in self._phase_percent:
                    self._phase_percent[phase_id] = max(
                        0.0, min(100.0, float(segment.get("percent") or 0.0))
                    )
                    self._units[phase_id] = (
                        float(segment["completed_units"]) if segment.get("completed_units") is not None else None,
                        float(segment["total_units"]) if segment.get("total_units") is not None else None,
                    )
            self._last_percent = max(0.0, min(100.0, float(initial_progress.get("percent") or 0.0)))
            phase = initial_progress.get("phase") or {}
            self._current_phase = str(phase.get("id") or "")
            if not self._current_phase:
                # Persisted remote progress carries ``phase_id``/``stage``
                # instead of the full phase dict; keep the tail-phase position
                # across the per-heartbeat tracker rebuilds.
                stage = str(
                    initial_progress.get("phase_id") or initial_progress.get("stage") or ""
                )
                stage = SUBTITLE_STAGE_IDS.get(stage, stage)
                if stage in self._phase_percent:
                    self._current_phase = stage
                    try:
                        self._phase_percent[stage] = max(
                            self._phase_percent[stage],
                            min(100.0, float(initial_progress.get("phase_percent") or 0.0)),
                        )
                    except (TypeError, ValueError):
                        pass
            self._label = str(initial_progress.get("label") or self._label)
            try:
                self._stable_media_events = max(0, int(initial_progress.get("media_events") or 0))
            except (TypeError, ValueError):
                self._stable_media_events = 0
            self.estimator.completed_units = self._last_percent
            self.estimator.elapsed_seconds = self._elapsed_offset
            self.estimator.started_at = self.wall_clock() - self._elapsed_offset

            media_duration = initial_progress.get("media_duration_seconds")
            processed_media = initial_progress.get("processed_media_seconds")
            average_rtf = initial_progress.get("average_rtf")
            if media_duration is not None:
                try:
                    self._media_duration_seconds = max(0.0, float(media_duration)) or None
                except (TypeError, ValueError):
                    self._media_duration_seconds = None
            if processed_media is not None:
                try:
                    self._processed_media_seconds = max(0.0, float(processed_media))
                except (TypeError, ValueError):
                    self._processed_media_seconds = None
            if average_rtf is not None:
                try:
                    self._average_rtf = max(0.0, float(average_rtf)) or None
                except (TypeError, ValueError):
                    self._average_rtf = None
            self._compat_fields = {
                key: initial_progress[key]
                for key in (
                    "stage", "stage_percent", "chunk_index", "chunk_count",
                    "phase_id", "completed", "total", "media_events",
                )
                if key in initial_progress
            }
        if isinstance(initial_estimate, dict):
            # Seed the bounded-smoothing anchor from the last persisted
            # estimate so per-heartbeat tracker rebuilds inherit the previous
            # prediction instead of being able to jump freely.
            try:
                announced = float(initial_estimate.get("remaining_seconds"))
            except (TypeError, ValueError):
                announced = 0.0
            if math.isfinite(announced) and announced > 0:
                self._announced_remaining = announced
            self._announced_phase = self._current_phase

    def configure_duration_estimate(
        self,
        media_duration_seconds: float | None,
        *,
        historical_rtfs: Iterable[float] = (),
        processed_media_seconds: float | None = None,
        average_rtf: float | None = None,
        stable_media_events: int = 0,
        phase_tail_seconds: dict[str, float] | None = None,
    ) -> None:
        """Scale subtitle ETA history by real media length instead of task percent.

        Historical values are end-to-end real-time factors (active task seconds
        per media second).  Once at least ``MIN_STABLE_MEDIA_EVENTS`` monotonic
        chunk advances exist, the live processed-media pace is bounded-blended
        into the historical RTF (see :meth:`_duration_eta`); only the genuinely
        serial tail phases (proofread, write) are added on top.
        """
        duration = max(0.0, float(media_duration_seconds or 0.0))
        if duration <= 0:
            return
        self._media_duration_seconds = duration
        if processed_media_seconds is not None:
            self._processed_media_seconds = max(
                float(self._processed_media_seconds or 0.0),
                min(duration, max(0.0, float(processed_media_seconds))),
            )
        try:
            live_rtf = float(average_rtf or 0.0)
        except (TypeError, ValueError):
            live_rtf = 0.0
        if live_rtf > 0:
            self._average_rtf = live_rtf
        try:
            self._stable_media_events = max(0, int(stable_media_events or 0))
        except (TypeError, ValueError):
            self._stable_media_events = 0
        self._phase_tail_seconds = {
            str(key): max(0.0, float(value))
            for key, value in dict(phase_tail_seconds or {}).items()
        }
        rtfs = []
        for raw in historical_rtfs:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                rtfs.append(value)
        self._prior_rtfs = rtfs
        if rtfs:
            self.estimator.prior_costs = [duration * value / 100.0 for value in rtfs]
            self._estimate_basis = "duration-history"
        elif self._average_rtf:
            self.estimator.prior_costs = [duration * self._average_rtf / 100.0]
            self._estimate_basis = "duration-live"
        elif self.kind == "subtitle":
            behavior = "proofread" if self.proofread else "fallback"
            bootstrap_rtf = SUBTITLE_BOOTSTRAP_RTFS[behavior]
            self.estimator.prior_costs = [duration * bootstrap_rtf / 100.0]
            self._estimate_basis = "duration-bootstrap"
        self._observe_estimator()

    def _observe_estimator(self) -> None:
        """Advance duration-led subtitle ETA by wall time, not stage jumps."""
        if self._estimate_basis.startswith("duration-") and self.estimator.prior_costs:
            seconds_per_percent = statistics.median(self.estimator.prior_costs)
            if seconds_per_percent > 0:
                self.estimator.completed_units = max(
                    0.0,
                    min(100.0, self.elapsed / seconds_per_percent),
                )
                self.estimator.elapsed_seconds = max(self.estimator.elapsed_seconds, self.elapsed)
                return
        self.estimator.observe(self._last_percent, self.elapsed)

    def _remaining_tail_seconds(self) -> tuple[float, bool]:
        """Serial tail after the media phase: ``(seconds, in_tail_phase)``.

        Only phases in ``SUBTITLE_SERIAL_TAIL_PHASES`` for the mode are summed
        (critical path).  When the current phase is already inside the tail,
        its consumed share is inherited via the phase percent — the remaining
        part is ``median * (1 - phase_fraction)`` and earlier tail phases are
        never re-charged, so the ETA never resets to a fresh full estimate.
        """
        chain = SUBTITLE_SERIAL_TAIL_PHASES["proofread" if self.proofread else "fallback"]
        history = self._phase_tail_seconds or {}
        current = self._current_phase
        total = 0.0
        reached_current = current not in chain
        for phase_id in chain:
            try:
                seconds = max(0.0, float(history.get(phase_id) or 0.0))
            except (TypeError, ValueError):
                seconds = 0.0
            if seconds <= 0:
                seconds = SUBTITLE_TAIL_BOOTSTRAP_SECONDS.get(phase_id, 0.0)
            if phase_id == current:
                reached_current = True
                fraction = max(0.0, min(1.0, (self._phase_percent.get(phase_id) or 0.0) / 100.0))
                total += max(0.0, seconds * (1.0 - fraction))
            elif reached_current:
                total += seconds
        return total, current in chain

    def _duration_eta(self) -> dict | None:
        """Blended live+history media ETA plus the serial tail.

        ``prior`` is the robust median of historical end-to-end RTFs.
        ``live`` is the measured RTF (active elapsed / processed media) and is
        only trusted after ``MIN_STABLE_MEDIA_EVENTS`` monotonic chunk
        advances, winsorized into ``[prior/8, prior*8]``.  Blend weight grows
        with the processed fraction: ``w = clamp(fraction * 1.25, 0.15,
        0.75)``.  Then ``remaining = remaining_media * blended_rtf +
        serial_tail``.  Each progress event may move the estimate by at most
        ``MAX_ETA_STEP_FACTOR`` (unless the phase changed), so slowdowns raise
        the ETA in bounded steps while heartbeats never jump wildly.
        """
        if self._media_duration_seconds is None:
            return None
        if not self._estimate_basis.startswith("duration-"):
            return None
        if self._last_percent >= 100.0:
            return None
        duration = float(self._media_duration_seconds)
        processed = min(duration, max(0.0, float(self._processed_media_seconds or 0.0)))
        fraction = processed / duration if duration > 0 else 0.0
        active_elapsed = max(0.0, self.elapsed)
        prior_rtfs = [float(value) for value in self._prior_rtfs if float(value) > 0]
        prior = statistics.median(prior_rtfs) if prior_rtfs else None
        live: float | None = None
        if processed > 0 and active_elapsed > 0:
            # RTF units are active seconds per media second, matching the
            # historical end-to-end priors and the bootstrap constants.  The
            # live processed pace (processed / elapsed) is media throughput;
            # its reciprocal is the dimensionally consistent live RTF.
            live = active_elapsed / processed
            if prior is not None:
                live = max(prior / 8.0, min(prior * 8.0, live))
        stable = self._stable_media_events >= MIN_STABLE_MEDIA_EVENTS and live is not None
        if prior is not None and stable:
            weight = max(
                LIVE_BLEND_WEIGHT_MIN,
                min(LIVE_BLEND_WEIGHT_MAX, fraction * LIVE_BLEND_K),
            )
            rtf = (1.0 - weight) * prior + weight * float(live)
            driver = "live_blend"
        elif prior is not None:
            rtf = float(prior)
            driver = "history_median"
        elif stable and fraction >= 0.02:
            rtf = float(live)
            driver = "live_blend"
        elif self._average_rtf:
            # Restart-carried pace measured on this task's own earlier attempt.
            rtf = float(self._average_rtf)
            driver = "live_blend"
        else:
            rtf = SUBTITLE_BOOTSTRAP_RTFS["proofread" if self.proofread else "fallback"]
            driver = "bootstrap"
        tail_seconds, in_tail_phase = self._remaining_tail_seconds()
        remaining_media = max(0.0, duration - processed)
        raw_remaining = remaining_media * rtf + tail_seconds
        remaining = raw_remaining
        recalibrating = False
        previous = self._announced_remaining
        phase_changed = self._current_phase != self._announced_phase
        if previous is not None and previous > 0 and not phase_changed:
            # Bounded smoothing: one progress event moves the estimate by at
            # most MAX_ETA_STEP_FACTOR.  Repeated snapshots at the same state
            # are idempotent because the announced anchor only moves on new
            # (processed, phase) signatures.  When the model wants to move
            # farther than the cap allows, the published interval keeps the
            # bounded value but is marked recalibrating so the UI can say
            # 「重新估算」 instead of pretending the bounded number is exact.
            bound_low = previous / MAX_ETA_STEP_FACTOR
            bound_high = previous * MAX_ETA_STEP_FACTOR
            if raw_remaining < bound_low or raw_remaining > bound_high:
                recalibrating = True
            remaining = max(bound_low, min(bound_high, raw_remaining))
        signature = (round(processed, 3), self._current_phase)
        if signature != self._eta_signature:
            self._eta_signature = signature
            self._announced_remaining = remaining
            self._announced_phase = self._current_phase
        samples = list(prior_rtfs)
        if stable and live is not None:
            samples.append(float(live))
        center = statistics.median(samples) if samples else rtf
        if samples and center > 0:
            spread = statistics.median([abs(value - center) for value in samples]) / center
        else:
            spread = 0.5
        raw_relative = max(0.15, min(0.8, 1.4826 * spread))
        if len(prior_rtfs) >= 3 and raw_relative <= 0.3:
            confidence = "high"
        elif len(prior_rtfs) >= 2 or stable or self.estimator.observations >= 1:
            confidence = "medium"
        else:
            confidence = "low"
        # A bounded correction that had to clamp the raw move means the model
        # disagrees with its own anchor: downgrade to low confidence BEFORE
        # the honesty floor so the published interval also widens to the
        # low band — a low-confidence range must never be narrower than the
        # medium one it replaced.
        if recalibrating:
            confidence = "low"
        # Honesty floor: weak evidence must not publish a narrow interval.
        floor = {"high": 0.15, "medium": 0.30, "low": 0.50}[confidence]
        relative = max(floor, raw_relative)
        result = {
            "remaining_seconds": round(max(0.0, remaining), 1),
            "total_seconds": round(active_elapsed + max(0.0, remaining), 1),
            "lower_seconds": round(max(0.0, remaining * (1.0 - relative)), 1),
            "upper_seconds": round(remaining * (1.0 + relative), 1),
            "confidence": confidence,
            "basis": "phase_history" if in_tail_phase else driver,
        }
        if recalibrating:
            result["recalibrating"] = True
        return result

    @property
    def elapsed(self) -> float:
        return self._elapsed_offset + max(0.0, self.clock() - self.started_at)

    @property
    def media_duration_seconds(self) -> float | None:
        return self._media_duration_seconds

    def update(
        self,
        phase_id: str,
        phase_percent: float,
        *,
        label: str = "",
        completed_units: float | None = None,
        total_units: float | None = None,
    ) -> tuple[dict, dict]:
        if phase_id not in self._phase_percent:
            phase_id = self.phases[0].id
        value = max(0.0, min(100.0, float(phase_percent or 0.0)))
        previous_percent = self._phase_percent[phase_id]
        sample_units = float(completed_units) if completed_units is not None else value
        previous_units, previous_elapsed = self._sample_points.get(
            phase_id,
            (0.0 if completed_units is not None else previous_percent, self.elapsed),
        )
        current_elapsed = self.elapsed
        delta_units = sample_units - previous_units
        delta_elapsed = current_elapsed - previous_elapsed
        if delta_units > 0 and delta_elapsed > 0:
            phase = next(item for item in self.phases if item.id == phase_id)
            self._pending_samples.append((
                phase.id,
                phase.category,
                delta_elapsed / delta_units,
                delta_units,
                delta_elapsed,
            ))
        self._sample_points[phase_id] = (max(previous_units, sample_units), current_elapsed)
        self._phase_percent[phase_id] = max(self._phase_percent[phase_id], value)
        self._current_phase = phase_id
        if label:
            self._label = str(label)
        self._units[phase_id] = (
            float(completed_units) if completed_units is not None else None,
            float(total_units) if total_units is not None else None,
        )
        computed = sum(phase.weight * self._phase_percent[phase.id] for phase in self.phases)
        self._last_percent = max(self._last_percent, min(100.0, computed))
        self._observe_estimator()
        return self.snapshot()

    def drain_cost_samples(self) -> list[tuple[str, str, float, float, float]]:
        samples = self._pending_samples
        self._pending_samples = []
        return samples

    def complete(self, label: str = "已完成") -> tuple[dict, dict]:
        for phase in self.phases:
            self._phase_percent[phase.id] = 100.0
        self._current_phase = self.phases[-1].id
        self._last_percent = 100.0
        self._label = label
        self._estimate_basis = ""
        self.estimator.observe(100.0, self.elapsed)
        return self.snapshot()

    def snapshot(self) -> tuple[dict, dict]:
        current_index = next(
            (index for index, phase in enumerate(self.phases) if phase.id == self._current_phase),
            -1,
        )
        current = self.phases[current_index] if current_index >= 0 else None
        segments = []
        for index, phase in enumerate(self.phases):
            value = round(self._phase_percent[phase.id], 1)
            completed, total = self._units.get(phase.id, (None, None))
            # 串行阶段轨按序判态：进度已进入后段后，前段最后一次上报可能
            # 不足 100%（断点恢复/聚合上报），但必然已被越过——判 complete
            # 而非 waiting，前端无需再派生修正（U⑨ 三态语义的域层根修）。
            if value >= 100 or index < current_index:
                state = "complete"
            elif index == current_index:
                state = "running"
            else:
                state = "waiting"
            segments.append({
                "id": phase.id,
                "label": phase.label,
                "category": phase.category,
                "weight": phase.weight,
                "percent": value,
                "state": state,
                "completed_units": completed,
                "total_units": total,
            })
        progress = {
            "schema_version": 2,
            "percent": round(self._last_percent, 1),
            "label": self._label,
            "phase": ({
                "id": current.id,
                "label": current.label,
                "category": current.category,
                "percent": round(self._phase_percent[current.id], 1),
            } if current else None),
            "segments": segments,
            "elapsed_active_seconds": round(self.elapsed, 1),
            "observed_at": self.wall_clock(),
        }
        progress.update(self._compat_fields)
        if self._media_duration_seconds is not None:
            progress["media_duration_seconds"] = round(self._media_duration_seconds, 3)
        if self._processed_media_seconds is not None:
            progress["processed_media_seconds"] = round(self._processed_media_seconds, 3)
        if self._average_rtf is not None:
            progress["average_rtf"] = round(self._average_rtf, 4)
        estimate = self.estimator.snapshot(now=self.wall_clock())
        estimate["basis"] = self._estimate_basis or (
            "phase_history" if self.estimator.prior_costs and self.estimator.observations
            else "live_blend" if self.estimator.observations
            else "phase_history" if self.estimator.prior_costs
            else "insufficient_data"
        )
        override = self._duration_eta()
        if override:
            estimate.update(override)
        if self._prior_rtfs:
            estimate["sample_count"] = len(self._prior_rtfs)
        return progress, estimate


__all__ = [
    "ESTIMATE_BASIS_VALUES",
    "QUEUE_BAND_K",
    "QUEUE_BASIS_VALUES",
    "QUEUE_SAMPLE_CONFIDENCE_HIGH",
    "QUEUE_SAMPLE_CONFIDENCE_MEDIUM",
    "QUEUE_SAMPLE_FLOOR",
    "PhaseDefinition",
    "ProgressTracker",
    "SUBTITLE_BOOTSTRAP_RTFS",
    "SUBTITLE_SERIAL_TAIL_PHASES",
    "SUBTITLE_STAGE_IDS",
    "SUBTITLE_TAIL_BOOTSTRAP_SECONDS",
    "phase_plan",
    "queued_progress",
    "queue_forecast_from_samples",
    "upgrade_progress",
]
