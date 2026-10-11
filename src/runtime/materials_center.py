"""Unified materials file center (资料板块统一文件中心, DATA-DELETE-REPAIR-1).

Lives in the runtime layer so the HTTP adapter can compose it from raw stores
without touching the application facade (architecture boundary:
``test_http_adapter_depends_only_on_domain_services``).  Hosts the two shared
course-data primitives the deletion engine also uses — the pair universe
(catalog ∪ learning ∪ tasks) and the bounded artifact rmtree — so the
inventory, the batch actions, and this file center resolve the same keys from
one source.

The study page documents tab aggregates three entry kinds — imported
documents, generated courseware PDFs, and exported AI-summary markdown —
with per-item export (stream the existing file, never a second copy) and
delete.  Courseware and summaries stay registry-free: the list is a bounded
scan of their closed namespaces, and entries are addressed by the on-disk
key so items remain manageable even after their lecture left the catalog
(学期滚动).  Summary deletion removes ONLY the exported markdown; the
learning_store records that drive the smart timeline and search stay put.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from src.runtime.course_data_inventory import (
    _COURSEWARE_KEY_RE,
    courseware_lecture_key,
)
from src.runtime.courseware_pdf import MANIFEST_SCHEMA
from src.runtime.document_alignment import document_storage_path, list_documents

MATERIALS_SUMMARY_KEY_RE = re.compile(r"^sum-[0-9a-f]{16}$")
MATERIALS_SCAN_LIMIT = 4096


class MaterialsActionError(Exception):
    """Closed-set rejection for a materials file-center action."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def _course_data_known_pairs(catalog_repository, learning_store, task_store) -> set[tuple[str, str]]:
    """Course/lecture pairs resolvable by the inventory (catalog ∪ learning ∪ tasks)."""
    known = {
        (str(lecture.get("course_id") or ""), str(sub_id))
        for sub_id, lecture in catalog_repository.lecture_course_pairs().items()
        if str(lecture.get("course_id") or "")
    }
    known.update(
        (str(course_id), str(sub_id))
        for course_id, sub_id in learning_store.course_data_category_aggregates()["course_sub"]
    )
    known.update(task_store.course_task_aggregates())
    return known


def _course_data_rmtree_owned(namespace_root: Path, name: str) -> tuple[bool, int]:
    """Bounded delete of one artifact directory; refuses anything outside the root."""
    try:
        candidate = (namespace_root / name).resolve()
        candidate.relative_to(namespace_root.resolve())
    except (ValueError, OSError):
        return False, 0
    if candidate.is_symlink() or not candidate.is_dir():
        return False, 0
    total = 0
    for child in candidate.rglob("*"):
        try:
            if child.is_file():
                total += int(child.stat().st_size)
        except OSError:
            continue
    shutil.rmtree(candidate, ignore_errors=True)
    return True, total


def summary_export_key(course_id: str, sub_id: str) -> str:
    """Opaque on-disk key for one lecture's exported summary markdown."""
    digest = hashlib.sha256(
        f"summary-md.v1\0{course_id}\0{sub_id}".encode("utf-8")
    ).hexdigest()
    return f"sum-{digest[:16]}"


def _materials_safe_stem(*parts: str, fallback: str) -> str:
    """Sanitized lecture-readable filename stem (same charset rules as the
    courseware download boundary)."""
    cleaned = []
    for part in parts:
        value = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", str(part or ""))
        value = re.sub(r"\s+", " ", value).strip().strip(". ")[:40].strip()
        if value:
            cleaned.append(value)
    name = "-".join(cleaned)
    return name or fallback


def courseware_pdf_download_name(lecture: dict) -> str:
    """Module-level download filename for one lecture's courseware PDF."""
    name = _materials_safe_stem(
        str((lecture or {}).get("date") or ""), str((lecture or {}).get("sub_title") or ""),
        fallback="",
    )
    return f"{name}.pdf" if name else "courseware.pdf"


