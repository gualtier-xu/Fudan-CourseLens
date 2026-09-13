#!/usr/bin/env python3
"""GitHub-runner benchmark for the accepted Standard ASR/OCR stack.

Benchmark-only harness: this script never changes product behavior, worker
code, or repository settings; its only output is one sanitized JSON report.

Measured phases (labelled TRUSTED_PUBLIC_WORKER_RUNNER_EVIDENCE):

* ASR over a deterministic 180-second 16 kHz mono fixture built in the runner
  from Mandarin ``espeak-ng`` speech, seeded low-level background noise, and
  bounded silence.  The production ``sequential`` strategy (4 threads per
  model, sequential decode) and the internal ``parallel`` strategy (2 threads
  per model, two decode workers) each run one cold and two warm passes in
  fresh processes over ``courselens_worker.asr.RecognizerPool``.
* OCR over 18 distinct synthetic 1280x720 slide frames decoded in memory
  through ``courselens_worker.ocr.process_slides`` with concurrency 1, one
  cold and two warm passes.

Derived 90/180/330-minute figures are labelled ASR_ONLY_LOWER_BOUND: they
exclude proofreading, OCR, and every network-dependent phase.  AI-provider
latency is never measured here (label AI_NETWORK_NOT_MEASURED_NO_SECRET).

No transcript text, OCR text, frame bytes, audio, credentials, or model
artifacts are ever emitted, checked in, or retained by this script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

SCHEMA = "standard_runner_benchmark_report.v1"
FIXTURE_DURATION_SECONDS = 180.0
FIXTURE_SAMPLE_RATE = 16_000
FIXTURE_SEED = 20260913
FIXTURE_NOISE_RMS = 0.002
FIXTURE_SPEECH_PEAK = 0.5
FIXTURE_SPEAK_RATE_WPM = 170
FIXTURE_SPEECH_SECONDS = 3.0
FIXTURE_PERIOD_SECONDS = 5.0
FIXTURE_VOICE = "cmn"
OCR_FRAME_COUNT = 18
OCR_FRAME_WIDTH = 1280
OCR_FRAME_HEIGHT = 720
OCR_CONCURRENCY = 1
STRATEGIES = ("sequential", "parallel")
SEQUENTIAL_THREADS = 4
PARALLEL_THREADS = 2
PROJECTION_MINUTES = (90, 180, 330)
PARALLEL_PEAK_RSS_LIMIT_BYTES = int(12.0 * (1 << 30))
# Public ubuntu-24.04 hosted runners report more physical memory than the
# private 2-vCPU/8-GB pool; the decision additionally caps peak RSS at this
# fraction of the memory the runner itself reports.
PARALLEL_PEAK_RSS_PHYSICAL_FRACTION = 0.75
PARALLEL_MIN_WARM_IMPROVEMENT = 0.15
LABEL_RUNNER_EVIDENCE = "TRUSTED_PUBLIC_WORKER_RUNNER_EVIDENCE"
LABEL_PROJECTION = "ASR_ONLY_LOWER_BOUND"
LABEL_AI_NETWORK = "AI_NETWORK_NOT_MEASURED_NO_SECRET"
SCRATCH_DIRNAME = "standard-runner-benchmark-scratch"
REPORT_DEFAULT_PATH = "runtime/reports/standard-runner-benchmark.json"
# Reports are digests and counts only; any of these keys means a leak.
FORBIDDEN_REPORT_KEYS = frozenset({
    "text", "tokens", "transcript", "content", "segments", "raw", "audio", "frame_bytes",
})

# Generic synthetic sentences (no real course, account, or media content).
SENTENCES = (
    "今天我们讨论信号处理中的基本概念",
    "采样定理决定了离散信号的表达能力",
    "频谱分析可以揭示信号的内部结构",
    "滤波器设计需要权衡精度与复杂度",
    "噪声模型直接影响系统的稳定性",
    "接下来我们看一个简单的计算例子",
    "这个结论在工程实践中经常被使用",
    "请注意图中曲线的变化趋势",
    "不同的参数选择会得到不同的结果",
    "课后请复习本节的关键公式",
    "下一章我们将讨论更复杂的情况",
    "如果有疑问可以在讨论区提出",
)


def _canonical_dumps(value: Any) -> str:
    try:
        from shared.evidence_contract import canonical_json
    except Exception:
        canonical_json = None
    if canonical_json is not None:
        return canonical_json(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _peak_rss_bytes() -> int:
    """Peak RSS of the current process; 0 when the platform cannot report it."""
    try:
        import resource
    except Exception:
        return 0
    try:
        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except Exception:
        return 0
    # Linux reports KiB, macOS reports bytes.
    return peak * 1024 if sys.platform.startswith("linux") else peak


def _meminfo_total_kb(label: str) -> int:
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith(label + ":"):
                    return int(line.split()[1])
    except Exception:
        pass
    return 0


def _physical_memory_bytes() -> int:
    """Total physical memory in bytes; 0 when the platform cannot report it."""
    reported = _meminfo_total_kb("MemTotal")
    if reported > 0:
        return reported * 1024
    try:
        return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    except Exception:
        return 0


def _available_memory_bytes() -> int:
    """Memory currently reported available; 0 when the platform cannot report it."""
    return _meminfo_total_kb("MemAvailable") * 1024


def _disk_usage_snapshot() -> dict[str, int]:
    """Total/free bytes on the filesystem holding the benchmark workspace."""
    try:
        usage = shutil.disk_usage(Path.cwd())
    except Exception:
        return {"total_bytes": 0, "free_bytes": 0}
    return {"total_bytes": int(usage.total), "free_bytes": int(usage.free)}


def assert_sanitized(node: Any, path: str = "report") -> None:
    """Fail closed if any forbidden (text-bearing) key reached the report."""
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).casefold() in FORBIDDEN_REPORT_KEYS:
                raise RuntimeError(f"benchmark report leaked forbidden key at {path}.{key}")
            assert_sanitized(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            assert_sanitized(value, f"{path}[{index}]")


# --------------------------------------------------------------------------
# Fixture construction
# --------------------------------------------------------------------------

def espeak_command(sentence: str, wav_path: Path) -> list[str]:
    return [
        "espeak-ng", "-v", FIXTURE_VOICE, "-s", str(FIXTURE_SPEAK_RATE_WPM),
        "-a", "180", "-w", str(wav_path), sentence,
    ]


def resample_command(wav_path: Path, pcm_path: Path) -> list[str]:
    return [
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(wav_path),
        "-ar", str(FIXTURE_SAMPLE_RATE), "-ac", "1", "-f", "f32le", "-y", str(pcm_path),
    ]


def _run_command(command: list[str]) -> None:
    result = subprocess.run(
        command, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "benchmark fixture command failed with a closed-set status "
            f"(exit {result.returncode})"
        )


def _capturing_runner(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def check_mandarin_voice(
    runner: Callable[[list[str]], Any] = _capturing_runner,
) -> None:
    """Fail clearly when the espeak-ng Mandarin voice is unavailable."""
    try:
        result = runner(["espeak-ng", "--voices"])
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"espeak-ng is unavailable: no Mandarin voice check possible ({type(exc).__name__})") from exc
    listing = str(getattr(result, "stdout", "") or "")
    if not isinstance(listing, str):
        listing = listing.decode("utf-8", errors="ignore")
    for line in listing.splitlines():
        columns = line.split()
        if len(columns) >= 2 and columns[1] == FIXTURE_VOICE:
            return
    raise RuntimeError(
        f"espeak-ng Mandarin voice '{FIXTURE_VOICE}' is unavailable; "
        "the benchmark refuses to substitute other voices or real media"
    )


def espeak_utterance_provider(
    scratch_dir: Path,
    *,
    runner: Callable[[list[str]], Any] = _run_command,
) -> Callable[[str], Any]:
    """Production utterance provider: espeak-ng WAV -> ffmpeg 16 kHz f32le."""
    def provide(sentence: str):
        stem = hashlib.sha256(sentence.encode("utf-8")).hexdigest()[:16]
        wav = scratch_dir / f"utt-{stem}.wav"
        pcm = scratch_dir / f"utt-{stem}.f32"
        try:
            runner(espeak_command(sentence, wav))
            runner(resample_command(wav, pcm))
            import numpy as np
            samples = np.fromfile(pcm, dtype="<f4")
        finally:
            wav.unlink(missing_ok=True)
            pcm.unlink(missing_ok=True)
        if samples.size == 0:
            raise RuntimeError("espeak-ng produced an empty utterance")
        return samples
    return provide


def build_fixture(
    out_pcm: Path,
    *,
    duration_seconds: float = FIXTURE_DURATION_SECONDS,
    sample_rate: int = FIXTURE_SAMPLE_RATE,
    seed: int = FIXTURE_SEED,
    noise_rms: float = FIXTURE_NOISE_RMS,
    speech_seconds: float = FIXTURE_SPEECH_SECONDS,
    period_seconds: float = FIXTURE_PERIOD_SECONDS,
    utterance_provider: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Build the deterministic speech+noise fixture and return its identity.

    The mix owns the exact sample count: utterances are peak-normalized and
    clipped to the timeline, and every span between them stays bounded
    noise-only silence.  Only the SHA-256 and construction parameters are
    returned; no audio leaves the scratch directory.
    """
    if period_seconds <= speech_seconds:
        raise ValueError("fixture silence must be bounded (period must exceed speech)")
    total_float = duration_seconds * sample_rate
    if abs(total_float - round(total_float)) > 1e-6:
        raise ValueError("fixture duration must produce an exact whole sample count")
    total_samples = int(round(total_float))
    if utterance_provider is None:
        raise ValueError("an utterance provider is required")
    import numpy as np

    rng = np.random.default_rng(seed)
    mix = rng.normal(0.0, noise_rms, total_samples)
    spans: list[tuple[int, int]] = []
    utterance_count = int(duration_seconds // period_seconds)
    max_utterance_samples = int((period_seconds - 0.25) * sample_rate)
    trimmed = 0
    for index in range(utterance_count):
        start = int(round(index * period_seconds * sample_rate))
        samples = np.asarray(utterance_provider(SENTENCES[index % len(SENTENCES)]), dtype=np.float64)
        if len(samples) > max_utterance_samples:
            samples = samples[:max_utterance_samples]
            trimmed += 1
        end = min(total_samples, start + len(samples))
        segment = samples[: end - start]
        peak = float(np.max(np.abs(segment))) if segment.size else 0.0
        if peak > 0:
            segment = segment * (FIXTURE_SPEECH_PEAK / peak)
        mix[start:end] += segment
        spans.append((start, end))
    gaps = [spans[i + 1][0] - spans[i][1] for i in range(len(spans) - 1)]
    if not spans or not gaps or min(gaps) < int(0.2 * sample_rate):
        raise RuntimeError("fixture bounded-silence assertion failed")
    if len(mix) != total_samples:
        raise RuntimeError("fixture sample count assertion failed")
    mix = np.clip(mix, -1.0, 1.0).astype("<f4")
    out_pcm.parent.mkdir(parents=True, exist_ok=True)
    out_pcm.write_bytes(mix.tobytes())
    return {
        "sha256": _sha256_file(out_pcm),
        "duration_seconds": duration_seconds,
        "sample_rate": sample_rate,
        "sample_count": total_samples,
        "channels": 1,
        "encoding": "f32le",
        "seed": seed,
        "noise_rms": noise_rms,
        "speech_peak": FIXTURE_SPEECH_PEAK,
        "voice": FIXTURE_VOICE,
        "speak_rate_wpm": FIXTURE_SPEAK_RATE_WPM,
        "sentence_pool_size": len(SENTENCES),
        "utterance_count": utterance_count,
        "utterances_trimmed": trimmed,
        "speech_seconds": speech_seconds,
        "period_seconds": period_seconds,
        "min_gap_seconds": round(min(gaps) / sample_rate, 3),
        "generator": "espeak-ng+ffmpeg+numpy-v1",
    }


# --------------------------------------------------------------------------
# ASR metrics
# --------------------------------------------------------------------------

def _default_normalizer(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from courselens_worker.formats import normalize_segments
    return normalize_segments(segments)


def summarize_asr_segments(
    segments: list[dict[str, Any]],
    *,
    duration_ms: int,
    normalizer: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Counts, timestamp validity, and a normalized digest — never the text."""
    normalize = normalizer or _default_normalizer
    normalized = normalize(list(segments))
    chars = 0
    valid = True
    for item in normalized:
        text = str(item.get("text") or "")
        chars += len(text)
        start, end = int(item.get("start_ms") or 0), int(item.get("end_ms") or 0)
        if not (0 <= start < end <= duration_ms + 1_000):
            valid = False
    return {
        "count": len(normalized),
        "chars": chars,
        "timestamps_valid": valid,
        "digest": _sha256_bytes(_canonical_dumps(normalized).encode("utf-8")),
    }


def run_asr_pass_metrics(
    *,
    strategy: str,
    pcm_path: Path,
    sensevoice_dir: Path,
    firered_dir: Path,
    duration_seconds: float,
    pool_factory: Callable[[int], Any] | None = None,
) -> dict[str, Any]:
    """One fresh-process pass of the production decode path for one strategy."""
    if strategy not in STRATEGIES:
        raise ValueError(f"unsupported strategy {strategy!r}")

    if pool_factory is None:
        from courselens_worker.asr import RecognizerPool
        sensevoice_dir, firered_dir = Path(sensevoice_dir), Path(firered_dir)
        pool_factory = lambda threads: RecognizerPool(sensevoice_dir, firered_dir, threads=threads)
    threads = PARALLEL_THREADS if strategy == "parallel" else SEQUENTIAL_THREADS
    pool = pool_factory(threads)
    duration_ms = int(duration_seconds * 1000)
    per_backend: dict[str, float] = {}

    def timed(backend: str):
        def execute():
            started = time.monotonic()
            result = pool.transcribe_pcm(pcm_path, backend, offset_seconds=0.0)
            per_backend[backend] = time.monotonic() - started
            return result
        return execute

    combined_started = time.monotonic()
    if strategy == "parallel":
        # Production-equivalent: preload both recognizers, then two decode workers.
        pool.get("sensevoice")
        pool.get("firered")
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="bench-asr") as executor:
            futures = [
                executor.submit(timed(backend))
                for backend in ("sensevoice", "firered")
            ]
            sense = futures[0].result()
            fire = futures[1].result()
    else:
        sense = timed("sensevoice")()
        fire = timed("firered")()
    combined = time.monotonic() - combined_started

    sense_summary = summarize_asr_segments(sense, duration_ms=duration_ms)
    fire_summary = summarize_asr_segments(fire, duration_ms=duration_ms)
    return {
        "strategy": strategy,
        "threads_per_model": threads,
        "per_backend_seconds": {
            "sensevoice": round(per_backend.get("sensevoice", 0.0), 3),
            "firered": round(per_backend.get("firered", 0.0), 3),
        },
        "combined_seconds": round(combined, 3),
        "peak_rss_bytes": _peak_rss_bytes(),
        "segment_stats": {"sensevoice": sense_summary, "firered": fire_summary},
        "timestamps_valid": bool(sense_summary["timestamps_valid"] and fire_summary["timestamps_valid"]),
        "digests": {"sensevoice": sense_summary["digest"], "firered": fire_summary["digest"]},
    }


# --------------------------------------------------------------------------
# OCR metrics
# --------------------------------------------------------------------------

def build_ocr_frame_bytes(index: int, *, width: int = OCR_FRAME_WIDTH, height: int = OCR_FRAME_HEIGHT, seed: int = FIXTURE_SEED) -> bytes:
    """One deterministic synthetic slide frame, PNG-encoded in memory."""
    import io
    from PIL import Image, ImageDraw, ImageFont

    rng = random.Random(f"{seed}:{index}")
    top = (rng.randint(200, 235), rng.randint(205, 240), rng.randint(210, 245))
    bottom = (rng.randint(235, 255), rng.randint(235, 255), rng.randint(235, 255))
    image = Image.new("RGB", (width, height), top)
    draw = ImageDraw.Draw(image)
    for row in range(height):
        blend = row / max(1, height - 1)
        color = tuple(int(a + (b - a) * blend) for a, b in zip(top, bottom))
        draw.line([(0, row), (width, row)], fill=color)
    try:
        font = ImageFont.load_default(size=40)
        small = ImageFont.load_default(size=28)
    except TypeError:
        font = ImageFont.load_default()
        small = font
    draw.rectangle([60, 50, width - 60, 130], outline=(30, 45, 80), width=2)
    draw.text((80, 70), f"合成幻灯片 {index + 1}", fill=(20, 35, 70), font=font)
    for bullet in range(3):
        y = 190 + bullet * 110
        draw.ellipse([80, y + 12, 96, y + 28], fill=(30, 45, 80))
        draw.text((116, y), f"要点 {bullet + 1}: 数值 {rng.randint(10, 99)}%", fill=(35, 45, 65), font=small)
    draw.rectangle([80, 590, 120 + rng.randint(0, 400), 650], fill=(60, 90, 150))
    draw.line([(140, 640), (180, 600 + rng.randint(0, 30)), (260, 620), (400, 595)], fill=(150, 60, 60), width=4)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def run_ocr_pass_metrics(
    *,
    frame_count: int = OCR_FRAME_COUNT,
    width: int = OCR_FRAME_WIDTH,
    height: int = OCR_FRAME_HEIGHT,
    seed: int = FIXTURE_SEED,
    frames_provider: Callable[[], list[bytes]] | None = None,
    engine_factory: Callable[[], Any] | None = None,
    concurrency: int = OCR_CONCURRENCY,
) -> dict[str, Any]:
    """One fresh-process OCR pass through the production process_slides path.

    The only replaced seam is the slide byte source (network fetch becomes the
    in-memory synthetic deck); recognition, decoding, and hashing are the
    production code.  Frames and recognized text never leave this function.
    """
    import courselens_worker.ocr as ocr_module

    os.environ.setdefault("COURSELENS_OCR_CONCURRENCY", str(concurrency))
    frames = frames_provider() if frames_provider is not None else [
        build_ocr_frame_bytes(index, width=width, height=height, seed=seed)
        for index in range(frame_count)
    ]
    frames_generated = len(frames)
    original_fetch = ocr_module.fetch_bytes
    original_engine = getattr(ocr_module, "_engine", None)
    patched_engine = engine_factory is not None
    if patched_engine:
        ocr_module._engine = engine_factory

    def local_fetch(source: dict[str, Any], **_kwargs: Any) -> bytes:
        return frames[int(source["frame_index"])]

    ocr_module.fetch_bytes = local_fetch
    try:
        slides = [
            {"page_num": index + 1, "created_sec": index * 10, "source": {"frame_index": index}}
            for index in range(len(frames))
        ]
        started = time.monotonic()
        pages, skipped = ocr_module.process_slides(slides, progress=lambda *_: None)
        elapsed = time.monotonic() - started
    finally:
        ocr_module.fetch_bytes = original_fetch
        if patched_engine:
            if original_engine is None:
                delattr(ocr_module, "_engine")
            else:
                ocr_module._engine = original_engine
        del frames
    digest = _sha256_bytes(_canonical_dumps([
        {"page_num": int(page.get("page_num") or 0), "dhash": str(page.get("dhash") or "")}
        for page in pages
    ]).encode("utf-8"))
    line_counts = [
        len([line for line in str(page.get("text") or "").splitlines() if line.strip()])
        for page in pages
    ]
    return {
        "recognized_pages": len(pages),
        "skipped_total": int(sum(skipped.values())),
        "skipped": {str(key): int(value) for key, value in skipped.items()},
        "pages_with_text": sum(1 for count in line_counts if count > 0),
        "total_lines": int(sum(line_counts)),
        "digest": digest,
        "elapsed_seconds": round(elapsed, 3),
        "peak_rss_bytes": _peak_rss_bytes(),
        "frame_count": frames_generated,
        "width": width,
        "height": height,
        "concurrency": concurrency,
    }


# --------------------------------------------------------------------------
# Report assembly
# --------------------------------------------------------------------------

def _strategy_summary(passes: list[dict[str, Any]], *, fixture_duration: float) -> dict[str, Any]:
    warm = [p["combined_seconds"] for p in passes if p.get("pass_kind") == "warm"]
    warm_sorted = sorted(warm)
    mid = len(warm_sorted) // 2
    if not warm_sorted:
        median = 0.0
    elif len(warm_sorted) % 2:
        median = warm_sorted[mid]
    else:
        median = (warm_sorted[mid - 1] + warm_sorted[mid]) / 2
    cold = next((p for p in passes if p.get("pass_kind") == "cold"), None)
    cold_overhead = max(0.0, (cold or {}).get("combined_seconds", 0.0) - median) if cold else 0.0
    threads = passes[0].get("threads_per_model") if passes else None
    return {
        "threads_per_model": threads,
        "passes": passes,
        "warm_median_combined_seconds": round(median, 3),
        "warm_rtf": round(median / fixture_duration, 6) if fixture_duration > 0 else None,
        "cold_overhead_seconds": round(cold_overhead, 3),
    }


def project_asr_only(summary: dict[str, Any], *, fixture_duration: float) -> dict[str, Any]:
    """Warm-RTF projections for longer inputs; excludes OCR/AI/proofread."""
    rtf = summary.get("warm_rtf")
    overhead = float(summary.get("cold_overhead_seconds") or 0.0)
    if rtf is None:
        return {"label": LABEL_PROJECTION, "available": False}
    projections = {}
    for minutes in PROJECTION_MINUTES:
        projections[str(minutes)] = {
            "input_minutes": minutes,
            "projected_seconds": round(minutes * 60 * rtf + overhead, 1),
            "basis": "warm_median_rtf_plus_cold_overhead",
        }
    return {
        "label": LABEL_PROJECTION,
        "available": True,
        "warm_rtf": rtf,
        "cold_overhead_seconds": round(overhead, 3),
        "excludes": ["ocr", "proofread", "ai_network", "download"],
        "projections": projections,
    }


def select_strategy(
    sequential: dict[str, Any],
    parallel: dict[str, Any],
    *,
    rss_limit_bytes: int = PARALLEL_PEAK_RSS_LIMIT_BYTES,
    physical_memory_bytes: int | None = None,
    min_improvement: float = PARALLEL_MIN_WARM_IMPROVEMENT,
) -> dict[str, Any]:
    """Provisional recommendation only; changes no repository setting."""
    parity = True
    for backend in ("sensevoice", "firered"):
        seq_digests = {p["digests"][backend] for p in sequential["passes"]}
        par_digests = {p["digests"][backend] for p in parallel["passes"]}
        if not seq_digests or seq_digests != par_digests:
            parity = False
    all_passes = sequential["passes"] + parallel["passes"]
    timestamps_valid = all(bool(p.get("timestamps_valid")) for p in all_passes)
    peak_rss = max(int(p.get("peak_rss_bytes") or 0) for p in parallel["passes"])
    rss_ok = 0 < peak_rss <= rss_limit_bytes
    physical_memory = int(physical_memory_bytes or 0)
    physical_limit = int(physical_memory * PARALLEL_PEAK_RSS_PHYSICAL_FRACTION)
    # Fail closed when the runner does not report its physical memory.
    rss_physical_ok = 0 < peak_rss <= physical_limit if physical_limit > 0 else False
    seq_median = float(sequential["warm_median_combined_seconds"])
    par_median = float(parallel["warm_median_combined_seconds"])
    improvement = (1.0 - par_median / seq_median) if seq_median > 0 and par_median > 0 else 0.0
    improvement_ok = improvement >= min_improvement
    recommended = (
        "parallel"
        if (parity and timestamps_valid and rss_ok and rss_physical_ok and improvement_ok)
        else "sequential"
    )
    return {
        "recommended": recommended,
        "scope": "provisional internal strategy recommendation; no repository variable or product setting is changed",
        "conditions": {
            "output_parity_both_backends": parity,
            "timestamps_valid_all_passes": timestamps_valid,
            "parallel_peak_rss_bytes": peak_rss,
            "parallel_peak_rss_within_limit": rss_ok,
            "parallel_peak_rss_limit_bytes": rss_limit_bytes,
            "parallel_peak_rss_within_physical_limit": rss_physical_ok,
            "parallel_peak_rss_physical_limit_bytes": physical_limit,
            "physical_memory_bytes_reported": physical_memory,
            "warm_improvement_fraction": round(improvement, 4),
            "warm_improvement_meets_threshold": improvement_ok,
            "min_warm_improvement": min_improvement,
        },
    }


def _runner_metadata() -> dict[str, Any]:
    return {
        "os": os.environ.get("RUNNER_OS") or platform.system(),
        "arch": os.environ.get("RUNNER_ARCH") or platform.machine(),
        "platform": platform.platform(),
        "image": os.environ.get("ImageOS") or "",
        "image_version": os.environ.get("ImageVersion") or "",
        "logical_cpus": os.cpu_count(),
        "python": platform.python_version(),
        "physical_memory_bytes": _physical_memory_bytes(),
        "available_memory_bytes": _available_memory_bytes(),
        "disk": _disk_usage_snapshot(),
    }


def _run_metadata() -> dict[str, Any]:
    return {
        "repository": os.environ.get("GITHUB_REPOSITORY"),
        "workflow": os.environ.get("GITHUB_WORKFLOW"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "head_sha": os.environ.get("GITHUB_SHA"),
        "event": os.environ.get("GITHUB_EVENT_NAME"),
    }


def assemble_report(
    *,
    fixture: dict[str, Any],
    asr_by_strategy: dict[str, list[dict[str, Any]]],
    ocr_passes: list[dict[str, Any]],
    cache_hit: bool,
    model_restore_seconds: float,
    fixture_build_seconds: float,
    generated_at: str,
) -> dict[str, Any]:
    """Assemble the sanitized report and fail closed on any text leakage."""
    asr_blocks = {
        strategy: _strategy_summary(passes, fixture_duration=fixture["duration_seconds"])
        for strategy, passes in asr_by_strategy.items()
    }
    runner_metadata = _runner_metadata()
    physical_memory = int(runner_metadata.get("physical_memory_bytes") or 0)
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "labels": {
            "measured_asr_ocr": LABEL_RUNNER_EVIDENCE,
            "long_input_projections": LABEL_PROJECTION,
            "ai_provider_latency": LABEL_AI_NETWORK,
        },
        "runner": runner_metadata,
        "run": _run_metadata(),
        "setup": {
            "cache_hit": bool(cache_hit),
            "model_restore_seconds": round(model_restore_seconds, 1),
            "fixture_build_seconds": round(fixture_build_seconds, 3),
        },
        "fixture": fixture,
        "asr": {
            "fixture_duration_seconds": fixture["duration_seconds"],
            "evidence": LABEL_RUNNER_EVIDENCE,
            "strategies": asr_blocks,
        },
        "ocr": {
            "evidence": LABEL_RUNNER_EVIDENCE,
            "concurrency": OCR_CONCURRENCY,
            "frame_count": OCR_FRAME_COUNT,
            "passes": ocr_passes,
            "warm_median_elapsed_seconds": _strategy_summary(
                [{"combined_seconds": p["elapsed_seconds"], "pass_kind": p["pass_kind"], "peak_rss_bytes": p["peak_rss_bytes"], "threads_per_model": 1} for p in ocr_passes],
                fixture_duration=1.0,
            )["warm_median_combined_seconds"],
        },
        "projections": {
            strategy: project_asr_only(block, fixture_duration=fixture["duration_seconds"])
            for strategy, block in asr_blocks.items()
        },
        "selection": select_strategy(
            asr_blocks["sequential"], asr_blocks["parallel"],
            physical_memory_bytes=physical_memory,
        ),
        "notes": [
            LABEL_AI_NETWORK + ": no AI API key is used or measured by this benchmark",
            "digests cover normalized segments (anchors, text, optional token timing) without emitting any of it",
        ],
    }
    assert_sanitized(report)
    return report


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def _child_command(script: Path, name: str, arguments: list[str]) -> list[str]:
    return [sys.executable, str(script), name, *arguments]


def _run_child(script: Path, name: str, arguments: list[str]) -> dict[str, Any]:
    result = subprocess.run(
        _child_command(script, name, arguments),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        raise RuntimeError(
            f"benchmark child pass {name!r} failed (exit {result.returncode}): {detail[-1][:400] if detail else 'no diagnostics'}"
        )
    return json.loads(result.stdout)


def _ensure_child_pythonpath() -> None:
    # Public Worker layout: the courselens_worker package lives at the
    # repository root, so the repo root is the only path children need.
    root = Path(__file__).resolve().parents[1]
    existing = os.environ.get("PYTHONPATH", "")
    parts = [part for part in existing.split(os.pathsep) if part]
    if str(root) not in parts:
        parts.insert(0, str(root))
    os.environ["PYTHONPATH"] = os.pathsep.join(parts)


def cmd_run(args: argparse.Namespace) -> int:
    script = Path(__file__).resolve()
    if not args.sensevoice_dir or not args.firered_dir:
        raise RuntimeError(
            "model directories missing: set SENSEVOICE_MODEL_DIR and FIRERED_MODEL_DIR "
            "(written by scripts/install_models.py) or pass --sensevoice-dir/--firered-dir"
        )
    _ensure_child_pythonpath()
    scratch_root = Path(args.scratch).resolve() if args.scratch else Path(tempfile.gettempdir()) / SCRATCH_DIRNAME
    scratch_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=scratch_root))
    try:
        check_mandarin_voice()
        fixture_started = time.monotonic()
        utterances = espeak_utterance_provider(run_dir)
        pcm = run_dir / "fixture.f32le"
        fixture = build_fixture(pcm, duration_seconds=args.duration_seconds, utterance_provider=utterances)
        fixture_build = time.monotonic() - fixture_started

        asr_by_strategy: dict[str, list[dict[str, Any]]] = {}
        for strategy in STRATEGIES:
            passes = []
            for index, kind in enumerate(("cold", "warm", "warm")):
                metrics = _run_child(script, "asr-pass", [
                    "--strategy", strategy,
                    "--pcm", str(pcm),
                    "--sensevoice-dir", args.sensevoice_dir,
                    "--firered-dir", args.firered_dir,
                    "--duration-seconds", str(args.duration_seconds),
                ])
                metrics["pass"] = index
                metrics["pass_kind"] = kind
                passes.append(metrics)
                print(f"[bench] asr {strategy} {kind}: combined={metrics['combined_seconds']}s", file=sys.stderr)
            asr_by_strategy[strategy] = passes

        ocr_passes = []
        for index, kind in enumerate(("cold", "warm", "warm")):
            metrics = _run_child(script, "ocr-pass", ["--frame-count", str(OCR_FRAME_COUNT)])
            metrics["pass"] = index
            metrics["pass_kind"] = kind
            ocr_passes.append(metrics)
            print(f"[bench] ocr {kind}: elapsed={metrics['elapsed_seconds']}s", file=sys.stderr)

        report = assemble_report(
            fixture=fixture,
            asr_by_strategy=asr_by_strategy,
            ocr_passes=ocr_passes,
            cache_hit=str(args.cache_hit).strip().lower() == "true",
            model_restore_seconds=float(args.model_restore_seconds or 0.0),
            fixture_build_seconds=fixture_build,
            generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"[bench] report written: {out_path}", file=sys.stderr)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    return 0


def cmd_asr_pass(args: argparse.Namespace) -> int:
    metrics = run_asr_pass_metrics(
        strategy=args.strategy,
        pcm_path=Path(args.pcm),
        sensevoice_dir=Path(args.sensevoice_dir),
        firered_dir=Path(args.firered_dir),
        duration_seconds=args.duration_seconds,
    )
    json.dump(metrics, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def cmd_ocr_pass(args: argparse.Namespace) -> int:
    metrics = run_ocr_pass_metrics(
        frame_count=args.frame_count,
        width=OCR_FRAME_WIDTH,
        height=OCR_FRAME_HEIGHT,
    )
    json.dump(metrics, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standard-stack runner benchmark (benchmark-only)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="orchestrate all passes and write the report")
    run.add_argument("--out", default=REPORT_DEFAULT_PATH)
    run.add_argument("--sensevoice-dir", default=os.environ.get("SENSEVOICE_MODEL_DIR"))
    run.add_argument("--firered-dir", default=os.environ.get("FIRERED_MODEL_DIR"))
    run.add_argument("--duration-seconds", type=float, default=FIXTURE_DURATION_SECONDS)
    run.add_argument("--cache-hit", default="false")
    run.add_argument("--model-restore-seconds", type=float, default=0.0)
    run.add_argument("--scratch", default=None)
    run.set_defaults(handler=cmd_run)

    asr = subparsers.add_parser("asr-pass", help="internal: one fresh-process ASR pass (JSON on stdout)")
    asr.add_argument("--strategy", required=True, choices=list(STRATEGIES))
    asr.add_argument("--pcm", required=True)
    asr.add_argument("--sensevoice-dir", required=True)
    asr.add_argument("--firered-dir", required=True)
    asr.add_argument("--duration-seconds", type=float, default=FIXTURE_DURATION_SECONDS)
    asr.set_defaults(handler=cmd_asr_pass)

    ocr = subparsers.add_parser("ocr-pass", help="internal: one fresh-process OCR pass (JSON on stdout)")
    ocr.add_argument("--frame-count", type=int, default=OCR_FRAME_COUNT)
    ocr.set_defaults(handler=cmd_ocr_pass)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
