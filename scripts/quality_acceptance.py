"""Run the resumable, privacy-safe real lecture quality acceptance.

The state and evidence produced by this command contain only anonymous hashes,
counts, durations, task/run identifiers, and closed-set states. Course names,
lecture names, credentials, URLs, subtitle text, OCR text, and summaries are
never serialized or printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
STATE_SCHEMA = "courselens.quality-acceptance-state.v2"
EVIDENCE_SCHEMA = "courselens.acceptance-gate-evidence.v1"
TERMINAL_TASK_STATES = {"completed", "failed", "canceled"}


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(_canonical(value) + b"\n")
    os.replace(temporary, path)


def _load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema": STATE_SCHEMA, "stage": "new", "samples": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA:
        raise RuntimeError("quality acceptance state schema is invalid")
    if not isinstance(value.get("samples", {}), dict):
        raise RuntimeError("quality acceptance sample state is invalid")
    return value


def _duration(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def select_quality_samples(repository: Any) -> list[dict[str, Any]]:
    """Select the longest lecture and two other courses deterministically."""
    candidates: list[dict[str, Any]] = []
    for course in repository.courses():
        course_id = str(course.get("course_id") or "")
        if (
            not course_id
            or str(course.get("authorization_state") or "") != "verified"
        ):
            continue
        for lecture in repository.lectures_for_course(course_id):
            sub_id = str(lecture.get("sub_id") or "")
            if not sub_id or not bool(lecture.get("has_playback", True)):
                continue
            candidates.append(
                {
                    "course_id": course_id,
                    "sub_id": sub_id,
                    "duration_seconds": _duration(lecture.get("duration_seconds")),
                    "sample_hash": _sha256(f"{course_id}\0{sub_id}".encode("utf-8")),
                    "course_hash": _sha256(course_id.encode("utf-8")),
                }
            )
    if not candidates:
        raise RuntimeError("no verified playable lecture is available")

    ranked = sorted(
        candidates,
        key=lambda row: (-float(row["duration_seconds"]), str(row["sample_hash"])),
    )
    selected = [ranked[0]]
    selected_courses = {str(ranked[0]["course_id"])}
    for row in sorted(candidates, key=lambda item: str(item["sample_hash"])):
        if row in selected or str(row["course_id"]) in selected_courses:
            continue
        selected.append(row)
        selected_courses.add(str(row["course_id"]))
        if len(selected) == 3:
            break
    if len(selected) < 3:
        for row in sorted(candidates, key=lambda item: str(item["sample_hash"])):
            if row in selected:
                continue
            selected.append(row)
            if len(selected) == 3:
                break
    return selected


def public_selection(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "sample_hash": str(item["sample_hash"]),
            "course_hash": str(item["course_hash"]),
            "duration_seconds": round(float(item["duration_seconds"]), 3),
            "role": "complete_lecture" if index == 0 else "quality_sample",
        }
        for index, item in enumerate(samples)
    ]


def selection_digest(samples: list[dict[str, Any]]) -> str:
    return _sha256(_canonical(public_selection(samples)))


def subtitle_coverage(segments: list[dict[str, Any]], duration_seconds: float) -> float:
    duration_ms = max(1, int(float(duration_seconds) * 1000))
    intervals: list[tuple[int, int]] = []
    for segment in segments:
        start = max(0, min(duration_ms, int(segment.get("start_ms") or 0)))
        end = max(start, min(duration_ms, int(segment.get("end_ms") or start)))
        if end > start:
            intervals.append((start, end))
    covered = 0
    cursor_start = cursor_end = 0
    for start, end in sorted(intervals):
        if end <= cursor_end:
            continue
        if start > cursor_end:
            covered += max(0, cursor_end - cursor_start)
            cursor_start, cursor_end = start, end
        else:
            cursor_end = end
    covered += max(0, cursor_end - cursor_start)
    return round(min(1.0, covered / duration_ms), 6)


def _read_range(application: Any, sub_id: str, start: int) -> dict[str, Any]:
    end = start + 65_535
    stream = application.open_remote_media(sub_id, f"bytes={start}-{end}")
    try:
        body = b"".join(stream.iter_bytes())
        if stream.status != 206 or len(body) != 65_536:
            raise RuntimeError("media range did not return the exact bounded body")
        return {
            "status": int(stream.status),
            "bytes": len(body),
            "body_sha256": _sha256(body),
            "content_range": bool(stream.content_range),
        }
    finally:
        stream.close()


def probe_media(application: Any, sample: dict[str, Any]) -> dict[str, Any]:
    sub_id = str(sample["sub_id"])
    head = application.open_remote_media(sub_id, head_only=True)
    try:
        total = int(head.total_length or head.content_length or 0)
        if head.status not in {200, 206} or total < 65_536:
            raise RuntimeError("media HEAD did not confirm a playable bounded source")
    finally:
        head.close()
    middle = max(0, min(total - 65_536, total // 2))
    return {
        "head_status": int(head.status),
        "total_bytes": total,
        "initial": _read_range(application, sub_id, 0),
        "middle": _read_range(application, sub_id, middle),
    }


def _wait_task(
    task_store: Any,
    task_id: str,
    *,
    timeout: float,
    poll_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = dict(task_store.get_task(task_id) or {})
        state = str(task.get("state") or "unknown")
        if state in TERMINAL_TASK_STATES:
            if state != "completed":
                raise RuntimeError(f"quality task ended in closed state {state}")
            return task
        time.sleep(max(1.0, poll_seconds))
    raise TimeoutError("quality task exceeded its bounded acceptance timeout")


def _cleanup_observation(
    application: Any, task_id: str, *, expected_state: str = "imported"
) -> dict[str, Any]:
    if expected_state not in {"imported", "canceled"}:
        raise ValueError("cleanup observation expected state is invalid")
    remote = dict(application.task_store.get_remote_run(task_id) or {})
    attempt_number = max(1, int(remote.get("attempt") or 1))
    attempt = dict(
        application.task_store.get_remote_attempt(task_id, attempt_number) or {}
    )
    run_id = int(remote.get("run_id") or 0)
    issue_number = int(remote.get("issue_number") or 0)
    coordinator = application.remote_coordinator
    if coordinator is None or not run_id or not issue_number:
        raise RuntimeError("remote cleanup metadata is incomplete")
    artifacts = coordinator.github.list_run_artifacts(
        coordinator.settings.public_repo, run_id
    )
    mailbox = coordinator.github.job_cleanup_summary(
        coordinator.settings.private_repo, issue_number
    )
    leases = application.task_store.list_remote_token_leases()
    checks = {
        "remote_state_matches": str(remote.get("remote_state") or "") == expected_state,
        "attempt_state_matches": str(attempt.get("import_state") or "") == expected_state,
        "cleanup_complete": str(attempt.get("cleanup_state") or "") == "complete",
        "artifacts_zero": len(artifacts) == 0,
        "mailbox_closed": str(mailbox.get("state") or "") == "closed",
        "mailbox_consumed": mailbox.get("consumed") is True,
        "mailbox_comments_zero": int(mailbox.get("comment_count") or 0) == 0,
        "leases_zero": len(leases) == 0,
        "temporary_result_key_zero": not application.credentials.has_secret(
            f"remote_result_private:{task_id}"
        ),
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise RuntimeError("remote cleanup failed: " + ",".join(failed))
    return {
        "run_id": run_id,
        "attempt": attempt_number,
        "artifact_count": len(artifacts),
        "mailbox_state": "closed",
        "mailbox_comment_count": int(mailbox.get("comment_count") or 0),
        "lease_count": len(leases),
        "cleanup_state": "complete",
    }


def _subtitle_observation(application: Any, sub_id: str, duration: float) -> dict[str, Any]:
    value = application.subtitle_segments(sub_id)
    segments = list(value.get("segments") or [])
    path = application.subtitle_file_path(sub_id)
    if not segments or path is None:
        raise RuntimeError("completed subtitle task has no imported transcript")
    return {
        "segment_count": len(segments),
        "coverage": subtitle_coverage(segments, duration),
        "file_sha256": _sha256(path.read_bytes()),
        "first_start_ms": int(segments[0].get("start_ms") or 0),
        "last_end_ms": int(segments[-1].get("end_ms") or 0),
    }


def _summary_observation(application: Any, sub_id: str, duration: float) -> dict[str, Any]:
    summary = application.learning_store.find_ai_artifact(sub_id, "timestamp_summary")
    chapters = application.learning_store.find_ai_artifact(sub_id, "lecture_chapters")
    pages = application.learning_store.get_done_ppt_pages(sub_id)
    if not summary or not chapters:
        raise RuntimeError("completed summary task has no verified imported artifacts")
    chapter_rows = list(dict(chapters.get("content") or {}).get("chapters") or [])
    starts = [int(item.get("start_ms") or 0) for item in chapter_rows]
    ends = [int(item.get("end_ms") or 0) for item in chapter_rows]
    monotonic = bool(starts) and starts == sorted(starts) and len(set(starts)) == len(starts)
    duration_ms = max(1, int(duration * 1000))
    coverage = bool(starts) and starts[0] <= 30_000 and ends[-1] >= int(duration_ms * 0.95)
    input_hash = str(summary.get("input_hash") or "")
    if len(input_hash) != 64 or any(char not in "0123456789abcdef" for char in input_hash):
        raise RuntimeError("summary input hash is invalid")
    return {
        "input_hash": input_hash,
        "summary_sha256": _sha256(str(summary.get("content_markdown") or "").encode("utf-8")),
        "chapter_count": len(chapter_rows),
        "chapters_monotonic": monotonic,
        "chapters_cover_lecture": coverage,
        "ocr_page_count": len(pages),
        "ocr_nonempty_count": sum(bool(str(page.get("text") or "").strip()) for page in pages),
    }


def _run_task(
    state_path: Path,
    state: dict[str, Any],
    sample_state: dict[str, Any],
    key: str,
    enqueue: Callable[[], dict[str, Any]],
    application: Any,
    *,
    timeout: float,
    poll_seconds: float,
    observe: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    existing = dict(sample_state.get(key) or {})
    task_id = str(existing.get("task_id") or "")
    if existing.get("status") == "passed" and task_id:
        return existing
    if not task_id:
        task = enqueue()
        task_id = str(task.get("task_id") or "")
        if not task_id:
            raise RuntimeError("quality task dispatch returned no task id")
        sample_state[key] = {"task_id": task_id, "status": "running"}
        _atomic_json(state_path, state)
    application.recover_remote_runs_on_startup()
    recovered = dict(application.task_store.get_task(task_id) or {})
    if recovered.get("state") in {"paused", "pausing"}:
        application.control_task(task_id, "resume")
    _wait_task(
        application.task_store,
        task_id,
        timeout=timeout,
        poll_seconds=poll_seconds,
    )
    observation = observe()
    cleanup = _cleanup_observation(application, task_id)
    sample_state[key] = {
        "task_id": task_id,
        "task_sha256": _sha256(task_id.encode("ascii")),
        "status": "passed",
        "observation": observation,
        "cleanup": cleanup,
    }
    _atomic_json(state_path, state)
    return dict(sample_state[key])


def run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    from credentials import CredentialStore
    from scripts.final_acceptance import build_context, gate_binding
    from src.application import CourseLensApplication
    from src.runtime.catalog_repository import CatalogRepository

    data_dir = args.data_dir.resolve()
    state_path = args.state.resolve()
    state = _load_state(state_path)
    repository = CatalogRepository(data_dir / "state.db")
    try:
        samples = select_quality_samples(repository)
    finally:
        repository.close()
    digest = selection_digest(samples)
    if state.get("selection_digest") not in {None, digest}:
        raise RuntimeError("authorized quality selection changed; start a new state file")
    state.update(
        {
            "selection_digest": digest,
            "selection": public_selection(samples),
            "stage": "selected",
        }
    )
    _atomic_json(state_path, state)

    store = CredentialStore(data_dir / "credentials.json")
    accounts = [item for item in store.list_accounts() if not item.get("requires_rotation")]
    if len(accounts) != 1:
        raise RuntimeError("quality acceptance requires exactly one active saved account")
    if not store.has_deepseek_key() or store.deepseek_key_requires_rotation():
        raise RuntimeError("quality acceptance requires one active saved DeepSeek key")
    os.environ["COURSELENS_DISABLE_AUTOMATION_MONITOR"] = "1"
    application = CourseLensApplication(data_dir)
    try:
        application.use_saved_credentials(str(accounts[0]["student_id"]))
        integrity = application.github_app.check_worker_integrity()
        if not integrity.get("trusted"):
            raise RuntimeError("quality acceptance requires the approved Worker tree")
        state["worker_tree"] = str(integrity.get("actual_tree") or "")

        full_samples = samples if args.full_quality_set else samples[:1]
        for sample in full_samples:
            sample_hash = str(sample["sample_hash"])
            sample_state = state.setdefault("samples", {}).setdefault(sample_hash, {})
            sample_state.setdefault("course_hash", str(sample["course_hash"]))
            sample_state.setdefault("duration_seconds", float(sample["duration_seconds"]))
            if not sample_state.get("media"):
                sample_state["media"] = probe_media(application, sample)
                _atomic_json(state_path, state)

        smoke = samples[0]
        smoke_state = state["samples"][str(smoke["sample_hash"])]
        smoke_duration = min(300.0, max(60.0, float(smoke["duration_seconds"])))
        # One automatic subtitle policy: behavior is selected from the
        # configured DeepSeek key at enqueue time, so the smoke gate runs it once.
        _run_task(
            state_path,
            state,
            smoke_state,
            "smoke_automatic",
            lambda: application.enqueue_subtitle(
                str(smoke["course_id"]),
                str(smoke["sub_id"]),
                duration_seconds=smoke_duration,
            ),
            application,
            timeout=args.task_timeout,
            poll_seconds=args.poll_seconds,
            observe=lambda: _subtitle_observation(
                application, str(smoke["sub_id"]), smoke_duration
            ),
        )
        state["stage"] = "smoke_completed"
        _atomic_json(state_path, state)

        for sample in samples:
            sample_state = state["samples"][str(sample["sample_hash"])]
            duration = float(sample["duration_seconds"])
            if duration <= 0:
                raise RuntimeError("quality sample is missing an authoritative duration")
            _run_task(
                state_path,
                state,
                sample_state,
                "automatic_full",
                lambda sample=sample: application.enqueue_subtitle(
                    str(sample["course_id"]), str(sample["sub_id"])
                ),
                application,
                timeout=args.task_timeout,
                poll_seconds=args.poll_seconds,
                observe=lambda sample=sample, duration=duration: _subtitle_observation(
                    application, str(sample["sub_id"]), duration
                ),
            )
            _run_task(
                state_path,
                state,
                sample_state,
                "summary_full",
                lambda sample=sample: application.enqueue_summary(
                    str(sample["course_id"]), str(sample["sub_id"]), include_ppt=True
                ),
                application,
                timeout=args.task_timeout,
                poll_seconds=args.poll_seconds,
                observe=lambda sample=sample, duration=duration: _summary_observation(
                    application, str(sample["sub_id"]), duration
                ),
            )
            quizzes = application.generate_quiz(
                str(sample["course_id"]), str(sample["sub_id"])
            )
            sample_state["quiz_count"] = len(quizzes)
            _atomic_json(state_path, state)

        complete_checks = complete_lecture_checks(
            state, str(samples[0]["sample_hash"])
        )
        failed = sorted(name for name, passed in complete_checks.items() if not passed)
        if failed:
            raise RuntimeError("complete lecture checks failed: " + ",".join(failed))
        context = build_context(ROOT)
        longest = state["samples"][str(samples[0]["sample_hash"])]
        complete_evidence = {
            "schema": EVIDENCE_SCHEMA,
            "gate": "complete_lecture",
            "status": "passed",
            "binding": gate_binding("complete_lecture", context),
            "sample_hash": str(samples[0]["sample_hash"]),
            "worker_tree": state["worker_tree"],
            "task_run_ids": [
                int(dict(longest[key]).get("cleanup", {}).get("run_id") or 0)
                for key in ("automatic_full", "summary_full")
            ],
            "checks": complete_checks,
        }
        _atomic_json(args.complete_evidence.resolve(), complete_evidence)
        state["complete_lecture_evidence_sha256"] = _sha256(
            args.complete_evidence.resolve().read_bytes()
        )
        if not args.full_quality_set:
            state["stage"] = "complete_lecture_passed"
            state["complete_lecture_checks"] = complete_checks
            _atomic_json(state_path, state)
            return {
                "status": "passed",
                "stage": state["stage"],
                "selection_digest": digest,
                "sample_count": len(samples),
                "complete_lecture_evidence_sha256": state[
                    "complete_lecture_evidence_sha256"
                ],
            }

        automatic_checks = automatic_quality_checks(state)
        failed = sorted(name for name, passed in automatic_checks.items() if not passed)
        if failed:
            raise RuntimeError("automatic quality checks failed: " + ",".join(failed))
        state["stage"] = "review_ready"
        state["automatic_checks"] = automatic_checks
        _atomic_json(state_path, state)
        return {
            "status": "review_required",
            "stage": state["stage"],
            "selection_digest": digest,
            "sample_count": len(samples),
            "complete_lecture_evidence_sha256": state[
                "complete_lecture_evidence_sha256"
            ],
        }
    finally:
        application.close()


def complete_lecture_checks(state: dict[str, Any], sample_hash: str) -> dict[str, bool]:
    samples = dict(state.get("samples") or {})
    sample = dict(samples.get(sample_hash) or {})
    automatic = dict(sample.get("automatic_full") or {})
    summary = dict(sample.get("summary_full") or {})
    summary_observation = dict(summary.get("observation") or {})
    return {
        "three_sample_media_probes_passed": (
            len(samples) == 3 and all(bool(dict(item).get("media")) for item in samples.values())
        ),
        "subtitle_task_passed": automatic.get("status") == "passed",
        "subtitle_coverage_at_least_95_percent": (
            float(dict(automatic.get("observation") or {}).get("coverage") or 0) >= 0.95
        ),
        "summary_task_passed": summary.get("status") == "passed",
        "input_hash_valid": len(str(summary_observation.get("input_hash") or "")) == 64,
        "chapters_monotonic": summary_observation.get("chapters_monotonic") is True,
        "chapters_cover_lecture": summary_observation.get("chapters_cover_lecture") is True,
        "ocr_observation_recorded": isinstance(
            summary_observation.get("ocr_page_count"), int
        ),
        "quiz_available": int(sample.get("quiz_count") or 0) > 0,
        "cleanup_passed": all(
            dict(item.get("cleanup") or {}).get("cleanup_state") == "complete"
            for item in (automatic, summary)
        ),
    }


def automatic_quality_checks(state: dict[str, Any]) -> dict[str, bool]:
    samples = list(dict(state.get("samples") or {}).values())
    automatic = [dict(item.get("automatic_full") or {}) for item in samples]
    summaries = [dict(item.get("summary_full") or {}) for item in samples]
    return {
        "sample_count": len(samples) == 3,
        "media_all_passed": all(bool(item.get("media")) for item in samples),
        "subtitle_tasks_all_passed": all(item.get("status") == "passed" for item in automatic),
        "subtitle_coverage_at_least_95_percent": all(
            float(dict(item.get("observation") or {}).get("coverage") or 0) >= 0.95
            for item in automatic
        ),
        "summary_tasks_all_passed": all(item.get("status") == "passed" for item in summaries),
        "input_hashes_valid": all(
            len(str(dict(item.get("observation") or {}).get("input_hash") or "")) == 64
            for item in summaries
        ),
        "chapters_monotonic": all(
            dict(item.get("observation") or {}).get("chapters_monotonic") is True
            for item in summaries
        ),
        "chapters_cover_lectures": all(
            dict(item.get("observation") or {}).get("chapters_cover_lecture") is True
            for item in summaries
        ),
        "ocr_pages_available": sum(
            int(dict(item.get("observation") or {}).get("ocr_page_count") or 0)
            for item in summaries
        ) >= 20,
        "quiz_available": all(int(item.get("quiz_count") or 0) > 0 for item in samples),
        "cleanup_all_passed": all(
            dict(item.get("cleanup") or {}).get("cleanup_state") == "complete"
            for item in (*automatic, *summaries)
        ),
    }


def macro_f1_from_counts(tp: int, fp: int, tn: int, fn: int) -> float:
    positive = 0.0 if 2 * tp + fp + fn == 0 else 2 * tp / (2 * tp + fp + fn)
    negative = 0.0 if 2 * tn + fp + fn == 0 else 2 * tn / (2 * tn + fp + fn)
    return (positive + negative) / 2.0


def record_review(args: argparse.Namespace) -> dict[str, Any]:
    from scripts.final_acceptance import build_context, gate_binding

    state = _load_state(args.state.resolve())
    if state.get("stage") not in {"review_ready", "passed"}:
        raise RuntimeError("automatic quality acceptance is not review-ready")
    automatic = automatic_quality_checks(state)
    macro_f1 = macro_f1_from_counts(args.smart_tp, args.smart_fp, args.smart_tn, args.smart_fn)
    manual = {
        "speech_windows_at_least_30": args.speech_reviewed >= 30,
        "speech_no_systematic_errors": args.speech_systematic_errors == 0,
        "ocr_pages_at_least_20": args.ocr_reviewed >= 20,
        "ocr_readability_at_least_90_percent": (
            args.ocr_reviewed > 0 and args.ocr_correct / args.ocr_reviewed >= 0.90
        ),
        "chapters_agree_at_least_80_percent": (
            args.chapter_reviewed > 0
            and args.chapter_agree / args.chapter_reviewed >= 0.80
        ),
        "smart_macro_f1_at_least_80_percent": macro_f1 >= 0.80,
        "exam_focus_false_skips_zero": args.exam_focus_false_skips == 0,
    }
    failed = sorted(
        name
        for name, passed in {**automatic, **manual}.items()
        if not passed
    )
    if failed:
        raise RuntimeError("quality review did not pass: " + ",".join(failed))
    context = build_context(ROOT)
    evidence = {
        "schema": EVIDENCE_SCHEMA,
        "gate": "authorized_real_quality_set",
        "status": "passed",
        "binding": gate_binding("authorized_real_quality_set", context),
        "selection_digest": str(state.get("selection_digest") or ""),
        "worker_tree": str(state.get("worker_tree") or ""),
        "automatic_checks": automatic,
        "manual_checks": manual,
        "review_counts": {
            "speech_windows": args.speech_reviewed,
            "ocr_pages": args.ocr_reviewed,
            "ocr_correct": args.ocr_correct,
            "chapters": args.chapter_reviewed,
            "chapters_agree": args.chapter_agree,
            "smart_tp": args.smart_tp,
            "smart_fp": args.smart_fp,
            "smart_tn": args.smart_tn,
            "smart_fn": args.smart_fn,
            "exam_focus_false_skips": args.exam_focus_false_skips,
        },
        "smart_macro_f1": round(macro_f1, 6),
    }
    output = args.evidence.resolve()
    _atomic_json(output, evidence)
    state["stage"] = "passed"
    state["quality_evidence_sha256"] = _sha256(output.read_bytes())
    _atomic_json(args.state.resolve(), state)
    return {
        "status": "passed",
        "evidence_sha256": state["quality_evidence_sha256"],
        "smart_macro_f1": round(macro_f1, 6),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state",
        type=Path,
        default=ROOT / "runtime" / "reports" / "quality-acceptance-state.json",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--data-dir", type=Path, default=ROOT / "runtime" / "data")
    run.add_argument("--task-timeout", type=float, default=6 * 60 * 60)
    run.add_argument("--poll-seconds", type=float, default=15.0)
    run.add_argument(
        "--full-quality-set",
        action="store_true",
        help="Run full compute for all three samples before optional manual review",
    )
    run.add_argument(
        "--complete-evidence",
        type=Path,
        default=ROOT / "runtime" / "reports" / "complete-lecture-evidence.json",
    )
    review = subparsers.add_parser("record-review")
    review.add_argument("--speech-reviewed", type=int, required=True)
    review.add_argument("--speech-systematic-errors", type=int, required=True)
    review.add_argument("--ocr-reviewed", type=int, required=True)
    review.add_argument("--ocr-correct", type=int, required=True)
    review.add_argument("--chapter-reviewed", type=int, required=True)
    review.add_argument("--chapter-agree", type=int, required=True)
    review.add_argument("--smart-tp", type=int, required=True)
    review.add_argument("--smart-fp", type=int, required=True)
    review.add_argument("--smart-tn", type=int, required=True)
    review.add_argument("--smart-fn", type=int, required=True)
    review.add_argument("--exam-focus-false-skips", type=int, required=True)
    review.add_argument(
        "--evidence",
        type=Path,
        default=ROOT / "runtime" / "reports" / "quality-acceptance-evidence.json",
    )
    args = parser.parse_args(argv)
    if args.command == "run":
        result = run_acceptance(args)
    else:
        counts = (
            args.speech_reviewed,
            args.speech_systematic_errors,
            args.ocr_reviewed,
            args.ocr_correct,
            args.chapter_reviewed,
            args.chapter_agree,
            args.smart_tp,
            args.smart_fp,
            args.smart_tn,
            args.smart_fn,
            args.exam_focus_false_skips,
        )
        if any(value < 0 for value in counts):
            raise RuntimeError("review counts must be non-negative")
        if args.ocr_correct > args.ocr_reviewed or args.chapter_agree > args.chapter_reviewed:
            raise RuntimeError("passing review counts cannot exceed reviewed counts")
        result = record_review(args)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
