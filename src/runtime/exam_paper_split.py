r"""真题/作业结构拆题 v2：把 doc_type ∈ exam_paper/homework 的已导入文档按题号切成
可检索单元，登记进搜索索引（source=exam_question）。纯结构正则，零 token、零外联；
不做 OCR——无文本页诚实跳过（照 document_alignment 先例）。切分结果按页面内容指纹
缓存，页文本未变不重算。

三族题号模式（优先级从高到低，防误切小问与 decimals）：
  1. 大题「一二、」  ^[一二三四五六七八九十]+、
  2. 分值「（3 分）」 [（(]\s*\d{1,2}\s*分[）)]
  3. 小题「12.」     ^\d{1,2}[．.、] 后必须跟非数字（放过 12.5 这类小数）

v2 加性扩展（N7A）：每个题单元额外保留题干正文（stem，跨页连续）、跨页标记、
可识别的小问/选项/分值，以及正文摘要 stem_hash。解析不出结构就保留 raw stem，
structure 记 "raw"——绝不从题干反推答案。旧调用（question_no/label/kind/
start_page/end_page/anchor_text/content_hash）语义与存在性不变。
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

SCHEMA = "courselens.exam-paper-split.v2"

# 会产出题单元的文档类型（作业/小测/往年题）。
SPLITTABLE_DOC_TYPES = ("exam_paper", "homework")

MAX_STEM_CHARS = 4000
MAX_SUBPARTS = 20
MAX_OPTIONS = 12
MAX_ANSWER_CHARS = 2000

_BIG_QUESTION_RE = re.compile(r"^[一二三四五六七八九十]+、\s*\S")
_SCORE_QUESTION_RE = re.compile(r"[（(]\s*\d{1,2}\s*分[）)]")
_SMALL_QUESTION_RE = re.compile(r"^\d{1,2}[．.、]\s*\D")
_SUBPART_RE = re.compile(r"^\s*[（(]\s*([0-9]{1,2})\s*[)）]\s*(\S.*)$")
_SUBPART_ALT_RE = re.compile(r"^\s*([0-9]{1,2})\s*[)）]\s*(\S.*)$")
_OPTION_RE = re.compile(r"^\s*([A-Ha-h])\s*[.．、)）]\s*(\S.*)$")
_POINTS_RE = re.compile(r"[（(]\s*(\d{1,3})\s*分[）)]")
# 只有文档自己写了答案标记才认答案；否则答案为空、状态 question_only。
# 冒号式标记可以在行内任意位置（真实真题常有「（接上页）参考答案：…」）；
# 无冒号时必须整行就是标记本身，避免「把答案写在答题卡上」这类正文被误当答案。
_ANSWER_COLON_RE = re.compile(
    r"(?:参考解答|解答|(?:参考|标准)?\s*答案(?:与解析|解析|要点)?)\s*[:：]"
)
_ANSWER_SECTION_RE = re.compile(r"^(?:参考解答|解答|答案与解析|参考答案)$")


def _page_num(value: Any) -> int:
    """页码容错：非整数/非正数一律退到 0，排在最前。

    拆题由已入库的行驱动，但页列表也可能来自调用方的临时字典；一条脏行不应该让
    整讲的题单元全军覆没（worker 侧抛异常等于整个任务失败）。
    """
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _line_kind(line: str) -> str | None:
    if _BIG_QUESTION_RE.search(line):
        return "big"
    if _SCORE_QUESTION_RE.search(line):
        return "score"
    if _SMALL_QUESTION_RE.match(line):
        return "small"
    return None


def extract_structure(stem: str) -> dict[str, Any]:
    """从题干正文里确定性抽取小问/选项/分值。

    置信门槛：小问与选项各自必须有 ≥2 个独立行命中、选项标签不重复，否则整族
    留空——宁可保留 raw stem，也不猜结构。
    """
    subparts: list[dict[str, str]] = []
    options: list[dict[str, str]] = []
    for raw_line in str(stem or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        option = _OPTION_RE.match(line)
        if option:
            options.append({"label": option.group(1).upper(), "text": option.group(2).strip()[:500]})
            continue
        subpart = _SUBPART_RE.match(line) or _SUBPART_ALT_RE.match(line)
        if subpart:
            subparts.append({"label": f"({subpart.group(1)})", "text": subpart.group(2).strip()[:1000]})
    labels = [item["label"] for item in options]
    if len(options) < 2 or len(set(labels)) != len(labels):
        options = []
    if len(subparts) < 2:
        subparts = []
    match = _POINTS_RE.search(str(stem or ""))
    return {
        "subparts": subparts[:MAX_SUBPARTS],
        "options": options[:MAX_OPTIONS],
        "points": int(match.group(1)) if match else None,
    }


def split_answer(stem: str) -> tuple[str, str]:
    """把题干与文档自带答案切开。

    只在出现显式答案标记时切分；没有标记就返回空答案（不猜、不推理）。
    """
    lines = str(stem or "").splitlines()
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        colon = _ANSWER_COLON_RE.search(line)
        if colon:
            # 标记前同一行的文字归题干，标记后同行的与后续行都是答案。
            answer = "\n".join(
                [line[colon.end():].strip(), *[item.strip() for item in lines[index + 1:]]]
            ).strip()
            question = "\n".join(
                [*[item.strip() for item in lines[:index]], line[:colon.start()].strip()]
            ).strip()
            if question and answer:
                return question[:MAX_STEM_CHARS], answer[:MAX_ANSWER_CHARS]
            continue
        if _ANSWER_SECTION_RE.match(line):
            answer = "\n".join(item.strip() for item in lines[index + 1:]).strip()
            question = "\n".join(lines[:index]).strip()
            if question and answer:
                return question[:MAX_STEM_CHARS], answer[:MAX_ANSWER_CHARS]
    return str(stem or "").strip()[:MAX_STEM_CHARS], ""


def split_questions(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把文档页切成题单元：题号=命中序号（1 起），含起止页、锚句与题干正文。

    题干正文自命中行起连续累积（跨页不断），因此跨页题的正文是完整的；
    结构（小问/选项/分值）只在可信时给出，否则 structure="raw"。
    """
    units: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    ordered = sorted(
        (page for page in pages if isinstance(page, dict)),
        key=lambda item: _page_num(item.get("page_num")),
    )
    for page in ordered:
        page_num = _page_num(page.get("page_num"))
        text = str(page.get("text") or "")
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            kind = _line_kind(line)
            # 优先级语义：大题/分值总能开新单元；小题只在没有任何打开单元时
            # 开题（大题内部的小问绝不另切）——这就是"防误切小问"。
            if kind in ("big", "score"):
                if current is not None:
                    units.append(current)
                    current = None
                current = {
                    "question_no": len(units) + 1,
                    "label": line[:30],
                    "kind": kind,
                    "start_page": page_num,
                    "end_page": page_num,
                    "anchor_text": line[:80],
                    "pages": [page_num],
                    "stem_lines": [line],
                }
                continue
            if kind == "small" and current is None:
                current = {
                    "question_no": len(units) + 1,
                    "label": line[:30],
                    "kind": kind,
                    "start_page": page_num,
                    "end_page": page_num,
                    "anchor_text": line[:80],
                    "pages": [page_num],
                    "stem_lines": [line],
                }
            elif current is not None:
                current["stem_lines"].append(line)
            if current is not None:
                current["end_page"] = page_num
                if page_num not in current["pages"]:
                    current["pages"].append(page_num)
    if current is not None:
        units.append(current)
    for unit in units:
        stem = "\n".join(unit.pop("stem_lines", [])).strip()[:MAX_STEM_CHARS]
        structure = extract_structure(stem)
        unit["stem"] = stem
        unit["stem_hash"] = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:32]
        unit["cross_page"] = int(unit["end_page"]) > int(unit["start_page"])
        unit["subparts"] = structure["subparts"]
        unit["options"] = structure["options"]
        unit["points"] = structure["points"]
        unit["structure"] = (
            "parsed" if (structure["subparts"] or structure["options"]
                         or structure["points"] is not None) else "raw"
        )
        # content_hash 覆盖正文摘要：同锚句不同正文不再是同一个版本。
        unit["content_hash"] = hashlib.sha256(
            "|".join([
                str(unit["start_page"]), str(unit["end_page"]),
                unit["anchor_text"], unit["stem_hash"],
            ]).encode("utf-8")
        ).hexdigest()[:32]
        unit.pop("pages", None)
    return units


