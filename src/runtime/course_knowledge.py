"""Course knowledge aggregation for the course-review surface (N7K).

Deterministic, local-only builder that turns what the machine already has —
timestamped subtitles, OCR'd slides, imported documents, assessment papers,
AI lecture IR / summaries, bookmarks and watch state — into one validated
``courselens.course-knowledge.v1`` document per course.

Design rules, in the order they constrain the code:

- **No generation here.**  Every key point is either an existing AI output or
  a deterministic excerpt of real evidence.  Nothing is invented to fill a
  gap; a lecture with no citable evidence is published as ``partial`` with its
  reason spelled out.
- **Bounded.**  Each source family has a hard per-lecture cap, and the cloud
  evidence packet has a per-item and total character budget, so a whole
  textbook can never be shipped off the machine by accident.
- **Revision tracked.**  Every input family feeds a signature; a changed
  signature marks only the affected lecture (and its course) stale instead of
  silently serving old knowledge.
- **Fail closed.**  The built document is validated by the frozen contract
  before it is stored.  A document that fails validation is never written, so
  readers keep the previous good snapshot.

This module only reads the local learning database: no network access, no LLM
call, no credential use.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing

from shared import course_knowledge_contract as contract

from .document_alignment import document_search_pages
from .search_index import plain_markdown
from .sqlite_utils import connect_learning_db

CONTRACT_VERSION = "course-knowledge-v1"

# Per-lecture hard caps.  A lecture is a review unit, not an archive.
MAX_TRANSCRIPT_SEGMENTS = 24
MAX_SLIDE_PAGES = 18
MAX_LECTURE_DOC_PAGES = 12
MAX_COURSE_DOC_PAGES = 10
MAX_ASSESSMENT_ITEMS = 20
MAX_BOOKMARKS = 8
MAX_KEY_POINTS = 10
MAX_TOPICS = 16
WINDOW_SECONDS = 45

# Cloud packet budget (item 11: never ship an unbounded textbook).  The
# per-item cap matches the Worker consumer's own MAX_ITEM_CHARS so an item is
# never shortened behind its verified content hash.
PACKET_ITEM_CHARS = 2_000
PACKET_TOTAL_CHARS = 24_000

_EVIDENCE_SEGMENT_RE = re.compile(r"^seg:[0-9a-f]{12}$")
_EVIDENCE_SLIDE_RE = re.compile(r"^(slevt|slent):[0-9a-f]{12}$")
_HASH_RE = re.compile(r"^[0-9a-f]{32,64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{12,64}$")

_HIGHLIGHT_RE = re.compile(r"重点|注意|考试|作业|例题|总结|关键|必须|容易错|定义|定理|公式")
_SPLIT_RE = re.compile(r"[。！？!?；;\n]")


class CourseKnowledgeError(ValueError):
    """Closed-code failure safe to surface through the local API."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else str(code))
        self.code = str(code)
        self.detail = str(detail)


# ---------------------------------------------------------------------------
# Small deterministic helpers


