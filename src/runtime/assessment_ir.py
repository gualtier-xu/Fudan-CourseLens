"""Assessment IR v1：作业/小测/往年题的可追溯最小存储与验证。

只做三件事，不建平台：

1. 把一份考核文档（doc_type ∈ exam_paper/homework）的题单元规范化成
   AssessmentItem——保留原题正文、页锚、小问/选项/分值，并**如实标注答案来源**；
2. 幂等落库（同文档内容指纹不重复），跨版本同题只分组、不物理合并；
3. 文档删除时显式级联，文档失据时标 orphaned；缺答案/缺页/答案冲突各自如实
   降级，绝不从题干反推答案。

答案来源闭集里 `ai_generated` 永不冒充 `official`：`official` 只留给教务/平台
正式发布的答案，材料自带的答案按上传者不可验证的最低档记 `user_material`。
存储身份 `item_id` 直接复用 course-knowledge 合同的 `assessment_item_id_for`，
因此 N7K 的引用视图与本存储天然同源；`assessment_id` 是同值的兼容别名。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from shared.course_knowledge_contract import (
    EVIDENCE_KINDS,
    assessment_item_id_for,
)

from .exam_paper_split import SPLITTABLE_DOC_TYPES, split_questions
from .sqlite_utils import connect_learning_db

SCHEMA = "courselens.assessment-ir.v1"

# kind 由 doc_type 映射（作业/小测/往年题）；未知或非考核类文档不产出题目。
ASSESSMENT_KINDS = ("exam_paper", "homework", "quiz", "other")
_KIND_BY_DOC_TYPE = {"exam_paper": "exam_paper", "homework": "homework", "other": "other", "quiz": "quiz"}

# 答案来源闭集。official=教务/平台正式发布；teacher_material=课程材料明确署名；
# user_material=本地材料自带（出处不可验证，默认档）；ai_generated=模型或本地生成；none=无答案。
ANSWER_SOURCES = ("official", "teacher_material", "user_material", "ai_generated", "none")
STATUSES = ("question_only", "answer_available", "conflicted", "orphaned")

MAX_STEM_CHARS = 4000
MAX_SUBPARTS = 20
MAX_OPTIONS = 12
MAX_ANSWER_CHARS = 2000
MAX_EVIDENCE_REFS = 16
MAX_ID_CHARS = 128
_SHA_RE = re.compile(r"^[0-9a-f]{32,64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{12,64}$")


def kind_for_document(doc_type: Any) -> str | None:
    """doc_type → AssessmentItem.kind；非考核类文档返回 None。"""
    value = str(doc_type or "").strip()
    if value not in SPLITTABLE_DOC_TYPES:
        return None
    return _KIND_BY_DOC_TYPE.get(value, "other")


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.match(value))


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def group_key_for(stem: str) -> str:
    """跨版本分组键：题干规范化文本的摘要。同题不同版本 → 同组，但各自成行。"""
    normalized = re.sub(r"\s+", "", str(stem or ""))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def normalize_assessment_item(value: Any) -> dict[str, Any] | None:
    """fail-closed 规范化一条 AssessmentItem；必填缺失或越界即拒绝（None）。"""
    if not isinstance(value, dict):
        return None
    course_id = _text(value.get("course_id"), MAX_ID_CHARS)
    sub_id = str(value.get("sub_id") or "").strip()[:MAX_ID_CHARS]
    document_id = _text(value.get("document_id"), MAX_ID_CHARS)
    kind = str(value.get("kind") or "")
    stem = _text(value.get("stem"), MAX_STEM_CHARS)
    answer = _text(value.get("answer"), MAX_ANSWER_CHARS)
    answer_source = str(value.get("answer_source") or "")
    content_hash = value.get("content_hash")
    question_no = _int_or_none(value.get("question_no"))
    if (
        not course_id
        or not document_id
        or not stem
        or kind not in ASSESSMENT_KINDS
        or answer_source not in ANSWER_SOURCES
        or not _is_hash(content_hash)
        or question_no is None
        or question_no < 1
    ):
        return None
    # 答案与来源必须自洽：有答案必有来源，来源非 none 必须有答案。
    if bool(answer) != (answer_source != "none"):
        return None
    status = str(value.get("status") or "")
    if status not in STATUSES:
        status = "answer_available" if answer else "question_only"
    subparts = []
    for item in (value.get("subparts") or [])[:MAX_SUBPARTS]:
        if isinstance(item, dict) and _text(item.get("text"), 1000):
            subparts.append({
                "label": _text(item.get("label"), 16),
                "text": _text(item.get("text"), 1000),
            })
    options = []
    for item in (value.get("options") or [])[:MAX_OPTIONS]:
        if isinstance(item, dict) and _text(item.get("text"), 500):
            options.append({
                "label": _text(item.get("label"), 8),
                "text": _text(item.get("text"), 500),
            })
    points = _int_or_none(value.get("points"))
    if points is not None and not (0 < points <= 1000):
        points = None
    refs = []
    for item in (value.get("evidence_refs") or [])[:MAX_EVIDENCE_REFS]:
        if not isinstance(item, dict):
            continue
        ref_kind = str(item.get("kind") or "")
        if ref_kind not in EVIDENCE_KINDS:
            continue
        # 定位键名与 course-knowledge 合同 locator 闭集对齐，转引用视图时是恒等映射。
        ref: dict[str, Any] = {"kind": ref_kind}
        if ref_kind in ("transcript", "bookmark"):
            start_ms = _int_or_none(item.get("start_ms"))
            end_ms = _int_or_none(item.get("end_ms"))
            if start_ms is None or end_ms is None or start_ms > end_ms or start_ms < 0:
                continue
            ref.update({"start_ms": start_ms, "end_ms": end_ms})
        elif ref_kind in ("slide", "document_page"):
            page = _int_or_none(item.get("page"))
            # 页码从 1 起：0/负数是坏锚，与 evidence packet 侧同一判据。
            if page is None or page < 1:
                continue
            ref["page"] = page
        else:
            question_no = _int_or_none(item.get("question_no"))
            if question_no is None or question_no < 1:
                continue
            ref["question_no"] = question_no
        if _text(item.get("source_id"), MAX_ID_CHARS):
            ref["source_id"] = _text(item.get("source_id"), MAX_ID_CHARS)
        refs.append(ref)
    revision_id = _text(value.get("revision_id"), 64)
    if revision_id and not _REVISION_RE.match(revision_id):
        revision_id = ""
    item_id = _text(value.get("item_id"), 64) or assessment_item_id_for({
        "course_id": course_id, "document_id": document_id,
        "question_no": question_no, "content_hash": content_hash,
    })
    return {
        # item_id 是 course-knowledge 合同的引用身份；assessment_id 是同值别名。
        "item_id": item_id,
        "assessment_id": item_id,
        "course_id": course_id,
        "sub_id": sub_id,
        "document_id": document_id,
        "kind": kind,
        "question_no": question_no,
        "stem": stem,
        "subparts": subparts,
        "options": options,
        "points": points,
        "answer": answer,
        "answer_source": answer_source,
        "evidence_refs": refs,
        "content_hash": str(content_hash),
        "group_key": _text(value.get("group_key"), 64) or group_key_for(stem),
        "status": status,
        "structure": "parsed" if (subparts or options or points is not None) else "raw",
        "revision_id": revision_id,
        "question_id": _text(value.get("question_id"), 64),
        "label": _text(value.get("label"), 200),
        "start_page": max(0, _int_or_none(value.get("start_page")) or 0),
        "end_page": max(0, _int_or_none(value.get("end_page")) or 0),
    }


def build_assessment_item(*, course_id: str, sub_id: str, document_id: str, kind: str,
                          question_no: int, stem: str, revision_id: str = "",
                          label: str = "", start_page: int = 0, end_page: int = 0,
                          answer_source: str = "none",
                          structure: dict[str, Any] | None = None,
                          document_sha256: str = "", content_hash: str = "",
                          question_id: str = "") -> dict[str, Any] | None:
    """从题干正文构造一条 AssessmentItem；答案从正文的显式标记切出。

    没有显式答案标记就答案为空、状态 question_only——不猜、不推理。

    ``content_hash``/``question_id`` 由调用方从结构化拆题结果传入时**直接沿用**：
    同一道题在拆题表(exam_questions)与本题库必须共享同一份内容哈希与引用身份，
    否则 course-knowledge 包里循环的 citation_id 无法回指到本题库。
    """
    from .exam_paper_split import extract_structure, split_answer

    question, answer = split_answer(stem)
    parsed = structure if isinstance(structure, dict) else extract_structure(question)
    if not (isinstance(content_hash, str) and _is_hash(content_hash)):
        content_hash = hashlib.sha256(
            f"{document_sha256}|{question_no}|{question}".encode("utf-8")
        ).hexdigest()
    source = answer_source
    if answer and source == "none":
        # 材料自带答案，但上传者不可验证 → 记最低可信档，绝不记 official。
        source = "user_material"
    refs: list[dict[str, Any]] = []
    if start_page or end_page:
        for page in range(int(start_page or 0), int(end_page or 0) + 1):
            if page > 0:
                refs.append({"kind": "document_page", "page": page, "source_id": str(document_id)})
    if not refs:
        refs = [{"kind": "assessment_item", "question_no": int(question_no),
                 "source_id": str(document_id)}]
    return normalize_assessment_item({
        "course_id": str(course_id), "sub_id": str(sub_id), "document_id": str(document_id),
        "kind": kind, "question_no": int(question_no), "stem": question or stem,
        "subparts": list(parsed.get("subparts") or []), "options": list(parsed.get("options") or []),
        "points": parsed.get("points"), "answer": answer, "answer_source": source,
        "evidence_refs": refs, "content_hash": content_hash, "revision_id": str(revision_id),
        "question_id": str(question_id),
        "label": str(label), "start_page": int(start_page or 0), "end_page": int(end_page or 0),
        "status": "answer_available" if answer else "question_only",
    })


def ensure_assessment_ir_schema(path: str | Path) -> None:
    with closing(connect_learning_db(path)) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS assessment_items (
                item_id TEXT PRIMARY KEY, course_id TEXT NOT NULL, sub_id TEXT NOT NULL,
                document_id TEXT NOT NULL, kind TEXT NOT NULL, question_no INTEGER NOT NULL,
                stem TEXT NOT NULL, subparts_json TEXT NOT NULL DEFAULT '[]',
                options_json TEXT NOT NULL DEFAULT '[]', points INTEGER,
                answer TEXT NOT NULL DEFAULT '', answer_source TEXT NOT NULL DEFAULT 'none',
                evidence_refs_json TEXT NOT NULL DEFAULT '[]', content_hash TEXT NOT NULL,
                group_key TEXT NOT NULL, status TEXT NOT NULL, structure TEXT NOT NULL DEFAULT 'raw',
                revision_id TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '',
                question_id TEXT NOT NULL DEFAULT '',
                start_page INTEGER NOT NULL DEFAULT 0, end_page INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_assessment_items_sub
                ON assessment_items(course_id, sub_id, question_no);
            CREATE INDEX IF NOT EXISTS idx_assessment_items_doc
                ON assessment_items(document_id);
            CREATE INDEX IF NOT EXISTS idx_assessment_items_group
                ON assessment_items(course_id, group_key);
            CREATE TABLE IF NOT EXISTS assessment_ir_state (
                document_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS assessment_ai_answers (
                item_id TEXT PRIMARY KEY, course_id TEXT NOT NULL,
                document_id TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL,
                revision_id TEXT NOT NULL DEFAULT '', stage TEXT NOT NULL,
                answer TEXT NOT NULL DEFAULT '', explanation TEXT NOT NULL DEFAULT '',
                citations_json TEXT NOT NULL DEFAULT '[]', model TEXT NOT NULL DEFAULT '',
                prompt_version TEXT NOT NULL DEFAULT '', error_code TEXT NOT NULL DEFAULT '',
                task_id TEXT NOT NULL DEFAULT '', input_hash TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_assessment_ai_course
                ON assessment_ai_answers(course_id);
            """
        )
        db.commit()