def _materials_known_maps(catalog_repository, learning_store, task_store):
    """Forward maps over the same pair universe the inventory uses."""
    known = _course_data_known_pairs(catalog_repository, learning_store, task_store)
    by_key = {}
    for course_id, sub_id in known:
        by_key[courseware_lecture_key(course_id, sub_id)] = (course_id, sub_id)
    by_summary_key = {}
    for course_id, sub_id in known:
        by_summary_key[summary_export_key(course_id, sub_id)] = (course_id, sub_id)
    try:
        lectures = catalog_repository.lecture_course_pairs()
    except (OSError, ValueError):
        lectures = {}
    return known, by_key, by_summary_key, lectures


def write_summary_export(
    output_dir,
    course_id: str,
    sub_id: str,
    *,
    markdown: str,
    chapters: list[dict],
    lecture: dict | None = None,
) -> dict:
    """Persist one lecture's AI summary as readable markdown under
    ``<output_dir>/summaries/<key>/``; one fixed filename per lecture,
    overwritten on every completion (幂等, never accumulates versions)."""
    lecture = dict(lecture or {})
    key = summary_export_key(str(course_id), str(sub_id))
    summary_root = (Path(output_dir) / "summaries").resolve()
    summary_dir = summary_root / key
    summary_dir.relative_to(summary_root)
    summary_dir.mkdir(parents=True, exist_ok=True)
    stem = _materials_safe_stem(
        str(lecture.get("date") or ""), str(lecture.get("sub_title") or ""),
        fallback="",
    )
    stem = f"总结-{stem}" if stem else f"总结-{sub_id}"
    target = summary_dir / f"{stem}.md"
    lines = [f"# {str(lecture.get('sub_title') or '本讲总结').strip() or '本讲总结'}", ""]
    overview = str(markdown or "").strip()
    if overview:
        lines.extend([overview, ""])
    normalized = []
    for index, item in enumerate(chapters or []):
        title = str((item or {}).get("title") or "").strip()
        text = str((item or {}).get("summary") or "").strip()
        start_ms = int((item or {}).get("start_ms") or 0)
        if not title and not text:
            continue
        normalized.append((start_ms, title or f"章节 {index + 1}", text))
    if normalized:
        lines.extend(["## 章节速览", ""])
        for start_ms, title, text in normalized:
            stamp = f"{start_ms // 60000:02d}:{start_ms // 1000 % 60:02d}"
            lines.append(f"- [{stamp}] {title}" + (f"：{text}" if text else ""))
        lines.append("")
    payload = "\n".join(lines).encode("utf-8")
    temporary = summary_dir / f".{target.name}.tmp"
    try:
        temporary.write_bytes(payload)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    # 恰一件纪律：讲次改名后旧文件名不残留（目录内只保留刚写入的一份）。
    for stale in summary_dir.glob("*.md"):
        if stale != target:
            stale.unlink(missing_ok=True)
    return {"key": key, "name": target.name, "path": str(target), "size": len(payload)}