# v1 列集合 + v2 加性列（旧库 ALTER 补齐，旧行原样保留）。
_QUESTION_COLUMNS = {
    "stem": "TEXT NOT NULL DEFAULT ''",
    "stem_hash": "TEXT NOT NULL DEFAULT ''",
    "subparts_json": "TEXT NOT NULL DEFAULT '[]'",
    "options_json": "TEXT NOT NULL DEFAULT '[]'",
    "points": "INTEGER",
    "structure": "TEXT NOT NULL DEFAULT 'raw'",
    "cross_page": "INTEGER NOT NULL DEFAULT 0",
}


def ensure_exam_paper_schema(db_path: str | Path) -> None:
    with closing(sqlite3.connect(db_path)) as db, db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS exam_questions (
                question_id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
                sub_id TEXT NOT NULL, course_id TEXT NOT NULL,
                question_no INTEGER NOT NULL, label TEXT NOT NULL, kind TEXT NOT NULL,
                start_page INTEGER NOT NULL, end_page INTEGER NOT NULL,
                anchor_text TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_exam_questions_sub
                ON exam_questions(sub_id, question_no);
            CREATE TABLE IF NOT EXISTS exam_split_state (
                document_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )
        columns = {str(row[1]) for row in db.execute("PRAGMA table_info(exam_questions)")}
        for column, definition in _QUESTION_COLUMNS.items():
            if column not in columns:
                db.execute(f"ALTER TABLE exam_questions ADD COLUMN {column} {definition}")


def _pages_content_hash(pages: list[dict[str, Any]]) -> str:
    """页指纹：页内容 + 拆题 schema 版本。

    带上 schema 版本是升级正确性所需：v1 缓存过的库页指纹未变时本会直接跳过，
    于是 v2 新列（stem/小问/分值）永远是空的。带版本后旧缓存必然失配、重算一次，
    其后稳定——这也是"旧库升级幂等"的实现方式。
    """
    joined = SCHEMA + "|" + "|".join(
        f"{_page_num(page.get('page_num'))}:{page.get('text_hash') or ''}"
        for page in sorted(
            (page for page in pages if isinstance(page, dict)),
            key=lambda item: _page_num(item.get("page_num")),
        )
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def list_exam_questions(db_path: str | Path, document_id: str) -> list[dict[str, Any]]:
    """按文档取全部题单元（含 v2 正文与结构）；供 Assessment IR 页面消费。"""
    ensure_exam_paper_schema(db_path)
    with closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM exam_questions WHERE document_id=? ORDER BY question_no",
            (str(document_id),),
        ).fetchall()
    result = []
    for row in rows:
        value = dict(row)
        for key in ("subparts", "options"):
            try:
                value[key] = json.loads(value.pop(f"{key}_json", "[]") or "[]")
            except (TypeError, ValueError):
                value[key] = []
        value["cross_page"] = bool(value.get("cross_page"))
        result.append(value)
    return result


def refresh_exam_questions(db_path: str | Path, document_id: str) -> dict[str, Any]:
    """重算并缓存一份考核文档的题单元；页指纹未变则跳过。"""
    ensure_exam_paper_schema(db_path)
    now = time.time()
    with closing(sqlite3.connect(db_path)) as db, db:
        db.row_factory = sqlite3.Row
        document = db.execute(
            "SELECT document_id, course_id, sub_id, sha256 FROM learning_documents WHERE document_id=?",
            (str(document_id),),
        ).fetchone()
        if document is None:
            raise KeyError("document_not_found")
        pages = db.execute(
            "SELECT page_num, text, text_hash FROM learning_document_pages WHERE document_id=? ORDER BY page_num",
            (str(document_id),),
        ).fetchall()
        fingerprint = _pages_content_hash([dict(page) for page in pages])
        marker = db.execute(
            "SELECT fingerprint FROM exam_split_state WHERE document_id=?",
            (str(document_id),),
        ).fetchone()
        if marker is not None and str(marker[0]) == fingerprint:
            count = db.execute(
                "SELECT COUNT(*) FROM exam_questions WHERE document_id=?",
                (str(document_id),),
            ).fetchone()[0]
            return {"schema": SCHEMA, "changed": False, "questions": int(count)}
        units = split_questions([dict(page) for page in pages])
        db.execute("DELETE FROM exam_questions WHERE document_id=?", (str(document_id),))
        for unit in units:
            question_id = hashlib.sha256(
                f"{document_id}:{unit['question_no']}".encode("utf-8")
            ).hexdigest()[:32]
            db.execute(
                """INSERT OR REPLACE INTO exam_questions(
                    question_id, document_id, sub_id, course_id, question_no, label, kind,
                    start_page, end_page, anchor_text, content_hash, created_at,
                    stem, stem_hash, subparts_json, options_json, points, structure, cross_page)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    question_id, str(document_id), str(document["sub_id"]), str(document["course_id"]),
                    unit["question_no"], unit["label"], unit["kind"], unit["start_page"],
                    unit["end_page"], unit["anchor_text"], unit["content_hash"], now,
                    unit["stem"], unit["stem_hash"],
                    json.dumps(unit["subparts"], ensure_ascii=False),
                    json.dumps(unit["options"], ensure_ascii=False),
                    unit["points"], unit["structure"], int(bool(unit["cross_page"])),
                ),
            )
        db.execute(
            """INSERT INTO exam_split_state(document_id, fingerprint, updated_at) VALUES(?,?,?)
               ON CONFLICT(document_id) DO UPDATE SET fingerprint=excluded.fingerprint, updated_at=excluded.updated_at""",
            (str(document_id), fingerprint, now),
        )
    return {"schema": SCHEMA, "changed": True, "questions": len(units)}


def search_exam_questions(db_path: str | Path, sub_id: str) -> list[dict[str, Any]]:
    """供 search_index 消费：本讲次考核文档的全部题单元。

    返回键保持 v1 兼容（question_id/question_no/label/anchor_text/content_hash/
    document_title），另加 v2 的 stem 供更准的命中摘要。
    """
    ensure_exam_paper_schema(db_path)
    with closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT q.question_id, q.question_no, q.label, q.anchor_text, q.content_hash,
                      q.stem, d.title AS document_title
                 FROM exam_questions q
                 JOIN learning_documents d ON d.document_id=q.document_id
                WHERE q.sub_id=? ORDER BY d.updated_at DESC, q.question_no""",
            (str(sub_id),),
        ).fetchall()
    return [dict(row) for row in rows]
