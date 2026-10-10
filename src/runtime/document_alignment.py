"""Local document registration and evidence-backed lecture alignment.

Original files stay below the local learning-data directory.  This module
extracts text from text-bearing documents without invoking local OCR.  Pages
without extractable text are explicitly marked as requiring OCR/review; they
must never be presented as successfully aligned.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import re
import sqlite3
import time
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .sqlite_utils import connect_learning_db


MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
MAX_DOCUMENT_PAGES = 2000
MAX_PAGE_TEXT_CHARS = 200_000
MAX_ZIP_EXPANDED_BYTES = 128 * 1024 * 1024
SUPPORTED_EXTENSIONS = {".pdf", ".pptx", ".docx", ".txt", ".md", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}
TEXT_EXTENSIONS = {".txt", ".md"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
ALIGNMENT_VERSION = "monotonic-text-v1"


class DocumentError(ValueError):
    """A closed-code document failure safe to expose through the local API."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = str(code)


def ensure_document_schema(path: str | Path) -> None:
    with closing(connect_learning_db(path)) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS learning_documents (
                document_id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL,
                sub_id TEXT NOT NULL,
                title TEXT NOT NULL,
                original_name TEXT NOT NULL,
                extension TEXT NOT NULL,
                media_type TEXT NOT NULL DEFAULT '',
                storage_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                page_count INTEGER NOT NULL DEFAULT 0,
                extraction_state TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                doc_type TEXT NOT NULL DEFAULT 'other',
                scope TEXT NOT NULL DEFAULT 'lecture',
                UNIQUE(course_id, sub_id, sha256)
            );
            CREATE INDEX IF NOT EXISTS idx_learning_documents_lecture
                ON learning_documents(course_id, sub_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS learning_document_pages (
                document_id TEXT NOT NULL,
                page_num INTEGER NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                text_hash TEXT NOT NULL DEFAULT '',
                visual_hash TEXT NOT NULL DEFAULT '',
                extraction_state TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(document_id, page_num),
                FOREIGN KEY(document_id) REFERENCES learning_documents(document_id)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_learning_document_pages_state
                ON learning_document_pages(document_id, extraction_state, page_num);
            CREATE TABLE IF NOT EXISTS document_alignments (
                alignment_id TEXT PRIMARY KEY, course_id TEXT NOT NULL, sub_id TEXT NOT NULL,
                document_hash TEXT NOT NULL, page_num INTEGER NOT NULL, start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL, confidence REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'suggested', evidence_json TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL
            );
            DELETE FROM document_alignments
             WHERE rowid NOT IN (
                 SELECT MAX(rowid) FROM document_alignments GROUP BY document_hash,page_num,sub_id
             );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_document_alignments_page
                ON document_alignments(document_hash, page_num, sub_id);
            """
        )
        # U8 加性迁移：既有库补 doc_type/scope 列（旧行默认 lecture+other）
        doc_columns = {
            str(row[1]) for row in db.execute("PRAGMA table_info(learning_documents)").fetchall()
        }
        if doc_columns and "doc_type" not in doc_columns:
            db.execute("ALTER TABLE learning_documents ADD COLUMN doc_type TEXT NOT NULL DEFAULT 'other'")
        if doc_columns and "scope" not in doc_columns:
            db.execute("ALTER TABLE learning_documents ADD COLUMN scope TEXT NOT NULL DEFAULT 'lecture'")
        db.commit()


def _safe_json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))


def _normal_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:MAX_PAGE_TEXT_CHARS]


def _validate_zip(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > 20_000 or sum(max(0, int(info.file_size)) for info in infos) > MAX_ZIP_EXPANDED_BYTES:
        raise DocumentError("document_archive_too_large")


def _tokens(value: str) -> set[str]:
    normalized = _normal_text(value).casefold()
    latin = set(re.findall(r"[a-z0-9_]{2,}", normalized))
    chinese = set(re.findall(r"[\u3400-\u9fff]", normalized))
    bigrams = {
        normalized[index:index + 2]
        for index in range(max(0, len(normalized) - 1))
        if re.fullmatch(r"[\u3400-\u9fff]{2}", normalized[index:index + 2])
    }
    return latin | chinese | bigrams


def _text_similarity(left: str, right: str) -> float:
    a = _tokens(left)
    b = _tokens(right)
    if not a or not b:
        return 0.0
    return 2.0 * len(a.intersection(b)) / (len(a) + len(b))


def _decode_upload(encoded: str) -> bytes:
    try:
        raw = base64.b64decode(str(encoded or ""), validate=True)
    except (ValueError, TypeError) as exc:
        raise DocumentError("document_payload_invalid") from exc
    if not raw:
        raise DocumentError("document_empty")
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise DocumentError("document_too_large")
    return raw


def _pptx_pages(raw: bytes) -> list[dict[str, Any]]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            _validate_zip(archive)
            names = [
                name for name in archive.namelist()
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
            ]
            names.sort(key=lambda name: int(re.search(r"(\d+)", Path(name).stem).group(1)))
            if len(names) > MAX_DOCUMENT_PAGES:
                raise DocumentError("document_page_limit_exceeded")
            pages = []
            for index, name in enumerate(names, start=1):
                xml = archive.read(name)
                root = ElementTree.fromstring(xml)
                text = _normal_text(" ".join(node.text or "" for node in root.iter() if node.tag.endswith("}t")))
                pages.append({
                    "page_num": index,
                    "text": text,
                    "visual_hash": hashlib.sha256(xml).hexdigest()[:32],
                    "extraction_state": "ready" if text else "ocr_required",
                    "metadata": {"source": "pptx_xml"},
                })
            if not pages:
                raise DocumentError("document_has_no_pages")
            return pages
    except DocumentError:
        raise
    except (KeyError, ElementTree.ParseError, zipfile.BadZipFile) as exc:
        raise DocumentError("document_format_invalid") from exc


def _docx_pages(raw: bytes) -> list[dict[str, Any]]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            _validate_zip(archive)
            xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(xml)
        paragraphs = []
        for paragraph in (node for node in root.iter() if node.tag.endswith("}p")):
            text = _normal_text("".join(child.text or "" for child in paragraph.iter() if child.tag.endswith("}t")))
            if text:
                paragraphs.append(text)
        text = "\n".join(paragraphs)
        return [{
            "page_num": 1,
            "text": text,
            "visual_hash": hashlib.sha256(xml).hexdigest()[:32],
            "extraction_state": "ready" if text else "ocr_required",
            "metadata": {"source": "docx_xml", "logical_page": True},
        }]
    except (KeyError, ElementTree.ParseError, zipfile.BadZipFile) as exc:
        raise DocumentError("document_format_invalid") from exc


def _pdf_pages(raw: bytes) -> list[dict[str, Any]]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - portable runtime includes pypdf
        raise DocumentError("pdf_reader_unavailable") from exc
    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):
                    raise DocumentError("document_password_required")
            except DocumentError:
                raise
            except Exception as exc:
                raise DocumentError("document_password_required") from exc
        pages = []
        if len(reader.pages) > MAX_DOCUMENT_PAGES:
            raise DocumentError("document_page_limit_exceeded")
        for index, page in enumerate(reader.pages, start=1):
            text = _normal_text(page.extract_text() or "")
            pages.append({
                "page_num": index,
                "text": text,
                "visual_hash": hashlib.sha256(f"{index}:{text}".encode("utf-8")).hexdigest()[:32] if text else "",
                "extraction_state": "ready" if text else "ocr_required",
                "metadata": {"source": "pdf_text" if text else "pdf_scan"},
            })
        if not pages:
            raise DocumentError("document_has_no_pages")
        return pages
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError("document_format_invalid") from exc


def _image_page(raw: bytes) -> list[dict[str, Any]]:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as image:
            if int(image.width) * int(image.height) > 100_000_000:
                raise DocumentError("document_image_too_large")
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            gray = image.convert("L").resize((9, 8))
            values = list(gray.getdata())
            bits = [values[row * 9 + column] > values[row * 9 + column + 1] for row in range(8) for column in range(8)]
            digest = f"{sum((1 << index) for index, bit in enumerate(bits) if bit):016x}"
            size = [int(image.width), int(image.height)]
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError("document_format_invalid") from exc
    return [{
        "page_num": 1,
        "text": "",
        "visual_hash": digest,
        "extraction_state": "ocr_required",
        "metadata": {"source": "image", "size": size},
    }]


def _text_pages(raw: bytes) -> list[dict[str, Any]]:
    text = ""
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        raise DocumentError("document_text_encoding_invalid")
    chunks = [chunk.strip() for chunk in text.split("\f") if chunk.strip()]
    if len(chunks) <= 1 and len(text) > 6000:
        chunks = [text[index:index + 6000].strip() for index in range(0, len(text), 6000)]
    if len(chunks) > MAX_DOCUMENT_PAGES:
        raise DocumentError("document_page_limit_exceeded")
    return [{
        "page_num": index,
        "text": _normal_text(chunk),
        "visual_hash": "",
        "extraction_state": "ready" if _normal_text(chunk) else "ocr_required",
        "metadata": {"source": "plain_text", "logical_page": True},
    } for index, chunk in enumerate(chunks or [text], start=1)]


def extract_pages(raw: bytes, extension: str) -> list[dict[str, Any]]:
    extension = str(extension).casefold()
    if extension == ".pptx":
        return _pptx_pages(raw)
    if extension == ".docx":
        return _docx_pages(raw)
    if extension == ".pdf":
        return _pdf_pages(raw)
    if extension in IMAGE_EXTENSIONS:
        return _image_page(raw)
    if extension in TEXT_EXTENSIONS:
        return _text_pages(raw)
    raise DocumentError("document_type_unsupported")


# U8 资料分类学闭集：类型六类；scope=lecture（挂讲次）或 course（课程级，
# 不参与时间轴对齐，只进搜索）。缺省等价旧行为，旧数据零迁移负担。
DOCUMENT_TYPES = ("courseware", "textbook", "notes", "exam_paper", "homework", "other")
DOCUMENT_SCOPES = ("lecture", "course")


def register_document(
    db_path: str | Path,
    storage_root: str | Path,
    *,
    course_id: str,
    sub_id: str,
    title: str,
    original_name: str,
    media_type: str,
    content_base64: str,
    doc_type: str = "other",
    scope: str = "lecture",
) -> dict[str, Any]:
    course_id = str(course_id or "").strip()
    sub_id = str(sub_id or "").strip()
    doc_type = str(doc_type or "other").strip()
    scope = str(scope or "lecture").strip()
    if doc_type not in DOCUMENT_TYPES:
        raise DocumentError("document_type_invalid")
    if scope not in DOCUMENT_SCOPES:
        raise DocumentError("document_scope_invalid")
    if scope == "course":
        sub_id = ""  # 课程级资料不挂讲次
    original_name = Path(str(original_name or "").replace("\\", "/")).name
    extension = Path(original_name).suffix.casefold()
    if not course_id or (scope == "lecture" and not sub_id):
        raise DocumentError("document_lecture_required")
    if extension not in SUPPORTED_EXTENSIONS:
        raise DocumentError("document_type_unsupported")
    raw = _decode_upload(content_base64)
    digest = hashlib.sha256(raw).hexdigest()
    document_id = hashlib.sha256(f"{course_id}:{sub_id}:{digest}".encode("utf-8")).hexdigest()[:32]
    pages = extract_pages(raw, extension)
    now = time.time()
    documents_root = (Path(storage_root) / "documents").resolve()
    documents_root.mkdir(parents=True, exist_ok=True)
    target_dir = documents_root / document_id
    target_dir.mkdir(parents=True, exist_ok=True)
    if documents_root not in target_dir.resolve().parents:
        raise DocumentError("document_storage_invalid")
    target_path = target_dir / f"original{extension}"
    if not target_path.exists():
        temporary_path = target_dir / f".original{extension}.tmp"
        try:
            temporary_path.write_bytes(raw)
            temporary_path.replace(target_path)
        finally:
            temporary_path.unlink(missing_ok=True)
    extraction_state = "ready" if all(page["extraction_state"] == "ready" for page in pages) else (
        "partial" if any(page["extraction_state"] == "ready" for page in pages) else "ocr_required"
    )
    ensure_document_schema(db_path)
    with closing(connect_learning_db(db_path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            """INSERT INTO learning_documents(
                   document_id,course_id,sub_id,title,original_name,extension,media_type,
                   storage_path,sha256,size_bytes,page_count,extraction_state,created_at,updated_at,
                   doc_type,scope
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(course_id,sub_id,sha256) DO UPDATE SET
                   title=excluded.title,original_name=excluded.original_name,
                   media_type=excluded.media_type,page_count=excluded.page_count,
                   extraction_state=excluded.extraction_state,updated_at=excluded.updated_at""",
            (
                document_id, course_id, sub_id, _normal_text(title)[:300] or Path(original_name).stem,
                original_name[:300], extension, str(media_type or "")[:120], str(target_path), digest,
                len(raw), len(pages), extraction_state, now, now,
                doc_type, scope,
            ),
        )
        db.execute("DELETE FROM learning_document_pages WHERE document_id=?", (document_id,))
        for page in pages:
            text = _normal_text(page.get("text") or "")
            db.execute(
                """INSERT INTO learning_document_pages(
                       document_id,page_num,text,text_hash,visual_hash,extraction_state,metadata_json
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    document_id, int(page["page_num"]), text,
                    hashlib.sha256(text.encode("utf-8")).hexdigest() if text else "",
                    str(page.get("visual_hash") or ""), str(page.get("extraction_state") or "ocr_required"),
                    _safe_json(page.get("metadata") or {}),
                ),
            )
        db.commit()
    return get_document(db_path, document_id, include_pages=True)


def _row_document(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value.pop("storage_path", None)
    return value


def list_documents(db_path: str | Path, *, course_id: str = "", sub_id: str = "") -> list[dict[str, Any]]:
    ensure_document_schema(db_path)
    clauses = []
    params: list[str] = []
    if course_id:
        clauses.append("course_id=?")
        params.append(str(course_id))
    if sub_id:
        clauses.append("sub_id=?")
        params.append(str(sub_id))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with closing(connect_learning_db(db_path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(f"SELECT * FROM learning_documents{where} ORDER BY updated_at DESC LIMIT 200", params).fetchall()
    return [_row_document(row) for row in rows]


def document_search_pages(db_path: str | Path, sub_id: str, *, course_id: str = "") -> list[dict[str, Any]]:
    """Search-visible document pages for a lecture.

    ``course_id`` additively widens the scope to the course-level documents
    (``scope='course'``, stored with an empty ``sub_id``): a student reading
    lecture 3 should still find the textbook chapter that belongs to the whole
    course.  The default stays lecture-only so every existing caller keeps its
    exact previous behaviour.
    """
    ensure_document_schema(db_path)
    where = "d.sub_id=?"
    params: list[Any] = [str(sub_id)]
    if course_id:
        where = "(d.sub_id=? OR (d.scope='course' AND d.course_id=?))"
        params.append(str(course_id))
    with closing(connect_learning_db(db_path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            f"""SELECT d.document_id,d.title,d.sha256,d.scope,d.doc_type,
                      p.page_num,p.text,p.text_hash,
                      a.start_ms,a.confidence,a.status,a.updated_at
               FROM learning_documents d
               JOIN learning_document_pages p ON p.document_id=d.document_id
               LEFT JOIN document_alignments a
                 ON a.document_hash=d.sha256 AND a.page_num=p.page_num AND a.sub_id=d.sub_id
              WHERE {where} AND p.text<>'' ORDER BY d.updated_at DESC,p.page_num""",
            params,
        ).fetchall()
    values = []
    for row in rows:
        value = dict(row)
        if value.get("status") not in {"suggested", "confirmed"} or float(value.get("confidence") or 0) < 0.62:
            value["start_ms"] = None
        values.append(value)
    return values


def get_document(db_path: str | Path, document_id: str, *, include_pages: bool = True) -> dict[str, Any]:
    ensure_document_schema(db_path)
    with closing(connect_learning_db(db_path)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM learning_documents WHERE document_id=?", (str(document_id),)).fetchone()
        if row is None:
            raise KeyError("document_not_found")
        value = _row_document(row)
        if include_pages:
            page_rows = db.execute(
                """SELECT p.*,a.alignment_id,a.start_ms,a.end_ms,a.confidence,a.status,
                          a.evidence_json,a.updated_at AS alignment_updated_at
                   FROM learning_document_pages p
                   LEFT JOIN document_alignments a
                     ON a.document_hash=? AND a.page_num=p.page_num AND a.sub_id=?
                   WHERE p.document_id=? ORDER BY p.page_num""",
                (str(row["sha256"]), str(row["sub_id"]), str(document_id)),
            ).fetchall()
            pages = []
            for page_row in page_rows:
                page = dict(page_row)
                page["metadata"] = json.loads(page.pop("metadata_json", "{}") or "{}")
                page["evidence"] = json.loads(page.pop("evidence_json", "{}") or "{}")
                pages.append(page)
            value["pages"] = pages
    return value


def document_storage_path(db_path: str | Path, document_id: str) -> tuple[Path, str]:
    ensure_document_schema(db_path)
    with closing(connect_learning_db(db_path)) as db:
        row = db.execute(
            "SELECT storage_path,extension FROM learning_documents WHERE document_id=?",
            (str(document_id),),
        ).fetchone()
    if row is None:
        raise KeyError("document_not_found")
    return Path(str(row[0])), str(row[1]).casefold()


def _transcript_windows(segments: list[dict[str, Any]], *, seconds: int = 45) -> list[dict[str, Any]]:
    values = []
    current: dict[str, Any] | None = None
    for segment in sorted(segments, key=lambda item: int(item.get("start_ms") or 0)):
        text = _normal_text(segment.get("text") or "")
        if not text:
            continue
        start_ms = max(0, int(segment.get("start_ms") or 0))
        end_ms = max(start_ms, int(segment.get("end_ms") or start_ms))
        bucket = start_ms // (seconds * 1000)
        if current is None or current["bucket"] != bucket:
            current = {"bucket": bucket, "start_ms": start_ms, "end_ms": end_ms, "texts": [text]}
            values.append(current)
        else:
            current["end_ms"] = max(current["end_ms"], end_ms)
            current["texts"].append(text)
    for item in values:
        item["text"] = _normal_text(" ".join(item.pop("texts")))
        item["source_hash"] = hashlib.sha256(item["text"].encode("utf-8")).hexdigest()
    return values


def _highlights(text: str) -> list[str]:
    pattern = re.compile(r"重点|注意|考试|作业|例题|总结|关键|必须|容易错")
    sentences = [part.strip() for part in re.split(r"[。！？!?；;\n]", text) if part.strip()]
    return [part[:160] for part in sentences if pattern.search(part)][:3]


def align_document(db_path: str | Path, document_id: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
    document = get_document(db_path, document_id, include_pages=True)
    pages = list(document.get("pages") or [])
    windows = _transcript_windows(segments)
    if not windows:
        raise DocumentError("alignment_transcript_unavailable")
    usable = [page for page in pages if _normal_text(page.get("text") or "")]
    duplicate_hashes = {
        value for value in {str(page.get("text_hash") or "") for page in usable}
        if value and sum(1 for page in usable if str(page.get("text_hash") or "") == value) > 1
    }
    page_count = max(1, len(pages))
    window_count = len(windows)
    local_scores: list[list[tuple[float, float, float]]] = []
    for page_index, page in enumerate(pages):
        row = []
        expected = page_index / max(1, page_count - 1) * max(0, window_count - 1)
        for window_index, window in enumerate(windows):
            similarity = _text_similarity(str(page.get("text") or ""), str(window.get("text") or ""))
            distance = abs(window_index - expected) / max(1.0, window_count * 0.28)
            prior = math.exp(-distance)
            row.append((similarity * 0.82 + prior * 0.18, similarity, prior))
        local_scores.append(row)

    # Dynamic programming enforces a non-decreasing page-to-time mapping.
    dp: list[list[float]] = [[-1e9] * window_count for _ in pages]
    parent: list[list[int]] = [[0] * window_count for _ in pages]
    if pages:
        for index in range(window_count):
            dp[0][index] = local_scores[0][index][0]
        for page_index in range(1, len(pages)):
            best_value = -1e9
            best_index = 0
            for window_index in range(window_count):
                if dp[page_index - 1][window_index] > best_value:
                    best_value = dp[page_index - 1][window_index]
                    best_index = window_index
                dp[page_index][window_index] = best_value + local_scores[page_index][window_index][0]
                parent[page_index][window_index] = best_index
        cursor = max(range(window_count), key=lambda index: dp[-1][index])
        chosen = [cursor]
        for page_index in range(len(pages) - 1, 0, -1):
            cursor = parent[page_index][cursor]
            chosen.append(cursor)
        chosen.reverse()
    else:
        chosen = []

    now = time.time()
    results = []
    with closing(connect_learning_db(db_path)) as db:
        for page_index, page in enumerate(pages):
            window_index = chosen[page_index]
            window = windows[window_index]
            score, similarity, prior = local_scores[page_index][window_index]
            alternatives = sorted((value[0] for value in local_scores[page_index]), reverse=True)
            margin = alternatives[0] - alternatives[1] if len(alternatives) > 1 else alternatives[0]
            confidence = max(0.0, min(1.0, similarity * 0.72 + prior * 0.18 + max(0.0, margin) * 0.10))
            reason = ""
            if not _normal_text(page.get("text") or ""):
                confidence = 0.0
                reason = "page_text_unavailable"
            elif str(page.get("text_hash") or "") in duplicate_hashes:
                confidence = min(confidence, 0.49)
                reason = "duplicate_page_text"
            status = "suggested" if confidence >= 0.62 else "review_required"
            alignment_id = hashlib.sha256(
                f"{document['sha256']}:{document['sub_id']}:{page['page_num']}".encode("utf-8")
            ).hexdigest()[:32]
            evidence = {
                "method": ALIGNMENT_VERSION,
                "text_similarity": round(similarity, 6),
                "position_prior": round(prior, 6),
                "source_hash": window["source_hash"],
                "page_text_hash": str(page.get("text_hash") or ""),
                "reason": reason,
                "highlights": _highlights(str(window.get("text") or "")),
            }
            db.execute(
                """INSERT INTO document_alignments(
                       alignment_id,course_id,sub_id,document_hash,page_num,start_ms,end_ms,
                       confidence,status,evidence_json,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(document_hash,page_num,sub_id) DO UPDATE SET
                       alignment_id=excluded.alignment_id,start_ms=excluded.start_ms,
                       end_ms=excluded.end_ms,confidence=excluded.confidence,status=excluded.status,
                       evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""",
                (
                    alignment_id, document["course_id"], document["sub_id"], document["sha256"],
                    int(page["page_num"]), int(window["start_ms"]), int(window["end_ms"]),
                    confidence, status, _safe_json(evidence), now,
                ),
            )
            results.append({
                "alignment_id": alignment_id,
                "page_num": int(page["page_num"]),
                "start_ms": int(window["start_ms"]),
                "end_ms": int(window["end_ms"]),
                "confidence": confidence,
                "status": status,
                "evidence": evidence,
            })
        db.commit()
    return {
        "document_id": str(document_id),
        "alignment_version": ALIGNMENT_VERSION,
        "page_count": len(pages),
        "suggested_count": sum(1 for item in results if item["status"] == "suggested"),
        "review_required_count": sum(1 for item in results if item["status"] == "review_required"),
        "alignments": results,
    }


def update_alignment(
    db_path: str | Path,
    *,
    alignment_id: str,
    start_ms: int,
    end_ms: int,
    status: str = "confirmed",
) -> dict[str, Any]:
    status = str(status or "confirmed")
    if status not in {"suggested", "review_required", "confirmed", "rejected"}:
        raise DocumentError("alignment_status_invalid")
    start_ms = max(0, int(start_ms))
    end_ms = max(start_ms, int(end_ms))
    now = time.time()
    with closing(connect_learning_db(db_path)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM document_alignments WHERE alignment_id=?", (str(alignment_id),)).fetchone()
        if row is None:
            raise KeyError("alignment_not_found")
        evidence = json.loads(str(row["evidence_json"] or "{}"))
        evidence["manual"] = True
        evidence["manual_updated_at"] = now
        db.execute(
            """UPDATE document_alignments SET start_ms=?,end_ms=?,confidence=?,status=?,
                       evidence_json=?,updated_at=? WHERE alignment_id=?""",
            (start_ms, end_ms, 1.0 if status == "confirmed" else float(row["confidence"]), status,
             _safe_json(evidence), now, str(alignment_id)),
        )
        db.commit()
        updated = db.execute("SELECT * FROM document_alignments WHERE alignment_id=?", (str(alignment_id),)).fetchone()
    value = dict(updated)
    value["evidence"] = json.loads(value.pop("evidence_json", "{}") or "{}")
    return value


def delete_document(db_path: str | Path, document_id: str) -> Path:
    ensure_document_schema(db_path)
    with closing(connect_learning_db(db_path)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT storage_path,sha256,sub_id FROM learning_documents WHERE document_id=?", (str(document_id),)).fetchone()
        if row is None:
            raise KeyError("document_not_found")
        db.execute("DELETE FROM document_alignments WHERE document_hash=? AND sub_id=?", (str(row["sha256"]), str(row["sub_id"])))
        db.execute("DELETE FROM learning_document_pages WHERE document_id=?", (str(document_id),))
        db.execute("DELETE FROM learning_documents WHERE document_id=?", (str(document_id),))
        db.commit()
    return Path(str(row["storage_path"])).parent


__all__ = [
    "ALIGNMENT_VERSION", "DocumentError", "MAX_DOCUMENT_BYTES", "align_document",
    "delete_document", "ensure_document_schema", "extract_pages", "get_document",
    "document_search_pages", "document_storage_path", "list_documents", "register_document", "update_alignment",
]