def materials_center_scan(
    *,
    data_root,
    catalog_repository,
    learning_store,
    task_store,
    course_id: str = "",
) -> dict:
    """Aggregate the three file-center entry kinds (read-only, bounded stat).

    ``course_id`` filters to one course; entries whose lecture pair is unknown
    (rolled-over catalog, orphaned namespace directories) carry empty
    course/sub ids so the frontend groups them under an explicit
    「未识别讲次」 group instead of leaving untouchable dark corners.
    """
    data_root = Path(data_root)
    wanted_course = str(course_id or "").strip()
    known, courseware_map, summary_map, lectures = _materials_known_maps(
        catalog_repository, learning_store, task_store,
    )

    def lecture_meta(pair: tuple[str, str] | None) -> dict:
        course_of, sub_of = (pair or ("", ""))
        row = {}
        if sub_of:
            row = dict(lectures.get(sub_of) or {})
        return {
            "course_id": str(course_of or row.get("course_id") or ""),
            "sub_id": str(sub_of or ""),
            "lecture_title": str(row.get("sub_title") or ""),
            "lecture_date": str(row.get("date") or ""),
        }

    entries: list[dict] = []
    truncated = False

    documents = list_documents(learning_store.path, course_id=wanted_course)
    for document in documents:
        document_id = str(document.get("document_id") or "")
        if not document_id:
            continue
        course_of = str(document.get("course_id") or "")
        sub_of = str(document.get("sub_id") or "")
        meta = lecture_meta((course_of, sub_of) if sub_of else None)
        original_name = str(document.get("original_name") or "")
        entries.append({
            "kind": "document",
            "id": document_id,
            **meta,
            "name": str(document.get("title") or "") or original_name or "课程资料",
            "download_name": original_name or f"{document_id}.bin",
            "size": int(document.get("size_bytes") or 0),
            "updated_at": float(document.get("updated_at") or 0.0),
            "doc_type": str(document.get("doc_type") or "other"),
            "scope": str(document.get("scope") or "lecture"),
        })

    def scan_namespace(root: Path, pattern, mapping, kind: str) -> None:
        nonlocal truncated
        try:
            scanned_entries = list(os.scandir(root))
        except OSError:
            return
        scanned = 0
        for entry in scanned_entries:
            scanned += 1
            if scanned > MATERIALS_SCAN_LIMIT:
                truncated = True
                break
            if not pattern.match(entry.name) or not entry.is_dir(follow_symlinks=False):
                continue
            pair = mapping.get(entry.name)
            if wanted_course and (pair is None or str(pair[0]) != wanted_course):
                continue
            # 未识别讲次的目录（学期滚动/孤儿）按空 meta 透传：全量视图在
            # 「未识别讲次」组中给出导出/删除出口，不留无法触碰的暗角。
            meta = lecture_meta(pair)
            entry_path = Path(entry.path)
            if kind == "courseware":
                pdf_path = entry_path / "slides.pdf"
                manifest_path = entry_path / "manifest.json"
                try:
                    if not pdf_path.is_file() or pdf_path.stat().st_size <= 0:
                        continue
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if str(manifest.get("schema") or "") != MANIFEST_SCHEMA:
                    continue
                generated_at = int(manifest.get("generated_at") or 0)
                if not generated_at:
                    try:
                        generated_at = int(pdf_path.stat().st_mtime)
                    except OSError:
                        generated_at = 0
                lecture_row = {}
                if meta["sub_id"]:
                    lecture_row = dict(lectures.get(meta["sub_id"]) or {})
                entries.append({
                    "kind": "courseware",
                    "id": entry.name,
                    **meta,
                    "name": courseware_pdf_download_name(lecture_row),
                    "download_name": courseware_pdf_download_name(lecture_row),
                    "size": int(pdf_path.stat().st_size),
                    "updated_at": float(generated_at),
                })
            else:  # summary
                try:
                    markdowns = sorted(
                        (child for child in entry_path.glob("*.md") if child.is_file()),
                        key=lambda child: child.stat().st_mtime,
                    )
                except OSError:
                    continue
                if not markdowns:
                    continue
                latest = markdowns[-1]
                try:
                    stat = latest.stat()
                except OSError:
                    continue
                entries.append({
                    "kind": "summary",
                    "id": entry.name,
                    **meta,
                    "name": latest.stem,
                    "download_name": latest.name,
                    "size": int(stat.st_size),
                    "updated_at": float(stat.st_mtime),
                })

    scan_namespace(
        data_root / "courseware", _COURSEWARE_KEY_RE, courseware_map, "courseware",
    )
    scan_namespace(
        data_root / "summaries", MATERIALS_SUMMARY_KEY_RE, summary_map, "summary",
    )
    entries.sort(key=lambda item: (-float(item.get("updated_at") or 0.0), str(item.get("name") or "")))
    return {"course_id": wanted_course, "entries": entries, "scan_truncated": truncated}