def _digest(value, *, length: int = 64) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def _text_digest(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _bounded(value: object, limit: int) -> str:
    text = _clean(value)
    return text if len(text) <= limit else text[:limit]


def _query(db: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Read-only query that tolerates an optional feature schema being absent."""
    try:
        return db.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def _ref_key(ref: dict) -> str:
    """Per-citation key for excerpt lookup.

    A document id is shared by every page of that document, so the key has to
    carry the locator too — keying by ``source_id`` alone silently dropped
    every document page from the packet.
    """
    locator = ref.get("locator") if isinstance(ref.get("locator"), dict) else {}
    return "|".join([
        str(ref.get("kind") or ""),
        str(ref.get("source_id") or ""),
        str(locator.get("page", "")),
        str(locator.get("start_ms", "")),
        str(locator.get("question_no", "")),
    ])


def _mmss(milliseconds: object) -> str:
    try:
        total = max(0, int(milliseconds)) // 1000
    except (TypeError, ValueError):
        return "00:00"
    return f"{total // 60:02d}:{total % 60:02d}"


def _source_label(ref: dict) -> str:
    """Label one source row at *its own* granularity.

    A source row is keyed by ``(kind, source_id)``, and how many rows that is
    differs by kind: one per document (all pages share the document id), but
    one per subtitle segment / slide / question / bookmark.  A label that only
    says "同步字幕" twenty times makes the source list unreadable, so the row
    carries whatever distinguishes it — while documents, where the row already
    *is* the whole document, keep the plain title.
    """
    kind = str(ref.get("kind") or "")
    locator = ref.get("locator") if isinstance(ref.get("locator"), dict) else {}
    if kind == "transcript":
        return f"同步字幕 {_mmss(locator.get('start_ms'))}–{_mmss(locator.get('end_ms'))}"
    if kind == "slide":
        return f"课间 PPT · 第 {locator.get('page', 1)} 页"
    if kind == "assessment_item":
        return f"真题 · 第 {locator.get('question_no', 1)} 题"
    if kind == "bookmark":
        label = str(ref.get("label") or "").strip()
        if label and label != "我的书签":
            return label
        return f"我的书签 · {_mmss(locator.get('start_ms'))}"
    if kind == "document_page":
        title = str(ref.get("label") or "").split(" · 第")[0].strip()
        return title or "本地文档"
    return kind or "来源"


def _make_ref(kind: str, source_id: str, revision_id: str, content_hash: str,
              locator: dict, label: str) -> dict:
    return {
        "kind": kind,
        "source_id": source_id,
        "revision_id": revision_id,
        "content_hash": content_hash,
        "locator": locator,
        "label": label,
    }


# ---------------------------------------------------------------------------
# Evidence collectors.  Each returns (refs, excerpts, meta, scope_by_source).


def _transcript_refs(segments: list[dict]) -> tuple[list[dict], dict, dict, dict]:
    """Transcript citations, one per citable segment (exact, jumpable anchors).

    Segments are taken from ``LearningStore.get_transcript_segments`` so legacy
    cache rows carry the same deterministic ``evidence.v1`` identity that the
    player and the search index already use.  A row still without a
    contract-shaped identity is not cited: a citation must resolve to a real
    segment, and a positional index is not an identity.
    """
    citable_segments: list[dict] = []
    dropped = 0
    for row in segments:
        text = _clean(row.get("text"))
        if not text:
            continue
        evidence_id = str(row.get("evidence_id") or "")
        if not _EVIDENCE_SEGMENT_RE.match(evidence_id):
            dropped += 1
            continue
        citable_segments.append({
            "evidence_id": evidence_id,
            "segment_index": int(row.get("index") or 0),
            "start_ms": max(0, int(row.get("start_ms") or 0)),
            "end_ms": max(0, int(row.get("end_ms") or 0)),
            "text": text,
        })
    if not citable_segments:
        return [], {}, {"total": 0, "dropped_no_identity": dropped, "windows": []}, {}
    revision = _digest(
        [[item["segment_index"], item["start_ms"], item["end_ms"]] for item in citable_segments], length=32
    )
    # Relevance first, then an even stride across the lecture, so a bounded set
    # still spans the whole recording instead of only the opening minutes.
    # ceil division matters: floor division made a 47-segment lecture keep
    # stride 1 -> the first 24 segments only, i.e. the opening half.
    stride = max(1, -(-len(citable_segments) // max(1, MAX_TRANSCRIPT_SEGMENTS)))
    selected = list(citable_segments[::stride])
    ranked = sorted(
        citable_segments,
        key=lambda item: (0 if _HIGHLIGHT_RE.search(item["text"]) else 1, -len(item["text"]), item["segment_index"]),
    )
    for segment in ranked:
        if len(selected) >= MAX_TRANSCRIPT_SEGMENTS:
            break
        if segment not in selected:
            selected.append(segment)
    selected = selected[:MAX_TRANSCRIPT_SEGMENTS]
    refs = [
        _make_ref(
            "transcript", item["evidence_id"], revision, _text_digest(item["text"]),
            {"start_ms": item["start_ms"], "end_ms": item["end_ms"]},
            _bounded(item["text"], 60),
        )
        for item in selected
    ]
    excerpts = {_ref_key(ref): _bounded(item["text"], PACKET_ITEM_CHARS)
                for ref, item in zip(refs, selected)}
    meta = {
        "total": len(citable_segments),
        "dropped_no_identity": dropped,
        "kept": len(refs),
        "windows": _windows(citable_segments),
    }
    return refs, excerpts, meta, {}


def _windows(segments: list[dict]) -> list[dict]:
    values: list[dict] = []
    current: dict | None = None
    for segment in segments:
        bucket = segment["start_ms"] // (WINDOW_SECONDS * 1000)
        if current is None or current["bucket"] != bucket:
            current = {"bucket": bucket, "texts": []}
            values.append(current)
        current["texts"].append(segment["text"])
    windows = []
    for value in values:
        text = _clean(" ".join(value["texts"]))
        if text:
            windows.append({"bucket": value["bucket"], "text": text})
    return windows


def _slide_refs(pages: list[dict]) -> tuple[list[dict], dict, dict, dict]:
    """Slide citations from ``LearningStore.get_done_ppt_pages``.

    The slide *event* identity is the stable reference; a page whose OCR text
    survived without its evidence metadata cannot be cited as a slide, so it is
    counted as dropped rather than cited by page number.
    """
    citable: list[dict] = []
    dropped = 0
    for row in pages:
        text = _clean(row.get("text"))
        event_id = str(row.get("event_id") or "")
        if not _EVIDENCE_SLIDE_RE.match(event_id):
            dropped += 1
            continue
        citable.append({
            "event_id": event_id,
            "page_num": int(row.get("page_num") or 0),
            "created_sec": int(row.get("created_sec") or 0),
            "text": text,
        })
    selected = citable[:MAX_SLIDE_PAGES]
    revision = _digest([[item["page_num"], item["created_sec"]] for item in citable], length=32)
    refs = [
        _make_ref(
            "slide", item["event_id"], revision, _text_digest(item["text"]),
            {"page": item["page_num"]}, f"第 {item['page_num']} 页",
        )
        for item in selected
    ]
    excerpts = {_ref_key(ref): _bounded(item["text"], PACKET_ITEM_CHARS)
                for ref, item in zip(refs, selected)}
    return refs, excerpts, {
        "total": len(citable),
        "dropped_no_identity": dropped,
        "kept": len(refs),
        "pages": [{"page": item["page_num"], "text": _bounded(item["text"], 160)} for item in selected],
    }, {}


def _document_refs(store_path, sub_id: str, course_id: str) -> tuple[list[dict], dict, dict, dict]:
    """Lecture-scope pages first, then course-scope pages (item 9: both visible)."""
    rows = document_search_pages(store_path, str(sub_id), course_id=str(course_id or ""))
    lecture_pages: list[dict] = []
    course_pages: list[dict] = []
    skipped = 0
    for row in rows:
        text = _clean(row.get("text"))
        text_hash = str(row.get("text_hash") or "")
        document_sha = str(row.get("sha256") or "")
        # 页正文哈希与文档修订号都必须能作为引用身份：取不到就整页跳过并计数，
        # 而不是让一条坏行把整门课的构建打挂（fail-closed 到「少一条」为止）。
        if not text or not _HASH_RE.match(text_hash) or not _REVISION_RE.match(document_sha):
            skipped += 1
            continue
        value = {
            "document_id": str(row.get("document_id") or ""),
            "sha256": document_sha,
            "title": _clean(row.get("title")),
            "scope": str(row.get("scope") or "lecture"),
            "page_num": int(row.get("page_num") or 0),
            "text": text,
            "text_hash": text_hash,
        }
        (course_pages if value["scope"] == "course" else lecture_pages).append(value)
    lecture_selected = lecture_pages[:MAX_LECTURE_DOC_PAGES]
    course_selected = course_pages[:MAX_COURSE_DOC_PAGES]

    def _build(pages: list[dict]) -> list[dict]:
        return [
            _make_ref(
                "document_page", page["document_id"], page["sha256"], page["text_hash"],
                {"page": page["page_num"]},
                _bounded(f"{page['title'] or '资料'} · 第 {page['page_num']} 页", 60),
            )
            for page in pages
        ]

    lecture_refs = _build(lecture_selected)
    course_refs = _build(course_selected)
    refs = lecture_refs + course_refs
    excerpts = {}
    for ref, page in zip(refs, lecture_selected + course_selected):
        excerpts[_ref_key(ref)] = _bounded(page["text"], PACKET_ITEM_CHARS)
    scope_by_source = {ref["source_id"]: "course" for ref in course_refs}
    return refs, excerpts, {
        "lecture_total": len(lecture_pages),
        "course_total": len(course_pages),
        "lecture_kept": len(lecture_selected),
        "course_kept": len(course_selected),
        "dropped_over_limit": max(0, len(lecture_pages) - len(lecture_selected))
        + max(0, len(course_pages) - len(course_selected)),
        "skipped_unusable": skipped,
        "pages": [
            {"scope": page["scope"], "title": _bounded(page["title"], 60), "page": page["page_num"]}
            for page in (lecture_selected + course_selected)[:10]
        ],
    }, scope_by_source


def _assessment_rows(db: sqlite3.Connection, course_id: str, sub_id: str) -> list[dict]:
    rows = _query(
        db,
        """SELECT q.question_id,q.question_no,q.label,q.content_hash,q.sub_id,
                  d.document_id,d.sha256 AS document_sha,d.title AS document_title
             FROM exam_questions q
             JOIN learning_documents d ON d.document_id=q.document_id
            WHERE q.course_id=? AND (q.sub_id=? OR q.sub_id='')
            ORDER BY q.sub_id DESC,q.question_no""",
        (str(course_id), str(sub_id)),
    )
    values = []
    for row in rows:
        content_hash = str(row["content_hash"] or "")
        document_sha = str(row["document_sha"] or "")
        document_id = str(row["document_id"] or "")
        if not _HASH_RE.match(content_hash) or not _REVISION_RE.match(document_sha):
            continue
        if not _HASH_RE.match(document_id):
            continue
        values.append({
            "question_id": str(row["question_id"]),
            "question_no": int(row["question_no"] or 0),
            "label": _clean(row["label"]) or f"第 {int(row['question_no'] or 0)} 题",
            "content_hash": content_hash,
            "sub_id": str(row["sub_id"] or ""),
            "document_id": document_id,
            "document_sha": document_sha,
            "document_title": _clean(row["document_title"]),
        })
    return values


def _assessment_refs(db: sqlite3.Connection, course_id: str, sub_id: str) -> tuple[list[dict], dict, dict, dict]:
    rows = _assessment_rows(db, course_id, sub_id)
    selected = rows[:MAX_ASSESSMENT_ITEMS]
    refs = [
        _make_ref(
            "assessment_item", item["question_id"], item["document_sha"], item["content_hash"],
            {"question_no": item["question_no"]}, _bounded(item["label"], 60),
        )
        for item in selected
    ]
    excerpts = {
        _ref_key(ref): _bounded(
            f"{item['document_title']}·{item['label']}" if item["document_title"] else item["label"],
            PACKET_ITEM_CHARS,
        )
        for ref, item in zip(refs, selected)
    }
    return refs, excerpts, {
        "total": len(rows),
        "kept": len(refs),
        "dropped_over_limit": max(0, len(rows) - len(refs)),
        "items": selected,
    }, {}


def _bookmark_refs(db: sqlite3.Connection, sub_id: str, *, include_notes: bool) -> tuple[list[dict], dict, dict, dict]:
    rows = _query(
        db,
        """SELECT bookmark_id,start_ms,end_ms,note,updated_at FROM bookmarks
           WHERE sub_id=? ORDER BY start_ms""",
        (str(sub_id),),
    )
    selected = rows[:MAX_BOOKMARKS]
    refs = []
    excerpts = {}
    for row in selected:
        note = _clean(row["note"])
        start_ms = max(0, int(row["start_ms"] or 0))
        end_ms = max(start_ms, int(row["end_ms"] or start_ms))
        bookmark_id = str(row["bookmark_id"])
        ref = _make_ref(
            "bookmark", bookmark_id,
            _digest([bookmark_id, float(row["updated_at"] or 0)], length=32),
            _text_digest(note),
            {"start_ms": start_ms, "end_ms": end_ms},
            # Personal notes travel only with an explicit refresh (item 11).
            _bounded(note, 60) if (include_notes and note) else "我的书签",
        )
        refs.append(ref)
        if include_notes and note:
            excerpts[_ref_key(ref)] = _bounded(note, PACKET_ITEM_CHARS)
    return refs, excerpts, {
        "total": len(rows),
        "kept": len(refs),
        "dropped_over_limit": max(0, len(rows) - len(refs)),
        "notes_included": bool(include_notes),
    }, {}


def _ai_artifacts(db: sqlite3.Connection, sub_id: str) -> dict[str, dict]:
    rows = _query(
        db,
        """SELECT kind,content_json,content_markdown,input_hash,updated_at
           FROM ai_artifacts
          WHERE sub_id=? AND status='ready'
            AND kind IN ('lecture_ir','timestamp_summary','lecture_summary')
          ORDER BY updated_at DESC""",
        (str(sub_id),),
    )
    values: dict[str, dict] = {}
    for row in rows:
        kind = str(row["kind"])
        if kind in values:
            continue
        try:
            content = json.loads(row["content_json"] or "{}")
        except (TypeError, ValueError):
            content = {}
        values[kind] = {
            "kind": kind,
            "content": content if isinstance(content, dict) else {},
            "markdown": str(row["content_markdown"] or ""),
            "input_hash": str(row["input_hash"] or ""),
            "updated_at": float(row["updated_at"] or 0.0),
        }
    return values


def _watch_row(db: sqlite3.Connection, sub_id: str) -> dict:
    rows = _query(
        db,
        "SELECT position_ms,completed,updated_at FROM watch_progress WHERE sub_id=?",
        (str(sub_id),),
    )
    if not rows:
        return {}
    row = rows[0]
    return {
        "position_ms": int(row["position_ms"] or 0),
        "completed": int(row["completed"] or 0),
        "updated_at": float(row["updated_at"] or 0.0),
    }


def _quiz_signal(db: sqlite3.Connection, sub_id: str) -> dict:
    rows = _query(db, "SELECT COUNT(*),COALESCE(MAX(created_at),0) FROM quiz_items WHERE sub_id=?", (str(sub_id),))
    if not rows:
        return {"count": 0, "latest": 0.0}
    return {"count": int(rows[0][0] or 0), "latest": float(rows[0][1] or 0.0)}


# ---------------------------------------------------------------------------
# Signatures


def lecture_input_signature(store_path, *, course_id: str, sub_id: str) -> dict:
    """Everything that, when changed, invalidates one lecture's knowledge."""
    with closing(connect_learning_db(store_path)) as db:
        db.row_factory = sqlite3.Row
        rows = {
            "transcript_source": _query(
                db, "SELECT source_mtime_ns,source_size,segment_count FROM transcript_sources WHERE sub_id=?",
                (str(sub_id),),
            ),
            "transcript": _query(
                db,
                """SELECT COUNT(*),COALESCE(MAX(end_ms),0),COALESCE(SUM(LENGTH(text)),0)
                   FROM transcript_segments WHERE sub_id=?""",
                (str(sub_id),),
            ),
            # 内容摘要：只按长度总量判变更会漏掉「同长度改写」（字幕纠错），
            # 一次导入的文本量很小，直接对全文取摘要最诚实。
            "transcript_digest": _query(
                db,
                "SELECT text FROM transcript_segments WHERE sub_id=? ORDER BY segment_index",
                (str(sub_id),),
            ),
            "slides": _query(
                db,
                """SELECT COUNT(*),COALESCE(MAX(ocr_at),0),COALESCE(SUM(LENGTH(text)),0)
                   FROM ppt_pages WHERE sub_id=? AND ocr_status='done' AND text IS NOT NULL AND text!=''""",
                (str(sub_id),),
            ),
            "documents": _query(
                db,
                """SELECT COUNT(*),COALESCE(SUM(LENGTH(p.text)),0),COALESCE(MAX(p.text_hash),'')
                   FROM learning_documents d JOIN learning_document_pages p ON p.document_id=d.document_id
                  WHERE d.sub_id=?""",
                (str(sub_id),),
            ),
            "course_documents": _query(
                db,
                """SELECT COUNT(*),COALESCE(SUM(LENGTH(p.text)),0)
                   FROM learning_documents d JOIN learning_document_pages p ON p.document_id=d.document_id
                  WHERE d.scope='course' AND d.course_id=?""",
                (str(course_id),),
            ),
            "artifacts": _query(
                db,
                # 只认内容身份（kind + 生产者 input_hash + 载荷摘要），不认写入
                # 时刻：同一次导入重放不该被当成"输入变了"，否则每重导入一次就
                # 多存一条快照，且刷新计划会永远报 stale。
                """SELECT kind,input_hash,content_json,content_markdown FROM ai_artifacts
                  WHERE sub_id=? AND status='ready'
                    AND kind IN ('lecture_ir','timestamp_summary','lecture_summary') ORDER BY kind""",
                (str(sub_id),),
            ),
            "assessment": _query(
                db,
                """SELECT COUNT(*),COALESCE(MAX(content_hash),'') FROM exam_questions
                  WHERE course_id=? AND (sub_id=? OR sub_id='')""",
                (str(course_id), str(sub_id)),
            ),
            "bookmarks": _query(
                db, "SELECT COUNT(*),COALESCE(MAX(updated_at),0) FROM bookmarks WHERE sub_id=?", (str(sub_id),)
            ),
        }
        watch = _watch_row(db, sub_id)
        quiz = _quiz_signal(db, sub_id)
    return {
        "transcript": (
            [list(row) for row in rows["transcript_source"]]
            + [list(row) for row in rows["transcript"]]
            + [_digest([str(row[0] or "") for row in rows["transcript_digest"]], length=32)]
        ),
        "slides": [list(row) for row in rows["slides"]],
        "documents": [list(row) for row in rows["documents"]],
        "course_documents": [list(row) for row in rows["course_documents"]],
        "artifacts": [
            [str(row["kind"]), str(row["input_hash"]),
             _digest([str(row["content_json"] or ""), str(row["content_markdown"] or "")], length=32)]
            for row in rows["artifacts"]
        ],
        "assessment": [list(row) for row in rows["assessment"]],
        "bookmarks": [list(row) for row in rows["bookmarks"]],
        "watch": [watch.get("position_ms", 0), watch.get("completed", 0), watch.get("updated_at", 0.0)],
        "quiz": [quiz["count"], quiz["latest"]],
    }


def signature_hash(signature: dict) -> str:
    return _digest(signature, length=32)


def course_signature_hash(course_id: str, lectures: list[dict]) -> str:
    """Course revision = the set of its lectures' revisions."""
    return _digest(
        {"course_id": str(course_id), "lectures": {item["sub_id"]: item["input_hash"] for item in lectures}},
        length=32,
    )


# ---------------------------------------------------------------------------
# Lecture knowledge


def _chapter_start(chapter: dict) -> int | None:
    value = chapter.get("start_ms")
    if value is None and chapter.get("start_seconds") is not None:
        value = chapter.get("start_seconds")
        try:
            return max(0, int(float(value) * 1000))
        except (TypeError, ValueError):
            return None
    try:
        return max(0, int(float(value))) if value is not None else None
    except (TypeError, ValueError):
        return None


def _citations_from_spans(spans, citation_by_source: dict[str, str]) -> list[str]:
    """Map ``evidence.v1`` span references onto declared citations.

    Span kinds with no citation family here (corrections) simply do not cite;
    a claim left with no citation is dropped by the caller.
    """
    values: list[str] = []
    for span in spans if isinstance(spans, list) else []:
        if not isinstance(span, dict):
            continue
        citation = citation_by_source.get(str(span.get("id") or ""))
        if citation and citation not in values:
            values.append(citation)
    return sorted(values)[:6]


def _citations_for_time(start_ms: int | None, references: list[dict]) -> list[str]:
    """Cite the transcript evidence covering a moment (or the first window)."""
    transcript = [ref for ref in references if ref["kind"] == "transcript"]
    if not transcript:
        return []
    if start_ms is None:
        return [transcript[0]["citation_id"]]
    for ref in transcript:
        if ref["locator"]["start_ms"] <= start_ms <= ref["locator"]["end_ms"]:
            return [ref["citation_id"]]
    return [min(transcript, key=lambda ref: abs(ref["locator"]["start_ms"] - start_ms))["citation_id"]]


def _key_points_from_ai(artifacts: dict[str, dict], references: list[dict],
                        citation_by_source: dict[str, str]) -> list[dict]:
    """Key points from accepted AI outputs, cited by their own evidence.

    The Lecture IR is the richer, span-bearing source so it goes first, but a
    summary landed in the same run is not thrown away: it tops the list up.
    Identical claims (by normalized text) are never listed twice.
    """
    points: list[dict] = []
    seen: set[str] = set()

    def add(text: object, citations: list[str], anchor_ms: object = None) -> bool:
        cleaned = _bounded(text, 600)
        key = contract.normalize_topic_title(cleaned)
        if not cleaned or not citations or not key or key in seen:
            return False
        seen.add(key)
        # RR-ANCHORFE-1：takeaway 时间戳锚随主张下发；只收非负整数毫秒，其余
        # （None/畸形）不写键——前端按缺键降级为不渲染锚。
        point: dict = {"text": cleaned, "citation_ids": citations}
        if isinstance(anchor_ms, int) and not isinstance(anchor_ms, bool) and anchor_ms >= 0:
            point["anchor_ms"] = int(anchor_ms)
        points.append(point)
        return True

    lecture_ir = artifacts.get("lecture_ir")
    for unit in (lecture_ir["content"].get("knowledge_units") or []) if lecture_ir else []:
        if len(points) >= MAX_KEY_POINTS:
            break
        if not isinstance(unit, dict):
            continue
        content = unit.get("content") if isinstance(unit.get("content"), dict) else {}
        add(
            "：".join(value for value in (
                _clean(unit.get("title")), _clean(content.get("text") or content.get("summary") or "")
            ) if value),
            _citations_from_spans(unit.get("spans"), citation_by_source),
        )

    summary = artifacts.get("timestamp_summary") or artifacts.get("lecture_summary")
    if summary is not None and len(points) < MAX_KEY_POINTS:
        content = summary["content"]
        # RR-ANCHORFE-1：takeaway 锚与 key_takeaways 等长对齐（store 已归一化；
        # 旧工件缺字段时按无锚 zip 兜底，不猜不补）。
        takeaway_anchors = list(content.get("takeaway_anchors") or [])
        for index, item in enumerate(content.get("key_takeaways") or []):
            if len(points) >= MAX_KEY_POINTS:
                break
            anchor = takeaway_anchors[index] if index < len(takeaway_anchors) else None
            add(item, _citations_for_time(None, references), anchor_ms=anchor)
        for chapter in content.get("chapters") or []:
            if len(points) >= MAX_KEY_POINTS:
                break
            if not isinstance(chapter, dict):
                continue
            add(
                "：".join(value for value in (
                    _clean(chapter.get("title")), _clean(chapter.get("summary"))
                ) if value),
                _citations_for_time(_chapter_start(chapter), references),
            )
        if len(points) < MAX_KEY_POINTS and summary["markdown"]:
            for part in _SPLIT_RE.split(plain_markdown(summary["markdown"])):
                if len(points) >= MAX_KEY_POINTS:
                    break
                if len(_clean(part)) < 12:
                    continue
                add(part, _citations_for_time(None, references))
    return points[:MAX_KEY_POINTS]


def _key_points_from_evidence(references: list[dict], windows: list[dict]) -> list[dict]:
    """Deterministic fallback: highlight sentences from real subtitle windows."""
    transcript = [ref for ref in references if ref["kind"] == "transcript"]
    if not transcript:
        return []
    by_id = {ref["citation_id"]: ref for ref in transcript}
    points: list[dict] = []
    for window in windows:
        sentences = [part.strip() for part in _SPLIT_RE.split(window["text"]) if part.strip()]
        chosen = [sentence for sentence in sentences if _HIGHLIGHT_RE.search(sentence)] or sentences[:1]
        text = _bounded(" ".join(chosen), 600)
        if len(text) < 8:
            continue
        citations = sorted(
            ref["citation_id"] for ref in transcript
            if ref["locator"]["start_ms"] // (WINDOW_SECONDS * 1000) == window["bucket"]
        ) or [transcript[0]["citation_id"]]
        points.append({"text": text, "citation_ids": [value for value in citations if value in by_id]})
        if len(points) >= MAX_KEY_POINTS:
            break
    return [point for point in points if point["citation_ids"]]


def _topics_for_lecture(sub_id: str, artifacts: dict[str, dict], references: list[dict],
                        citation_by_source: dict[str, str]) -> list[dict]:
    """Topics come from AI chapters / IR sections; no AI means no invented topics."""
    values: list[dict] = []
    lecture_ir = artifacts.get("lecture_ir")
    if lecture_ir:
        sections = lecture_ir["content"].get("sections")
        for section in sections if isinstance(sections, list) else []:
            if not isinstance(section, dict):
                continue
            title = _clean(section.get("title"))
            citations = _citations_from_spans(section.get("spans"), citation_by_source)
            if title and citations:
                values.append({"title": title, "aliases": [], "lecture_ids": [sub_id],
                               "citation_ids": citations, "status": "ready"})
    if not values:
        summary = artifacts.get("timestamp_summary") or artifacts.get("lecture_summary")
        for chapter in (summary["content"].get("chapters") or []) if summary else []:
            if not isinstance(chapter, dict):
                continue
            title = _clean(chapter.get("title"))
            citations = _citations_for_time(_chapter_start(chapter), references)
            if title and citations:
                values.append({"title": title, "aliases": [], "lecture_ids": [sub_id],
                               "citation_ids": citations, "status": "ready"})
    deduped: list[dict] = []
    seen: set[str] = set()
    for value in values:
        key = contract.normalize_topic_title(value["title"])
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return deduped[:MAX_TOPICS]


def build_lecture_knowledge(
    store,
    *,
    course_id: str,
    sub_id: str,
    include_personal_notes: bool = False,
    now: float | None = None,
) -> dict:
    """One lecture's knowledge, or an honest ``partial`` view when evidence is thin."""
    store_path = store.path
    with closing(connect_learning_db(store_path)) as db:
        db.row_factory = sqlite3.Row
        transcript_refs, transcript_excerpts, transcript_meta, _ = _transcript_refs(store.get_transcript_segments(sub_id))
        slide_refs, slide_excerpts, slide_meta, _ = _slide_refs(store.get_done_ppt_pages(sub_id))
        document_refs, document_excerpts, document_meta, scope_by_source = _document_refs(
            store_path, sub_id, course_id
        )
        assessment_refs, assessment_excerpts, assessment_meta, _ = _assessment_refs(db, course_id, sub_id)
        bookmark_refs, bookmark_excerpts, bookmark_meta, _ = _bookmark_refs(
            db, sub_id, include_notes=include_personal_notes
        )
        artifacts = _ai_artifacts(db, sub_id)
        watch = _watch_row(db, sub_id)
        quiz = _quiz_signal(db, sub_id)

    raw = transcript_refs + slide_refs + document_refs + assessment_refs + bookmark_refs
    references = [dict(ref, citation_id=contract.citation_id_for(ref)) for ref in raw]
    # 只把「一个 source_id 对应一条引用」的两族放进 span 映射：文档页/题目/书签
    # 的 source_id 被多条引用共享（同一文档多页），放进来会互相覆盖、指错页。
    citation_by_source = {
        ref["source_id"]: ref["citation_id"]
        for ref in references if ref["kind"] in {"transcript", "slide"}
    }

    key_points = _key_points_from_ai(artifacts, references, citation_by_source)
    if not key_points:
        key_points = _key_points_from_evidence(references, transcript_meta.get("windows") or [])
    topics = _topics_for_lecture(sub_id, artifacts, references, citation_by_source)

    has_ai = bool(artifacts.get("lecture_ir") or artifacts.get("timestamp_summary") or artifacts.get("lecture_summary"))
    reasons: list[str] = []
    if not transcript_refs:
        reasons.append("transcript_missing")
    if not has_ai:
        reasons.append("summary_missing")
    if not key_points:
        reasons.append("never_built")
    status = "ready" if (transcript_refs and has_ai and key_points) else "partial"

    return {
        "sub_id": str(sub_id),
        "course_id": str(course_id),
        "input_hash": signature_hash(lecture_input_signature(store_path, course_id=course_id, sub_id=sub_id)),
        "status": status,
        "stale_reasons": [] if status == "ready" else sorted(set(reasons)),
        "key_points": key_points,
        "topics": topics,
        "source_coverage": {
            "transcript_segments": len(transcript_refs),
            "slide_pages": len(slide_refs),
            "document_pages": document_meta.get("lecture_kept", 0),
            "course_document_pages": document_meta.get("course_kept", 0),
            "assessment_items": len(assessment_meta.get("items") or []),
            "bookmarks": len(bookmark_refs),
            "lecture_ir": bool(artifacts.get("lecture_ir")),
            "summary": bool(artifacts.get("timestamp_summary") or artifacts.get("lecture_summary")),
        },
        "evidence_refs": references,
        "updated_at": float(time.time() if now is None else now),
        "_excerpts": {
            **transcript_excerpts, **slide_excerpts, **document_excerpts,
            **assessment_excerpts, **bookmark_excerpts,
        },
        "_scope_by_source": scope_by_source,
        "_assessment_items": assessment_meta.get("items") or [],
        "_dropped": {
            "transcript_no_identity": transcript_meta.get("dropped_no_identity", 0),
            "slides_no_identity": slide_meta.get("dropped_no_identity", 0),
            "transcript_over_limit": max(0, transcript_meta.get("total", 0) - transcript_meta.get("kept", 0)),
            "slides_over_limit": max(0, slide_meta.get("total", 0) - slide_meta.get("kept", 0)),
            "documents_over_limit": document_meta.get("dropped_over_limit", 0),
            "assessment_over_limit": assessment_meta.get("dropped_over_limit", 0),
            "bookmarks_over_limit": bookmark_meta.get("dropped_over_limit", 0),
        },
        "_signals": {"watch": watch, "quiz": quiz},
    }


# ---------------------------------------------------------------------------
# Course aggregation


def merge_topics(entries: list[dict]) -> list[dict]:
    """Merge topics by normalized title or explicit alias only (item 13).

    Uncertain matches stay as two topics: a wrong merge hides a whole subject,
    while a missed merge only costs one extra row.
    """
    buckets: list[dict] = []

    def keys_of(value: dict) -> set[str]:
        return {contract.normalize_topic_title(value.get("title"))} | {
            contract.normalize_topic_title(alias) for alias in value.get("aliases") or []
        }

    for entry in entries:
        title = _clean(entry.get("title"))
        if not title:
            continue
        candidate = {
            "title": title,
            "aliases": [str(value) for value in (entry.get("aliases") or []) if str(value or "").strip()],
            "lecture_ids": [str(value) for value in (entry.get("lecture_ids") or []) if str(value or "").strip()],
            "citation_ids": [str(value) for value in (entry.get("citation_ids") or []) if str(value or "").strip()],
            "status": str(entry.get("status") or "ready"),
        }
        target = None
        for bucket in buckets:
            if keys_of(bucket) & keys_of(candidate):
                target = bucket
                break
        if target is None:
            buckets.append(candidate)
            continue
        for value in [candidate["title"], *candidate["aliases"]]:
            if contract.normalize_topic_title(value) not in keys_of(target):
                target["aliases"].append(value)
        for sub_id in candidate["lecture_ids"]:
            if sub_id not in target["lecture_ids"]:
                target["lecture_ids"].append(sub_id)
        for citation in candidate["citation_ids"]:
            if citation not in target["citation_ids"]:
                target["citation_ids"].append(citation)
        if candidate["status"] == "partial":
            target["status"] = "partial"
    return buckets[:MAX_TOPICS]


def build_course_knowledge(
    store,
    *,
    course_id: str,
    sub_ids: list[str],
    include_personal_notes: bool = False,
    now: float | None = None,
) -> tuple[dict, dict]:
    """Build and validate one course document; returns ``(document, diagnostics)``.

    Validation happens here, before any write, so a malformed build can never
    replace a good snapshot.
    """
    lectures = [
        build_lecture_knowledge(
            store,
            course_id=course_id,
            sub_id=sub_id,
            include_personal_notes=include_personal_notes,
            now=now,
        )
        for sub_id in sorted({str(value) for value in sub_ids if str(value)})
    ]
    if not lectures:
        raise CourseKnowledgeError("course_has_no_lectures")
    now_value = float(time.time() if now is None else now)

    excerpts = {lecture["sub_id"]: lecture.pop("_excerpts") for lecture in lectures}
    scope_by_source = {lecture["sub_id"]: lecture.pop("_scope_by_source") for lecture in lectures}
    dropped = {lecture["sub_id"]: lecture.pop("_dropped") for lecture in lectures}
    signals = {lecture["sub_id"]: lecture.pop("_signals") for lecture in lectures}
    assessment_rows = {lecture["sub_id"]: lecture.pop("_assessment_items") for lecture in lectures}

    # Cross-lecture topic merging, then each lecture keeps the merged topics it
    # actually contributed to.
    merged = merge_topics([
        {**topic, "lecture_ids": [lecture["sub_id"]]}
        for lecture in lectures
        for topic in lecture["topics"]
    ])
    for lecture in lectures:
        # 合并后的主题带着多讲的引用；讲次级视图只保留本讲自己声明的引用，
        # 一条都留不下的主题就不挂到这一讲（宁可少一行，不落悬空引用）。
        own = {ref["citation_id"] for ref in lecture["evidence_refs"]}
        attached = []
        for topic in merged:
            if lecture["sub_id"] not in topic["lecture_ids"]:
                continue
            citations = [value for value in topic["citation_ids"] if value in own]
            if not citations:
                continue
            attached.append({**topic, "lecture_ids": [lecture["sub_id"]], "citation_ids": citations})
        lecture["topics"] = attached

    sources: dict[tuple[str, str], dict] = {}
    for lecture in lectures:
        for ref in lecture["evidence_refs"]:
            key = (ref["kind"], ref["source_id"])
            if key in sources:
                continue
            course_scope = scope_by_source[lecture["sub_id"]].get(ref["source_id"]) == "course"
            scope = "course" if course_scope else "lecture"
            sources[key] = {
                "kind": ref["kind"],
                "scope": scope,
                "sub_id": "" if course_scope else lecture["sub_id"],
                "external_id": ref["source_id"],
                "revision_id": ref["revision_id"],
                "label": _source_label(ref),
            }

    citation_of_question: dict[str, str] = {}
    document_of_question: dict[str, str] = {}
    for items in assessment_rows.values():
        for item in items:
            document_of_question[item["question_id"]] = item["document_id"]
    for lecture in lectures:
        for ref in lecture["evidence_refs"]:
            if ref["kind"] == "assessment_item":
                citation_of_question.setdefault(ref["source_id"], ref["citation_id"])

    assessment_items = []
    for question_id in sorted(citation_of_question):
        item = next(
            (value for items in assessment_rows.values() for value in items if value["question_id"] == question_id),
            None,
        )
        if item is None or not _HASH_RE.match(document_of_question.get(question_id, "")):
            continue
        assessment_items.append({
            "course_id": str(course_id),
            "sub_id": item["sub_id"],
            "document_id": document_of_question[question_id],
            "question_no": item["question_no"],
            "label": item["label"],
            "content_hash": item["content_hash"],
            "citation_ids": [citation_of_question[question_id]],
        })

    root_refs: list[dict] = []
    for ref in sorted(
        (ref for lecture in lectures for ref in lecture["evidence_refs"] if ref["kind"] == "assessment_item"),
        key=lambda ref: ref["citation_id"],
    ):
        candidate = {key: value for key, value in ref.items() if key != "citation_id"}
        validated = contract.validate_evidence_ref(candidate, course_id, "root")
        if all(existing["citation_id"] != validated["citation_id"] for existing in root_refs):
            root_refs.append(validated)

    coverage = {
        "lectures_total": len(lectures),
        "lectures_ready": sum(1 for lecture in lectures if lecture["status"] == "ready"),
        "lectures_partial": sum(1 for lecture in lectures if lecture["status"] == "partial"),
        "lectures_stale": sum(1 for lecture in lectures if lecture["status"] == "stale"),
        "transcript_segments": sum(lecture["source_coverage"]["transcript_segments"] for lecture in lectures),
        "slide_pages": sum(lecture["source_coverage"]["slide_pages"] for lecture in lectures),
        "document_pages": sum(lecture["source_coverage"]["document_pages"] for lecture in lectures),
        # 课程级文档是全课共享的：同一页会被每一讲的包各自拉一次，这里按
        # (文档, 页) 去重报「全课可用页数」，而不是把讲次数乘进去。
        "course_document_pages": len({
            (ref["source_id"], ref["locator"].get("page"))
            for lecture in lectures
            for ref in lecture["evidence_refs"]
            if ref["kind"] == "document_page"
            and scope_by_source[lecture["sub_id"]].get(ref["source_id"]) == "course"
        }),
        "assessment_items": len(assessment_items),
        "bookmarks": sum(lecture["source_coverage"]["bookmarks"] for lecture in lectures),
    }
    course_status = "ready" if all(lecture["status"] == "ready" for lecture in lectures) else "partial"
    reasons: list[str] = []
    if course_status != "ready":
        reasons.append("summary_missing" if any(
            "summary_missing" in lecture["stale_reasons"] for lecture in lectures
        ) else "never_built")

    document = {
        "contract": contract.CONTRACT_ID,
        "course_id": str(course_id),
        "input_hash": course_signature_hash(course_id, lectures),
        "status": course_status,
        "stale_reasons": sorted(set(reasons)),
        "coverage": coverage,
        "topics": merged,
        "lectures": lectures,
        "assessment_items": assessment_items,
        "sources": list(sources.values()),
        "evidence_refs": root_refs,
        "updated_at": now_value,
    }
    return contract.validate_course_knowledge(document), {
        "excerpts": excerpts,
        "dropped": dropped,
        "signals": signals,
        "scope_by_source": scope_by_source,
    }


# ---------------------------------------------------------------------------
# Staleness, packets and refresh planning


def _refresh_status_counts(document: dict) -> None:
    """Coverage counts describe the document as published, not as built."""
    coverage = document["coverage"]
    coverage["lectures_ready"] = sum(1 for item in document["lectures"] if item["status"] == "ready")
    coverage["lectures_partial"] = sum(1 for item in document["lectures"] if item["status"] == "partial")
    coverage["lectures_stale"] = sum(1 for item in document["lectures"] if item["status"] == "stale")


def _stamp_staleness(document: dict, stored: dict | None) -> dict:
    """Mark a fresh build stale only where its recorded revision differs."""
    if stored is None:
        document["status"] = "stale"
        document["stale_reasons"] = sorted(set(document["stale_reasons"]) | {"never_built"})
        for lecture in document["lectures"]:
            lecture["status"] = "stale"
            lecture["stale_reasons"] = sorted(set(lecture["stale_reasons"]) | {"never_built"})
        _refresh_status_counts(document)
        return document
    stored_lectures = {
        lecture["sub_id"]: lecture for lecture in (stored.get("lectures") or [])
        if isinstance(lecture, dict)
    }
    changed = stored.get("input_hash") != document["input_hash"]
    for lecture in document["lectures"]:
        previous = stored_lectures.get(lecture["sub_id"])
        if previous is None:
            lecture["status"] = "stale"
            lecture["stale_reasons"] = sorted(set(lecture["stale_reasons"]) | {"never_built"})
        elif previous.get("input_hash") != lecture["input_hash"]:
            lecture["status"] = "stale"
            lecture["stale_reasons"] = sorted(set(lecture["stale_reasons"]) | {"input_changed"})
    if changed:
        document["status"] = "stale"
        document["stale_reasons"] = sorted(set(document["stale_reasons"]) | {"input_changed"})
    _refresh_status_counts(document)
    return document


def build_evidence_packet(
    store,
    *,
    course_id: str,
    sub_id: str,
    include_personal_notes: bool = False,
    item_chars: int = PACKET_ITEM_CHARS,
    total_chars: int = PACKET_TOTAL_CHARS,
) -> dict:
    """Bounded per-lecture evidence packet for the summary job payload."""
    lecture = build_lecture_knowledge(
        store, course_id=course_id, sub_id=sub_id,
        include_personal_notes=include_personal_notes,
    )
    excerpts = lecture["_excerpts"]
    item_cap = max(1, int(item_chars))
    total_cap = max(1, int(total_chars))
    # Source precedence decides the order (teacher documents before subtitles),
    # never whether conflicting evidence survives.
    ordered = sorted(
        lecture["evidence_refs"],
        key=lambda ref: (-(1 if ref["kind"] in {"document_page", "slide"} else 0), ref["citation_id"]),
    )
    items: list[dict] = []
    reasons: dict[str, int] = {}
    dropped_items = 0
    dropped_chars = 0
    used = 0

    def _drop(reason: str, size: int) -> None:
        nonlocal dropped_items, dropped_chars
        dropped_items += 1
        dropped_chars += size
        reasons[reason] = reasons.get(reason, 0) + 1

    for ref in ordered:
        full = _clean(excerpts.get(_ref_key(ref), ""))
        if not full:
            _drop("empty_text", 0)
            continue
        if len(full) > item_cap:
            # 绝不发送被截断的正文：正文一旦与 content_hash 不符，下游会
            # 如实判为"被改过"并整条拒绝——宁可少一条，不做假条目。
            _drop("item_over_limit", len(full))
            continue
        if used + len(full) > total_cap:
            _drop("packet_over_limit", len(full))
            continue
        used += len(full)
        items.append({
            "citation_id": ref["citation_id"],
            "kind": ref["kind"],
            "source_id": ref["source_id"],
            "revision_id": ref["revision_id"],
            "content_hash": ref["content_hash"],
            "locator": ref["locator"],
            "label": ref["label"],
            "text": full,
        })
    return {
        "contract": contract.CONTRACT_ID,
        "kind": "evidence_packet",
        "course_id": str(course_id),
        "sub_id": str(sub_id),
        "input_hash": lecture["input_hash"],
        "limits": {"item_chars": item_cap, "total_chars": total_cap},
        # ``items``/``chars``/``reasons`` 是 Worker 侧消费者读取的三键；组装期
        # 各族丢弃计数留在 build_dropped，两者语义不同不混用。
        "dropped": {"items": dropped_items, "chars": dropped_chars, "reasons": reasons},
        "build_dropped": dict(lecture["_dropped"]),
        "source_coverage": lecture["source_coverage"],
        "items": items,
        "used_chars": used,
    }


# 课程上下文的形状由 Worker 消费者钉死：它只保留**扁平标量**（字符串/数字），
# 嵌套结构会被整键丢弃。因此这里把主题与讲次压成两条限长的句子，而不是发结构体。
CONTEXT_TEXT_CHARS = 190


def _join_limited(values: list[str], limit: int = CONTEXT_TEXT_CHARS) -> str:
    text = "；".join(value for value in values if value)
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def course_context(
    store,
    *,
    course_id: str,
    sub_ids: list[str],
    lecture_titles: dict[str, str] | None = None,
    current_sub_id: str = "",
) -> dict:
    """Small, flat course context for a summary job (no bulk text).

    Every value is a short scalar so a consumer that keeps only scalars (the
    Worker's ``validate_course_context``) does not silently drop half of it.
    """
    document, _ = build_course_knowledge(store, course_id=course_id, sub_ids=sub_ids)
    titles = {str(key): str(value) for key, value in (lecture_titles or {}).items()}
    lectures = document["lectures"]
    labels = [
        " ".join(value for value in (titles.get(lecture["sub_id"], ""), lecture["sub_id"]) if value)
        for lecture in lectures
    ]
    current_index = next(
        (index for index, lecture in enumerate(lectures, start=1)
         if lecture["sub_id"] == str(current_sub_id)),
        0,
    )
    return {
        "contract": contract.CONTRACT_ID,
        "kind": "course_context",
        "course_id": str(course_id),
        "lecture_count": len(lectures),
        "current_lecture_index": current_index,
        "topics": _join_limited([topic["title"] for topic in document["topics"]]),
        "lecture_list": _join_limited(labels),
        "assessment_items": len(document["assessment_items"]),
        "source_files": document["coverage"]["course_document_pages"],
    }


def course_review(
    store,
    *,
    course_id: str,
    sub_ids: list[str],
    include_personal_notes: bool = False,
) -> dict:
    """Live course review: fresh build compared against the stored snapshot."""
    stored = store.get_course_knowledge_snapshot(course_id)
    try:
        document, diagnostics = build_course_knowledge(
            store, course_id=course_id, sub_ids=sub_ids,
            include_personal_notes=include_personal_notes,
        )
    except CourseKnowledgeError as error:
        document = None
        diagnostics = {"error_code": error.code}
    except contract.CourseKnowledgeContractError as error:
        document = None
        diagnostics = {"error_code": error.code}
    if document is None:
        if stored is None:
            raise CourseKnowledgeError(diagnostics.get("error_code") or "course_knowledge_unavailable")
        stored_document = stored.get("document")
        fallback = dict(stored_document) if isinstance(stored_document, dict) else {}
        fallback["status"] = "error"
        fallback["stale_reasons"] = ["snapshot_invalid"]
        return {
            "document": fallback,
            "snapshot": {key: value for key, value in stored.items() if key != "document"},
            "diagnostics": diagnostics,
            "fresh": False,
        }
    document = _stamp_staleness(document, stored["document"] if stored else None)
    return {
        "document": contract.validate_course_knowledge(document),
        "snapshot": (
            {key: value for key, value in stored.items() if key != "document"} if stored else None
        ),
        "diagnostics": diagnostics,
        "fresh": True,
    }


def save_course_knowledge(
    store,
    *,
    course_id: str,
    sub_ids: list[str],
    include_personal_notes: bool = False,
    keep: int = 3,
    now: float | None = None,
) -> dict:
    """Build, validate, then store one snapshot; returns the stored record.

    The build is validated before this function touches the database, so a
    failed build leaves the previous good snapshot in place.
    """
    document, diagnostics = build_course_knowledge(
        store, course_id=course_id, sub_ids=sub_ids,
        include_personal_notes=include_personal_notes, now=now,
    )
    stored = store.save_course_knowledge_snapshot(
        course_id=course_id,
        input_hash=document["input_hash"],
        contract_version=CONTRACT_VERSION,
        document=document,
        status=document["status"],
        now=now,
    )
    store.prune_course_knowledge_snapshots(course_id, keep=keep)
    return {"snapshot": stored, "document": document, "diagnostics": diagnostics}


def refresh_plan(
    store,
    *,
    course_id: str,
    sub_ids: list[str],
    active_sub_ids: set[str] | None = None,
    include_personal_notes: bool = False,
) -> dict:
    """Which lectures need a summary run, and why the rest do not (item 17)."""
    active = {str(value) for value in (active_sub_ids or set())}
    selected = sorted({str(value) for value in sub_ids if str(value)})
    if not selected:
        return {"queued": [], "skipped": [], "blocked": [], "counts": {"queued": 0, "skipped": 0, "blocked": 0}}
    stored = store.get_course_knowledge_snapshot(course_id)
    stored_lectures = {
        lecture["sub_id"]: lecture
        for lecture in ((stored or {}).get("document") or {}).get("lectures") or []
        if isinstance(lecture, dict)
    }
    queued: list[dict] = []
    skipped: list[dict] = []
    blocked: list[dict] = []
    with closing(connect_learning_db(store.path)) as db:
        db.row_factory = sqlite3.Row
        for sub_id in selected:
            segments = _query(
                db,
                """SELECT COUNT(*) FROM transcript_segments
                   WHERE sub_id=? AND text IS NOT NULL AND text!=''""",
                (sub_id,),
            )
            has_transcript = bool(segments and int(segments[0][0] or 0) > 0)
            lecture = build_lecture_knowledge(
                store, course_id=course_id, sub_id=sub_id,
                include_personal_notes=include_personal_notes,
            )
            previous = stored_lectures.get(sub_id)
            coverage = lecture["source_coverage"]
            has_ai = bool(coverage["lecture_ir"] or coverage["summary"])
            if not has_transcript:
                blocked.append({"sub_id": sub_id, "reason": "transcript_missing"})
            elif sub_id in active:
                skipped.append({"sub_id": sub_id, "reason": "already_running"})
            elif not has_ai:
                # No AI output has ever been produced for these inputs.  A
                # matching revision is not "up to date" here: without this
                # branch a lecture could never get its first summary, because
                # the stored revision already equals the current one.
                queued.append({"sub_id": sub_id, "reason": "missing"})
            elif previous is not None and previous.get("input_hash") == lecture["input_hash"]:
                skipped.append({"sub_id": sub_id, "reason": "up_to_date"})
            else:
                queued.append({"sub_id": sub_id, "reason": "stale"})
    return {
        "queued": queued,
        "skipped": skipped,
        "blocked": blocked,
        "counts": {"queued": len(queued), "skipped": len(skipped), "blocked": len(blocked)},
    }


__all__ = [
    "CONTRACT_VERSION",
    "PACKET_ITEM_CHARS",
    "PACKET_TOTAL_CHARS",
    "MAX_TRANSCRIPT_SEGMENTS",
    "MAX_SLIDE_PAGES",
    "MAX_LECTURE_DOC_PAGES",
    "MAX_COURSE_DOC_PAGES",
    "MAX_ASSESSMENT_ITEMS",
    "MAX_BOOKMARKS",
    "MAX_KEY_POINTS",
    "CourseKnowledgeError",
    "lecture_input_signature",
    "signature_hash",
    "course_signature_hash",
    "build_lecture_knowledge",
    "build_course_knowledge",
    "build_evidence_packet",
    "course_context",
    "course_review",
    "save_course_knowledge",
    "refresh_plan",
    "merge_topics",
]
