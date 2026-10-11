"""Opt-in student workflows that build on existing local stores."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .sqlite_utils import connect_learning_db
from .subtitle_reader import is_evidence_id, segment_evidence_id
from .exam_schedule import PLAN_STRATEGIES, refresh_context_state, user_confirmed_context

# evidence.v1：source_hash 是文档级指纹；text_hash 是被引原文的内容摘要。两者不可混用。
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# 本地确定性出题器没有模型，只发最诚实的原文复述题；提示词版本进入 quiz 身份。
# v2（C⑨）：题干只给章节标题+提问，不再引原文——原文在提交后随答案揭示，
# 避免学生读题干即抄到答案。
QUIZ_PROMPT_VERSION = "quiz-recall-v2"
_QUIZ_STEP_EVIDENCE_KEYS = ("end_ms", "text", "text_hash", "source_hash", "evidence_id", "prompt_version")
# Lecture IR 关键时点的合同身份（worker 侧 compute_id 生成的 unit:<12hex>）。
_UNIT_ID_RE = re.compile(r"^unit:[0-9a-f]{12}$")
_REVIEW_STEP_REASONS = (
    "考试临近的章节重点", "未观看章节", "章节重点", "错题优先", "考点复习",
    "未作答题目", "关键时点回顾", "考核临近",
)


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.match(value))


def _segment_window(segment: dict[str, Any]) -> tuple[int, int]:
    """Absolute ms anchors; ``start_ms``/``end_ms`` win over legacy seconds."""
    start_value = segment.get("start_ms")
    if start_value is not None:
        start_ms = max(0, int(float(start_value)))
    else:
        start_ms = max(0, int(float(segment.get("start_seconds") or 0) * 1000))
    end_value = segment.get("end_ms")
    if end_value is not None:
        end_ms = max(0, int(float(end_value)))
    elif segment.get("end_seconds") is not None:
        end_ms = max(0, int(float(segment.get("end_seconds")) * 1000))
    else:
        end_ms = start_ms
    return start_ms, end_ms


def _chapter_title_for(chapters: list[dict[str, Any]] | None, start_ms: int) -> str:
    """The title of the chapter in progress at ``start_ms`` ("" when none).

    Chapters arrive sorted ascending; the last chapter whose start is at or
    before the anchor is the one being taught.
    """
    title = ""
    for item in chapters or []:
        if _chapter_start_ms(item) <= start_ms:
            candidate = str(item.get("title") or "").strip()
            if candidate:
                title = candidate
    return title


def _chapter_start_ms(chapter: dict[str, Any]) -> int:
    value = chapter.get("start_ms")
    if value is not None:
        return max(0, int(float(value)))
    return max(0, int(float(chapter.get("start_seconds") or 0) * 1000))


def ensure_student_feature_schema(path: str | Path) -> None:
    with closing(connect_learning_db(path)) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS bookmarks (
                bookmark_id TEXT PRIMARY KEY, course_id TEXT NOT NULL, sub_id TEXT NOT NULL,
                start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open', explanation_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_bookmarks_sub_time ON bookmarks(sub_id,start_ms);
            CREATE TABLE IF NOT EXISTS document_alignments (
                alignment_id TEXT PRIMARY KEY, course_id TEXT NOT NULL, sub_id TEXT NOT NULL,
                document_hash TEXT NOT NULL, page_num INTEGER NOT NULL, start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL, confidence REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'suggested', evidence_json TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quiz_items (
                quiz_id TEXT PRIMARY KEY, course_id TEXT NOT NULL, sub_id TEXT NOT NULL,
                question_type TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL,
                explanation TEXT NOT NULL DEFAULT '', difficulty TEXT NOT NULL DEFAULT 'medium',
                evidence_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quiz_attempts (
                attempt_id TEXT PRIMARY KEY, quiz_id TEXT NOT NULL, answer TEXT NOT NULL DEFAULT '',
                correct INTEGER, confidence INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                FOREIGN KEY(quiz_id) REFERENCES quiz_items(quiz_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_quiz_attempts_quiz ON quiz_attempts(quiz_id,created_at DESC);
            CREATE TABLE IF NOT EXISTS review_plans (
                plan_id TEXT PRIMARY KEY, title TEXT NOT NULL, exam_at REAL NOT NULL,
                available_minutes INTEGER NOT NULL, scope_json TEXT NOT NULL DEFAULT '{}',
                steps_json TEXT NOT NULL DEFAULT '[]', updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS concept_edges (
                edge_id TEXT PRIMARY KEY, from_course_id TEXT NOT NULL, from_sub_id TEXT NOT NULL,
                to_course_id TEXT NOT NULL, to_sub_id TEXT NOT NULL, concept TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}', confidence REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS watch_events (
                event_id TEXT PRIMARY KEY, course_id TEXT NOT NULL, sub_id TEXT NOT NULL,
                event TEXT NOT NULL, position_ms INTEGER NOT NULL, playback_rate REAL NOT NULL,
                occurred_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS search_answers (
                task_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL DEFAULT '',
                query TEXT NOT NULL DEFAULT '', course_ids_json TEXT NOT NULL DEFAULT '[]',
                sub_id TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
                answer TEXT NOT NULL DEFAULT '', citations_json TEXT NOT NULL DEFAULT '[]',
                grounded INTEGER NOT NULL DEFAULT 0, model TEXT NOT NULL DEFAULT '',
                prompt_version TEXT NOT NULL DEFAULT '', error_code TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_search_answers_sub ON search_answers(sub_id,created_at DESC);
            """
        )
        bookmark_columns = {str(row[1]) for row in db.execute("PRAGMA table_info(bookmarks)")}
        resolution_added = "resolution_status" not in bookmark_columns
        explanation_state_added = "explanation_state" not in bookmark_columns
        bookmark_migrations = {
            "evidence_json": "TEXT NOT NULL DEFAULT '[]'",
            "input_hash": "TEXT NOT NULL DEFAULT ''",
            "model": "TEXT NOT NULL DEFAULT ''",
            "prompt_version": "TEXT NOT NULL DEFAULT ''",
            "task_id": "TEXT NOT NULL DEFAULT ''",
            "error_code": "TEXT NOT NULL DEFAULT ''",
            "attempt": "INTEGER NOT NULL DEFAULT 0",
            "resolution_status": "TEXT NOT NULL DEFAULT 'open'",
            "explanation_state": "TEXT NOT NULL DEFAULT 'idle'",
        }
        for column, definition in bookmark_migrations.items():
            if column not in bookmark_columns:
                db.execute(f"ALTER TABLE bookmarks ADD COLUMN {column} {definition}")
        resolution_where = "1=1" if resolution_added else "resolution_status='' OR resolution_status IS NULL"
        db.execute(
            f"""UPDATE bookmarks
                   SET resolution_status=CASE WHEN status='resolved' THEN 'resolved' ELSE 'open' END
                 WHERE {resolution_where}"""
        )
        explanation_where = "1=1" if explanation_state_added else "explanation_state='' OR explanation_state IS NULL"
        db.execute(
            f"""UPDATE bookmarks
                   SET explanation_state=CASE
                       WHEN status='explained' THEN 'completed'
                       WHEN status='needs-context' THEN 'needs_context'
                       ELSE 'idle'
                   END
                 WHERE {explanation_where}"""
        )
        db.commit()