def materials_center_delete(
    *,
    data_root,
    catalog_repository,
    learning_store,
    task_store,
    kind: str,
    entry_id: str,
) -> dict:
    """Delete one file-center entry by kind + on-disk key.

    Documents are handled by the bound ``delete_learning_document`` operation
    (rows + directory + search refresh); this helper owns the two
    registry-free kinds.  Summary deletion never touches learning_store rows.
    """
    if str(kind) not in ("courseware", "summary"):
        raise MaterialsActionError("materials_kind_invalid")
    entry_id = str(entry_id or "").strip()
    data_root = Path(data_root)
    _, courseware_map, summary_map, _ = _materials_known_maps(
        catalog_repository, learning_store, task_store,
    )
    if str(kind) == "courseware":
        if not _COURSEWARE_KEY_RE.match(entry_id):
            raise MaterialsActionError("materials_entry_invalid")
        pair = courseware_map.get(entry_id)
        if pair and task_store.find_active("courseware_pdf", str(pair[1])) is not None:
            raise MaterialsActionError("materials_courseware_busy")
        deleted, _size = _course_data_rmtree_owned(data_root / "courseware", entry_id)
    else:
        if not MATERIALS_SUMMARY_KEY_RE.match(entry_id):
            raise MaterialsActionError("materials_entry_invalid")
        deleted, _size = _course_data_rmtree_owned(data_root / "summaries", entry_id)
    if not deleted:
        raise FileNotFoundError("materials_entry_missing")
    return {"deleted": True, "kind": str(kind), "id": entry_id}


def materials_center_file_path(
    *, data_root, kind: str, entry_id: str,
) -> tuple[Path, str]:
    """Resolve one file-center entry to its on-disk file for export streaming.

    Returns (path, content_type); every resolution is path-checked to stay
    inside its closed namespace, and the existing file is streamed directly —
    the export boundary never creates a second copy in the app directory.
    """
    kind = str(kind or "").strip()
    entry_id = str(entry_id or "").strip()
    data_root = Path(data_root)
    if kind == "document":
        path, extension = document_storage_path(data_root / "learning.db", entry_id)
        root = (data_root / "documents").resolve()
        resolved = Path(str(path)).resolve()
        if root not in resolved.parents or not resolved.is_file():
            raise FileNotFoundError("materials_file_unavailable")
        content_type = {
            ".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown",
        }.get(str(extension or "").lower(), "application/octet-stream")
        return resolved, content_type
    if kind == "courseware":
        if not _COURSEWARE_KEY_RE.match(entry_id):
            raise FileNotFoundError("materials_file_unavailable")
        resolved = (data_root / "courseware" / entry_id / "slides.pdf").resolve()
        resolved.relative_to((data_root / "courseware").resolve())
        if not resolved.is_file() or resolved.stat().st_size <= 0:
            raise FileNotFoundError("materials_file_unavailable")
        return resolved, "application/pdf"
    if kind == "summary":
        if not MATERIALS_SUMMARY_KEY_RE.match(entry_id):
            raise FileNotFoundError("materials_file_unavailable")
        summary_dir = (data_root / "summaries" / entry_id).resolve()
        summary_dir.relative_to((data_root / "summaries").resolve())
        if not summary_dir.is_dir():
            raise FileNotFoundError("materials_file_unavailable")
        markdowns = sorted(
            (child for child in summary_dir.glob("*.md") if child.is_file()),
            key=lambda child: child.stat().st_mtime,
        )
        if not markdowns:
            raise FileNotFoundError("materials_file_unavailable")
        return markdowns[-1].resolve(), "text/markdown"
    raise FileNotFoundError("materials_file_unavailable")
