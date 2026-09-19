"""CPU-only ASR over bounded transient PCM chunks.

Subtitle anchors come from frame-energy voiced regions inside each decoding
window instead of the full window, so silence never produces a cue and
continuous speech degrades to one bounded region.  Recognized segments carry
optional evidence.v1 provenance stamped only when the decoded-PCM fingerprint
chain is verifiable for the whole run.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import numpy as np
import sherpa_onnx

from shared.evidence_contract import (
    NAMESPACE_SEGMENT,
    NAMESPACE_SOURCE,
    canonical_json,
    compute_id,
)

from .formats import normalize_segments
from .source import (
    MediaResponseProfile,
    pinned_media_proxy,
)

SAMPLE_RATE = 16_000
PCM_CHUNK_SECONDS = 10 * 60
ASR_WINDOW_SECONDS = 30

# Frame-energy voice activity: deterministic, NumPy-only, no model.  The one
# documented calibration knob is COURSELENS_ASR_ENERGY_RATIO (voiced threshold
# as a ratio over the window noise floor); everything else is a fixed default
# chosen conservatively so noisy recordings expand toward the old full-window
# behavior instead of losing speech.
VAD_FRAME_SECONDS = 0.025
VAD_HOP_SECONDS = 0.010
VAD_NOISE_PERCENTILE = 10.0
VAD_SILENCE_FLOOR_RMS = 1e-4
VAD_PEAK_GUARD_RATIO = 0.1
VAD_MERGE_GAP_SECONDS = 0.4
VAD_PAD_SECONDS = 0.15
VAD_MIN_REGION_SECONDS = 0.1
VAD_MAX_REGION_SECONDS = float(ASR_WINDOW_SECONDS)
ASR_ENERGY_RATIO_ENV = "COURSELENS_ASR_ENERGY_RATIO"
ASR_ENERGY_RATIO_DEFAULT = 3.0
ASR_ENERGY_RATIO_MIN = 1.5
ASR_ENERGY_RATIO_MAX = 10.0

# Evidence provenance stamped on complete-run output.  The fingerprint hashes
# only the decoded PCM representation; URLs, secrets, and course identifiers
# never enter it, and no original-media hash is claimed.
PRODUCER_ID = "courselens-worker"
PCM_FINGERPRINT_DOMAIN = b"courselens-pcm-fingerprint-v1"
_PCM_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


def asr_energy_ratio() -> float:
    """Resolve the single calibration knob; anything invalid falls back."""
    raw = os.environ.get(ASR_ENERGY_RATIO_ENV)
    if raw is None or str(raw).strip() == "":
        return ASR_ENERGY_RATIO_DEFAULT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return ASR_ENERGY_RATIO_DEFAULT
    if not math.isfinite(value) or not (
        ASR_ENERGY_RATIO_MIN <= value <= ASR_ENERGY_RATIO_MAX
    ):
        return ASR_ENERGY_RATIO_DEFAULT
    return value


def detect_voiced_regions(
    samples: "np.ndarray",
    *,
    sample_rate: int = SAMPLE_RATE,
    energy_ratio: float | None = None,
    merge_gap_seconds: float = VAD_MERGE_GAP_SECONDS,
    pad_seconds: float = VAD_PAD_SECONDS,
    min_region_seconds: float = VAD_MIN_REGION_SECONDS,
    max_region_seconds: float = VAD_MAX_REGION_SECONDS,
) -> list[tuple[int, int]]:
    """Bounded voiced (start_sample, end_sample) regions inside one window.

    Frames are 25 ms with a 10 ms hop; a frame is voiced when its RMS exceeds
    max(noise_floor * energy_ratio, silence_floor) with the threshold capped at
    peak * guard so continuous speech can never push the threshold above
    itself.  Runs shorter than ``min_region_seconds`` are dropped, gaps below
    ``merge_gap_seconds`` are merged, regions longer than
    ``max_region_seconds`` are split, and padding is bounded by the window and
    by half of each neighboring gap so regions stay ordered and disjoint.
    """
    if energy_ratio is None:
        energy_ratio = asr_energy_ratio()
    total = int(len(samples))
    frame = max(1, int(VAD_FRAME_SECONDS * sample_rate))
    hop = max(1, int(VAD_HOP_SECONDS * sample_rate))
    if total < frame:
        return []
    squared = np.square(samples, dtype=np.float64)
    cumulative = np.concatenate(([0.0], np.cumsum(squared)))
    frame_count = (total - frame) // hop + 1
    frame_starts = np.arange(frame_count, dtype=np.int64) * hop
    energies = np.sqrt((cumulative[frame_starts + frame] - cumulative[frame_starts]) / frame)
    noise_floor = float(np.percentile(energies, VAD_NOISE_PERCENTILE))
    peak = float(np.max(energies))
    threshold = max(noise_floor * energy_ratio, VAD_SILENCE_FLOOR_RMS)
    threshold = min(threshold, max(peak * VAD_PEAK_GUARD_RATIO, VAD_SILENCE_FLOOR_RMS))
    voiced = energies >= threshold
    regions: list[list[int]] = []
    index = 0
    while index < frame_count:
        if not voiced[index]:
            index += 1
            continue
        stop = index
        while stop + 1 < frame_count and voiced[stop + 1]:
            stop += 1
        regions.append([int(frame_starts[index]), min(int(frame_starts[stop]) + frame, total)])
        index = stop + 1
    min_samples = max(1, int(min_region_seconds * sample_rate))
    regions = [region for region in regions if region[1] - region[0] >= min_samples]
    merged: list[list[int]] = []
    gap_samples = int(merge_gap_seconds * sample_rate)
    for region in regions:
        if merged and region[0] - merged[-1][1] < gap_samples:
            merged[-1][1] = max(merged[-1][1], region[1])
        else:
            merged.append(region)
    max_samples = max(1, int(max_region_seconds * sample_rate))
    bounded: list[list[int]] = []
    for start, end in merged:
        cursor = start
        while end - cursor > max_samples:
            bounded.append([cursor, cursor + max_samples])
            cursor += max_samples
        bounded.append([cursor, end])
    pad_samples = int(pad_seconds * sample_rate)
    padded: list[tuple[int, int]] = []
    for position, (start, end) in enumerate(bounded):
        previous_end = padded[-1][1] if padded else 0
        next_start = bounded[position + 1][0] if position + 1 < len(bounded) else total
        region_start = max(previous_end, start - min(pad_samples, (start - previous_end) // 2))
        region_end = min(next_start, end + min(pad_samples, (next_start - end) // 2))
        padded.append((region_start, max(region_start, region_end)))
    return padded


def _native_token_timing(result: Any, start_ms: int, end_ms: int) -> list[list[Any]] | None:
    """Absolute [text, start_ms, None] triples when native timing is well-shaped.

    Any doubt omits the whole set: tokens and timestamps must exist with equal
    non-zero length, each stamp must parse, stay inside the segment anchors,
    and be non-descending.  Timing is never clamped or otherwise fabricated.
    """
    tokens = getattr(result, "tokens", None)
    timestamps = getattr(result, "timestamps", None)
    if not isinstance(tokens, (list, tuple)) or not isinstance(timestamps, (list, tuple)):
        return None
    if not tokens or len(tokens) != len(timestamps):
        return None
    output: list[list[Any]] = []
    previous = start_ms - 1
    for token, stamp in zip(tokens, timestamps):
        try:
            moment = start_ms + int(round(float(stamp) * 1000.0))
        except (TypeError, ValueError, OverflowError):
            return None
        text = str(token or "").strip()
        if not text or moment < previous or moment < start_ms or moment > end_ms:
            return None
        output.append([text, moment, None])
        previous = moment
    return output


class ASRError(RuntimeError):
    pass


def _decode_failure(stderr: str) -> ASRError:
    """Classify bounded FFmpeg diagnostics without exposing their text."""
    value = str(stderr or "").casefold()
    for status, message in (
        ("401", "authorized media request returned HTTP 401"),
        ("403", "authorized media request returned HTTP 403"),
        ("404", "authorized media request returned HTTP 404"),
        ("429", "authorized media request returned HTTP 429"),
    ):
        if (
            f"server returned {status}" in value
            or f"http error {status}" in value
            or f"returned error: {status}" in value
        ):
            return ASRError(message)
    if "server returned 5" in value or "http error 5" in value:
        return ASRError("authorized media request returned HTTP 5xx")
    if "moov atom" in value:
        return ASRError("authorized media is missing a readable MP4 index")
    if "invalid data" in value:
        return ASRError("authorized media format was rejected by ffmpeg")
    return ASRError("ffmpeg could not decode the authorized media stream")


def _media_response_error(profile: MediaResponseProfile) -> ASRError | None:
    http_messages = {
        "http_401": "authorized media request returned HTTP 401",
        "http_403": "authorized media request returned HTTP 403",
        "http_404": "authorized media request returned HTTP 404",
        "http_429": "authorized media request returned HTTP 429",
        "http_4xx": "authorized media request returned HTTP 4xx",
        "http_5xx": "authorized media request returned HTTP 5xx",
        "http_3xx": "authorized media request returned an unsupported redirect",
        "http_other": "authorized media request returned an unsupported status",
    }
    if profile.http in http_messages:
        return ASRError(http_messages[profile.http])
    if profile.content == "content_html" or profile.magic == "magic_html":
        return ASRError("authorized media response contained HTML")
    if profile.content == "content_json" or profile.magic == "magic_json":
        return ASRError("authorized media response contained JSON")
    if profile.magic != "magic_iso_bmff":
        return ASRError("authorized media signature was rejected")
    return None


def _drain_bounded(pipe, output: bytearray, *, limit: int = 16 * 1024) -> None:
    while True:
        block = pipe.read(4096)
        if not block:
            return
        remaining = limit - len(output)
        if remaining > 0:
            output.extend(block[:remaining])


def _run_bounded_process(
    command: list[str],
    *,
    timeout: int,
    capture_stdout: bool,
) -> tuple[int, bytes, bytes]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    stdout = bytearray()
    stderr = bytearray()
    readers: list[threading.Thread] = []
    if process.stdout is not None:
        readers.append(threading.Thread(
            target=_drain_bounded,
            args=(process.stdout, stdout),
            name="courselens-media-stdout",
            daemon=True,
        ))
    if process.stderr is not None:
        readers.append(threading.Thread(
            target=_drain_bounded,
            args=(process.stderr, stderr),
            name="courselens-media-stderr",
            daemon=True,
        ))
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + max(1, int(timeout))
    try:
        remaining = max(1, int(deadline - time.monotonic()))
        process.wait(timeout=remaining)
    except Exception:
        process.kill()
        process.wait()
        raise
    finally:
        for reader in readers:
            reader.join(timeout=5)
    return int(process.returncode or 0), bytes(stdout), bytes(stderr)


def _run_media_proxy(
    source: dict[str, Any],
    command: Callable[[str], list[str]],
    *,
    timeout: int,
    capture_stdout: bool,
) -> tuple[int, bytes, bytes]:
    with pinned_media_proxy(source) as proxy:
        return _run_bounded_process(
            command(proxy.url),
            timeout=timeout,
            capture_stdout=capture_stdout,
        )


def _probe_duration(source: dict[str, Any]) -> float:
    try:
        returncode, stdout, _ = _run_media_proxy(
            source,
            lambda media_url: [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", "-i", media_url,
            ],
            timeout=120,
            capture_stdout=True,
        )
    except (subprocess.TimeoutExpired, OSError):
        raise ASRError("authorized media duration probe timed out")
    try:
        duration = float(stdout.decode("ascii", errors="ignore").strip())
    except (TypeError, ValueError):
        duration = 0.0
    if returncode != 0 or duration <= 0:
        raise ASRError("authorized media duration could not be determined")
    return duration


class RecognizerPool:
    def __init__(self, sensevoice_dir: Path, firered_dir: Path, *, threads: int = 4):
        self.sensevoice_dir = sensevoice_dir
        self.firered_dir = firered_dir
        self.threads = max(1, min(4, int(threads)))
        self._recognizers: dict[str, Any] = {}

    @staticmethod
    def _model(directory: Path) -> Path:
        for name in ("model.int8.onnx", "model.onnx"):
            path = directory / name
            if path.is_file():
                return path
        raise ASRError("configured ASR model directory is incomplete")

    def get(self, backend: str):
        if backend in self._recognizers:
            return self._recognizers[backend]
        directory = self.sensevoice_dir if backend == "sensevoice" else self.firered_dir
        model = self._model(directory)
        tokens = directory / "tokens.txt"
        if not tokens.is_file():
            raise ASRError("configured ASR token file is missing")
        if backend == "sensevoice":
            recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(model), tokens=str(tokens), num_threads=self.threads,
                use_itn=True, debug=False, provider="cpu",
            )
        elif backend == "firered":
            recognizer = sherpa_onnx.OfflineRecognizer.from_fire_red_asr_ctc(
                model=str(model), tokens=str(tokens), num_threads=self.threads,
                debug=False, provider="cpu",
            )
        else:
            raise ASRError("unsupported ASR backend")
        self._recognizers[backend] = recognizer
        return recognizer

    def transcribe_pcm(self, path: Path, backend: str, *, offset_seconds: float) -> list[dict[str, Any]]:
        recognizer = self.get(backend)
        samples = np.memmap(path, dtype=np.float32, mode="r")
        window_samples = SAMPLE_RATE * ASR_WINDOW_SECONDS
        energy_ratio = asr_energy_ratio()
        base = int(offset_seconds * 1000)
        work: list[tuple[Any, int, int]] = []
        for start in range(0, len(samples), window_samples):
            end = min(len(samples), start + window_samples)
            if end - start < SAMPLE_RATE // 2:
                continue
            window = samples[start:end]
            for region_start, region_end in detect_voiced_regions(
                window, energy_ratio=energy_ratio,
            ):
                stream = recognizer.create_stream()
                stream.accept_waveform(
                    SAMPLE_RATE, np.asarray(window[region_start:region_end])
                )
                work.append((
                    stream,
                    base + int((start + region_start) / SAMPLE_RATE * 1000),
                    base + int((start + region_end) / SAMPLE_RATE * 1000),
                ))
        if not work:
            del samples
            return []
        if hasattr(recognizer, "decode_streams"):
            recognizer.decode_streams([item[0] for item in work])
        else:
            for stream, _, _ in work:
                recognizer.decode_stream(stream)
        segments: list[dict[str, Any]] = []
        for stream, start_ms, end_ms in work:
            result = stream.result
            text = " ".join(str(result.text or "").replace("<sil>", "").split()).strip()
            if not text:
                continue
            segment: dict[str, Any] = {"start_ms": start_ms, "end_ms": end_ms, "text": text}
            tokens = _native_token_timing(result, start_ms, end_ms)
            if tokens is not None:
                segment["tokens"] = tokens
            segments.append(segment)
        del samples
        return normalize_segments(segments)


def _ffmpeg_proxy_command(
    target: Path,
    media_url: str,
    *,
    offset: float,
    duration: float,
) -> list[str]:
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", f"{offset:.3f}", "-i", media_url, "-t", f"{duration:.3f}",
        "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-y", str(target),
    ]


def _decode_chunk_from_url(
    media_url: str,
    target: Path,
    *,
    offset: float,
    duration: float,
) -> None:
    try:
        returncode, _, ffmpeg_stderr = _run_bounded_process(
            _ffmpeg_proxy_command(
                target, media_url, offset=offset, duration=duration,
            ),
            timeout=900,
            capture_stdout=False,
        )
    except subprocess.TimeoutExpired:
        target.unlink(missing_ok=True)
        raise ASRError("authorized media decode timed out")
    except OSError:
        target.unlink(missing_ok=True)
        raise ASRError("authorized media upstream connection failed")
    if returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise _decode_failure(ffmpeg_stderr.decode("utf-8", errors="replace"))


def _decode_chunk(source: dict[str, Any], target: Path, *, offset: float, duration: float) -> None:
    """Decode one bounded chunk through a transient pinned media session."""
    with pinned_media_proxy(source) as proxy:
        _decode_chunk_from_url(proxy.url, target, offset=offset, duration=duration)


def _pcm_file_digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.digest()


def _advance_pcm_fingerprint(state: str | None, chunk_digest: bytes) -> str:
    """Chain one chunk digest into the serializable run fingerprint state.

    The chain state is a plain hex string, so a checkpoint taken after any
    chunk lets a resumed run reproduce the exact final fingerprint — and
    therefore the same segment IDs — as an uninterrupted run.
    """
    chained = hashlib.sha256()
    chained.update(bytes.fromhex(state) if state else PCM_FINGERPRINT_DOMAIN)
    chained.update(chunk_digest)
    return chained.hexdigest()


def _timing_config_hash(energy_ratio: float) -> str:
    config = {
        "algorithm": "frame-energy-v1",
        "energy_ratio": energy_ratio,
        "frame_seconds": VAD_FRAME_SECONDS,
        "hop_seconds": VAD_HOP_SECONDS,
        "noise_percentile": VAD_NOISE_PERCENTILE,
        "silence_floor_rms": VAD_SILENCE_FLOOR_RMS,
        "peak_guard_ratio": VAD_PEAK_GUARD_RATIO,
        "merge_gap_seconds": VAD_MERGE_GAP_SECONDS,
        "pad_seconds": VAD_PAD_SECONDS,
        "min_region_seconds": VAD_MIN_REGION_SECONDS,
        "max_region_seconds": VAD_MAX_REGION_SECONDS,
        "window_seconds": ASR_WINDOW_SECONDS,
    }
    return hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()[:16]


def _source_evidence_id(fingerprint: str, duration: float) -> str:
    return compute_id(NAMESPACE_SOURCE, {
        "kind": "recording",
        "origin": "external_import",
        "title": None,
        "duration_ms": int(duration * 1000),
        "source_sha256": fingerprint,
    })


def _stamp_segment_identity(
    segments: list[dict[str, Any]],
    *,
    source_id: str,
    source_hash: str,
    provenance: dict[str, Any],
) -> None:
    """Stamp contract-shaped segment IDs plus bounded provenance in place."""
    for segment in segments:
        segment["source_hash"] = source_hash
        segment["segment_id"] = compute_id(NAMESPACE_SEGMENT, {
            "source_id": source_id,
            "start_ms": int(segment["start_ms"]),
            "end_ms": int(segment["end_ms"]),
            "text": str(segment["text"]),
            "lang": segment.get("lang"),
            "no_speech": False,
            "producer": provenance.get("producer"),
            "model": provenance.get("model"),
            "config_hash": provenance.get("config_hash"),
        })
        segment["provenance"] = dict(provenance)


def transcribe(
    job: dict[str, Any],
    *,
    sensevoice_dir: Path,
    firered_dir: Path,
    proofread: Callable[..., list[dict[str, Any]]] | None,
    progress: Callable[[str, int, int], None],
    checkpoint: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    payload = dict(job.get("payload") or {})
    source = dict(payload.get("media") or {})
    mode = str(payload.get("mode") or "automatic")
    if mode != "automatic":
        raise ASRError("unsupported subtitle mode")
    # 自动策略：配置了校对提供方（DeepSeek Key）时走双模型+AI 校对链，
    # 否则走非 AI 回退（仅精识别模型）。分支键是行为，不是历史模式标签。
    proofread_enabled = proofread is not None
    start_seconds = float(source.get("start_seconds") or 0)
    duration = float(source.get("duration_seconds") or 0)
    if duration <= 0:
        duration = _probe_duration(source) - start_seconds
    if start_seconds < 0:
        raise ASRError("media start is invalid")
    if duration <= 0 or duration > 12 * 60 * 60:
        raise ASRError("media duration is missing or outside the supported range")
    strategy = str(os.environ.get("COURSELENS_ASR_STRATEGY") or "sequential").strip().lower()
    if strategy not in {"sequential", "parallel"}:
        strategy = "sequential"
    recognizer_threads = 2 if proofread_enabled and strategy == "parallel" else 4
    pool = RecognizerPool(sensevoice_dir, firered_dir, threads=recognizer_threads)
    if proofread_enabled and strategy == "parallel":
        pool.get("sensevoice")
        pool.get("firered")
    prior = dict(payload.get("checkpoint") or {})
    if prior and str(prior.get("mode") or "") != mode:
        raise ASRError("checkpoint subtitle mode does not match the job")
    sense_segments: list[dict[str, Any]] = list(prior.get("raw_sensevoice") or [])
    firered_segments: list[dict[str, Any]] = list(prior.get("raw_firered") or [])
    total_chunks = max(1, int((duration + PCM_CHUNK_SECONDS - 1) // PCM_CHUNK_SECONDS))
    completed_chunks = max(0, min(total_chunks, int(prior.get("completed_chunks") or 0)))
    # Provenance is stamped only when the fingerprint chain covers every chunk
    # of the run.  A legacy checkpoint without chain state makes that
    # impossible, so the output omits provenance entirely and the client
    # compatibility seam mints its honest fallback identity instead.
    fingerprint_state: str | None = None
    verifiable_fingerprint = True
    if completed_chunks > 0:
        prior_state = prior.get("pcm_fingerprint")
        if isinstance(prior_state, str) and _PCM_FINGERPRINT_RE.match(prior_state):
            fingerprint_state = prior_state
        else:
            verifiable_fingerprint = False
    started = time.monotonic()
    # Keep one authorized CDN playback session for the complete task.  The
    # runner still launches one bounded FFmpeg process and retains only one
    # transient PCM file per chunk. Rotate the proxy's hidden signed URL before
    # each later chunk so an expired URL is never the first request of a new
    # decode session; a real 401/403 inside a chunk still gets only one bounded
    # refresh retry in the proxy.
    with tempfile.TemporaryDirectory(prefix="courselens-pcm-") as temporary, pinned_media_proxy(source) as proxy:
        root = Path(temporary)
        for index in range(completed_chunks, total_chunks):
            if index > completed_chunks:
                proxy.refresh_source()
            relative_offset = index * PCM_CHUNK_SECONDS
            absolute_offset = start_seconds + relative_offset
            chunk_duration = min(PCM_CHUNK_SECONDS, duration - relative_offset)
            pcm = root / f"chunk-{index:04d}.f32le"
            _decode_chunk_from_url(
                proxy.url,
                pcm,
                offset=absolute_offset,
                duration=chunk_duration,
            )
            if verifiable_fingerprint:
                fingerprint_state = _advance_pcm_fingerprint(
                    fingerprint_state, _pcm_file_digest(pcm)
                )
            if proofread_enabled and strategy == "parallel":
                with ThreadPoolExecutor(max_workers=2, thread_name_prefix="asr") as executor:
                    sense_future = executor.submit(
                        pool.transcribe_pcm, pcm, "sensevoice", offset_seconds=absolute_offset
                    )
                    fire_future = executor.submit(
                        pool.transcribe_pcm, pcm, "firered", offset_seconds=absolute_offset
                    )
                    sense_segments.extend(sense_future.result())
                    firered_segments.extend(fire_future.result())
            else:
                if proofread_enabled:
                    sense_segments.extend(
                        pool.transcribe_pcm(pcm, "sensevoice", offset_seconds=absolute_offset)
                    )
                firered_segments.extend(
                    pool.transcribe_pcm(pcm, "firered", offset_seconds=absolute_offset)
                )
            pcm.unlink(missing_ok=True)
            progress("asr", index + 1, total_chunks)
            if checkpoint is not None:
                state: dict[str, Any] = {
                    "completed_chunks": index + 1,
                    "total_chunks": total_chunks,
                    "mode": mode,
                    "raw_sensevoice": normalize_segments(sense_segments),
                    "raw_firered": normalize_segments(firered_segments),
                }
                if fingerprint_state is not None:
                    state["pcm_fingerprint"] = fingerprint_state
                checkpoint(state)
    if not proofread_enabled:
        final = firered_segments
    else:
        def proofread_checkpoint(value: dict[str, Any]) -> None:
            if checkpoint is not None:
                state: dict[str, Any] = {
                    "stage": "proofread",
                    "completed_chunks": total_chunks,
                    "total_chunks": total_chunks,
                    "mode": mode,
                    "raw_sensevoice": normalize_segments(sense_segments),
                    "raw_firered": normalize_segments(firered_segments),
                    **value,
                }
                if fingerprint_state is not None:
                    state["pcm_fingerprint"] = fingerprint_state
                checkpoint(state)

        final = proofread(
            sense_segments,
            firered_segments,
            prior,
            proofread_checkpoint,
        )
    final_segments = normalize_segments(final)
    raw_sensevoice = normalize_segments(sense_segments)
    raw_firered = normalize_segments(firered_segments)
    if verifiable_fingerprint and fingerprint_state is not None:
        config_hash = _timing_config_hash(asr_energy_ratio())
        source_id = _source_evidence_id(fingerprint_state, duration)
        _stamp_segment_identity(
            raw_sensevoice,
            source_id=source_id,
            source_hash=fingerprint_state,
            provenance={
                "producer": PRODUCER_ID,
                "model": "sensevoice",
                "config_hash": config_hash,
            },
        )
        _stamp_segment_identity(
            raw_firered,
            source_id=source_id,
            source_hash=fingerprint_state,
            provenance={
                "producer": PRODUCER_ID,
                "model": "firered",
                "config_hash": config_hash,
            },
        )
        final_model = (
            "sensevoice+firered:proofread" if proofread_enabled else "firered"
        )
        _stamp_segment_identity(
            final_segments,
            source_id=source_id,
            source_hash=fingerprint_state,
            provenance={
                "producer": PRODUCER_ID,
                "model": final_model,
                "config_hash": config_hash,
            },
        )
    return {
        "mode": mode,
        "segments": final_segments,
        "raw_sensevoice": raw_sensevoice,
        "raw_firered": raw_firered,
        "metrics": {
            "duration_seconds": duration,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "chunks": total_chunks,
            "threads_per_model": recognizer_threads,
            "strategy": strategy if proofread_enabled else "single-model",
            "start_seconds": start_seconds,
        },
    }