def create_bookmark(path: str | Path, *, course_id: str, sub_id: str, start_ms: int,
                    end_ms: int, note: str = "", evidence: list[dict[str, Any]] | None = None,
                    input_hash: str = "", prompt_version: str = "bookmark-answer-v1") -> dict[str, Any]:
    ensure_student_feature_schema(path)
    now = time.time()
    bookmark_id = hashlib.sha256(f"{course_id}:{sub_id}:{start_ms}:{now}".encode()).hexdigest()[:32]
    evidence = list(evidence or [])[:16]
    value = {
        "bookmark_id": bookmark_id, "course_id": str(course_id), "sub_id": str(sub_id),
        "start_ms": max(0, int(start_ms)), "end_ms": max(int(start_ms), int(end_ms)),
        "note": str(note)[:2000], "status": "open", "resolution_status": "open",
        "explanation_state": "idle", "explanation": {}, "evidence": evidence,
        "input_hash": str(input_hash), "model": "", "prompt_version": str(prompt_version),
        "task_id": "", "error_code": "", "attempt": 0,
        "created_at": now, "updated_at": now,
    }
    with closing(connect_learning_db(path)) as db:
        db.execute(
            """INSERT INTO bookmarks(
                bookmark_id,course_id,sub_id,start_ms,end_ms,note,status,explanation_json,
                created_at,updated_at,evidence_json,input_hash,model,prompt_version,task_id,
                error_code,attempt,resolution_status,explanation_state
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                value["bookmark_id"], value["course_id"], value["sub_id"], value["start_ms"], value["end_ms"],
                value["note"], value["status"], _json(value["explanation"]), now, now,
                _json(evidence), value["input_hash"], "", value["prompt_version"], "", "", 0,
                "open", "idle",
            ),
        )
        db.commit()
    return value


def list_bookmarks(path: str | Path, *, sub_id: str = "") -> list[dict[str, Any]]:
    ensure_student_feature_schema(path)
    with closing(connect_learning_db(path)) as db:
        db.row_factory = sqlite3.Row
        if sub_id:
            rows = db.execute("SELECT * FROM bookmarks WHERE sub_id=? ORDER BY start_ms", (str(sub_id),)).fetchall()
        else:
            rows = db.execute("SELECT * FROM bookmarks ORDER BY updated_at DESC LIMIT 500").fetchall()
    values = []
    for row in rows:
        value = dict(row)
        value["explanation"] = json.loads(value.pop("explanation_json", "{}") or "{}")
        value["evidence"] = json.loads(value.pop("evidence_json", "[]") or "[]")
        values.append(value)
    return values


def update_bookmark_explanation(path: str | Path, *, bookmark_id: str,
                                explanation: dict[str, Any], status: str = "completed",
                                model: str = "", task_id: str = "", error_code: str = "") -> dict[str, Any]:
    ensure_student_feature_schema(path)
    now = time.time()
    state = str(status or "completed")
    with closing(connect_learning_db(path)) as db:
        cursor = db.execute(
            """UPDATE bookmarks SET explanation_json=?,status=?,explanation_state=?,model=?,
                       task_id=CASE WHEN ?<>'' THEN ? ELSE task_id END,error_code=?,updated_at=?
                 WHERE bookmark_id=?""",
            (
                _json(explanation), "explained" if state == "completed" else state, state,
                str(model), str(task_id), str(task_id), str(error_code), now, str(bookmark_id),
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError("bookmark not found")
        db.commit()
    return next(item for item in list_bookmarks(path) if item["bookmark_id"] == str(bookmark_id))


def update_bookmark_task(
    path: str | Path,
    *,
    bookmark_id: str,
    task_id: str,
    explanation_state: str,
    input_hash: str = "",
    prompt_version: str = "",
    error_code: str = "",
    increment_attempt: bool = False,
) -> dict[str, Any]:
    ensure_student_feature_schema(path)
    now = time.time()
    with closing(connect_learning_db(path)) as db:
        cursor = db.execute(
            """UPDATE bookmarks SET task_id=?,explanation_state=?,status=?,
                       input_hash=CASE WHEN ?<>'' THEN ? ELSE input_hash END,
                       prompt_version=CASE WHEN ?<>'' THEN ? ELSE prompt_version END,
                       error_code=?,attempt=attempt+?,updated_at=? WHERE bookmark_id=?""",
            (
                str(task_id), str(explanation_state), str(explanation_state),
                str(input_hash), str(input_hash), str(prompt_version), str(prompt_version),
                str(error_code), 1 if increment_attempt else 0, now, str(bookmark_id),
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError("bookmark not found")
        db.commit()
    return next(item for item in list_bookmarks(path) if item["bookmark_id"] == str(bookmark_id))


def set_bookmark_resolution(path: str | Path, *, bookmark_id: str, resolved: bool) -> dict[str, Any]:
    ensure_student_feature_schema(path)
    resolution = "resolved" if resolved else "open"
    now = time.time()
    with closing(connect_learning_db(path)) as db:
        cursor = db.execute(
            "UPDATE bookmarks SET resolution_status=?,updated_at=? WHERE bookmark_id=?",
            (resolution, now, str(bookmark_id)),
        )
        if cursor.rowcount != 1:
            raise KeyError("bookmark not found")
        db.commit()
    return next(item for item in list_bookmarks(path) if item["bookmark_id"] == str(bookmark_id))


def delete_bookmark(path: str | Path, *, bookmark_id: str) -> dict[str, Any]:
    """Physically remove one local bookmark (PLAYER-UX-1④).

    Local-first data has no other copy, so deletion is physical: the timeline
    marker and the study-list row both disappear on the next read. Unknown ids
    raise KeyError exactly like the other bookmark mutators.
    """
    ensure_student_feature_schema(path)
    with closing(connect_learning_db(path)) as db:
        cursor = db.execute("DELETE FROM bookmarks WHERE bookmark_id=?", (str(bookmark_id),))
        if cursor.rowcount != 1:
            raise KeyError("bookmark not found")
        db.commit()
    return {"bookmark_id": str(bookmark_id), "deleted": True}


def build_review_steps(*, chapters: list[dict[str, Any]], quiz_items: list[dict[str, Any]],
                       exam_at: float, available_minutes: int,
                       watched_seconds: float = 0.0,
                       wrong_quiz_ids: set[str] | None = None,
                       strategy: str = "coverage", course_id: str = "", sub_id: str = "",
                       daily_minutes: int | None = None,
                       key_moments: list[dict[str, Any]] | None = None,
                       attempt_stats: dict[str, dict[str, int]] | None = None,
                       assessment_events: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Deterministic local planner over chapters, quizzes, IR key moments, and
    confirmed assessment events (P13-B 联动冻结件2).

    Reasons come from a closed honest set; exam time, course scope, and plan
    state never leave this module, and no score prediction or exam-leak
    wording (“必考”/“押题”) is ever produced.
    """
    now = time.time()
    budget = max(15, int(available_minutes))
    exam_lead = float(exam_at) - now
    if daily_minutes is not None and exam_lead > 0:
        days_remaining = max(1, int(-(-exam_lead // 86400)))
        budget = min(budget, max(1, int(daily_minutes)) * days_remaining)
    wrong = {str(value) for value in (wrong_quiz_ids or set())}
    stats = attempt_stats if isinstance(attempt_stats, dict) else {}
    if not wrong:
        wrong = {
            quiz_id for quiz_id, entry in stats.items()
            if isinstance(entry, dict) and entry.get("attempts") and entry.get("last_correct") != 1
        }
    # 未作答 = 题库中没有任何作答记录的题目，与错题（最近一次作答错误）区分。
    unanswered = {
        str(item.get("quiz_id") or "") for item in quiz_items if str(item.get("quiz_id") or "")
    } - set(stats) - wrong
    urgency = 2 if float(exam_at) - now < 3 * 24 * 3600 else 1
    scored = []
    for index, item in enumerate(chapters):
        start_ms = _chapter_start_ms(item)
        title = str(item.get("title") or "")
        importance = 2 if re.search(r"重点|考试|例题|作业|总结|关键", title) else 0
        unseen = 1 if start_ms / 1000.0 >= float(watched_seconds or 0) else 0
        scored.append((-(importance + unseen * urgency), index, item))
    ordered = [item for _score, _index, item in sorted(scored)]
    selected_strategy = strategy if strategy in PLAN_STRATEGIES else "coverage"
    ordered_quizzes = sorted(
        quiz_items,
        key=lambda item: (str(item.get("quiz_id") or "") not in wrong, str(item.get("quiz_id") or "")),
    )
    weak_quizzes = [item for item in ordered_quizzes if str(item.get("quiz_id") or "") in wrong | unanswered]
    rest_quizzes = [item for item in ordered_quizzes if str(item.get("quiz_id") or "") not in wrong | unanswered]
    wrong_quizzes = [item for item in ordered_quizzes if str(item.get("quiz_id") or "") in wrong]
    steps: list[dict[str, Any]] = []

    def emit_watch(chapter: dict[str, Any]) -> None:
        nonlocal budget
        if not budget:
            return
        start_ms = _chapter_start_ms(chapter)
        start_seconds = start_ms / 1000.0
        minutes = min(25, budget)
        evidence: dict[str, Any] = {"start_ms": start_ms, "source": "lecture_chapters"}
        end_ms = chapter.get("end_ms")
        if end_ms is not None:
            evidence["end_ms"] = max(start_ms, int(float(end_ms)))
        chapter_id = str(chapter.get("chapter_id") or "")
        if chapter_id:
            evidence["chapter_id"] = chapter_id
        source_refs = chapter.get("source_refs")
        if isinstance(source_refs, list) and source_refs:
            evidence["source_refs"] = list(source_refs)[:16]
        chapter_evidence_id = chapter.get("evidence_id")
        if is_evidence_id(chapter_evidence_id):
            evidence["evidence_id"] = str(chapter_evidence_id)
        steps.append({
            "order": len(steps) + 1, "kind": "watch", "title": chapter.get("title") or "章节复习",
            "start_seconds": start_seconds, "start_ms": start_ms, "minutes": minutes,
            "reason": "考试临近的章节重点" if urgency > 1 else ("未观看章节" if start_seconds >= float(watched_seconds or 0) else "章节重点"),
            "evidence": evidence,
            "evidence_id": evidence.get("evidence_id"),
        })
        budget -= minutes

    def emit_quiz(quiz: dict[str, Any]) -> None:
        nonlocal budget
        quiz_id = str(quiz.get("quiz_id") or "")
        evidence = dict(quiz.get("evidence") or {})
        quiz_evidence: dict[str, Any] = {"start_ms": int(evidence.get("start_ms") or 0), "source": "quiz"}
        for key in _QUIZ_STEP_EVIDENCE_KEYS:
            value = evidence.get(key)
            if value in (None, ""):
                continue
            quiz_evidence[key] = value[:1000] if key == "text" and isinstance(value, str) else value
        reason = "错题优先" if quiz_id in wrong else ("未作答题目" if quiz_id in unanswered else "考点复习")
        steps.append({"order": len(steps) + 1, "kind": "quiz", "title": quiz.get("question") or "复习题",
                      "quiz_id": quiz.get("quiz_id"), "minutes": min(5, budget),
                      "reason": reason,
                      "evidence": quiz_evidence,
                      "evidence_id": quiz_evidence.get("evidence_id"),
                      "course_id": str(quiz.get("course_id") or ""),
                      "sub_id": str(quiz.get("sub_id") or "")})
        budget = max(0, budget - 5)

    def emit_quiz_slice(quizzes: list[dict[str, Any]]) -> None:
        # Preserve the accepted slice semantics: at least one quiz survives
        # even an exhausted budget, matching the legacy single-loop behavior.
        for quiz in quizzes[: max(1, budget // 5)]:
            emit_quiz(quiz)

    def emit_key_moments() -> None:
        nonlocal budget
        for unit in (key_moments or [])[:12]:
            unit_id = str(unit.get("id") or "")
            time_range = unit.get("time") if isinstance(unit.get("time"), dict) else {}
            try:
                start_ms = max(0, int(time_range.get("start_ms") or 0))
                end_ms = max(start_ms, int(time_range.get("end_ms") or start_ms))
            except (TypeError, ValueError):
                continue
            if not _UNIT_ID_RE.match(unit_id) or budget <= 0:
                continue
            minutes = min(10, budget)
            steps.append({
                "order": len(steps) + 1, "kind": "key_moment",
                "title": str(unit.get("title") or "")[:200] or "关键时点回顾",
                "start_ms": start_ms, "minutes": minutes, "reason": "关键时点回顾",
                "evidence": {"start_ms": start_ms, "end_ms": end_ms,
                             "source": "lecture_ir", "evidence_id": unit_id},
                "evidence_id": unit_id,
            })
            budget -= minutes

    def emit_assessment(events: list[dict[str, Any]] | None) -> None:
        # P13-B 联动冻结件2：已确认考核是外部硬截止，恒排在全部策略分支之前
        # （按传入序=due_at 升序）；预算耗尽仍保底最临近 1 步（同
        # emit_quiz_slice 保底语义），其余 budget>0 才发；步数防御性帽 6。
        nonlocal budget
        for index, event in enumerate(list(events or [])[:6]):
            if index and budget <= 0:
                break
            event_id = str(event.get("event_id") or "")
            course = str(event.get("course_id") or "")
            try:
                due_at = float(event.get("due_at") or 0.0)
            except (TypeError, ValueError):
                due_at = 0.0
            minutes = min(15, budget)
            steps.append({
                "order": len(steps) + 1, "kind": "assessment",
                "title": str(event.get("title") or ""),
                "minutes": minutes, "reason": "考核临近",
                "event_id": event_id,
                "evidence": {
                    "source": "assessment_event", "event_id": event_id,
                    "category": str(event.get("category") or ""),
                    "due_at": due_at,
                    "due_bucket": str(event.get("due_bucket") or ""),
                    "course_id": course,
                },
                "evidence_id": None,
                "course_id": course,
                "sub_id": "",
            })
            budget -= minutes

    emit_assessment(assessment_events)
    if selected_strategy == "wrong_first":
        emit_quiz_slice(wrong_quizzes)
        for chapter in ordered:
            emit_watch(chapter)
        emit_key_moments()
        emit_quiz_slice(rest_quizzes)
    elif selected_strategy == "weak_first":
        emit_quiz_slice(weak_quizzes)
        for chapter in ordered:
            emit_watch(chapter)
        emit_key_moments()
        emit_quiz_slice(rest_quizzes)
    elif selected_strategy == "mixed":
        chapter_queue = list(ordered)
        weak_queue = list(weak_quizzes)
        while chapter_queue and weak_queue:
            emit_watch(chapter_queue.pop(0))
            emit_quiz(weak_queue.pop(0))
        for chapter in chapter_queue:
            emit_watch(chapter)
        emit_key_moments()
        for quiz in weak_queue:
            emit_quiz(quiz)
        emit_quiz_slice(rest_quizzes)
    else:
        for chapter in ordered:
            emit_watch(chapter)
        emit_key_moments()
        emit_quiz_slice(ordered_quizzes)
    for index, step in enumerate(steps, start=1):
        step["order"] = index
        step["estimated_minutes"] = int(step.get("minutes") or 0)
        step["status"] = "planned"
        step["course_id"] = str(step.get("course_id") or course_id)
        # P13-B 冻结：考核步是课程级步（无讲次归属），sub_id 恒空串，
        # 不回落到计划创建时的讲次身份。
        step["sub_id"] = "" if step["kind"] == "assessment" else str(step.get("sub_id") or sub_id)
        step.setdefault("evidence_id", None)
    return steps


def build_quiz_items(*, course_id: str, sub_id: str, segments: list[dict[str, Any]],
                     chapters: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Create evidence-first recall prompts without inventing answers.

    quiz 身份来自证据（evidence_id，缺省时回退到毫秒锚点+原文内容）加提示词
    版本，与列表位置无关；本地无模型，因此只发原文复述短答题，不编造选项。
    """
    emphasis = re.compile(r"重点|注意|考试|作业|例题|总结|关键|必须|容易错|考点")
    chapter_starts = [_chapter_start_ms(item) for item in chapters or []]
    candidates = []
    for segment in segments:
        text = str(segment.get("text") or "").strip()
        if len(text) < 8:
            continue
        start_ms, end_ms = _segment_window(segment)
        score = (3 if emphasis.search(text) else 0) + (2 if any(abs(start_ms - value) <= 30_000 for value in chapter_starts) else 0)
        candidates.append((score, start_ms, end_ms, text, segment))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2], item[3]))
    items = []
    for score, start_ms, end_ms, text, segment in candidates[:20]:
        evidence_id = segment_evidence_id(segment)
        identity: dict[str, Any] = {
            "course_id": str(course_id), "sub_id": str(sub_id), "prompt_version": QUIZ_PROMPT_VERSION,
        }
        if evidence_id:
            identity["evidence_id"] = evidence_id
        else:
            identity.update({"start_ms": start_ms, "end_ms": end_ms, "text": text})
        quiz_id = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:32]
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        anchor = f"{start_ms // 60000:02d}:{start_ms // 1000 % 60:02d}"
        # C⑨：题干=章节标题+提问，绝不引用原文（原文 80 字曾直接进题干，
        # 学生免答即抄）；原文只留在证据与答案里，提交后揭示。
        chapter_title = _chapter_title_for(chapters, start_ms)
        if chapter_title:
            question = f"章节「{chapter_title}」（课程 {anchor} 起）：用自己的话复述老师在这里讲的要点。"
        else:
            question = f"课程 {anchor} 起的段落：用自己的话复述老师在这里讲的要点。"
        evidence: dict[str, Any] = {
            "start_ms": start_ms, "end_ms": end_ms, "text": text, "text_hash": text_hash,
            "prompt_version": QUIZ_PROMPT_VERSION,
        }
        source_hash = segment.get("source_hash")
        # 文档指纹仅在合法且不与原文摘要同值时保留，避免重新混入旧语义
        if _is_sha256(source_hash) and source_hash != text_hash:
            evidence["source_hash"] = source_hash
        if evidence_id:
            evidence["evidence_id"] = evidence_id
        items.append({
            "quiz_id": quiz_id,
            "course_id": str(course_id), "sub_id": str(sub_id), "question_type": "short_answer",
            "question": question,
            "answer": text,
            "explanation": "答案与原文在你提交后显示，可跳转回课程画面对照。",
            "difficulty": "hard" if score >= 3 else "medium",
            "evidence": evidence,
        })
    return validate_quiz_items(items)


def validate_quiz_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valid = []
    seen = set()
    for item in items:
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        evidence = dict(item.get("evidence") or {})
        text = str(evidence.get("text") or "").strip()
        if not text and answer and "start_ms" in evidence:
            text = answer
        if not question or not answer or not text:
            continue
        start_ms = max(0, int(evidence.get("start_ms") or 0))
        end_ms = max(start_ms, int(evidence.get("end_ms") or start_ms))
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        text_hash = evidence.get("text_hash")
        source_hash = str(evidence.get("source_hash") or "")
        if text_hash is not None:
            # 新格式：text_hash 必须与被引原文一致，否则视为证据被篡改，闭集拒绝
            if not _is_sha256(text_hash) or str(text_hash) != digest:
                continue
            if source_hash and not _is_sha256(source_hash):
                continue  # source_hash 在此格式下是文档指纹，只接受合法 64 位十六进制
        elif source_hash and source_hash != digest:
            # 旧格式：source_hash 曾是原文摘要，仍按原文摘要校验以保持旧行可读
            continue
        kept: dict[str, Any] = {"start_ms": start_ms, "end_ms": end_ms, "text": text}
        if text_hash is not None:
            kept["text_hash"] = digest
        else:
            kept["source_hash"] = digest
        if source_hash and source_hash != digest:
            kept["source_hash"] = source_hash
        if is_evidence_id(evidence.get("evidence_id")):
            kept["evidence_id"] = str(evidence["evidence_id"])
        prompt_version = str(evidence.get("prompt_version") or "")[:64]
        if prompt_version:
            kept["prompt_version"] = prompt_version
        dedupe = (str(item.get("course_id") or ""), str(item.get("sub_id") or ""), str(item.get("quiz_id") or ""))
        if dedupe in seen:
            continue
        seen.add(dedupe)
        value = dict(item)
        value["evidence"] = kept
        valid.append(value)
    return valid


def save_quiz_items(path: str | Path, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = time.time()
    items = validate_quiz_items(items)
    with closing(connect_learning_db(path)) as db:
        for item in items:
            db.execute(
                """INSERT INTO quiz_items(
                    quiz_id,course_id,sub_id,question_type,question,answer,explanation,
                    difficulty,evidence_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(quiz_id) DO UPDATE SET question_type=excluded.question_type,
                question=excluded.question,answer=excluded.answer,explanation=excluded.explanation,
                difficulty=excluded.difficulty,evidence_json=excluded.evidence_json""",
                (
                    str(item["quiz_id"]), str(item["course_id"]), str(item["sub_id"]),
                    str(item.get("question_type") or "short_answer"), str(item.get("question") or ""),
                    str(item.get("answer") or ""), str(item.get("explanation") or ""),
                    str(item.get("difficulty") or "medium"), _json(item.get("evidence") or {}), now,
                ),
            )
        db.commit()
    return items


def list_quiz_items(path: str | Path, *, course_id: str = "", sub_id: str = "") -> list[dict[str, Any]]:
    clauses = []
    params: list[str] = []
    if course_id:
        clauses.append("course_id=?")
        params.append(str(course_id))
    if sub_id:
        clauses.append("sub_id=?")
        params.append(str(sub_id))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with closing(connect_learning_db(path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(f"SELECT * FROM quiz_items{where} ORDER BY created_at DESC LIMIT 500", params).fetchall()
    result = []
    for row in rows:
        value = dict(row)
        value["evidence"] = json.loads(value.pop("evidence_json", "{}") or "{}")
        result.append(value)
    return result


def _positive_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _plan_context(value: dict[str, Any], scope: dict[str, Any], *, now: float) -> dict[str, Any]:
    """Stored exam context wins; legacy rows derive an honest user-confirmed one."""
    stored = scope.get("exam")
    if isinstance(stored, dict) and stored.get("exam_source") in ("fudan_jwgl", "user_confirmed"):
        return refresh_context_state(stored, now=now)
    return user_confirmed_context(float(value.get("exam_at") or 0), now=now)


def _plan_course_scope(scope: dict[str, Any]) -> list[str] | None:
    stored = scope.get("course_scope")
    if isinstance(stored, list):
        ids = [str(item) for item in stored if str(item)]
        if ids:
            return ids
    course_id = str(scope.get("course_id") or "")
    return [course_id] if course_id else None


def save_review_plan(path: str | Path, *, title: str, exam_at: float, available_minutes: int,
                     scope: dict[str, Any], steps: list[dict[str, Any]],
                     exam_context: dict[str, Any] | None = None,
                     strategy: str = "coverage",
                     daily_minutes: int | None = None,
                     course_scope: list[str] | None = None) -> dict[str, Any]:
    now = time.time()
    plan_id = hashlib.sha256(f"{title}:{exam_at}:{now}".encode()).hexdigest()[:32]
    stored_scope = dict(scope or {})
    if isinstance(exam_context, dict):
        stored_scope["exam"] = exam_context
    if strategy and strategy != "coverage":
        stored_scope["strategy"] = str(strategy)
    positive_minutes = _positive_int_or_none(daily_minutes)
    if positive_minutes is not None:
        stored_scope["daily_minutes"] = positive_minutes
    if course_scope:
        stored_scope["course_scope"] = [str(item) for item in course_scope]
    with closing(connect_learning_db(path)) as db:
        db.execute(
            "INSERT INTO review_plans(plan_id,title,exam_at,available_minutes,scope_json,steps_json,updated_at) VALUES(?,?,?,?,?,?,?)",
            (plan_id, str(title), float(exam_at), max(15, int(available_minutes)), _json(stored_scope), _json(steps), now),
        )
        db.commit()
    value = {
        "plan_id": plan_id,
        "title": str(title),
        "exam_at": float(exam_at),
        "available_minutes": max(15, int(available_minutes)),
        "scope": stored_scope,
        "steps": steps,
        "updated_at": now,
    }
    value["exam_context"] = dict(exam_context) if isinstance(exam_context, dict) else user_confirmed_context(float(exam_at), now=now)
    value["strategy"] = str(strategy or "coverage")
    value["daily_minutes"] = positive_minutes
    value["course_scope"] = [str(item) for item in course_scope] if course_scope else _plan_course_scope(stored_scope)
    # U10 期末特化（纯本地）：复习目标记忆留存率 0.9；间隔上限压缩到
    # 「剩余天数的一半」——期末前半程就把强度拉满，后半夜不再排新间隔。
    remaining_days = max(0, int(-(-(float(exam_at) - now) // 86400)))
    value["retention_target"] = 0.9
    value["max_interval_days"] = max(1, remaining_days // 2)
    return value


def list_review_plans(path: str | Path) -> list[dict[str, Any]]:
    now = time.time()
    with closing(connect_learning_db(path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM review_plans ORDER BY updated_at DESC LIMIT 100").fetchall()
    result = []
    for row in rows:
        value = dict(row)
        scope = json.loads(value.pop("scope_json", "{}") or "{}")
        if not isinstance(scope, dict):
            scope = {}
        value["scope"] = scope
        value["steps"] = json.loads(value.pop("steps_json", "[]") or "[]")
        value["exam_context"] = _plan_context(value, scope, now=now)
        value["strategy"] = str(scope.get("strategy") or "coverage")
        value["daily_minutes"] = _positive_int_or_none(scope.get("daily_minutes"))
        value["course_scope"] = _plan_course_scope(scope)
        result.append(value)
    return result


def quiz_attempt_stats(path: str | Path) -> dict[str, dict[str, int]]:
    """Per-quiz attempt summary for honest weak-point prioritization.

    ``last_correct`` is the latest attempt's correctness flag (``-1`` when
    the attempt never recorded one); ``wrong`` counts ungraded attempts.
    """
    ensure_student_feature_schema(path)
    with closing(connect_learning_db(path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT quiz_id, correct FROM quiz_attempts ORDER BY created_at"
        ).fetchall()
    stats: dict[str, dict[str, int]] = {}
    for row in rows:
        entry = stats.setdefault(str(row["quiz_id"]), {"attempts": 0, "wrong": 0, "last_correct": -1})
        entry["attempts"] += 1
        correct = row["correct"]
        if correct is None:
            entry["wrong"] += 1
        entry["last_correct"] = -1 if correct is None else int(correct)
    return stats


def evidence_answer(query: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [item for item in results if item.get("target") and item.get("snippet")]
    if not usable:
        return {"answer": "资料不足，无法根据当前课程资料回答。", "citations": [], "grounded": False}
    citations = []
    for item in usable[:5]:
        target = dict(item.get("target") or {})
        citation = {
            "citation_id": str(item.get("result_id") or ""),
            "course_id": str(item.get("course_id") or ""),
            "sub_id": str(item.get("sub_id") or ""),
            "start_seconds": float(target.get("start_seconds") or 0),
            "snippet": str(item.get("snippet") or "")[:500],
            "source_hash": str(item.get("source_hash") or ""),
        }
        evidence_id = item.get("evidence_id")
        if is_evidence_id(evidence_id):
            # 稳定证据身份随引用保留；缺失或畸形时不发明
            citation["evidence_id"] = str(evidence_id)
        citations.append(citation)
    answer = "；".join(str(item["snippet"]) for item in citations[:3])
    return {"answer": f"根据课程资料：{answer}", "citations": citations, "grounded": True, "query": str(query)}


def evidence_packet(results: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    packet = []
    for index, item in enumerate(results[:max(1, min(12, int(limit)))], start=1):
        snippet = str(item.get("snippet") or "").strip()
        course_id = str(item.get("course_id") or "").strip()
        sub_id = str(item.get("sub_id") or "").strip()
        target = dict(item.get("target") or {})
        if not snippet or not course_id or not sub_id or target.get("start_seconds") is None:
            continue
        start_ms = max(0, int(float(target.get("start_seconds") or 0) * 1000))
        end_ms = max(start_ms, int(float(target.get("end_seconds") or target.get("start_seconds") or 0) * 1000))
        entry = {
            "citation_id": str(item.get("result_id") or f"evidence-{index}"),
            "course_id": course_id,
            "sub_id": sub_id,
            "start_ms": start_ms,
            "end_ms": max(start_ms + 1, end_ms),
            "source_hash": str(item.get("source_hash") or hashlib.sha256(snippet.encode("utf-8")).hexdigest()),
            "text": snippet[:1000],
            "source": str(item.get("source") or "transcript"),
            "label": str(item.get("label") or "")[:200],
        }
        evidence_id = item.get("evidence_id")
        if is_evidence_id(evidence_id):
            # 稳定证据身份优先保留；citation_id/result_id 仍是引用与允许清单键
            entry["evidence_id"] = str(evidence_id)
        packet.append(entry)
    return packet


def validate_grounded_answer(answer: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    allowed = {}
    for item in evidence:
        text = str(item.get("text") or "")
        source_hash = str(item.get("source_hash") or "")
        if not text or not source_hash or hashlib.sha256(text.encode("utf-8")).hexdigest() != source_hash:
            continue
        if not str(item.get("course_id") or "") or not str(item.get("sub_id") or ""):
            continue
        allowed[str(item.get("citation_id"))] = item
    citation_ids = [str(value) for value in answer.get("citations") or [] if str(value) in allowed]
    if not bool(answer.get("grounded")) or not citation_ids or not str(answer.get("answer") or "").strip():
        return {"answer": "资料不足，无法根据当前课程资料回答。", "citations": [], "grounded": False}
    citations = []
    for citation_id in citation_ids[:8]:
        item = allowed[citation_id]
        citation = {
            "citation_id": citation_id,
            "course_id": str(item["course_id"]),
            "sub_id": str(item["sub_id"]),
            "start_seconds": float(item["start_ms"]) / 1000.0,
            "end_seconds": float(item["end_ms"]) / 1000.0,
            "snippet": str(item["text"])[:500],
            "source_hash": str(item["source_hash"]),
            "source": str(item.get("source") or "transcript"),
            "label": str(item.get("label") or ""),
        }
        evidence_id = item.get("evidence_id")
        if is_evidence_id(evidence_id):
            citation["evidence_id"] = str(evidence_id)
        citations.append(citation)
    return {"answer": str(answer["answer"]).strip(), "citations": citations, "grounded": True}


# ---- 深度问答答案存储（DEFECT-2 根修）：deep-QA 答案唯一本地落点 ----
# ---- 学生付了真实 LLM 费用的结果必须可复看：task_id 主键一行一任务，   ----
# ---- 引用按 validate_grounded_answer 输出原样 JSON 往返（含 start_seconds ----
# ---- 供前端 citationSeekTarget 跳回原位）。state 闭集 ready|insufficient|failed。----
SEARCH_ANSWER_STATES = ("ready", "insufficient", "failed")


def save_search_answer(
    path: str | Path, *, task_id: str, state: str, input_hash: str = "",
    query: str = "", course_ids: list[str] | None = None, sub_id: str = "",
    answer: str = "", citations: list | None = None, grounded: bool = False,
    model: str = "", prompt_version: str = "", error_code: str = "",
) -> dict[str, Any]:
    """写一行深度问答答案（task_id 主键，冲突即更新），并回读该行。

    写入纪律与 save_ai_answer 同型：闭集外 state=ValueError；调用侧
    （application._persist_deep_search_answer）落地尽力而为，失败绝不挡导入。
    """
    answer_id = str(task_id or "").strip()
    if not answer_id:
        raise ValueError("search answer task id is required")
    answer_state = str(state or "")
    if answer_state not in SEARCH_ANSWER_STATES:
        raise ValueError(f"unknown search answer state: {answer_state}")
    ensure_student_feature_schema(path)
    now = time.time()
    with closing(connect_learning_db(path)) as db:
        db.execute(
            """INSERT INTO search_answers(
                   task_id,input_hash,query,course_ids_json,sub_id,state,answer,
                   citations_json,grounded,model,prompt_version,error_code,
                   created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(task_id) DO UPDATE SET
                   input_hash=excluded.input_hash, query=excluded.query,
                   course_ids_json=excluded.course_ids_json, sub_id=excluded.sub_id,
                   state=excluded.state, answer=excluded.answer,
                   citations_json=excluded.citations_json, grounded=excluded.grounded,
                   model=excluded.model, prompt_version=excluded.prompt_version,
                   error_code=excluded.error_code, updated_at=excluded.updated_at""",
            (
                answer_id, str(input_hash or ""), str(query or ""),
                _json(list(course_ids or [])), str(sub_id or ""), answer_state,
                str(answer or ""), _json(list(citations or [])),
                1 if grounded else 0, str(model or ""), str(prompt_version or ""),
                str(error_code or ""), now, now,
            ),
        )
        db.commit()
    return load_search_answer(path, task_id=answer_id) or {}


def load_search_answer(path: str | Path, *, task_id: str) -> dict[str, Any] | None:
    """按 task_id 读一行深度问答答案；无行=None（调用侧据实呈「记录还没就绪」）。

    读前幂等建表（与 list_ai_answers 同惯例）：修复前遗留的旧 learning.db 无
    search_answers 表，GET 读出面必须非致命——绝不让老库把读出打成 500。
    """
    answer_id = str(task_id or "").strip()
    if not answer_id:
        return None
    ensure_student_feature_schema(path)
    with closing(connect_learning_db(path)) as db:
        row = db.execute(
            """SELECT task_id,input_hash,query,course_ids_json,sub_id,state,answer,
                      citations_json,grounded,model,prompt_version,error_code,
                      created_at,updated_at
                 FROM search_answers WHERE task_id=?""",
            (answer_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        course_ids = list(json.loads(row[3] or "[]"))
        citations = list(json.loads(row[7] or "[]"))
    except (TypeError, ValueError):
        course_ids, citations = [], []
    return {
        "task_id": str(row[0]), "input_hash": str(row[1]), "query": str(row[2]),
        "course_ids": course_ids, "sub_id": str(row[4]), "state": str(row[5]),
        "answer": str(row[6]), "citations": citations, "grounded": bool(row[8]),
        "model": str(row[9]), "prompt_version": str(row[10]), "error_code": str(row[11]),
        "created_at": float(row[12]), "updated_at": float(row[13]),
    }




# ---- 学习洞察（D7）：个人回看热度的事件流。事件类型闭集；数据只落本地
# ---- learning DB，零外呼；默认关（前端开关门控），服务端只做闭集校验与存储。
WATCH_EVENT_TYPES = ("pause", "seek_back", "replay", "slow_rate")
WATCH_EVENTS_MAX_BATCH = 50
WATCH_EVENTS_MAX_ROWS = 2000


def insert_watch_events(
    path: str | Path, *, course_id: str, sub_id: str, events: list[dict[str, Any]] | None,
) -> int:
    """批量写入学习洞察事件；非法事件类型/畸形数值逐条跳过，绝不整批失败。

    event_id 由 (sub_id, event, position_ms, occurred_at, index) 确定性派生并
    INSERT OR IGNORE：同一批内重复与重放批次天然幂等。返回实际插入行数。"""
    ensure_student_feature_schema(path)
    now = time.time()
    rows: list[tuple[Any, ...]] = []
    for index, item in enumerate(list(events or [])[:WATCH_EVENTS_MAX_BATCH]):
        event = str((item or {}).get("event") or "")
        if event not in WATCH_EVENT_TYPES:
            continue
        try:
            position_ms = max(0, int(item.get("position_ms") or 0))
            raw_rate = item.get("playback_rate")
            playback_rate = 1.0 if raw_rate is None else float(raw_rate)
            raw_occurred = item.get("occurred_at")
            occurred_at = now if raw_occurred is None else float(raw_occurred)
        except (TypeError, ValueError):
            continue
        if not (0.0 < playback_rate <= 16.0):
            continue
        event_id = hashlib.sha256(
            f"{sub_id}:{event}:{position_ms}:{occurred_at:.3f}:{index}".encode("utf-8")
        ).hexdigest()[:32]
        rows.append((event_id, str(course_id), str(sub_id), event, position_ms, playback_rate, occurred_at))
    if not rows:
        return 0
    with closing(connect_learning_db(path)) as db:
        before = db.total_changes
        db.executemany(
            """INSERT OR IGNORE INTO watch_events(
                   event_id,course_id,sub_id,event,position_ms,playback_rate,occurred_at
               ) VALUES(?,?,?,?,?,?,?)""",
            rows,
        )
        db.commit()
        return db.total_changes - before


def list_watch_events(path: str | Path, *, sub_id: str) -> list[dict[str, Any]]:
    ensure_student_feature_schema(path)
    with closing(connect_learning_db(path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT event_id,course_id,sub_id,event,position_ms,playback_rate,occurred_at
                 FROM watch_events WHERE sub_id=? ORDER BY occurred_at LIMIT ?""",
            (str(sub_id), WATCH_EVENTS_MAX_ROWS),
        ).fetchall()
    return [dict(row) for row in rows]


def clear_watch_events(path: str | Path, *, sub_id: str = "") -> int:
    """一键抹除：带 sub_id 只清该讲次，空串清全部（「抹掉我的热点记录」）。"""
    ensure_student_feature_schema(path)
    with closing(connect_learning_db(path)) as db:
        if sub_id:
            cursor = db.execute("DELETE FROM watch_events WHERE sub_id=?", (str(sub_id),))
        else:
            cursor = db.execute("DELETE FROM watch_events")
        db.commit()
    return int(cursor.rowcount or 0)


# ---- Assessment IR 投影（N7A）：本地出题 → 可复习的 AssessmentItem 形状。
# 本地出题没有模型参与，答案就是被引原文，因此答案来源恒为 ai_generated——
# 绝不冒充 official/teacher_material。quiz 没有 document_id，也就不发明合同的
# `cka:` 引用身份（那个身份由 document_id 派生），只用 `quiz:<quiz_id>` 作为
# 本地 id；真正的考核文档题目由 assessment_ir 存储并给出合同身份。
QUIZ_ASSESSMENT_ANSWER_SOURCE = "ai_generated"


def assessment_items_from_quizzes(path: str | Path, *, course_id: str = "",
                                   sub_id: str = "") -> list[dict[str, Any]]:
    """把 quiz_items 投影成 AssessmentItem 形状，供复习聚合消费。

    投影是只读视图：不写库、不动 quiz_items，也不碰 quiz_attempts——作答记录
    与题目定义始终分表独立，重新投影不会改变任何一条历史作答。
    """
    projected = []
    for item in list_quiz_items(path, course_id=course_id, sub_id=sub_id):
        evidence = dict(item.get("evidence") or {})
        start_ms = max(0, int(evidence.get("start_ms") or 0))
        end_ms = max(start_ms, int(evidence.get("end_ms") or start_ms))
        evidence_refs = [{"kind": "transcript", "start_ms": start_ms, "end_ms": end_ms}]
        evidence_id = evidence.get("evidence_id")
        if is_evidence_id(evidence_id):
            evidence_refs[0]["source_id"] = str(evidence_id)
        answer = str(item.get("answer") or "").strip()
        projected.append({
            "item_id": f"quiz:{item.get('quiz_id')}",
            "assessment_id": f"quiz:{item.get('quiz_id')}",
            "course_id": str(item.get("course_id") or ""),
            "sub_id": str(item.get("sub_id") or ""),
            "document_id": "",
            "kind": "quiz",
            "question_no": 0,
            "stem": str(item.get("question") or ""),
            "subparts": [],
            "options": [],
            "points": None,
            "answer": answer,
            "answer_source": QUIZ_ASSESSMENT_ANSWER_SOURCE,
            "evidence_refs": evidence_refs,
            "content_hash": str(evidence.get("text_hash") or ""),
            "status": "answer_available" if answer else "question_only",
            "quiz_id": str(item.get("quiz_id") or ""),
        })
    return projected


def assessment_attempts(path: str | Path, *, quiz_id: str = "") -> list[dict[str, Any]]:
    """某题（或全部题）的作答历史；与题目投影彼此独立。"""
    ensure_student_feature_schema(path)
    clauses, params = [], []
    if quiz_id:
        clauses.append("quiz_id=?")
        params.append(str(quiz_id))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with closing(connect_learning_db(path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            f"SELECT * FROM quiz_attempts{where} ORDER BY created_at, attempt_id",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


# ---- 课程级「本课程练习」视图（N8A）：讲次级本地回忆题 → 课程复习工作台。
#
# 这些题不是 LLM 生成的题，答案是课程字幕原文。所以：
#  - 不进 AssessmentItem 闭集（那是「作业/真题」的引用视图），单独用一个视图承载，
#    条目里不出现 answer_source 字段，也绝不冒充 ai_generated；
#  - 答案在提交前一律不下发（前端沿用既有「提交后揭示课程字幕依据」交互）；
#  - 来源文案写清是课程字幕，不是官方答案。
LOCAL_PRACTICE_VIEW = "generated_quiz"
LOCAL_PRACTICE_LABEL = "本课程练习"
LOCAL_PRACTICE_SOURCE_LABEL = "课程字幕依据（非官方）"
LOCAL_PRACTICE_EMPTY_HINT = "还没有本课程练习。到某一讲的「测验与复习」里生成一次，题目就会汇总到这里。"
MAX_LOCAL_PRACTICE_ITEMS = 60


def local_practice_view(path: str | Path, *, course_id: str,
                        lecture_labels: dict[str, str] | None = None,
                        empty_sub_id: str = "") -> dict[str, Any]:
    """课程级「本课程练习」：按课次分组、错题优先排序，答案一律不下发。

    排序只用既有 quiz_attempts 统计，三个量各自如实：
    ``last_correct == 0`` 是最近一次明确答错，``wrong`` 是提交了但没判对错的条数
    （既有语义，不算答错），``attempts == 0`` 是还没做过。不做掌握百分比、不做间隔
    重复——统计是排序依据，不是新的学习模型。
    """
    labels = dict(lecture_labels or {})
    stats = quiz_attempt_stats(path)
    items: list[dict[str, Any]] = []
    for item in list_quiz_items(path, course_id=course_id):
        quiz_id = str(item.get("quiz_id") or "")
        if not quiz_id:
            continue
        entry = stats.get(quiz_id) or {"attempts": 0, "wrong": 0, "last_correct": -1}
        attempts = int(entry.get("attempts") or 0)
        ungraded = int(entry.get("wrong") or 0)
        wrong = entry.get("last_correct") == 0
        evidence = dict(item.get("evidence") or {})
        start_ms = max(0, int(evidence.get("start_ms") or 0))
        end_ms = max(start_ms, int(evidence.get("end_ms") or start_ms))
        sub_id = str(item.get("sub_id") or "")
        items.append({
            "quiz_id": quiz_id,
            "sub_id": sub_id,
            "lecture_label": labels.get(sub_id, "") or "这一讲",
            "question": str(item.get("question") or ""),
            "difficulty": str(item.get("difficulty") or "medium"),
            "answered": attempts > 0,
            "wrong": bool(wrong),
            "ungraded": ungraded,
            "attempts": attempts,
            # 只给时间锚，不给答案：学生提交后才在前端揭示字幕原文。
            "evidence": {"start_ms": start_ms, "end_ms": end_ms},
        })
    # 错题优先：最近一次答错的在前，其次有没判对错的提交，再其次没做过的，
    # 已经做对的最后；每组内按课次与题目身份保序。
    items.sort(key=lambda value: (
        0 if value["wrong"] else (1 if value["ungraded"] else (2 if value["answered"] else 3)),
        -value["ungraded"], value["sub_id"], value["quiz_id"],
    ))
    lectures: dict[str, dict[str, Any]] = {}
    for item in items:
        bucket = lectures.setdefault(item["sub_id"], {
            "sub_id": item["sub_id"], "label": item["lecture_label"],
            "count": 0, "answered": 0, "wrong": 0,
        })
        bucket["count"] += 1
        bucket["answered"] += 1 if item["answered"] else 0
        bucket["wrong"] += 1 if item["wrong"] else 0
    ordered_lectures = sorted(lectures.values(), key=lambda value: value["sub_id"])
    view: dict[str, Any] = {
        "view": LOCAL_PRACTICE_VIEW,
        "label": LOCAL_PRACTICE_LABEL,
        "course_id": str(course_id),
        "source_label": LOCAL_PRACTICE_SOURCE_LABEL,
        # 答案下发与否是硬事实，不是前端约定：这个视图里根本没有答案字段。
        "answer_visible_before_submit": False,
        "counts": {
            "total": len(items),
            "answered": sum(1 for item in items if item["answered"]),
            "wrong": sum(1 for item in items if item["wrong"]),
            "unanswered": sum(1 for item in items if not item["answered"]),
            "lectures": len(ordered_lectures),
        },
        "lectures": ordered_lectures,
        "items": items[:MAX_LOCAL_PRACTICE_ITEMS],
    }
    if not items:
        # 空题库不是真题：给一个明确动作，且复用既有生成入口（讲次里的测验与复习）。
        view["empty_action"] = {
            "action": "open_quiz_entry",
            "label": "去生成本课程练习",
            "hint": LOCAL_PRACTICE_EMPTY_HINT,
            "sub_id": str(empty_sub_id or ""),
        }
    return view


__all__ = [
    "assessment_attempts", "assessment_items_from_quizzes",
    "build_quiz_items", "build_review_steps", "create_bookmark", "clear_watch_events",
    "ensure_student_feature_schema", "evidence_answer", "evidence_packet", "insert_watch_events",
    "list_bookmarks", "list_quiz_items", "list_review_plans", "list_watch_events",
    "load_search_answer", "local_practice_view", "quiz_attempt_stats",
    "save_search_answer", "validate_grounded_answer", "validate_quiz_items",
    "save_quiz_items", "save_review_plan", "set_bookmark_resolution",
    "SEARCH_ANSWER_STATES", "update_bookmark_explanation", "update_bookmark_task",
    "WATCH_EVENT_TYPES",
    "LOCAL_PRACTICE_LABEL", "LOCAL_PRACTICE_SOURCE_LABEL", "LOCAL_PRACTICE_VIEW",
]
