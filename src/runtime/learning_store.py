"""Persistent local learning state for the browser course viewer."""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from .learning_schema import initialize_learning_schema
from .subtitle_reader import assign_fallback_evidence_ids, is_evidence_id, sanitize_evidence_metadata


MAX_MEDIA_SECONDS = 7 * 24 * 60 * 60

# POLISH-1 F8b：「没听懂」热点标记的固定 note 字面量（player-core 写入侧同源）。
NOT_UNDERSTOOD_NOTE = "没听懂"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DECK_ID_RE = re.compile(r"^deck-[0-9a-f]{12}$")


def _normalized_anchor_ms(value: Any) -> int | None:
    """RR-ANCHORFE-1：takeaway 锚只收非负整数毫秒。

    None/布尔/负数/非整数浮点/非有限值一律视为无锚——锚错比锚缺更伤信任，
    客户端侧与生产端同守 fail-closed 降级纪律。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    milliseconds = int(value)
    if milliseconds < 0 or milliseconds != value:
        return None
    return milliseconds


def _normalized_review_views(review_views: Any) -> dict[str, Any] | None:
    """P2-CONTRACT-1 ④：review_views 存储侧 shape 守门（fail-closed）。

    非 dict 或四档全缺 → None（不落任何行）；单档畸形（items/events 非列表、
    briefing 无速览文本）整档丢弃。逐条文本帽/锚白名单/引用闭集校验归 worker
    派生调用收口，存储侧只拒绝装不进冻结 schema 的形状。
    """
    if not isinstance(review_views, dict):
        return None
    views: dict[str, Any] = {}
    for tier in ("study_guide", "faq"):
        body = review_views.get(tier)
        if isinstance(body, dict) and isinstance(body.get("items"), list) and body["items"]:
            views[tier] = body
    events = review_views.get("timeline")
    if isinstance(events, dict) and isinstance(events.get("events"), list) and events["events"]:
        views["timeline"] = events
    briefing = review_views.get("briefing")
    if isinstance(briefing, dict) and str(briefing.get("speed_read") or "").strip():
        views["briefing"] = briefing
    return views or None


def _ppt_evidence_metadata(page: object) -> dict[str, str]:
    """Closed-set bounded evidence identity from one worker page record.

    Contract-shaped IDs, the deck scope token, and the content digest are
    kept; legacy records and anything malformed store nothing, and no value
    is ever invented or repaired here.
    """
    row = page if isinstance(page, dict) else {}
    metadata: dict[str, str] = {}
    for key in ("entity_id", "event_id"):
        value = row.get(key)
        if is_evidence_id(value):
            metadata[key] = str(value)
    deck_id = row.get("deck_id")
    if isinstance(deck_id, str) and _DECK_ID_RE.match(deck_id):
        metadata["deck_id"] = deck_id
    content = row.get("content_sha256") or row.get("source_sha256")
    if isinstance(content, str) and _SHA256_RE.match(content):
        metadata["content_sha256"] = content
    return metadata


def _quality_deterministic_counts(
    segments: list[dict[str, Any]], chapters: list[dict[str, Any]]
) -> dict[str, int]:
    """P11 合同 §②：确定性完整性计数（客户端 import 时对 DB 现算）。

    非单调 start_ms、duration≤0、空 text、chapter start_ms 越 transcript
    范围——机器可确定性检验的完整性问题不交给 LLM（judge 幻觉面最小化）。
    """
    segments_total = len(segments)
    non_monotonic = 0
    bad_duration = 0
    empty_text = 0
    previous_start: int | None = None
    transcript_end_ms = 0
    for segment in segments:
        start_ms = max(0, int(segment.get("start_ms") or 0))
        end_ms = max(0, int(segment.get("end_ms") or 0))
        if previous_start is not None and start_ms < previous_start:
            non_monotonic += 1
        previous_start = start_ms
        if end_ms <= start_ms:
            bad_duration += 1
        if not str(segment.get("text") or "").strip():
            empty_text += 1
        transcript_end_ms = max(transcript_end_ms, end_ms)
    chapters_total = len(chapters)
    chapter_out_of_range = sum(
        1
        for chapter in chapters
        if int(chapter.get("start_ms") or 0) < 0
        or int(chapter.get("start_ms") or 0) > transcript_end_ms
    )
    return {
        "segments_total": segments_total,
        "non_monotonic": non_monotonic,
        "bad_duration": bad_duration,
        "empty_text": empty_text,
        "chapters_total": chapters_total,
        "chapter_out_of_range": chapter_out_of_range,
    }


def _quality_report_markdown(
    subtitle: dict[str, Any] | None, summary: dict[str, Any] | None
) -> str:
    """P11 合同 §④：content_markdown=纯计数一行式人话摘要，零原文内容。"""
    parts: list[str] = []
    if isinstance(subtitle, dict):
        findings = [item for item in subtitle.get("findings") or [] if isinstance(item, dict)]
        term_issues = sum(
            1 for item in findings
            if item.get("code") in {"glossary_violation", "homophone_suspect", "term_inconsistent"}
        )
        readability_issues = sum(
            1 for item in findings if item.get("code") in {"broken_flow", "garbled"}
        )
        parts.append(
            f"字幕抽检 {int(subtitle.get('sample_size') or 0)} 段："
            f"术语疑点 {term_issues}、可读性 {readability_issues}"
        )
    else:
        parts.append("字幕抽检未评审")
    if isinstance(summary, dict):
        findings = [item for item in summary.get("findings") or [] if isinstance(item, dict)]
        factuality = sum(1 for item in findings if item.get("dimension") == "factuality")
        alignment = sum(1 for item in findings if item.get("dimension") == "alignment")
        readability = sum(1 for item in findings if item.get("dimension") == "readability")
        parts.append(
            f"总结评审：事实性 {factuality}、章节对齐 {alignment}、可读性 {readability}"
        )
    else:
        parts.append("总结评审未评审")
    return "｜".join(parts)


class _ClosingConnection(sqlite3.Connection):
    """Commit or roll back like sqlite3.Connection, then release the file."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class LearningStore:
    """Store user-owned viewing state separately from runtime task state.

    Connections are intentionally short-lived. The HTTP server and application
    workers use different threads, and opening a connection per operation is
    both inexpensive for this tiny database and avoids sharing SQLite
    connection objects across threads.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            self.search_fts_enabled = initialize_learning_schema(db)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(
            self.path,
            timeout=10.0,
            factory=_ClosingConnection,
        )
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @staticmethod
    def _milliseconds(seconds: object) -> int:
        try:
            value = float(seconds)
        except (TypeError, ValueError):
            value = 0.0
        if value != value or value in {float("inf"), float("-inf")}:
            value = 0.0
        return int(round(max(0.0, min(value, MAX_MEDIA_SECONDS)) * 1000.0))

    @staticmethod
    def _playback_rate(value: object) -> float:
        try:
            rate = float(value)
        except (TypeError, ValueError):
            rate = 1.0
        if rate != rate or rate in {float("inf"), float("-inf")}:
            rate = 1.0
        return round(max(0.25, min(rate, 4.0)), 2)

    @staticmethod
    def _public(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any]:
        if row is None:
            return {
                "position_seconds": 0.0,
                "duration_seconds": 0.0,
                "progress_percent": 0.0,
                "completed": False,
                "playback_rate": 1.0,
                "updated_at": 0.0,
            }
        data = dict(row)
        position_ms = max(0, int(data.get("position_ms") or 0))
        duration_ms = max(0, int(data.get("duration_ms") or 0))
        percent = min(100.0, position_ms * 100.0 / duration_ms) if duration_ms else 0.0
        return {
            "sub_id": str(data.get("sub_id") or ""),
            "course_id": str(data.get("course_id") or ""),
            "position_seconds": round(position_ms / 1000.0, 3),
            "duration_seconds": round(duration_ms / 1000.0, 3),
            "progress_percent": round(percent, 1),
            "completed": bool(data.get("completed")),
            "playback_rate": float(data.get("playback_rate") or 1.0),
            "updated_at": float(data.get("updated_at") or 0.0),
        }

    def get_watch_progress(self, sub_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM watch_progress WHERE sub_id=?",
                (str(sub_id),),
            ).fetchone()
        return self._public(row)

    def list_watch_progress(self) -> dict[str, dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM watch_progress").fetchall()
        return {str(row["sub_id"]): self._public(row) for row in rows}

    def save_watch_progress(
        self,
        *,
        course_id: str,
        sub_id: str,
        position_seconds: object,
        duration_seconds: object,
        completed: bool = False,
        playback_rate: object = 1.0,
    ) -> dict[str, Any]:
        course_id = str(course_id or "").strip()
        sub_id = str(sub_id or "").strip()
        if not course_id or not sub_id:
            raise ValueError("course_id and sub_id are required")
        position_ms = self._milliseconds(position_seconds)
        duration_ms = self._milliseconds(duration_seconds)
        if duration_ms:
            position_ms = min(position_ms, duration_ms)
        completed_value = bool(completed)
        if duration_ms and position_ms * 100 >= duration_ms * 95:
            completed_value = True
        now = time.time()
        with self._connect() as db:
            db.execute(
                """INSERT INTO watch_progress(
                       sub_id,course_id,position_ms,duration_ms,completed,
                       playback_rate,updated_at
                   ) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(sub_id) DO UPDATE SET
                       course_id=excluded.course_id,
                       position_ms=excluded.position_ms,
                       duration_ms=excluded.duration_ms,
                       completed=excluded.completed,
                       playback_rate=excluded.playback_rate,
                       updated_at=excluded.updated_at""",
                (
                    sub_id,
                    course_id,
                    position_ms,
                    duration_ms,
                    int(completed_value),
                    self._playback_rate(playback_rate),
                    now,
                ),
            )
        return self.get_watch_progress(sub_id)

    def get_transcript_source(self, sub_id: str) -> dict[str, Any] | None:
        """夜10-C N10B-4：读链自愈用的源登记 getter（行缺失返回 None）。"""
        with self._connect() as db:
            row = db.execute(
                """SELECT sub_id,source_path,source_mtime_ns,source_size,segment_count,updated_at
                   FROM transcript_sources WHERE sub_id=?""",
                (str(sub_id or "").strip(),),
            ).fetchone()
        return dict(row) if row is not None else None

    def transcript_source_matches(
        self,
        sub_id: str,
        *,
        source_path: str,
        source_mtime_ns: int,
        source_size: int,
    ) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT source_path,source_mtime_ns,source_size FROM transcript_sources WHERE sub_id=?",
                (str(sub_id),),
            ).fetchone()
        return bool(
            row
            and row["source_path"] == str(source_path)
            and int(row["source_mtime_ns"]) == int(source_mtime_ns)
            and int(row["source_size"]) == int(source_size)
        )

    def replace_transcript_segments(
        self,
        sub_id: str,
        *,
        source_path: str,
        source_mtime_ns: int,
        source_size: int,
        segments: list[dict[str, Any]],
    ) -> None:
        sub_id = str(sub_id or "").strip()
        if not sub_id:
            raise ValueError("sub_id is required")
        if not isinstance(segments, list):
            # 夜10-C T19：非列表负载曾以裸 TypeError 逃逸（契约边界收口）。
            raise ValueError("transcript segments payload is invalid")
        normalized: list[tuple[str, int, int, int, str, str]] = []
        previous_start = -1
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            # 夜10-C T19：畸形时间戳行跳过（证据层免疫与 worker 侧 normalize
            # 同纪律）；合法行的存储行为零变化。
            try:
                start_ms = max(0, int(segment.get("start_ms") or 0))
                end_ms = max(start_ms + 1, int(segment.get("end_ms") or 0))
            except (TypeError, ValueError):
                continue
            text = " ".join(str(segment.get("text") or "").split())
            if not text or start_ms < previous_start:
                continue
            previous_start = start_ms
            # Additive evidence seam: bounded safe metadata survives the cache;
            # legacy rows store nothing and get deterministic IDs on read.
            metadata = sanitize_evidence_metadata(segment)
            evidence_json = (
                json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if metadata
                else ""
            )
            normalized.append((sub_id, len(normalized) + 1, start_ms, end_ms, text, evidence_json))
        with self._connect() as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("DELETE FROM transcript_segments WHERE sub_id=?", (sub_id,))
            db.execute(
                """INSERT INTO transcript_sources(
                       sub_id,source_path,source_mtime_ns,source_size,segment_count,updated_at
                   ) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(sub_id) DO UPDATE SET
                       source_path=excluded.source_path,
                       source_mtime_ns=excluded.source_mtime_ns,
                       source_size=excluded.source_size,
                       segment_count=excluded.segment_count,
                       updated_at=excluded.updated_at""",
                (
                    sub_id,
                    str(source_path),
                    int(source_mtime_ns),
                    int(source_size),
                    len(normalized),
                    time.time(),
                ),
            )
            db.executemany(
                """INSERT INTO transcript_segments(
                       sub_id,segment_index,start_ms,end_ms,text,evidence_json
                   ) VALUES(?,?,?,?,?,?)""",
                normalized,
            )

    def get_transcript_segments(self, sub_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT segment_index,start_ms,end_ms,text,evidence_json
                   FROM transcript_segments WHERE sub_id=?
                   ORDER BY segment_index""",
                (str(sub_id),),
            ).fetchall()
        segments: list[dict[str, Any]] = []
        for row in rows:
            segment = {
                "index": int(row["segment_index"]),
                "start_ms": int(row["start_ms"]),
                "end_ms": int(row["end_ms"]),
                "text": str(row["text"]),
            }
            try:
                metadata = json.loads(row["evidence_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            if isinstance(metadata, dict):
                for key in ("index", "start_ms", "end_ms", "text"):
                    metadata.pop(key, None)
                segment.update(metadata)
            segments.append(segment)
        return assign_fallback_evidence_ids(segments)

    def insert_ppt_pages_pending(self, sub_id: str, items: list[dict]) -> int:
        if not items:
            return 0
        with self._connect() as db:
            before = db.total_changes
            db.executemany(
                """INSERT OR IGNORE INTO ppt_pages(
                       sub_id,page_num,created_sec,pptimgurl,ocr_status
                   ) VALUES(?,?,?,?,'pending')""",
                [
                    (
                        str(sub_id),
                        int(item["page_num"]),
                        int(item.get("created_sec") or 0),
                        str(item.get("pptimgurl") or ""),
                    )
                    for item in items
                ],
            )
            return max(0, db.total_changes - before)

    def update_ppt_page(
        self,
        sub_id: str,
        page_num: int,
        text: str | None,
        status: str,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE ppt_pages SET text=?,ocr_status=?,ocr_at=?
                   WHERE sub_id=? AND page_num=?""",
                (text, str(status), time.time(), str(sub_id), int(page_num)),
            )

    def update_ppt_page_dhash(self, sub_id: str, page_num: int, dhash: str | None) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE ppt_pages SET dhash=? WHERE sub_id=? AND page_num=?",
                (dhash, str(sub_id), int(page_num)),
            )

    def get_done_ppt_pages(self, sub_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT page_num,created_sec,text,evidence_json
                   FROM ppt_pages
                   WHERE sub_id=? AND ocr_status='done'
                     AND text IS NOT NULL AND text!=''
                   ORDER BY created_sec,page_num""",
                (str(sub_id),),
            ).fetchall()
        pages: list[dict[str, Any]] = []
        for row in rows:
            page = {
                "page_num": int(row["page_num"]),
                "created_sec": int(row["created_sec"]),
                "text": str(row["text"]),
            }
            try:
                metadata = json.loads(row["evidence_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            if isinstance(metadata, dict):
                page.update(metadata)
            pages.append(page)
        return pages

    def count_total_ppt_pages(self, sub_id: str) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM ppt_pages WHERE sub_id=?",
                (str(sub_id),),
            ).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _artifact(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        try:
            data["content"] = json.loads(data.pop("content_json") or "{}")
        except (TypeError, ValueError):
            data["content"] = {}
            data.pop("content_json", None)
        try:
            data["metrics"] = json.loads(data.pop("metrics_json") or "{}")
        except (TypeError, ValueError):
            data["metrics"] = {}
            data.pop("metrics_json", None)
        return data

    def find_ai_artifact(
        self,
        sub_id: str,
        kind: str,
        *,
        input_hash: str | None = None,
        prompt_version: str | None = None,
    ) -> dict[str, Any] | None:
        clauses = ["sub_id=?", "kind=?"]
        args: list[Any] = [str(sub_id), str(kind)]
        if input_hash is not None:
            clauses.append("input_hash=?")
            args.append(str(input_hash))
        if prompt_version is not None:
            clauses.append("prompt_version=?")
            args.append(str(prompt_version))
        with self._connect() as db:
            row = db.execute(
                f"""SELECT * FROM ai_artifacts
                    WHERE {' AND '.join(clauses)}
                    ORDER BY updated_at DESC LIMIT 1""",
                args,
            ).fetchone()
        return self._artifact(row)

    def begin_ai_artifact(
        self,
        *,
        course_id: str,
        sub_id: str,
        kind: str,
        input_hash: str,
        prompt_version: str,
        content: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        artifact_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute(
                """INSERT INTO ai_artifacts(
                       artifact_id,course_id,sub_id,kind,status,input_hash,
                       prompt_version,content_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(sub_id,kind,input_hash,prompt_version) DO UPDATE SET
                       status='running',model=NULL,content_markdown=NULL,
                       content_json=excluded.content_json,error=NULL,
                       metrics_json='{}',
                       updated_at=excluded.updated_at""",
                (
                    artifact_id,
                    str(course_id),
                    str(sub_id),
                    str(kind),
                    "running",
                    str(input_hash),
                    str(prompt_version),
                    json.dumps(content or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        artifact = self.find_ai_artifact(
            sub_id,
            kind,
            input_hash=input_hash,
            prompt_version=prompt_version,
        )
        assert artifact is not None
        return artifact

    def complete_ai_artifact(
        self,
        artifact_id: str,
        *,
        model: str,
        markdown: str,
        content: dict[str, Any],
        metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._connect() as db:
            db.execute(
                """UPDATE ai_artifacts SET status='ready',model=?,
                       content_markdown=?,content_json=?,metrics_json=?,error=NULL,updated_at=?
                   WHERE artifact_id=?""",
                (
                    str(model),
                    str(markdown),
                    json.dumps(content, ensure_ascii=False),
                    json.dumps(metrics or {}, ensure_ascii=False),
                    time.time(),
                    str(artifact_id),
                ),
            )
            row = db.execute(
                "SELECT * FROM ai_artifacts WHERE artifact_id=?",
                (str(artifact_id),),
            ).fetchone()
        artifact = self._artifact(row)
        assert artifact is not None
        return artifact

    def import_remote_summary(
        self,
        *,
        course_id: str,
        sub_id: str,
        input_hash: str,
        model: str,
        markdown: str,
        chapters: list[dict[str, Any]],
        ppt_pages: list[dict[str, Any]],
        metrics: dict[str, Any] | None = None,
        key_takeaways: list[str] | None = None,
        takeaway_anchors: list[Any] | None = None,
        memory_applied: int = 0,
        review_views: dict[str, Any] | None = None,
    ) -> None:
        """Atomically import verified OCR, summary, and chapter outputs."""
        now = time.time()
        with self._connect() as db:
            row = db.execute(
                "SELECT MAX(end_ms) FROM transcript_segments WHERE sub_id=?",
                (str(sub_id),),
            ).fetchone()
        transcript_end_ms = max(0, int((row[0] if row else 0) or 0))
        normalized_chapters: list[dict[str, Any]] = []
        for index, item in enumerate(chapters):
            start_ms = max(0, int(item.get("start_ms") or 0))
            next_start = (
                max(start_ms + 1, int(chapters[index + 1].get("start_ms") or 0))
                if index + 1 < len(chapters)
                else max(start_ms + 1, transcript_end_ms)
            )
            normalized_chapters.append({
                "chapter_id": f"C{index + 1:04d}",
                "title": str(item.get("title") or "").strip(),
                "summary": str(item.get("summary") or "").strip(),
                "start_ms": start_ms,
                "end_ms": next_start,
                "source_refs": [],
            })
        # RR-ANCHORFE-1：takeaway 逐条时间戳锚与 key_takeaways 走同一过滤管线
        # （strip/截断/去空），保证等长对齐。加性字段：旧读面按「未知字段忽略」
        # 降级，None 锚表示没有足够把握、前端不渲染。
        aligned_takeaways: list[str] = []
        aligned_anchors: list[int | None] = []
        raw_anchors = list(takeaway_anchors or [])
        for index, item in enumerate(key_takeaways or []):
            text = str(item or "").strip()[:60]
            if not text:
                continue
            aligned_takeaways.append(text)
            aligned_anchors.append(
                _normalized_anchor_ms(raw_anchors[index]) if index < len(raw_anchors) else None
            )
        summary_content = {
            "schema_version": 1,
            "overview": str(markdown)[:5000],
            "key_takeaways": aligned_takeaways,
            "takeaway_anchors": aligned_anchors,
            "chapters": normalized_chapters,
            "generation": {
                "input_hash": str(input_hash),
                "prompt_version": "actions-summary-v1",
                "model": str(model),
                # RR-P6MEM-1：课程记忆注入数（生成侧实报；>0 才落，省键=旧形状）。
                **({"memory_applied": max(0, int(memory_applied))} if int(memory_applied or 0) > 0 else {}),
            },
        }
        artifacts = [
            ("timestamp_summary", str(markdown), summary_content, "actions-summary-v1"),
            (
                "lecture_chapters",
                "\n".join(
                    f"- [{item['start_ms'] // 60000:02d}:{item['start_ms'] // 1000 % 60:02d}] {item['title']}"
                    for item in normalized_chapters
                ),
                {"chapters": normalized_chapters, "anchor_count": len(normalized_chapters)},
                "actions-chapters-v1",
            ),
        ]
        # P2-CONTRACT-1 ④：多视图复习包随总结整体入库（kind=review_views，
        # review-views-v1）。shape fail-closed——畸形/四档全缺不落行，绝不
        # 挡总结导入；老结果无键=None 同样零行（幂等）。生命周期随总结：
        # 同 input_hash 重跑 upsert 覆盖，读取面 find_ai_artifact 取最新。
        normalized_views = _normalized_review_views(review_views)
        if normalized_views:
            briefing = normalized_views.get("briefing") or {}
            review_markdown = "\n".join(
                [str(briefing.get("speed_read") or "").strip()]
                + [
                    f"- {str(line or '').strip()}"
                    for line in (briefing.get("must_know") or [])
                    if str(line or "").strip()
                ]
            ).strip()
            artifacts.append((
                "review_views",
                review_markdown,
                {
                    "schema_version": 1,
                    "views": normalized_views,
                    "generation": {
                        "input_hash": str(input_hash),
                        "prompt_version": "review-views-v1",
                        "model": str(model),
                    },
                },
                "review-views-v1",
            ))
        with self._connect() as db:
            for page in ppt_pages:
                page_num = int(page.get("page_num") or 0)
                if page_num <= 0:
                    continue
                metadata = _ppt_evidence_metadata(page)
                evidence_json = (
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    if metadata
                    else ""
                )
                db.execute(
                    """INSERT INTO ppt_pages(
                           sub_id,page_num,created_sec,pptimgurl,dhash,text,ocr_status,ocr_at,evidence_json
                       ) VALUES(?,?,?,?,?,?,'done',?,?)
                       ON CONFLICT(sub_id,page_num) DO UPDATE SET
                           created_sec=excluded.created_sec,dhash=excluded.dhash,
                           text=excluded.text,ocr_status='done',ocr_at=excluded.ocr_at,
                           evidence_json=excluded.evidence_json""",
                    (
                        str(sub_id), page_num, int(page.get("created_sec") or 0), "",
                        str(page.get("dhash") or ""), str(page.get("text") or ""), now,
                        evidence_json,
                    ),
                )
            for kind, artifact_markdown, content, prompt_version in artifacts:
                db.execute(
                    """INSERT INTO ai_artifacts(
                           artifact_id,course_id,sub_id,kind,status,input_hash,
                           prompt_version,model,content_markdown,content_json,
                           metrics_json,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(sub_id,kind,input_hash,prompt_version) DO UPDATE SET
                           status='ready',model=excluded.model,
                           content_markdown=excluded.content_markdown,
                           content_json=excluded.content_json,
                           metrics_json=excluded.metrics_json,error=NULL,
                           updated_at=excluded.updated_at""",
                    (
                        uuid.uuid4().hex, str(course_id), str(sub_id), kind, "ready",
                        str(input_hash), prompt_version, str(model), artifact_markdown,
                        json.dumps(content, ensure_ascii=False),
                        json.dumps(metrics or {}, ensure_ascii=False), now, now,
                    ),
                )

    def import_lecture_ir(
        self,
        *,
        course_id: str,
        sub_id: str,
        input_hash: str,
        view: dict[str, Any] | None,
        metrics: dict[str, Any] | None = None,
    ) -> bool:
        """Additively import the deterministic Lecture IR view from a result.

        Accepts only the accepted worker shape
        ``{"contract": "evidence.v1", "sections": [], "knowledge_units": [],
        "key_moments": []}``; anything else stores nothing and returns False
        so a malformed output can never masquerade as accepted evidence.
        """
        if not isinstance(view, dict) or view.get("contract") != "evidence.v1":
            return False
        sections = view.get("sections")
        knowledge_units = view.get("knowledge_units")
        key_moments = view.get("key_moments")
        if not all(isinstance(part, list) for part in (sections, knowledge_units, key_moments)):
            return False
        normalized = {
            "contract": "evidence.v1",
            "sections": sections,
            "knowledge_units": knowledge_units,
            "key_moments": key_moments,
        }
        now = time.time()
        with self._connect() as db:
            db.execute(
                """INSERT INTO ai_artifacts(
                       artifact_id,course_id,sub_id,kind,status,input_hash,
                       prompt_version,model,content_markdown,content_json,
                       metrics_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(sub_id,kind,input_hash,prompt_version) DO UPDATE SET
                       status='ready',model='',
                       content_markdown='',
                       content_json=excluded.content_json,
                       metrics_json=excluded.metrics_json,error=NULL,
                       updated_at=excluded.updated_at""",
                (
                    uuid.uuid4().hex, str(course_id), str(sub_id), "lecture_ir", "ready",
                    str(input_hash), "lecture-ir-v1", "", "",
                    json.dumps(normalized, ensure_ascii=False),
                    json.dumps(metrics or {}, ensure_ascii=False), now, now,
                ),
            )
        return True

    def import_quality_report(
        self,
        *,
        course_id: str,
        sub_id: str,
        input_hash: str,
        model: str,
        report: dict[str, Any],
        metrics: dict[str, Any] | None = None,
    ) -> None:
        """P11 质量抽检报告落库（合同 §④：shape fail-closed，闭集计数零内容）。

        非 dict / schema_version≠1 / 双 mode 均 null → 不落任何行并抛
        ValueError("judge_output_invalid")。确定性完整性计数对 DB 现算
        （不经 LLM 不经 payload）；读取走既有 GET /api/v3/artifacts
        （kind=quality_report），同 input_hash 重检 upsert 覆盖，零新端点。
        """
        if not isinstance(report, dict) or report.get("schema_version") != 1:
            raise ValueError("judge_output_invalid")
        subtitle = report.get("subtitle")
        summary = report.get("summary")
        if subtitle is None and summary is None:
            raise ValueError("judge_output_invalid")
        if subtitle is not None and not isinstance(subtitle, dict):
            raise ValueError("judge_output_invalid")
        if summary is not None and not isinstance(summary, dict):
            raise ValueError("judge_output_invalid")
        segments = self.get_transcript_segments(str(sub_id))
        summary_row = self.find_ai_artifact(str(sub_id), "timestamp_summary")
        chapters = [
            chapter
            for chapter in (dict(summary_row or {}).get("content") or {}).get("chapters") or []
            if isinstance(chapter, dict)
        ] if summary_row else []
        content = {
            "schema_version": 1,
            "subtitle": subtitle,
            "summary": summary,
            "deterministic": _quality_deterministic_counts(segments, chapters),
            "generation": {
                "input_hash": str(input_hash),
                "prompt_version": "quality-judge-v1",
                "model": str(model),
            },
        }
        now = time.time()
        with self._connect() as db:
            db.execute(
                """INSERT INTO ai_artifacts(
                       artifact_id,course_id,sub_id,kind,status,input_hash,
                       prompt_version,model,content_markdown,content_json,
                       metrics_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(sub_id,kind,input_hash,prompt_version) DO UPDATE SET
                       status='ready',model=excluded.model,
                       content_markdown=excluded.content_markdown,
                       content_json=excluded.content_json,
                       metrics_json=excluded.metrics_json,error=NULL,
                       updated_at=excluded.updated_at""",
                (
                    uuid.uuid4().hex, str(course_id), str(sub_id), "quality_report", "ready",
                    str(input_hash), "quality-judge-v1", str(model),
                    _quality_report_markdown(subtitle, summary),
                    json.dumps(content, ensure_ascii=False),
                    json.dumps(dict(metrics or {}), ensure_ascii=False), now, now,
                ),
            )

    def interrupt_ai_artifacts(self, sub_id: str, kind: str, error: str) -> int:
        """Close any worker-owned artifact left running after cancellation."""
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE ai_artifacts SET status='failed',error=?,updated_at=?
                   WHERE sub_id=? AND kind=? AND status='running'""",
                (str(error), time.time(), str(sub_id), str(kind)),
            )
            return int(cursor.rowcount or 0)

    def save_course_knowledge_snapshot(
        self,
        *,
        course_id: str,
        input_hash: str,
        contract_version: str,
        document: dict[str, Any],
        status: str = "ready",
        now: float | None = None,
    ) -> dict[str, Any]:
        """Store one validated course-knowledge snapshot (additive, never replaces).

        The previous snapshot is deliberately left in place: a later build that
        fails validation simply never reaches this call, so readers keep the
        last good snapshot instead of an empty or half-written one.
        """
        payload = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        lectures = document.get("lectures") if isinstance(document, dict) else None
        lecture_count = len(lectures) if isinstance(lectures, list) else 0
        snapshot_id = hashlib.sha256(
            f"course-knowledge.v1\0{course_id}\0{contract_version}\0{input_hash}".encode("utf-8")
        ).hexdigest()[:32]
        timestamp = time.time() if now is None else float(now)
        with self._connect() as db:
            existing = db.execute(
                "SELECT document_json,status FROM course_knowledge_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if existing is not None and str(existing[1]) == str(status):
                try:
                    stored_document = json.loads(str(existing[0]) or "{}")
                except (TypeError, ValueError):
                    stored_document = None
                if (
                    isinstance(stored_document, dict)
                    and self._course_knowledge_fingerprint(stored_document)
                    == self._course_knowledge_fingerprint(document)
                ):
                    # 同一内容身份重放（重启重建、重复导入）不重写：行时间戳与
                    # 文档 updated_at 都保持原样，重放是真正的 no-op，表也不会
                    # 无界增长。
                    return self._course_knowledge_row(db.execute(
                        """SELECT snapshot_id,course_id,status,input_hash,contract_version,
                                  document_json,lecture_count,updated_at
                           FROM course_knowledge_snapshots WHERE snapshot_id=?""",
                        (snapshot_id,),
                    ).fetchone())
            db.execute(
                """INSERT INTO course_knowledge_snapshots(
                       snapshot_id,course_id,status,input_hash,contract_version,
                       document_json,lecture_count,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(snapshot_id) DO UPDATE SET
                       status=excluded.status,document_json=excluded.document_json,
                       lecture_count=excluded.lecture_count,updated_at=excluded.updated_at""",
                (
                    snapshot_id, str(course_id), str(status), str(input_hash),
                    str(contract_version), payload, int(lecture_count), timestamp,
                ),
            )
        return {
            "snapshot_id": snapshot_id,
            "course_id": str(course_id),
            "status": str(status),
            "input_hash": str(input_hash),
            "contract_version": str(contract_version),
            "document": document,
            "lecture_count": int(lecture_count),
            "updated_at": timestamp,
        }

    def get_course_knowledge_snapshot(self, course_id: str) -> dict[str, Any] | None:
        """Newest stored snapshot for a course, or None when never built."""
        with self._connect() as db:
            row = db.execute(
                """SELECT snapshot_id,course_id,status,input_hash,contract_version,
                          document_json,lecture_count,updated_at
                   FROM course_knowledge_snapshots WHERE course_id=?
                   ORDER BY updated_at DESC, rowid DESC LIMIT 1""",
                (str(course_id),),
            ).fetchone()
        return self._course_knowledge_row(row)

    def list_course_knowledge_snapshots(self, course_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT snapshot_id,course_id,status,input_hash,contract_version,
                          document_json,lecture_count,updated_at
                   FROM course_knowledge_snapshots WHERE course_id=?
                   ORDER BY updated_at DESC, rowid DESC LIMIT ?""",
                (str(course_id), max(1, int(limit))),
            ).fetchall()
        return [value for value in (self._course_knowledge_row(row) for row in rows) if value]

    def prune_course_knowledge_snapshots(self, course_id: str, *, keep: int = 3) -> int:
        """Drop all but the newest ``keep`` snapshots; pruning never picks."""
        retained = max(1, int(keep))
        with self._connect() as db:
            cursor = db.execute(
                """DELETE FROM course_knowledge_snapshots
                   WHERE course_id=? AND snapshot_id NOT IN (
                       SELECT snapshot_id FROM course_knowledge_snapshots
                       WHERE course_id=? ORDER BY updated_at DESC, rowid DESC LIMIT ?
                   )""",
                (str(course_id), str(course_id), retained),
            )
            return int(cursor.rowcount or 0)

    @staticmethod
    def _course_knowledge_fingerprint(document: dict[str, Any]) -> str:
        """Content identity of a snapshot, ignoring build timestamps.

        ``updated_at`` records when the knowledge was built, not what it says;
        excluding it is what lets a restart-time rebuild be a true no-op.
        """
        body = {key: value for key, value in document.items() if key != "updated_at"}
        lectures = body.get("lectures")
        if isinstance(lectures, list):
            body["lectures"] = [
                {key: value for key, value in lecture.items() if key != "updated_at"}
                if isinstance(lecture, dict) else lecture
                for lecture in lectures
            ]
        return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _course_knowledge_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        try:
            value["document"] = json.loads(value.pop("document_json") or "{}")
        except (TypeError, ValueError):
            value["document"] = {}
            value.pop("document_json", None)
        return value

    def search_rebuild_required(self) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT value FROM learning_schema_meta WHERE key='search_rebuild_required'"
            ).fetchone()
        return bool(row and str(row[0]) == "1")

    def mark_search_rebuild_complete(self) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO learning_schema_meta(key,value) VALUES('search_rebuild_required','0')
                   ON CONFLICT(key) DO UPDATE SET value='0'"""
            )

    def sync_search_catalog(self, rows: list[dict[str, Any]], *, prune: bool = True) -> None:
        """Synchronize manifest-owned labels without copying file paths."""

        normalized = [
            (
                str(row.get("sub_id") or ""),
                str(row.get("course_id") or ""),
                str(row.get("course_title") or ""),
                str(row.get("lecture_title") or ""),
                str(row.get("teacher") or ""),
                str(row.get("catalog_version") or ""),
                time.time(),
            )
            for row in rows
            if str(row.get("sub_id") or "") and str(row.get("course_id") or "")
        ]
        keep = [row[0] for row in normalized]
        with self._connect() as db:
            db.executemany(
                """INSERT INTO search_catalog(
                       sub_id,course_id,course_title,lecture_title,teacher,
                       catalog_version,updated_at
                   ) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(sub_id) DO UPDATE SET
                       course_id=excluded.course_id,
                       course_title=excluded.course_title,
                       lecture_title=excluded.lecture_title,
                       teacher=excluded.teacher,
                       catalog_version=excluded.catalog_version,
                       updated_at=excluded.updated_at""",
                normalized,
            )
            if prune:
                stale = db.execute("SELECT sub_id FROM search_catalog").fetchall()
                keep_ids = set(keep)
                if not keep_ids:
                    if self.search_fts_enabled:
                        db.execute("DELETE FROM search_fts")
                    db.execute("DELETE FROM search_documents")
                    db.execute("DELETE FROM search_catalog")
                else:
                    db.execute("CREATE TEMP TABLE search_keep_ids(sub_id TEXT PRIMARY KEY)")
                    db.executemany(
                        "INSERT INTO search_keep_ids(sub_id) VALUES(?)",
                        [(sub_id,) for sub_id in keep_ids],
                    )
                    stale_query = (
                        "SELECT doc_key FROM search_documents "
                        "WHERE sub_id NOT IN (SELECT sub_id FROM search_keep_ids)"
                    )
                    if self.search_fts_enabled:
                        db.execute(
                            "DELETE FROM search_fts WHERE doc_key IN (" + stale_query + ")"
                        )
                    db.execute(
                        "DELETE FROM search_documents WHERE sub_id NOT IN "
                        "(SELECT sub_id FROM search_keep_ids)"
                    )
                    db.execute(
                        "DELETE FROM search_catalog WHERE sub_id NOT IN "
                        "(SELECT sub_id FROM search_keep_ids)"
                    )
                    db.execute("DROP TABLE search_keep_ids")

    def _delete_search_documents(self, db: sqlite3.Connection, sub_id: str) -> None:
        keys = [
            str(row[0])
            for row in db.execute(
                "SELECT doc_key FROM search_documents WHERE sub_id=?",
                (str(sub_id),),
            ).fetchall()
        ]
        if self.search_fts_enabled:
            db.executemany("DELETE FROM search_fts WHERE doc_key=?", [(key,) for key in keys])
        db.execute("DELETE FROM search_documents WHERE sub_id=?", (str(sub_id),))

    def search_indexed_version(self, sub_id: str) -> str:
        with self._connect() as db:
            row = db.execute(
                "SELECT indexed_version FROM search_catalog WHERE sub_id=?",
                (str(sub_id),),
            ).fetchone()
        return str(row[0]) if row else ""

    def search_source_signature(self, sub_id: str, *, course_id: str = "") -> dict[str, Any]:
        with self._connect() as db:
            transcript = db.execute(
                """SELECT source_path,source_mtime_ns,source_size,segment_count,updated_at
                   FROM transcript_sources WHERE sub_id=?""",
                (str(sub_id),),
            ).fetchone()
            ppt = db.execute(
                """SELECT COUNT(*),COALESCE(MAX(ocr_at),0),COALESCE(SUM(LENGTH(text)),0)
                   FROM ppt_pages WHERE sub_id=? AND ocr_status='done'
                     AND text IS NOT NULL AND text!=''""",
                (str(sub_id),),
            ).fetchone()
            artifacts = db.execute(
                """SELECT kind,artifact_id,input_hash,updated_at
                   FROM ai_artifacts a
                   WHERE sub_id=? AND status='ready'
                     AND kind IN ('timestamp_summary','lecture_summary','lecture_chapters')
                     AND updated_at=(
                       SELECT MAX(updated_at) FROM ai_artifacts newer
                       WHERE newer.sub_id=a.sub_id AND newer.kind=a.kind
                         AND newer.status='ready'
                     )
                   ORDER BY kind""",
                (str(sub_id),),
            ).fetchall()
            try:
                documents = db.execute(
                    """SELECT COUNT(*),COALESCE(MAX(d.updated_at),0),
                              COALESCE(SUM(LENGTH(p.text)),0),COALESCE(MAX(a.updated_at),0)
                       FROM learning_documents d
                       JOIN learning_document_pages p ON p.document_id=d.document_id
                       LEFT JOIN document_alignments a
                         ON a.document_hash=d.sha256 AND a.page_num=p.page_num AND a.sub_id=d.sub_id
                      WHERE d.sub_id=?""",
                    (str(sub_id),),
                ).fetchone()
            except sqlite3.OperationalError:
                documents = None
            course_documents = []
            if course_id:
                try:
                    row = db.execute(
                        """SELECT COUNT(*),COALESCE(MAX(d.updated_at),0),
                                  COALESCE(SUM(LENGTH(p.text)),0)
                           FROM learning_documents d
                           JOIN learning_document_pages p ON p.document_id=d.document_id
                          WHERE d.scope='course' AND d.course_id=?""",
                        (str(course_id),),
                    ).fetchone()
                except sqlite3.OperationalError:
                    row = None
                course_documents = list(row) if row else []
        return {
            "transcript": list(transcript) if transcript else [],
            "ppt": list(ppt) if ppt else [],
            "documents": list(documents) if documents else [],
            "course_documents": course_documents,
            "artifacts": [list(row) for row in artifacts],
        }

    def replace_search_documents(
        self,
        sub_id: str,
        documents: list[dict[str, Any]],
        *,
        indexed_version: str,
    ) -> None:
        sub_id = str(sub_id or "").strip()
        if not sub_id:
            raise ValueError("sub_id is required")
        now = time.time()
        normalized = [
            (
                str(item["doc_key"]),
                sub_id,
                str(item["source"]),
                str(item.get("source_ref") or ""),
                str(item.get("document_title") or ""),
                int(item["start_ms"]) if item.get("start_ms") is not None else None,
                str(item.get("display_text") or ""),
                str(item.get("search_text") or ""),
                str(item.get("source_version") or ""),
                now,
            )
            for item in documents
            if str(item.get("doc_key") or "") and str(item.get("search_text") or "")
        ]
        with self._connect() as db:
            self._delete_search_documents(db, sub_id)
            db.executemany(
                """INSERT INTO search_documents(
                       doc_key,sub_id,source,source_ref,document_title,start_ms,
                       display_text,search_text,source_version,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                normalized,
            )
            if self.search_fts_enabled:
                db.executemany(
                    "INSERT INTO search_fts(doc_key,search_text) VALUES(?,?)",
                    [(row[0], row[7]) for row in normalized],
                )
            db.execute(
                "UPDATE search_catalog SET indexed_version=?,updated_at=? WHERE sub_id=?",
                (str(indexed_version), now, sub_id),
            )

    @staticmethod
    def _like_pattern(term: str) -> str:
        escaped = str(term).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    def search_documents(
        self,
        *,
        terms: list[str],
        normalized_query: str,
        course_ids: list[str],
        sources: list[str],
        limit: int,
        offset: int,
        sub_id: str = "",
    ) -> tuple[int, list[dict[str, Any]]]:
        """Search derived documents; callers validate public query limits."""

        usable_terms = [str(term).casefold() for term in terms if str(term)]
        if not usable_terms:
            return 0, []
        long_terms = [term for term in usable_terms if len(term) >= 3]
        short_terms = [term for term in usable_terms if len(term) < 3]
        use_fts = bool(self.search_fts_enabled and long_terms)
        from_sql = "search_documents d JOIN search_catalog c ON c.sub_id=d.sub_id"
        where: list[str] = []
        where_params: list[Any] = []
        if use_fts:
            from_sql += " JOIN search_fts ON search_fts.doc_key=d.doc_key"
            fts_query = " AND ".join(
                f'"{term.replace(chr(34), chr(34) * 2)}"' for term in long_terms
            )
            where.append("search_fts MATCH ?")
            where_params.append(fts_query)
            match_score = "bm25(search_fts)"
        else:
            short_terms = usable_terms
            match_score = "0.0"
        for term in short_terms:
            where.append("d.search_text LIKE ? ESCAPE '\\'")
            where_params.append(self._like_pattern(term))
        if course_ids:
            where.append(f"c.course_id IN ({','.join('?' for _ in course_ids)})")
            where_params.extend(str(value) for value in course_ids)
        if sources:
            where.append(f"d.source IN ({','.join('?' for _ in sources)})")
            where_params.extend(str(value) for value in sources)
        selected_sub = str(sub_id or "").strip()
        if selected_sub:
            where.append("d.sub_id=?")
            where_params.append(selected_sub)
        where_sql = " AND ".join(where) if where else "1=1"

        select_params: list[Any] = [normalized_query, normalized_query]
        select_sql = f"""
            SELECT d.*,c.course_id,c.course_title,c.lecture_title,c.teacher,
                   CASE
                     WHEN d.source='title' AND (
                       c.course_title=? COLLATE NOCASE OR c.lecture_title=? COLLATE NOCASE
                     ) THEN 0 ELSE 1
                   END AS exact_rank,
                   {match_score} AS match_score,
                   CASE d.source
                     WHEN 'title' THEN 0 WHEN 'chapter' THEN 1
                     WHEN 'transcript' THEN 2 WHEN 'ppt' THEN 3 ELSE 4
                   END AS source_rank
            FROM {from_sql}
            WHERE {where_sql}
        """
        with self._connect() as db:
            total_row = db.execute(
                f"SELECT COUNT(*) FROM {from_sql} WHERE {where_sql}",
                where_params,
            ).fetchone()
            rows = db.execute(
                select_sql
                + """ ORDER BY exact_rank,match_score,source_rank,
                             c.course_title,c.lecture_title,
                             COALESCE(d.start_ms,0),d.doc_key
                     LIMIT ? OFFSET ?""",
                [*select_params, *where_params, int(limit), int(offset)],
            ).fetchall()
        return int(total_row[0]) if total_row else 0, [dict(row) for row in rows]

    # --- course-data inventory aggregates (read-only, append-only) ----------

    @staticmethod
    def _merge_inventory_aggregate(
        existing: dict[str, Any], *, count: int, text_bytes: int, last_updated_at: float,
    ) -> None:
        existing["count"] = int(existing.get("count") or 0) + int(count or 0)
        existing["text_bytes"] = int(existing.get("text_bytes") or 0) + int(text_bytes or 0)
        existing["last_updated_at"] = max(
            float(existing.get("last_updated_at") or 0.0), float(last_updated_at or 0.0),
        )

    def course_data_category_aggregates(self) -> dict[str, Any]:
        """Frozen course-data inventory aggregation (read-only GROUP BY pass).

        One aggregate query per logical table; no content column is ever
        selected — only COUNT, SUM(LENGTH(...)) text-volume proxies, and MAX
        timestamps.  Tables owned by optional feature schemas may be absent;
        those categories are then omitted instead of fabricated.  Returns
        course-keyed aggregates, sub-keyed aggregates for the tables without a
        course column (transcript, ppt), global aggregates for the tables
        without a course/sub column (review), and the document_id resolution
        map used to attribute the ``documents/<id>/`` file namespace.
        """
        course_sub: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        sub_keyed: dict[str, dict[str, dict[str, Any]]] = {}
        global_aggregates: dict[str, dict[str, Any]] = {}
        merge = self._merge_inventory_aggregate

        def query(db: sqlite3.Connection, sql: str) -> list:
            try:
                return db.execute(sql).fetchall()
            except sqlite3.OperationalError:
                return []

        def absorb_course(row: tuple, category: str, count: int, text_bytes: int, last: float) -> None:
            bucket = course_sub.setdefault((str(row[0]), str(row[1])), {})
            aggregate = bucket.setdefault(category, {"count": 0, "text_bytes": 0, "last_updated_at": 0.0})
            merge(aggregate, count=count, text_bytes=text_bytes, last_updated_at=last)

        def absorb_sub(sub_id: str, category: str, count: int, text_bytes: int, last: float) -> None:
            bucket = sub_keyed.setdefault(str(sub_id), {})
            aggregate = bucket.setdefault(category, {"count": 0, "text_bytes": 0, "last_updated_at": 0.0})
            merge(aggregate, count=count, text_bytes=text_bytes, last_updated_at=last)

        with self._connect() as db:
            for row in query(
                db,
                """SELECT course_id,sub_id,COUNT(*),MAX(updated_at)
                   FROM watch_progress GROUP BY course_id,sub_id""",
            ):
                absorb_course(row, "progress", row[2], 0, row[3])
            for row in query(
                db,
                """SELECT sub_id,COUNT(*),COALESCE(SUM(LENGTH(text)),0)
                   FROM transcript_segments GROUP BY sub_id""",
            ):
                absorb_sub(row[0], "transcript", row[1], row[2], 0.0)
            for row in query(
                db,
                """SELECT sub_id,MAX(updated_at) FROM transcript_sources GROUP BY sub_id""",
            ):
                absorb_sub(row[0], "transcript", 0, 0, row[1])
            for row in query(
                db,
                """SELECT sub_id,COUNT(*),COALESCE(SUM(LENGTH(COALESCE(text,''))),0),MAX(ocr_at)
                   FROM ppt_pages GROUP BY sub_id""",
            ):
                absorb_sub(row[0], "ppt", row[1], row[2], row[3])
            for row in query(
                db,
                """SELECT course_id,sub_id,COUNT(*),
                          COALESCE(SUM(LENGTH(COALESCE(content_markdown,''))
                                       +LENGTH(COALESCE(content_json,''))),0),
                          MAX(updated_at)
                   FROM ai_artifacts GROUP BY course_id,sub_id""",
            ):
                absorb_course(row, "artifacts", row[2], row[3], row[4])
            for row in query(
                db,
                """SELECT d.course_id,d.sub_id,COUNT(DISTINCT d.document_id),
                          COALESCE(SUM(LENGTH(COALESCE(p.text,''))),0),MAX(d.updated_at)
                   FROM learning_documents d
                   LEFT JOIN learning_document_pages p ON p.document_id=d.document_id
                   GROUP BY d.course_id,d.sub_id""",
            ):
                absorb_course(row, "documents", row[2], row[3], row[4])
            for row in query(
                db,
                """SELECT course_id,sub_id,COUNT(*),MAX(created_at)
                   FROM content_references GROUP BY course_id,sub_id""",
            ):
                absorb_course(row, "references", row[2], 0, row[3])
            for row in query(
                db,
                """SELECT course_id,sub_id,COUNT(*),MAX(created_at)
                   FROM smart_timeline_segments GROUP BY course_id,sub_id""",
            ):
                absorb_course(row, "timeline", row[2], 0, row[3])
            for row in query(
                db,
                """SELECT course_id,sub_id,COUNT(*),MAX(updated_at)
                   FROM document_alignments GROUP BY course_id,sub_id""",
            ):
                absorb_course(row, "timeline", row[2], 0, row[3])
            for row in query(
                db,
                """SELECT c.course_id,d.sub_id,COUNT(*),
                          COALESCE(SUM(LENGTH(d.search_text)+LENGTH(d.display_text)),0),
                          MAX(d.updated_at)
                   FROM search_documents d
                   JOIN search_catalog c ON c.sub_id=d.sub_id
                   GROUP BY c.course_id,d.sub_id""",
            ):
                absorb_course(row, "search", row[2], row[3], row[4])
            for row in query(
                db,
                """SELECT course_id,sub_id,COUNT(*),MAX(updated_at)
                   FROM bookmarks GROUP BY course_id,sub_id""",
            ):
                absorb_course(row, "bookmarks", row[2], 0, row[3])
            for row in query(
                db,
                """SELECT i.course_id,i.sub_id,COUNT(DISTINCT i.quiz_id),
                          COALESCE(SUM(LENGTH(i.question)+LENGTH(i.explanation)),0)
                            + COALESCE((SELECT SUM(LENGTH(a.answer)) FROM quiz_attempts a
                                        WHERE a.quiz_id IN (SELECT i2.quiz_id FROM quiz_items i2
                                                            WHERE i2.course_id=i.course_id
                                                              AND i2.sub_id=i.sub_id)),0),
                          MAX(i.created_at),
                          COALESCE((SELECT MAX(a.created_at) FROM quiz_attempts a
                                    WHERE a.quiz_id IN (SELECT i2.quiz_id FROM quiz_items i2
                                                        WHERE i2.course_id=i.course_id
                                                          AND i2.sub_id=i.sub_id)),0)
                   FROM quiz_items i
                   GROUP BY i.course_id,i.sub_id""",
            ):
                # COUNTS-VERIFY-1 F1：题干/解析按 items 恰计一次（不再被
                # LEFT JOIN 联表行数=作答数重复放大），作答内容经子查询逐条
                # 计入、范围仍限本组既有 items 的 quiz（孤儿 attempts 不计，
                # 与旧语义一致）。
                absorb_course(
                    row, "quizzes", row[2], row[3],
                    max(float(row[4] or 0.0), float(row[5] or 0.0)),
                )
            for row in query(
                db,
                "SELECT COUNT(*),MAX(updated_at) FROM review_plans",
            ):
                aggregate = global_aggregates.setdefault(
                    "review", {"count": 0, "text_bytes": 0, "last_updated_at": 0.0},
                )
                merge(aggregate, count=row[0], text_bytes=0, last_updated_at=row[1])
            document_ids = {
                str(row[0]): (str(row[1]), str(row[2]))
                for row in query(
                    db,
                    "SELECT document_id,course_id,sub_id FROM learning_documents",
                )
            }
        return {
            "course_sub": course_sub,
            "sub": sub_keyed,
            "global": global_aggregates,
            "document_ids": document_ids,
        }

    def not_understood_bookmark_counts(self) -> dict[str, int]:
        """Per-course count of「没听懂」hotspot bookmarks (read-only GROUP BY).

        POLISH-1 F8b（化身走查 FULL-CLIENT-INSPECT F8b）：数据页课程明细的
        热点核对面。「没听懂」标记落在既有 bookmarks 底座（player-core
        POST /api/v3/bookmarks，note 固定为「没听懂」字面量），与普通书签
        同表但语义独立；书签删除为硬删，计数随删自然回落。可选 schema 缺表
        时诚实返回空表，绝不虚构。"""
        try:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT course_id, COUNT(*) FROM bookmarks WHERE note = ? GROUP BY course_id",
                    (NOT_UNDERSTOOD_NOTE,),
                ).fetchall()
        except sqlite3.OperationalError:
            return {}
        return {str(row[0]): int(row[1] or 0) for row in rows if str(row[0])}

    def close(self) -> None:
        """Compatibility hook; operations do not retain open connections."""


__all__ = ["LearningStore"]