def _row_to_item(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    for key in ("subparts", "options", "evidence_refs"):
        try:
            value[key] = json.loads(value.pop(f"{key}_json", "[]") or "[]")
        except (TypeError, ValueError):
            value[key] = []
    value["assessment_id"] = value.get("item_id", "")
    return value


def save_assessment_items(path: str | Path, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """幂等写入：item_id 冲突即更新，同内容重复刷新不产生新行。"""
    ensure_assessment_ir_schema(path)
    valid = [item for item in (normalize_assessment_item(value) for value in items) if item]
    now = time.time()
    with closing(connect_learning_db(path)) as db:
        for item in valid:
            db.execute(
                """INSERT INTO assessment_items(
                    item_id,course_id,sub_id,document_id,kind,question_no,stem,
                    subparts_json,options_json,points,answer,answer_source,
                    evidence_refs_json,content_hash,group_key,status,structure,revision_id,label,
                    question_id,start_page,end_page,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(item_id) DO UPDATE SET
                    stem=excluded.stem, subparts_json=excluded.subparts_json,
                    options_json=excluded.options_json, points=excluded.points,
                    answer=excluded.answer, answer_source=excluded.answer_source,
                    evidence_refs_json=excluded.evidence_refs_json, status=excluded.status,
                    structure=excluded.structure, updated_at=excluded.updated_at""",
                (
                    item["item_id"], item["course_id"], item["sub_id"], item["document_id"],
                    item["kind"], item["question_no"], item["stem"],
                    json.dumps(item["subparts"], ensure_ascii=False),
                    json.dumps(item["options"], ensure_ascii=False), item["points"],
                    item["answer"], item["answer_source"],
                    json.dumps(item["evidence_refs"], ensure_ascii=False),
                    item["content_hash"], item["group_key"], item["status"], item["structure"],
                    item["revision_id"], item["label"], item["question_id"],
                    item["start_page"], item["end_page"], now, now,
                ),
            )
        db.commit()
    return valid


def list_assessment_items(path: str | Path, *, course_id: str = "", sub_id: str = "",
                          document_id: str = "", limit: int = 500) -> list[dict[str, Any]]:
    ensure_assessment_ir_schema(path)
    clauses, params = [], []
    for column, value in (("course_id", course_id), ("sub_id", sub_id),
                          ("document_id", document_id)):
        if value:
            clauses.append(f"{column}=?")
            params.append(str(value))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    capped = max(1, min(2000, int(limit)))
    with closing(connect_learning_db(path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            f"SELECT * FROM assessment_items{where} ORDER BY question_no, item_id LIMIT {capped}",
            params,
        ).fetchall()
    return [_row_to_item(row) for row in rows]


def _document_fingerprint(document: dict[str, Any], pages: list[dict[str, Any]]) -> str:
    joined = "|".join([
        str(document.get("sha256") or ""),
        str(document.get("doc_type") or ""),
        *[f"{page.get('page_num')}:{page.get('text_hash') or ''}" for page in pages],
    ])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def refresh_assessment_items(db_path: str | Path, document_id: str) -> dict[str, Any]:
    """把一份考核文档投影成 AssessmentItem 并落库；文档指纹未变则跳过重算。"""
    ensure_assessment_ir_schema(db_path)
    now = time.time()
    with closing(connect_learning_db(db_path)) as db:
        db.row_factory = sqlite3.Row
        tables = {str(row[0]) for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "learning_documents" not in tables:
            return {"schema": SCHEMA, "changed": False, "items": 0, "reason": "no_documents"}
        document = db.execute(
            "SELECT document_id, course_id, sub_id, title, sha256, doc_type "
            "FROM learning_documents WHERE document_id=?",
            (str(document_id),),
        ).fetchone()
        if document is None:
            raise KeyError("document_not_found")
        document = dict(document)
        kind = kind_for_document(document.get("doc_type"))
        if kind is None:
            return {"schema": SCHEMA, "changed": False, "items": 0, "reason": "not_assessment_document"}
        pages = [dict(row) for row in db.execute(
            "SELECT page_num, text, text_hash FROM learning_document_pages "
            "WHERE document_id=? ORDER BY page_num",
            (str(document_id),),
        ).fetchall()]
        fingerprint = _document_fingerprint(document, pages)
        marker = db.execute(
            "SELECT fingerprint FROM assessment_ir_state WHERE document_id=?",
            (str(document_id),),
        ).fetchone()
        if marker is not None and str(marker[0]) == fingerprint:
            count = db.execute(
                "SELECT COUNT(*) FROM assessment_items WHERE document_id=?",
                (str(document_id),),
            ).fetchone()[0]
            return {"schema": SCHEMA, "changed": False, "items": int(count)}
        document_sha = str(document.get("sha256") or "")
        revision_id = document_sha if _REVISION_RE.match(document_sha) else ""
        items = []
        for unit in split_questions(pages):
            item = build_assessment_item(
                course_id=str(document["course_id"]), sub_id=str(document["sub_id"]),
                document_id=str(document_id), kind=kind, question_no=unit["question_no"],
                stem=unit["stem"], revision_id=revision_id,
                label=unit.get("label") or str(document.get("title") or ""),
                start_page=unit["start_page"], end_page=unit["end_page"],
                structure=unit, document_sha256=document_sha,
                # 与拆题表共享同一份内容哈希与题身份（见 build_assessment_item 注释）
                content_hash=str(unit.get("content_hash") or ""),
                question_id=hashlib.sha256(
                    f"{document_id}:{unit['question_no']}".encode("utf-8")
                ).hexdigest()[:32],
            )
            if item is not None:
                items.append(item)
    stored = save_assessment_items(db_path, items)
    _reconcile_document_items(db_path, str(document_id), {item["item_id"] for item in stored})
    with closing(connect_learning_db(db_path)) as db:
        db.execute(
            """INSERT INTO assessment_ir_state(document_id, fingerprint, updated_at)
               VALUES(?,?,?)
               ON CONFLICT(document_id) DO UPDATE SET
                   fingerprint=excluded.fingerprint, updated_at=excluded.updated_at""",
            (str(document_id), fingerprint, now),
        )
        db.commit()
    return {"schema": SCHEMA, "changed": True, "items": len(stored)}


def _reconcile_document_items(db_path: str | Path, document_id: str, keep: set[str]) -> int:
    """删掉本文档已不存在的旧行（题号减少/正文改写后的残留），保幂等。"""
    with closing(connect_learning_db(db_path)) as db:
        rows = [str(row[0]) for row in db.execute(
            "SELECT item_id FROM assessment_items WHERE document_id=?", (document_id,)
        ).fetchall()]
        stale = [item_id for item_id in rows if item_id not in keep]
        for item_id in stale:
            db.execute("DELETE FROM assessment_items WHERE item_id=?", (item_id,))
        db.commit()
    if stale:
        # 题目行消失，挂在上面的 AI 结果一并消失：绝不留下没有题目的解答。
        clear_ai_answers(db_path, item_ids=stale)
    return len(stale)


def group_assessment_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 group_key 分组：同题跨版本只分组，不物理合并，也不挑一个当正本。"""
    groups: dict[str, dict[str, Any]] = {}
    for item in items:
        key = str(item.get("group_key") or "")
        entry = groups.setdefault(key, {"group_key": key, "items": [], "answers": []})
        entry["items"].append(item)
        answer = str(item.get("answer") or "")
        if answer and answer not in entry["answers"]:
            entry["answers"].append(answer)
    result = []
    for entry in groups.values():
        entry["items"].sort(key=lambda value: (str(value.get("revision_id") or ""),
                                               str(value.get("item_id") or "")))
        entry["revision_count"] = len({str(value.get("revision_id") or "")
                                       for value in entry["items"]})
        entry["conflicted"] = len(entry["answers"]) > 1
        result.append(entry)
    result.sort(key=lambda value: str(value["group_key"]))
    return result


def mark_conflicts(path: str | Path, *, course_id: str) -> int:
    """把同一题目组内答案互异的行标成 conflicted（并列保留，不当正本）。"""
    ensure_assessment_ir_schema(path)
    items = list_assessment_items(path, course_id=course_id)
    changed = 0
    now = time.time()
    with closing(connect_learning_db(path)) as db:
        for group in group_assessment_items(items):
            if not group["conflicted"]:
                continue
            for item in group["items"]:
                if item["status"] == "conflicted":
                    continue
                db.execute(
                    "UPDATE assessment_items SET status='conflicted', updated_at=? WHERE item_id=?",
                    (now, item["item_id"]),
                )
                changed += 1
        db.commit()
    return changed


def delete_assessment_items(path: str | Path, *, document_id: str) -> int:
    """文档删除时的显式级联。

    不用 FK ON DELETE CASCADE：既有先例（task_store）显示连接未开
    ``PRAGMA foreign_keys`` 时级联不生效，这里显式删行。
    """
    ensure_assessment_ir_schema(path)
    with closing(connect_learning_db(path)) as db:
        cursor = db.execute("DELETE FROM assessment_items WHERE document_id=?", (str(document_id),))
        db.execute("DELETE FROM assessment_ir_state WHERE document_id=?", (str(document_id),))
        db.commit()
        removed = int(cursor.rowcount or 0)
    # AI 结果按 document_id 显式级联（同一张表的另一条链，行数各自如实返回）。
    clear_ai_answers(path, document_id=str(document_id))
    return removed


def sweep_orphan_assessment_items(path: str | Path) -> int:
    """失据清理：文档已不在 learning_documents 的题目标 orphaned（不删行、不猜内容）。"""
    ensure_assessment_ir_schema(path)
    now = time.time()
    with closing(connect_learning_db(path)) as db:
        tables = {str(row[0]) for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "learning_documents" not in tables:
            return 0
        rows = db.execute(
            """SELECT a.item_id FROM assessment_items a
                LEFT JOIN learning_documents d ON d.document_id=a.document_id
               WHERE d.document_id IS NULL AND a.status<>'orphaned'"""
        ).fetchall()
        for row in rows:
            db.execute("UPDATE assessment_items SET status='orphaned', updated_at=? WHERE item_id=?",
                       (now, str(row[0])))
        db.commit()
    return len(rows)


def assessment_view(item: dict[str, Any], ai: dict[str, Any] | None = None) -> dict[str, Any]:
    """转成前端 course-review 消费的视图：合同 8 键 + 答案出处。

    合同 v1 的 AssessmentItem **不含答案字段**，所以答案与出处是额外加的诚实标注：
    `answer_source` 说清答案从哪来，`has_answer` 只在真有答案时为真。`citation_ids`
    给的是与课次知识同一空间的引用身份（由 `assessment_evidence_ref` 派生），
    因此知识点里循环的 citation_id 能回指到这一条；没有合同身份的本地出题（无
    document_id）一律给空数组，不发明引用。

    ``ai`` 是本题的 AI 结果行（可为 None）。**存储行里的材料答案一个字节都不动**：
    有材料答案时 AI 只挂在 `ai_explanation`；没有材料答案时才由视图层把 AI 文本
    当答案呈现，并把来源如实标成 `ai_generated`——绝不冒充 official。
    """
    from shared.course_knowledge_contract import citation_id_for

    ref = assessment_evidence_ref(item)
    citation_id = ""
    if ref["source_id"] and ref["revision_id"] and ref["content_hash"]:
        citation_id = citation_id_for(ref)
    material_answer = str(item.get("answer") or "").strip()
    has_answer = bool(material_answer) and item.get("answer_source") != "none"
    answer = material_answer if has_answer else ""
    answer_source = str(item.get("answer_source") or "none")
    ai_view = ai_answer_view(ai, item)
    if not has_answer and ai_view.get("ai_state") == "ready" and ai_view.get("ai_explanation"):
        answer, answer_source, has_answer = ai_view["ai_explanation"], "ai_generated", True
    return {
        "item_id": str(item.get("item_id") or ""),
        "course_id": str(item.get("course_id") or ""),
        "sub_id": str(item.get("sub_id") or ""),
        "document_id": str(item.get("document_id") or ""),
        "question_no": int(item.get("question_no") or 0),
        "label": str(item.get("label") or ""),
        "content_hash": str(item.get("content_hash") or ""),
        "citation_ids": [citation_id] if citation_id else [],
        "answer_source": answer_source,
        "has_answer": has_answer,
        "answer": answer,
        "ai_explanation": ai_view.get("ai_explanation", ""),
        "ai_state": ai_view.get("ai_state", ""),
        "ai_citations": ai_view.get("ai_citations", []),
        "ai_error_code": ai_view.get("ai_error_code", ""),
        "status": str(item.get("status") or ""),
    }


def assessment_evidence_ref(item: dict[str, Any]) -> dict[str, Any]:
    """把一条存储题目转成 course-knowledge 合同的 EvidenceRef。

    只给身份与位置，不带任何答案字段——引用视图永不假装知道答案。source_id 用拆题表
    的 question_id（与客户端产包用同一个），因此同一道题在包里的 citation_id 与本
    题库的引用**逐位相同**，知识点引用得回来。
    """
    return {
        "kind": "assessment_item",
        "source_id": str(item.get("question_id") or item.get("document_id") or "")[:128],
        "revision_id": str(item.get("revision_id") or ""),
        "content_hash": str(item.get("content_hash") or ""),
        "locator": {"question_no": int(item.get("question_no") or 0)},
        "label": str(item.get("label") or "")[:200],
    }


# ---- AI 解答/解析（N8A）：只在学生显式点选一道题后请求，结果只落本地读面 ----
#
# 三条硬约束写在这里，后面每个函数都按它做：
#  ① 唯一生成入口是用户动作（course-review/actions 的 explain_assessment），
#     导入 / 刷新 / 自动材料规则 / quiz_after_import 一律不得触发；
#  ② 证据只从本机已有材料里取有界窗口（题干、文档页、PPT 页、字幕时间窗），
#     证据不足就诚实拒答，绝不补外部知识；
#  ③ AI 文本永不进 CourseKnowledge 合同文档、云端证据包、日志明文或共享夹具，
#     只在本模块这张本地表与本地 assessment_view 读面出现。
AI_ANSWER_PROMPT_VERSION = "assessment-answer-v1"
# 进行中 → 终态闭集；stale 不落库，只在读面按当前题目身份现算。
AI_ANSWER_STAGES = ("queued", "running", "ready", "insufficient", "failed")
AI_ANSWER_IN_FLIGHT = ("queued", "running")
DECLINED_ANSWER_TEXT = "资料不足，无法根据当前课程资料回答。"

MAX_AI_ANSWER_CHARS = 8000
MAX_AI_CITATIONS = 8
# 证据窗口：条数、单条字数、总字数都封顶，且页面/字幕窗口有明确上界。
MAX_EVIDENCE_ENTRIES = 6
MAX_EVIDENCE_ITEM_CHARS = 1000
MAX_EVIDENCE_TOTAL_CHARS = 4000
MAX_EVIDENCE_PAGES = 4
_AI_EVIDENCE_KINDS = ("assessment_item", "document_page", "slide", "transcript", "bookmark")


def _evidence_anchor_ref(kind: str, locator: dict[str, Any]) -> tuple[str, str]:
    """引用出处的人话标签 + 可跳转锚的自检键。"""
    if kind in ("transcript", "bookmark"):
        return "字幕", f"{(locator.get('start_ms') or 0) // 60000:02d}:{(locator.get('start_ms') or 0) // 1000 % 60:02d}"
    if kind in ("slide", "document_page"):
        return ("课件第 %d 页" if kind == "slide" else "资料第 %d 页") % int(locator.get("page") or 0), "p%d" % int(locator.get("page") or 0)
    return "题目本身", "q%d" % int(locator.get("question_no") or 0)


def assessment_question_text(item: dict[str, Any]) -> str:
    """题干 + 小问 + 选项：进 query。选项/小问是题目的一部分，不是可引用材料。"""
    parts = [str(item.get("stem") or "").strip()]
    for sub in item.get("subparts") or []:
        text = str((sub or {}).get("text") or "").strip()
        if text:
            label = str((sub or {}).get("label") or "").strip()
            parts.append(f"（{label}）{text}" if label else text)
    options = []
    for option in item.get("options") or []:
        text = str((option or {}).get("text") or "").strip()
        if text:
            label = str((option or {}).get("label") or "").strip()
            options.append(f"{label}. {text}" if label else text)
    if options:
        parts.append("选项：" + "；".join(options))
    return "\n".join(part for part in parts if part)[:MAX_STEM_CHARS]


def _make_evidence_entry(*, item: dict[str, Any], kind: str, source_id: str,
                         text: str, locator: dict[str, Any], label: str) -> dict[str, Any] | None:
    body = str(text or "").strip()[:MAX_EVIDENCE_ITEM_CHARS]
    if not body or kind not in _AI_EVIDENCE_KINDS:
        return None
    default_label, anchor = _evidence_anchor_ref(kind, locator)
    return {
        # citation_id 由题目身份 + 锚派生：同一道题的同一处证据永远同一个 id，
        # 所以「复用已有结果」不是靠字符串碰运气。
        "citation_id": f"aia:{item.get('item_id')}:{kind}:{anchor}",
        "kind": kind,
        "source_id": str(source_id or ""),
        "course_id": str(item.get("course_id") or ""),
        "sub_id": str(item.get("sub_id") or ""),
        "label": (str(label or "").strip() or default_label)[:200],
        "text": body,
        "source_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "locator": dict(locator),
    }


def build_assessment_evidence(db_path: str | Path, item: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """为一道题组装有界证据窗口；取不到任何可用证据时返回 ("", 闭集原因码)。

    证据只来自本机已有材料：题目本身、题目页窗内的资料页、题目 evidence_refs 指向的
    PPT/字幕。**绝不带答案字段**——材料里的正文（可能恰好包含答案）是来源材料，
    但题目的 `answer`/`answer_source` 一个字节都不进证据包。
    """
    course_id = str(item.get("course_id") or "")
    if not course_id:
        return [], "assessment_evidence_unavailable"
    entries: list[dict[str, Any]] = []
    used_chars = 0

    def _push(entry: dict[str, Any] | None) -> None:
        nonlocal used_chars
        if entry is None or len(entries) >= MAX_EVIDENCE_ENTRIES:
            return
        if used_chars + len(entry["text"]) > MAX_EVIDENCE_TOTAL_CHARS:
            return
        if any(existing["citation_id"] == entry["citation_id"] for existing in entries):
            return
        used_chars += len(entry["text"])
        entries.append(entry)

    # ① 题目本身：可引用（跳回这道题），也是判分锚。
    _push(_make_evidence_entry(
        item=item, kind="assessment_item",
        source_id=str(item.get("question_id") or item.get("document_id") or ""),
        text=assessment_question_text(item),
        locator={"question_no": int(item.get("question_no") or 0)},
        label=str(item.get("label") or "") or "题目",
    ))

    refs = [ref for ref in (item.get("evidence_refs") or []) if isinstance(ref, dict)]
    page_refs: list[int] = []
    transcript_windows: list[tuple[str, int, int]] = []
    for ref in refs:
        kind = str(ref.get("kind") or "")
        if kind in ("document_page", "slide"):
            page = _int_or_none(ref.get("page"))
            if page and page >= 1:
                page_refs.append(int(page))
        elif kind == "transcript":
            start, end = _int_or_none(ref.get("start_ms")), _int_or_none(ref.get("end_ms"))
            if start is not None and end is not None and end >= start >= 0:
                transcript_windows.append((str(ref.get("source_id") or ""), int(start), int(end)))

    with closing(connect_learning_db(db_path)) as db:
        db.row_factory = sqlite3.Row
        tables = {str(row[0]) for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

        # ② 资料页：优先题目自带的页窗（含文档首页退化），再看 refs 里点到的页。
        document_id = str(item.get("document_id") or "")
        if document_id and "learning_document_pages" in tables:
            start_page = max(0, _int_or_none(item.get("start_page")) or 0)
            end_page = max(0, _int_or_none(item.get("end_page")) or 0)
            if not start_page and page_refs:
                start_page = min(page_refs)
            if not end_page:
                end_page = max([start_page, *page_refs]) if page_refs else start_page
            window = [page for page in range(max(1, start_page), max(1, end_page) + 1)][:MAX_EVIDENCE_PAGES]
            for page in window:
                row = db.execute(
                    "SELECT text FROM learning_document_pages WHERE document_id=? AND page_num=?",
                    (document_id, int(page)),
                ).fetchone()
                _push(_make_evidence_entry(
                    item=item, kind="document_page", source_id=document_id,
                    text=str((row or {})["text"] if row else ""),
                    locator={"page": int(page)},
                    label=f"{str(item.get('label') or '资料')} · 第 {page} 页",
                ))

        # ③ 字幕时间窗：题目 refs 点到的讲次窗口，逐条截取，窗口本身有界。
        if "transcript_segments" in tables:
            for source_id, start_ms, end_ms in transcript_windows[:MAX_EVIDENCE_PAGES]:
                sub_id = source_id or str(item.get("sub_id") or "")
                if not sub_id:
                    continue
                rows = db.execute(
                    """SELECT start_ms, end_ms, text FROM transcript_segments
                        WHERE sub_id=? AND end_ms>=? AND start_ms<=?
                        ORDER BY start_ms LIMIT 20""",
                    (sub_id, int(start_ms), int(end_ms)),
                ).fetchall()
                joined = "".join(str(row["text"] or "") for row in rows)
                _push(_make_evidence_entry(
                    item=item, kind="transcript", source_id=sub_id, text=joined,
                    locator={"start_ms": int(start_ms), "end_ms": int(end_ms)},
                    label="字幕",
                ))

    if len(entries) < 2:
        # 只剩「题目本身」等于没有材料可依据：诚实说资料不足，不拿题干自证。
        return [], "assessment_evidence_unavailable"
    return entries, ""


def _entry_has_jumpable_anchor(entry: dict[str, Any]) -> bool:
    """引用必须有可以带学生回去的锚，否则不许进允许清单。"""
    kind = str(entry.get("kind") or "")
    locator = dict(entry.get("locator") or {})
    if kind in ("transcript", "bookmark"):
        return _int_or_none(locator.get("start_ms")) is not None
    if kind in ("slide", "document_page"):
        page = _int_or_none(locator.get("page"))
        return page is not None and page >= 1
    if kind == "assessment_item":
        return bool(str(entry.get("source_id") or ""))
    return False


def validate_assessment_answer(answer: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """与 ``student_features.validate_grounded_answer`` 逐条同构的引用校验。

    三条判据完全一致：citation_id 必须落在允许清单里、`source_hash` 必须与正文逐字
    一致（正文被改过整条拒绝）、grounded 与 citations 缺一即诚实拒答。唯一差异在
    归属校验：那条要求每条证据同时有 course_id 与 sub_id，而**课程级资料（scope=course）
    按设计没有讲次**（document_alignment.py:345 把 sub_id 置空），照抄会恒拒答。这里
    改成「course_id 必须非空，且必须有可跳转的锚」，锚按 kind 各自判定。
    """
    allowed: dict[str, dict[str, Any]] = {}
    for entry in evidence:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "")
        source_hash = str(entry.get("source_hash") or "")
        if not text or not source_hash:
            continue
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != source_hash:
            continue
        if not str(entry.get("course_id") or ""):
            continue
        if not _entry_has_jumpable_anchor(entry):
            continue
        citation_id = str(entry.get("citation_id") or "")
        if citation_id:
            allowed[citation_id] = entry
    citation_ids = [str(value) for value in answer.get("citations") or [] if str(value) in allowed]
    if not bool(answer.get("grounded")) or not citation_ids or not str(answer.get("answer") or "").strip():
        return {"answer": DECLINED_ANSWER_TEXT, "citations": [], "grounded": False}
    citations = []
    for citation_id in citation_ids[:MAX_AI_CITATIONS]:
        entry = allowed[citation_id]
        citations.append({
            "citation_id": citation_id,
            "kind": str(entry.get("kind") or ""),
            "source_id": str(entry.get("source_id") or ""),
            "label": str(entry.get("label") or ""),
            "snippet": str(entry.get("text") or "")[:500],
            "locator": dict(entry.get("locator") or {}),
        })
    return {"answer": str(answer["answer"]).strip(), "citations": citations, "grounded": True}


def ai_answer_input_hash(item: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    """同题同证据同提示词版本 → 同一个 input_hash：重复点击天然复用，不重复烧云。"""
    identity = {
        "item_id": str(item.get("item_id") or ""),
        "content_hash": str(item.get("content_hash") or ""),
        "revision_id": str(item.get("revision_id") or ""),
        "prompt_version": AI_ANSWER_PROMPT_VERSION,
        "evidence": [str(entry.get("citation_id") or "") for entry in evidence],
    }
    return hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]


def save_ai_answer(path: str | Path, *, item: dict[str, Any], stage: str, answer: str = "",
                   citations: list[dict[str, Any]] | None = None, model: str = "",
                   error_code: str = "", task_id: str = "", input_hash: str = "") -> dict[str, Any]:
    """写一行 AI 结果（item_id 主键，冲突即更新），并回读该行。"""
    ensure_assessment_ir_schema(path)
    item_id = str(item.get("item_id") or "")
    if not item_id:
        raise ValueError("assessment item id is required")
    stage = str(stage or "")
    if stage not in AI_ANSWER_STAGES:
        raise ValueError(f"unknown ai answer stage: {stage}")
    explanation = str(answer or "").strip()[:MAX_AI_ANSWER_CHARS]
    now = time.time()
    with closing(connect_learning_db(path)) as db:
        db.execute(
            """INSERT INTO assessment_ai_answers(
                   item_id,course_id,document_id,content_hash,revision_id,stage,answer,
                   explanation,citations_json,model,prompt_version,error_code,task_id,
                   input_hash,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(item_id) DO UPDATE SET
                   stage=excluded.stage, answer=excluded.answer,
                   explanation=excluded.explanation, citations_json=excluded.citations_json,
                   model=excluded.model, prompt_version=excluded.prompt_version,
                   error_code=excluded.error_code, task_id=excluded.task_id,
                   input_hash=excluded.input_hash, updated_at=excluded.updated_at""",
            (
                item_id, str(item.get("course_id") or ""), str(item.get("document_id") or ""),
                str(item.get("content_hash") or ""), str(item.get("revision_id") or ""),
                stage, explanation if stage == "ready" else "",
                explanation, json.dumps(list(citations or []), ensure_ascii=False)[:20000],
                str(model or ""), AI_ANSWER_PROMPT_VERSION, str(error_code or ""),
                str(task_id or ""), str(input_hash or ""), now, now,
            ),
        )
        db.commit()
    return list_ai_answers(path, item_ids=[item_id]).get(item_id) or {}


def list_ai_answers(path: str | Path, *, course_id: str = "",
                    item_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """按 item_id 取 AI 结果行；course_id 与 item_ids 同时给时按交集过滤。"""
    ensure_assessment_ir_schema(path)
    clauses, params = [], []
    if course_id:
        clauses.append("course_id=?")
        params.append(str(course_id))
    wanted = [str(value) for value in (item_ids or []) if str(value)]
    if item_ids is not None:
        if not wanted:
            return {}
        clauses.append(f"item_id IN ({','.join('?' * len(wanted))})")
        params.extend(wanted)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with closing(connect_learning_db(path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(f"SELECT * FROM assessment_ai_answers{where}", params).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = dict(row)
        try:
            citations = json.loads(value.pop("citations_json", "[]") or "[]")
        except (TypeError, ValueError):
            citations = []
        value["citations"] = citations if isinstance(citations, list) else []
        result[str(value.get("item_id") or "")] = value
    return result


def clear_ai_answers(path: str | Path, *, document_id: str = "",
                     item_ids: list[str] | None = None) -> int:
    """文档删除 / 题目行消失时的显式级联；AI 结果绝不比它的题目活得久。"""
    ensure_assessment_ir_schema(path)
    with closing(connect_learning_db(path)) as db:
        deleted = 0
        if document_id:
            cursor = db.execute(
                "DELETE FROM assessment_ai_answers WHERE document_id=?", (str(document_id),))
            deleted += int(cursor.rowcount or 0)
        wanted = [str(value) for value in (item_ids or []) if str(value)]
        if wanted:
            cursor = db.execute(
                f"DELETE FROM assessment_ai_answers WHERE item_id IN ({','.join('?' * len(wanted))})",
                wanted,
            )
            deleted += int(cursor.rowcount or 0)
        db.commit()
        return deleted


def ai_answer_view(ai: dict[str, Any] | None, item: dict[str, Any]) -> dict[str, Any]:
    """把一行 AI 结果与当前题目身份对齐；对不上就如实说它已经过期。

    身份靠三样东西对：item_id（由 content_hash 派生）、content_hash、revision_id。
    题目内容更新、文档删除或来源失据之后，旧结果一律不再当当前答案显示。
    """
    empty = {"ai_state": "", "ai_explanation": "", "ai_citations": [], "ai_error_code": ""}
    if not isinstance(ai, dict) or not ai:
        return empty
    stage = str(ai.get("stage") or "")
    item_id = str(item.get("item_id") or "")
    stale = (
        not item_id
        or str(ai.get("item_id") or "") != item_id
        or str(ai.get("content_hash") or "") != str(item.get("content_hash") or "")
        or str(item.get("status") or "") == "orphaned"
        or str(ai.get("revision_id") or "") not in {"", str(item.get("revision_id") or "")}
    )
    if stale:
        # 过期不是失败：如实告诉前端「这份解答对不上现在的题目了」。
        return {**empty, "ai_state": "stale"}
    return {
        "ai_state": stage,
        "ai_explanation": str(ai.get("explanation") or "") if stage == "ready" else "",
        "ai_citations": list(ai.get("citations") or []) if stage == "ready" else [],
        "ai_error_code": str(ai.get("error_code") or ""),
    }
