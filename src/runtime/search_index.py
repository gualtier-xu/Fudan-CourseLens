"""Incremental local search index built from the catalog and learning sources."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any, Callable

from src.runtime.document_alignment import document_search_pages
from src.runtime.exam_paper_split import search_exam_questions
from src.runtime.subtitle_reader import is_evidence_id


SEARCH_SOURCES = ("title", "transcript", "ppt", "document", "summary", "chapter", "exam_question")


def normalize_search_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.casefold().split())


def plain_markdown(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"```(?:\w+)?", " ", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[([^]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_~>|]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _version(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SearchDocument:
    doc_key: str
    source: str
    source_ref: str
    document_title: str
    start_ms: int | None
    display_text: str
    search_text: str
    source_version: str

    def public(self) -> dict[str, Any]:
        return asdict(self)


class LearningSearchIndex:
    """Own background indexing state; source data remains in existing stores."""

    def __init__(
        self,
        store,
        catalog_snapshot: Callable[[], dict[str, Any]],
        subtitle_sync: Callable[[str], dict[str, Any]],
        resource_coordinator=None,
    ):
        self.store = store
        self.catalog_snapshot = catalog_snapshot
        self.subtitle_sync = subtitle_sync
        self.resources = resource_coordinator
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending_all = False
        self._pending_sub_ids: set[str] = set()
        self._force = False
        self._state: dict[str, Any] = {
            "state": "idle",
            "processed": 0,
            "total": 0,
            "error_count": 0,
            "updated_at": 0.0,
            "fts_enabled": bool(store.search_fts_enabled),
        }

    def start(self) -> None:
        self.request_refresh(force=self.store.search_rebuild_required())

    def request_refresh(
        self,
        sub_ids: list[str] | set[str] | tuple[str, ...] | None = None,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            if sub_ids is None:
                self._pending_all = True
                self._pending_sub_ids.clear()
            elif not self._pending_all:
                self._pending_sub_ids.update(str(value) for value in sub_ids if str(value))
            self._force = self._force or bool(force)
            if self._state.get("state") != "indexing":
                self._state.update({
                    "state": "indexing",
                    "processed": 0,
                    "updated_at": time.time(),
                })
            if not self._thread or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run,
                    name="learning-search-index",
                    daemon=True,
                )
                self._thread.start()
        self._wake.set()
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def close(self, timeout: float = 2.0) -> bool:
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
        return not bool(thread and thread.is_alive())

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                return
            with self._lock:
                full = self._pending_all
                requested = set(self._pending_sub_ids)
                force = self._force
                self._pending_all = False
                self._pending_sub_ids.clear()
                self._force = False
            if not full and not requested:
                continue
            try:
                self._refresh(None if full else requested, force=force)
            except Exception as exc:
                with self._lock:
                    self._state.update({
                        "state": "error",
                        "updated_at": time.time(),
                        "last_error": type(exc).__name__,
                    })

    @staticmethod
    def _catalog_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        courses = snapshot.get("courses") or {}
        rows: list[dict[str, Any]] = []
        for sub_id, lecture in (snapshot.get("lectures") or {}).items():
            course_id = str(lecture.get("course_id") or "")
            course = courses.get(course_id) or {}
            labels = {
                "course_id": course_id,
                "course_title": str(course.get("title") or course_id),
                "teacher": str(course.get("teacher") or ""),
                "lecture_title": str(lecture.get("sub_title") or sub_id),
            }
            rows.append({"sub_id": str(sub_id), **labels, "catalog_version": _version(labels)})
        return rows

    def _refresh(self, requested: set[str] | None, *, force: bool) -> None:
        snapshot = self.catalog_snapshot()
        if snapshot.get("authorization_state") != "ready":
            with self._lock:
                self._state.update({
                    "state": "action_required",
                    "processed": 0,
                    "total": 0,
                    "error_count": 0,
                    "updated_at": time.time(),
                })
            return
        catalog = self._catalog_rows(snapshot)
        self.store.sync_search_catalog(catalog, prune=requested is None)
        catalog_by_sub = {row["sub_id"]: row for row in catalog}
        sub_ids = sorted(catalog_by_sub if requested is None else requested & set(catalog_by_sub))
        with self._lock:
            self._state.update({
                "state": "indexing",
                "processed": 0,
                "total": len(sub_ids),
                "error_count": 0,
                "updated_at": time.time(),
            })
            self._state.pop("last_error", None)
        errors = 0
        completed = 0
        for sub_id in sub_ids:
            if self._stop.is_set():
                break
            try:
                if self.resources is None:
                    self.subtitle_sync(sub_id)
                else:
                    with self.resources.claim("search", cancel_event=self._stop):
                        self.subtitle_sync(sub_id)
            except Exception:
                errors += 1
            try:
                catalog_row = catalog_by_sub[sub_id]
                signature = {
                    "catalog": catalog_row["catalog_version"],
                    "sources": self.store.search_source_signature(
                        sub_id, course_id=str(catalog_row.get("course_id") or "")
                    ),
                }
                indexed_version = _version(signature)
                if force or self.store.search_indexed_version(sub_id) != indexed_version:
                    if self.resources is None:
                        documents = self._documents_for(sub_id, catalog_row, indexed_version)
                        self.store.replace_search_documents(
                            sub_id,
                            [document.public() for document in documents],
                            indexed_version=indexed_version,
                        )
                    else:
                        with self.resources.claim("search", cancel_event=self._stop):
                            documents = self._documents_for(sub_id, catalog_row, indexed_version)
                            self.store.replace_search_documents(
                                sub_id,
                                [document.public() for document in documents],
                                indexed_version=indexed_version,
                            )
            except Exception:
                errors += 1
            completed += 1
            with self._lock:
                self._state.update({
                    "processed": completed,
                    "error_count": errors,
                    "updated_at": time.time(),
                })
        if requested is None and completed == len(sub_ids) and not self._stop.is_set():
            self.store.mark_search_rebuild_complete()
        with self._lock:
            self._state.update({
                "state": "ready" if not self._stop.is_set() else "idle",
                "processed": completed,
                "error_count": errors,
                "updated_at": time.time(),
            })

    def _documents_for(
        self,
        sub_id: str,
        catalog: dict[str, Any],
        indexed_version: str,
    ) -> list[SearchDocument]:
        course_title = str(catalog.get("course_title") or "")
        lecture_title = str(catalog.get("lecture_title") or sub_id)
        teacher = str(catalog.get("teacher") or "")
        documents: list[SearchDocument] = []

        def add(
            source: str,
            source_ref: object,
            title: str,
            text: object,
            *,
            start_ms: int | None = None,
            source_version: str = indexed_version,
        ) -> None:
            display = plain_markdown(text) if source == "summary" else " ".join(str(text or "").split())
            search_text = normalize_search_text(f"{title} {display}")
            if not display or not search_text:
                return
            ref = str(source_ref)
            documents.append(SearchDocument(
                doc_key=f"{source}:{sub_id}:{ref}",
                source=source,
                source_ref=ref,
                document_title=str(title),
                start_ms=start_ms,
                display_text=display,
                search_text=search_text,
                source_version=str(source_version),
            ))

        add(
            "title",
            "catalog",
            lecture_title,
            " ".join(value for value in (course_title, teacher, lecture_title) if value),
            source_version=str(catalog.get("catalog_version") or indexed_version),
        )
        for segment in self.store.get_transcript_segments(sub_id):
            add(
                "transcript",
                # 稳定证据身份优先作为 source_ref；旧索引/旧行回落到段序号
                segment.get("evidence_id") or segment.get("index"),
                "同步字幕",
                segment.get("text"),
                start_ms=int(segment.get("start_ms") or 0),
                source_version=_version(segment),
            )
        for page in self.store.get_done_ppt_pages(sub_id):
            page_num = int(page.get("page_num") or 0)
            event_id = page.get("event_id")
            add(
                "ppt",
                # The slide event identity is the stable PPT evidence
                # reference; legacy rows without metadata keep the page
                # number so old indexes stay addressable.
                event_id if is_evidence_id(event_id) else page_num,
                f"PPT 第 {page_num} 页",
                page.get("text"),
                start_ms=max(0, int(page.get("created_sec") or 0) * 1000),
                source_version=_version(page),
            )
        for page in document_search_pages(
            self.store.path, sub_id, course_id=str(catalog.get("course_id") or "")
        ):
            page_num = int(page.get("page_num") or 0)
            course_scope = str(page.get("scope") or "lecture") == "course"
            add(
                "document",
                # 课程级资料挂在本课的每一讲下：doc_key 仍含本讲 sub_id，
                # 同一份教材在不同讲次各自可检索，互不覆盖。
                f"{page.get('document_id')}:{page_num}",
                (
                    f"{page.get('title') or '课程资料'} · 第 {page_num} 页"
                    if course_scope
                    else f"{page.get('title') or '本地讲义'} · 第 {page_num} 页"
                ),
                page.get("text"),
                start_ms=int(page["start_ms"]) if page.get("start_ms") is not None else None,
                source_version=_version({
                    "text_hash": page.get("text_hash"),
                    "alignment_updated_at": page.get("updated_at"),
                    "alignment_status": page.get("status"),
                    "scope": page.get("scope"),
                }),
            )
        for question in search_exam_questions(self.store.path, sub_id):
            add(
                "exam_question",
                str(question["question_id"]),
                f"第 {question['question_no']} 题 · {question.get('document_title') or '真题'}",
                question.get("anchor_text"),
                source_version=str(question.get("content_hash") or indexed_version),
            )
        timestamp_summary = self.store.find_ai_artifact(sub_id, "timestamp_summary")
        summary = (
            timestamp_summary
            if timestamp_summary and timestamp_summary.get("status") == "ready"
            else self.store.find_ai_artifact(sub_id, "lecture_summary")
        )
        if summary and summary.get("status") == "ready":
            add(
                "summary",
                summary.get("artifact_id") or "latest",
                "AI 课程笔记",
                (
                    " ".join([
                        str((summary.get("content") or {}).get("overview") or ""),
                        *[str(item) for item in (summary.get("content") or {}).get("key_takeaways") or []],
                    ])
                    if summary.get("kind") == "timestamp_summary"
                    else summary.get("content_markdown") or (summary.get("content") or {}).get("markdown") or ""
                ),
                source_version=str(summary.get("input_hash") or summary.get("artifact_id") or indexed_version),
            )
        chapters = (
            timestamp_summary
            if timestamp_summary and timestamp_summary.get("status") == "ready"
            else self.store.find_ai_artifact(sub_id, "lecture_chapters")
        )
        if chapters and chapters.get("status") == "ready":
            for index, chapter in enumerate((chapters.get("content") or {}).get("chapters") or []):
                if not isinstance(chapter, dict):
                    continue
                text = " ".join(
                    value for value in (
                        str(chapter.get("title") or ""),
                        str(chapter.get("summary") or ""),
                        " ".join(str(value) for value in (chapter.get("keywords") or [])),
                    ) if value
                )
                add(
                    "chapter",
                    index + 1,
                    str(chapter.get("title") or "AI 章节"),
                    text,
                    start_ms=max(0, int(
                        chapter.get("start_ms")
                        if chapter.get("start_ms") is not None
                        else float(chapter.get("start_seconds") or 0) * 1000
                    )),
                    source_version=str(chapters.get("input_hash") or chapters.get("artifact_id") or indexed_version),
                )
        return documents

    @staticmethod
    def _snippet(text: str, terms: list[str], radius: int = 72) -> str:
        value = " ".join(str(text or "").split())
        folded = normalize_search_text(value)
        indexes = [folded.find(term) for term in terms if folded.find(term) >= 0]
        start = max(0, (min(indexes) if indexes else 0) - radius)
        end = min(len(value), start + radius * 2 + 36)
        return ("…" if start else "") + value[start:end] + ("…" if end < len(value) else "")

    def search(
        self,
        query: object,
        *,
        course_ids: list[str] | None = None,
        sources: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        sub_id: str = "",
    ) -> dict[str, Any]:
        normalized = normalize_search_text(query)
        if len(normalized) < 2:
            raise ValueError("q must contain at least 2 characters")
        if len(normalized) > 100:
            raise ValueError("q must not exceed 100 characters")
        terms = normalized.split()
        if len(terms) > 8:
            raise ValueError("q must not contain more than 8 keywords")
        selected_sources = list(dict.fromkeys(str(value).strip() for value in (sources or []) if str(value).strip()))
        invalid = [value for value in selected_sources if value not in SEARCH_SOURCES]
        if invalid:
            raise ValueError("invalid search source")
        selected_courses = list(dict.fromkeys(str(value).strip() for value in (course_ids or []) if str(value).strip()))
        selected_sub = str(sub_id or "").strip()
        page_limit = max(1, min(50, int(limit)))
        page_offset = max(0, int(offset))
        total, rows = self.store.search_documents(
            terms=terms,
            normalized_query=normalized,
            course_ids=selected_courses,
            sources=selected_sources,
            limit=page_limit,
            offset=page_offset,
            sub_id=selected_sub,
        )
        results: list[dict[str, Any]] = []
        # 课程级资料挂在本课每一讲下索引，跨讲检索会给同一页返回多份命中；
        # 这里按 (source, source_ref) 折叠成一条，并把其余讲次列在
        # also_in_lectures 里——去噪不等于藏信息。
        collapsed: dict[tuple[str, str], dict[str, Any]] = {}
        duplicates = 0
        for row in rows:
            source = str(row.get("source") or "")
            source_ref = str(row.get("source_ref") or "")
            if source == "document" and source_ref:
                existing = collapsed.get((source, source_ref))
                if existing is not None:
                    duplicates += 1
                    other = str(row.get("sub_id") or "")
                    if other and other != existing.get("sub_id"):
                        existing.setdefault("also_in_lectures", []).append(other)
                    continue
            start_ms = row.get("start_ms")
            view = "summary" if source == "summary" else "lecture" if source == "title" else "player"
            snippet = self._snippet(str(row.get("display_text") or ""), terms)
            result = {
                "result_id": str(row.get("doc_key") or ""),
                "source": source,
                "evidence_id": source_ref if is_evidence_id(source_ref) else "",
                "course_id": str(row.get("course_id") or ""),
                "course_title": str(row.get("course_title") or ""),
                "sub_id": str(row.get("sub_id") or ""),
                "lecture_title": str(row.get("lecture_title") or ""),
                "document_title": str(row.get("document_title") or ""),
                "snippet": snippet,
                "source_hash": hashlib.sha256(snippet.encode("utf-8")).hexdigest(),
                "target": {
                    "view": view,
                    "start_seconds": round(int(start_ms) / 1000.0, 3) if start_ms is not None else None,
                },
            }
            if source == "document" and source_ref:
                collapsed[(source, source_ref)] = result
            results.append(result)
        return {
            "query": str(query or "").strip(),
            "filters": {"course_ids": selected_courses, "sources": selected_sources},
            "total": max(0, int(total) - duplicates),
            "duplicates_collapsed": duplicates,
            "limit": page_limit,
            "offset": page_offset,
            "results": results,
            "index": self.status(),
        }


__all__ = [
    "LearningSearchIndex",
    "SEARCH_SOURCES",
    "SearchDocument",
    "normalize_search_text",
    "plain_markdown",
]
