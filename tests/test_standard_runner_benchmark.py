"""Tests for the benchmark-only runner harness.

Every model and OCR seam is faked here: no model load, download, or
inference runs in these tests, and no transcript or OCR text is asserted
on or emitted — only counts, digests, and structure.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "standard_runner_benchmark.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "synthetic-smoke.yml"
INSTALLER_PATH = REPO_ROOT / "scripts" / "install_models.py"

_spec = importlib.util.spec_from_file_location("standard_runner_benchmark", SCRIPT_PATH)
bench = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("standard_runner_benchmark", bench)
_spec.loader.exec_module(bench)

# The faked-seam tests import the courselens_worker package at the repo root.
for _path in (str(REPO_ROOT),):
    if _path not in sys.path:
        sys.path.insert(0, _path)

try:
    import numpy  # noqa: F401
except ImportError:  # pragma: no cover - CI always has numpy
    numpy = None


def _fake_engine_result(_array):
    return (("b", "文本甲"), ("b", "文本乙")), 0.01


class _FakePool:
    def __init__(self, threads: int):
        self.threads = threads
        self.got = []

    def get(self, backend):
        self.got.append(backend)
        return object()

    def transcribe_pcm(self, path, backend, *, offset_seconds):
        assert offset_seconds == 0.0
        return [
            {"start_ms": 100, "end_ms": 2100, "text": "你好 世界 ", "tokens": [["你", 100, None]]},
            {"start_ms": 3000, "end_ms": 4900, "text": "测试语句"},
        ]


def _simple_normalizer(segments):
    output = []
    for item in sorted(segments, key=lambda value: int(value.get("start_ms") or 0)):
        text = " ".join(str(item.get("text") or "").split()).strip()
        if not text:
            continue
        output.append({"start_ms": int(item["start_ms"]), "end_ms": int(item["end_ms"]), "text": text})
    return output


# --------------------------------------------------------------------------
# Fixture contract
# --------------------------------------------------------------------------

def _sine_provider(rate=16000, seconds=1.5):
    import numpy as np

    calls = []

    def provide(sentence):
        calls.append(sentence)
        t = np.arange(int(seconds * rate), dtype=np.float64) / rate
        return 0.4 * np.sin(2 * np.pi * 220.0 * t)

    provide.calls = calls
    return provide


def test_fixture_command_shapes():
    espeak = bench.espeak_command("句子", Path("out.wav"))
    assert espeak[0] == "espeak-ng" and "-v" in espeak and bench.FIXTURE_VOICE in espeak
    assert "out.wav" in espeak and "句子" in espeak
    resample = bench.resample_command(Path("in.wav"), Path("out.pcm"))
    assert resample[0] == "ffmpeg" and str(FIXTURE_RATE := 16000) in resample
    assert "f32le" in resample and "1" in resample[resample.index("-ac") + 1]


def test_check_mandarin_voice_passes_and_fails():
    class _Result:
        stdout = " Pty Language       Age/Gender VoiceName\n  5  cmn             --/M      Mandarin\n  5  en              --/M      English\n"

    bench.check_mandarin_voice(lambda _cmd: _Result())
    class _Missing:
        stdout = "  5  en              --/M      English\n"

    with pytest.raises(RuntimeError, match="cmn"):
        bench.check_mandarin_voice(lambda _cmd: _Missing())
    with pytest.raises(RuntimeError, match="unavailable"):
        bench.check_mandarin_voice(lambda _cmd: (_ for _ in ()).throw(OSError("missing binary")))


@pytest.mark.skipif(numpy is None, reason="numpy unavailable")
def test_build_fixture_exact_count_silence_and_determinism(tmp_path):
    provider = _sine_provider()
    first = tmp_path / "a.f32le"
    identity = bench.build_fixture(
        first, duration_seconds=12.0, period_seconds=4.0, utterance_provider=provider,
    )
    assert identity["sample_count"] == int(12.0 * 16000) == first.stat().st_size // 4
    assert identity["utterance_count"] == 3
    assert identity["utterances_trimmed"] == 0
    assert identity["min_gap_seconds"] >= 0.2
    assert len(identity["sha256"]) == 64
    assert provider.calls == [bench.SENTENCES[0], bench.SENTENCES[1], bench.SENTENCES[2]]
    second = tmp_path / "b.f32le"
    identity_two = bench.build_fixture(
        second, duration_seconds=12.0, period_seconds=4.0, utterance_provider=_sine_provider(),
    )
    assert identity_two["sha256"] == identity["sha256"]
    assert bench.build_fixture(
        tmp_path / "c.f32le", duration_seconds=12.0, period_seconds=4.0,
        utterance_provider=_sine_provider(seconds=10.0),
    )["utterances_trimmed"] == 3


@pytest.mark.skipif(numpy is None, reason="numpy unavailable")
def test_build_fixture_rejects_unbounded_silence_and_nonintegral(tmp_path):
    with pytest.raises(ValueError, match="bounded"):
        bench.build_fixture(tmp_path / "x.f32le", duration_seconds=4.0, period_seconds=2.0,
                            utterance_provider=_sine_provider())
    with pytest.raises(ValueError, match="sample count"):
        bench.build_fixture(tmp_path / "y.f32le", duration_seconds=1.00003,
                            utterance_provider=_sine_provider())


def test_fixture_constants_match_contract():
    assert bench.FIXTURE_DURATION_SECONDS == 180.0
    assert bench.FIXTURE_SAMPLE_RATE == 16_000
    assert bench.OCR_FRAME_COUNT == 18
    assert bench.OCR_FRAME_WIDTH == 1280 and bench.OCR_FRAME_HEIGHT == 720
    assert bench.STRATEGIES == ("sequential", "parallel")
    assert bench.SEQUENTIAL_THREADS == 4 and bench.PARALLEL_THREADS == 2
    assert bench.LABEL_RUNNER_EVIDENCE == "TRUSTED_PUBLIC_WORKER_RUNNER_EVIDENCE"
    assert bench.LABEL_PROJECTION == "ASR_ONLY_LOWER_BOUND"
    assert bench.LABEL_AI_NETWORK == "AI_NETWORK_NOT_MEASURED_NO_SECRET"


# --------------------------------------------------------------------------
# ASR/OCR metric collection (faked seams)
# --------------------------------------------------------------------------

def test_summarize_asr_segments_counts_validity_and_digest():
    good = [
        {"start_ms": 100, "end_ms": 2100, "text": " 你好 世界 "},
        {"start_ms": 3000, "end_ms": 4900, "text": "测试语句"},
    ]
    summary = bench.summarize_asr_segments(good, duration_ms=180_000, normalizer=_simple_normalizer)
    assert summary["count"] == 2 and summary["chars"] == 9
    assert summary["timestamps_valid"] is True
    assert re.fullmatch(r"[0-9a-f]{64}", summary["digest"])
    again = bench.summarize_asr_segments(good, duration_ms=180_000, normalizer=_simple_normalizer)
    assert again["digest"] == summary["digest"]
    invalid = bench.summarize_asr_segments(
        [{"start_ms": 5000, "end_ms": 5000, "text": "坏锚点"}],
        duration_ms=180_000, normalizer=_simple_normalizer,
    )
    assert invalid["timestamps_valid"] is False


def test_run_asr_pass_metrics_structure_and_parity(tmp_path):
    def factory(threads):
        return _FakePool(threads)

    first = bench.run_asr_pass_metrics(
        strategy="parallel", pcm_path=tmp_path / "fixture.f32le",
        sensevoice_dir=tmp_path, firered_dir=tmp_path,
        duration_seconds=180.0, pool_factory=factory,
    )
    assert first["threads_per_model"] == 2
    assert set(first["per_backend_seconds"]) == {"sensevoice", "firered"}
    assert first["combined_seconds"] >= max(first["per_backend_seconds"].values())
    assert isinstance(first["peak_rss_bytes"], int) and first["peak_rss_bytes"] >= 0
    assert first["timestamps_valid"] is True
    assert first["segment_stats"]["sensevoice"]["count"] == 2
    assert first["segment_stats"]["sensevoice"]["chars"] == 9
    second = bench.run_asr_pass_metrics(
        strategy="parallel", pcm_path=tmp_path / "fixture.f32le",
        sensevoice_dir=tmp_path, firered_dir=tmp_path,
        duration_seconds=180.0, pool_factory=factory,
    )
    assert second["digests"] == first["digests"]
    sequential = bench.run_asr_pass_metrics(
        strategy="sequential", pcm_path=tmp_path / "fixture.f32le",
        sensevoice_dir=tmp_path, firered_dir=tmp_path,
        duration_seconds=180.0, pool_factory=factory,
    )
    assert sequential["threads_per_model"] == 4
    assert sequential["digests"] == first["digests"]
    assert "text" not in json.dumps(first) and "text" not in json.dumps(sequential)


def test_run_ocr_pass_metrics_with_fake_engine():
    rapidocr = sys.modules.get("rapidocr_onnxruntime")
    if rapidocr is None:
        stub = types.ModuleType("rapidocr_onnxruntime")
        stub.RapidOCR = object
        sys.modules["rapidocr_onnxruntime"] = stub
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    frames = [bench.build_ocr_frame_bytes(index) for index in range(3)]
    metrics = bench.run_ocr_pass_metrics(
        frames_provider=lambda: list(frames),
        engine_factory=lambda: _fake_engine_result,
        concurrency=1,
    )
    assert metrics["recognized_pages"] == 3
    assert metrics["pages_with_text"] == 3
    assert metrics["total_lines"] == 6
    assert metrics["skipped_total"] == 0
    assert metrics["frame_count"] == 3
    assert re.fullmatch(r"[0-9a-f]{64}", metrics["digest"])
    assert not (set(metrics) & {"text", "tokens", "transcript", "content", "pages"})


# --------------------------------------------------------------------------
# Report assembly, projections, selection
# --------------------------------------------------------------------------

def _canned_pass(strategy, combined, *, valid=True, rss=1 << 30, digests=None):
    digests = digests or {"sensevoice": "a" * 64, "firered": "b" * 64}
    return {
        "pass": 0, "pass_kind": "cold", "strategy": strategy, "threads_per_model": 4,
        "per_backend_seconds": {"sensevoice": combined / 2, "firered": combined / 2},
        "combined_seconds": combined, "peak_rss_bytes": rss,
        "segment_stats": {
            "sensevoice": {"count": 10, "chars": 100, "timestamps_valid": valid, "digest": digests["sensevoice"]},
            "firered": {"count": 10, "chars": 100, "timestamps_valid": valid, "digest": digests["firered"]},
        },
        "timestamps_valid": valid, "digests": digests,
    }


def _canned_strategy_passes(strategy, warm_combined, *, valid=True, rss=1 << 30, digest="a" * 64):
    digests = {"sensevoice": digest, "firered": ("b" if strategy == "sequential" else "b") * 64}
    passes = [_canned_pass(strategy, warm_combined + 20.0, valid=valid, rss=rss, digests=digests)]
    for _ in range(2):
        passes.append({**passes[0], "pass_kind": "warm", "combined_seconds": warm_combined})
    return passes


FIXTURE = {
    "sha256": "c" * 64, "duration_seconds": 180.0, "sample_rate": 16000,
    "sample_count": 2_880_000, "channels": 1, "encoding": "f32le", "seed": 20260913,
    "noise_rms": 0.002, "speech_peak": 0.5, "voice": "cmn", "speak_rate_wpm": 170,
    "sentence_pool_size": 12, "utterance_count": 36, "utterances_trimmed": 0,
    "speech_seconds": 3.0, "period_seconds": 5.0, "min_gap_seconds": 2.0,
    "generator": "espeak-ng+ffmpeg+numpy-v1",
}


def _ocr_pass(kind, elapsed):
    return {
        "recognized_pages": 18, "skipped_total": 0, "skipped": {}, "pages_with_text": 18,
        "total_lines": 54, "digest": "d" * 64, "elapsed_seconds": elapsed,
        "peak_rss_bytes": 1 << 30, "frame_count": 18, "width": 1280, "height": 720,
        "concurrency": 1, "pass": 0, "pass_kind": kind,
    }


def test_assemble_report_labels_schema_and_sanitization():
    asr = {
        "sequential": _canned_strategy_passes("sequential", warm_combined=60.0),
        "parallel": _canned_strategy_passes("parallel", warm_combined=40.0),
    }
    ocr = [_ocr_pass("cold", 12.0), _ocr_pass("warm", 9.0), _ocr_pass("warm", 10.0)]
    report = bench.assemble_report(
        fixture=FIXTURE, asr_by_strategy=asr, ocr_passes=ocr,
        cache_hit=True, model_restore_seconds=12.0, fixture_build_seconds=3.2,
        generated_at="2026-09-13T00:00:00Z",
    )
    assert report["schema"] == bench.SCHEMA
    assert report["labels"]["measured_asr_ocr"] == "TRUSTED_PUBLIC_WORKER_RUNNER_EVIDENCE"
    assert report["labels"]["long_input_projections"] == "ASR_ONLY_LOWER_BOUND"
    assert report["labels"]["ai_provider_latency"] == "AI_NETWORK_NOT_MEASURED_NO_SECRET"
    assert report["asr"]["strategies"]["sequential"]["warm_median_combined_seconds"] == 60.0
    assert report["ocr"]["warm_median_elapsed_seconds"] == 9.5
    assert report["fixture"]["sha256"] == "c" * 64
    assert report["selection"]["recommended"] in {"sequential", "parallel"}
    dumped = json.dumps(report, ensure_ascii=False)
    for forbidden in ('"text"', '"tokens"', '"transcript"'):
        assert forbidden not in dumped

    leaky = {**asr, "sequential": [{**asr["sequential"][0], "text": "泄露文本"}]}
    with pytest.raises(RuntimeError, match="leaked forbidden key"):
        bench.assemble_report(
            fixture=FIXTURE, asr_by_strategy=leaky, ocr_passes=ocr,
            cache_hit=False, model_restore_seconds=0.0, fixture_build_seconds=0.0,
            generated_at="2026-09-13T00:00:00Z",
        )


def test_project_asr_only_math_and_label():
    summary = {
        "warm_rtf": 60.0 / 180.0,
        "cold_overhead_seconds": 25.0,
    }
    projection = bench.project_asr_only(summary, fixture_duration=180.0)
    assert projection["label"] == "ASR_ONLY_LOWER_BOUND"
    assert projection["available"] is True
    assert projection["projections"]["90"]["projected_seconds"] == pytest.approx(1800.0 + 25.0, abs=0.11)
    assert projection["projections"]["180"]["projected_seconds"] == pytest.approx(3600.0 + 25.0, abs=0.11)
    assert projection["projections"]["330"]["projected_seconds"] == pytest.approx(6600.0 + 25.0, abs=0.11)
    assert set(projection["excludes"]) >= {"ocr", "proofread", "ai_network"}


def test_select_strategy_conditions():
    def blocks(seq_warm, par_warm, *, valid=True, par_rss=1 << 30, par_digest="a" * 64):
        sequential = {
            "passes": _canned_strategy_passes("sequential", seq_warm, valid=valid),
            "warm_median_combined_seconds": seq_warm, "cold_overhead_seconds": 1.0,
        }
        parallel = {
            "passes": _canned_strategy_passes("parallel", par_warm, valid=valid, rss=par_rss, digest=par_digest),
            "warm_median_combined_seconds": par_warm, "cold_overhead_seconds": 1.0,
        }
        return sequential, parallel

    gib = 1 << 30
    sequential, parallel = blocks(60.0, 40.0)
    selection = bench.select_strategy(sequential, parallel, physical_memory_bytes=16 * gib)
    assert selection["recommended"] == "parallel"
    assert selection["conditions"]["warm_improvement_fraction"] >= 0.15

    sequential, parallel = blocks(60.0, 56.0)
    assert bench.select_strategy(
        sequential, parallel, physical_memory_bytes=16 * gib,
    )["recommended"] == "sequential"

    sequential, parallel = blocks(60.0, 40.0, par_digest="f" * 64)
    selection = bench.select_strategy(sequential, parallel, physical_memory_bytes=16 * gib)
    assert selection["recommended"] == "sequential"
    assert selection["conditions"]["output_parity_both_backends"] is False

    sequential, parallel = blocks(60.0, 40.0, par_rss=int(13.0 * gib))
    selection = bench.select_strategy(sequential, parallel, physical_memory_bytes=16 * gib)
    assert selection["recommended"] == "sequential"
    assert selection["conditions"]["parallel_peak_rss_within_limit"] is False

    sequential, parallel = blocks(60.0, 40.0, par_rss=int(8.0 * gib))
    selection = bench.select_strategy(sequential, parallel, physical_memory_bytes=8 * gib)
    assert selection["recommended"] == "sequential"
    assert selection["conditions"]["parallel_peak_rss_within_limit"] is True
    assert selection["conditions"]["parallel_peak_rss_within_physical_limit"] is False

    sequential, parallel = blocks(60.0, 40.0)
    selection = bench.select_strategy(sequential, parallel, physical_memory_bytes=None)
    assert selection["recommended"] == "sequential"
    assert selection["conditions"]["parallel_peak_rss_within_physical_limit"] is False
    assert selection["conditions"]["physical_memory_bytes_reported"] == 0

    sequential, parallel = blocks(60.0, 40.0, valid=False)
    selection = bench.select_strategy(sequential, parallel, physical_memory_bytes=16 * gib)
    assert selection["recommended"] == "sequential"
    assert selection["conditions"]["timestamps_valid_all_passes"] is False
    assert "no repository variable or product setting is changed" in selection["scope"]


# --------------------------------------------------------------------------
# Workflow static assertions
# --------------------------------------------------------------------------

def _job_block(text):
    lines = text.splitlines(keepends=True)
    start = next(
        index for index, line in enumerate(lines)
        if line.startswith("  standard-runner-benchmark:")
    )
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":"):
            end = index
            break
    return "".join(lines[start:end])


def test_workflow_benchmark_job_static_contract():
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "standard-runner-benchmark:" in text
    job = _job_block(text)
    assert "runs-on: ubuntu-24.04" in job
    assert re.search(r"timeout-minutes:\s*\d+", job)
    assert "github.event_name == 'workflow_dispatch'" in job
    assert "espeak-ng" in job and "ffmpeg" in job and "cmn" in job
    assert "python scripts/install_models.py" in job
    assert 'COURSELENS_OCR_CONCURRENCY: "1"' in job
    for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        assert f'{variable}: "1"' in job
    assert "--out runtime/reports/standard-runner-benchmark.json" in job
    assert "retention-days: 14" in job
    assert "if-no-files-found: error" in job
    assert "if: always()" in job and "standard-runner-benchmark-scratch" in job
    assert "--duration-seconds" not in job
    model_hashes = re.findall(r'"sha256": "([0-9a-f]{64})"', INSTALLER_PATH.read_text(encoding="utf-8"))
    assert len(model_hashes) == 2
    for digest in model_hashes:
        assert digest[:8] in job, "model cache key must embed the installer's checksum pins"
    assert "asr-models-v1-sensevoice-7d1efa21-firered-1da8b737" in job
    for action in re.findall(r"uses:\s*(\S+)@(\S+)", text):
        assert re.fullmatch(r"[0-9a-f]{40}", action[1]), f"unpinned action {action[0]}@{action[1]}"
    assert "${{ secrets." not in text


def test_cli_resolves_model_dirs_from_installer_env(monkeypatch):
    monkeypatch.setenv("SENSEVOICE_MODEL_DIR", "/models/sensevoice")
    monkeypatch.setenv("FIRERED_MODEL_DIR", "/models/firered")
    args = bench.build_parser().parse_args(["run"])
    assert args.sensevoice_dir == "/models/sensevoice"
    assert args.firered_dir == "/models/firered"
    monkeypatch.delenv("SENSEVOICE_MODEL_DIR", raising=False)
    monkeypatch.delenv("FIRERED_MODEL_DIR", raising=False)
    args = bench.build_parser().parse_args(["run"])
    assert args.sensevoice_dir is None and args.firered_dir is None


def test_workflow_leaves_existing_jobs_intact():
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "name: Synthetic CPU compute smoke" in text
    for existing in ("  asr:", "workflow_dispatch:"):
        assert existing in text
    assert text.index("  asr:") < text.index("  standard-runner-benchmark:")
