"""Evidence-backed cross-course concepts and relationships."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from collections import defaultdict
from contextlib import closing
from itertools import combinations
from pathlib import Path
from typing import Any

from .sqlite_utils import connect_learning_db


CONCEPT_EXTRACTOR_VERSION = "evidence-concepts-zh-v1"
_GENERIC_SUFFIXES = ("算法", "方法", "模型", "理论", "概念")
_TECH_SUFFIXES = (
    "算法", "定理", "模型", "方法", "矩阵", "分布", "函数", "空间", "网络",
    "方程", "变换", "回归", "分类", "概率", "统计", "优化", "结构", "系统",
)
_ENGLISH_STOP = {
    "the", "and", "for", "with", "from", "this", "that", "then", "into", "course",
    "chapter", "example", "homework", "slide", "student", "teacher",
}


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))


def _normal_text(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def _normal_key(value: str) -> str:
    value = _normal_text(value).casefold().strip("“”‘’「」『』《》()（）[]【】`'\".,，。:：;；")
    value = re.sub(r"\s+", "", value)
    for suffix in _GENERIC_SUFFIXES:
        if value.endswith(suffix) and len(value) > len(suffix) + 1:
            value = value[:-len(suffix)]
            break
    return value


def ensure_concept_schema(path: str | Path) -> None:
    with closing(connect_learning_db(path)) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS concepts_v3 (
                concept_id TEXT PRIMARY KEY,
                canonical_name TEXT NOT NULL,
                normalized_key TEXT NOT NULL UNIQUE,
                aliases_json TEXT NOT NULL DEFAULT '[]',
                extractor_version TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS concept_mentions_v3 (
                mention_id TEXT PRIMARY KEY,
                concept_id TEXT NOT NULL,
                course_id TEXT NOT NULL,
                sub_id TEXT NOT NULL,
                start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL,
                source TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                text TEXT NOT NULL,
                confidence REAL NOT NULL,
                input_hash TEXT NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(concept_id) REFERENCES concepts_v3(concept_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_concept_mentions_course
                ON concept_mentions_v3(course_id,concept_id,start_ms);
            CREATE TABLE IF NOT EXISTS concept_edges_v3 (
                edge_id TEXT PRIMARY KEY,
                concept_id TEXT NOT NULL,
                from_course_id TEXT NOT NULL,
                from_sub_id TEXT NOT NULL,
                to_course_id TEXT NOT NULL,
                to_sub_id TEXT NOT NULL,
                relation TEXT NOT NULL DEFAULT 'related',
                evidence_json TEXT NOT NULL DEFAULT '[]',
                confidence REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                origin TEXT NOT NULL DEFAULT 'automatic',
                extractor_version TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(concept_id) REFERENCES concepts_v3(concept_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_concept_edges_course
                ON concept_edges_v3(from_course_id,to_course_id,status);
            """
        )
        db.commit()


def _candidate_names(text: str) -> set[str]:
    text = _normal_text(text)
    names: set[str] = set()
    for match in re.finditer(r"[“「『《`]([^”」』》`]{2,40})[”」』》`]", text):
        names.add(_normal_text(match.group(1)))
    for match in re.finditer(r"\b[A-Za-z][A-Za-z0-9+.#-]{2,24}\b", text):
        value = match.group(0)
        if value.casefold() not in _ENGLISH_STOP:
            names.add(value)
    for suffix in _TECH_SUFFIXES:
        for match in re.finditer(rf"[\u4e00-\u9fff]{{0,6}}{suffix}", text):
            value = match.group(0)
            parts = re.split(r"(?:这个|那个|我们|课程|老师|通过|使用|采用|关于|其中|以及|或者|一个|一种|的|和|与|及|是|在|对|把|将)", value)
            value = parts[-1].strip()
            if 2 <= len(value) <= 12:
                names.add(value)
    marker = re.compile(r"(?:称为|叫做|定义为|定义是|概念是|核心是|使用|采用)([A-Za-z][A-Za-z0-9+.#-]{2,24}|[\u4e00-\u9fff]{2,10})")
    for match in marker.finditer(text):
        value = match.group(1)
        value = re.split(r"(?:来|去|进行|可以|能够|实现|解决|处理|计算|得到)", value)[0]
        if 2 <= len(value) <= 12:
            names.add(value)
    return {name for name in names if len(_normal_key(name)) >= 2}


def _explicit_prerequisite(text: str, name: str) -> bool:
    compact = _normal_text(text)
    escaped = re.escape(name)
    return bool(re.search(rf"(?:先修|前置|基础).{{0,12}}{escaped}|{escaped}.{{0,12}}(?:先修|前置|基础)", compact))


def analyze_concepts(path: str | Path, sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Persist only concepts with independently hashed evidence in two courses."""
    ensure_concept_schema(path)
    now = time.time()
    mentions_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    aliases_by_key: dict[str, set[str]] = defaultdict(set)
    for source in sources:
        course_id = str(source.get("course_id") or "").strip()
        sub_id = str(source.get("sub_id") or "").strip()
        if not course_id or not sub_id:
            continue
        for segment in list(source.get("segments") or []):
            text = _normal_text(segment.get("text") or "")
            if not text:
                continue
            start_ms = max(0, int(segment.get("start_ms") or 0))
            end_ms = max(start_ms, int(segment.get("end_ms") or start_ms))
            source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            source_kind = str(segment.get("source") or "transcript")
            for name in _candidate_names(text):
                key = _normal_key(name)
                aliases_by_key[key].add(name)
                marker_confidence = 0.92 if re.search(rf"(?:称为|叫做|定义|先修|前置|基础).{{0,20}}{re.escape(name)}", text) else 0.82
                mentions_by_key[key].append({
                    "course_id": course_id, "sub_id": sub_id,
                    "start_ms": start_ms, "end_ms": end_ms,
                    "source": source_kind, "source_hash": source_hash,
                    "text": text[:1000], "confidence": marker_confidence,
                    "prerequisite": _explicit_prerequisite(text, name),
                })

    cross_keys = {
        key for key, mentions in mentions_by_key.items()
        if len({item["course_id"] for item in mentions}) >= 2
    }
    created_edges: list[dict[str, Any]] = []
    touched_edge_ids: set[str] = set()
    analyzed_courses = sorted({str(item.get("course_id") or "") for item in sources if item.get("course_id")})
    with closing(connect_learning_db(path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        for key in sorted(cross_keys):
            aliases = sorted(aliases_by_key[key], key=lambda value: (len(value), value.casefold()))
            canonical = aliases[0] if aliases else key
            concept_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
            db.execute(
                """INSERT INTO concepts_v3(
                    concept_id,canonical_name,normalized_key,aliases_json,extractor_version,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?) ON CONFLICT(concept_id) DO UPDATE SET
                    canonical_name=excluded.canonical_name,aliases_json=excluded.aliases_json,
                    extractor_version=excluded.extractor_version,updated_at=excluded.updated_at""",
                (concept_id, canonical, key, _json(aliases), CONCEPT_EXTRACTOR_VERSION, now, now),
            )
            by_course: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for mention in mentions_by_key[key]:
                by_course[mention["course_id"]].append(mention)
                input_hash = hashlib.sha256(_json({"concept": key, **mention}).encode("utf-8")).hexdigest()
                mention_id = hashlib.sha256(
                    f"{concept_id}:{mention['course_id']}:{mention['sub_id']}:{mention['start_ms']}:{mention['source_hash']}".encode()
                ).hexdigest()[:32]
                db.execute(
                    """INSERT INTO concept_mentions_v3(
                        mention_id,concept_id,course_id,sub_id,start_ms,end_ms,source,source_hash,
                        text,confidence,input_hash,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(mention_id) DO UPDATE SET
                        text=excluded.text,confidence=excluded.confidence,input_hash=excluded.input_hash,
                        updated_at=excluded.updated_at""",
                    (
                        mention_id, concept_id, mention["course_id"], mention["sub_id"], mention["start_ms"],
                        mention["end_ms"], mention["source"], mention["source_hash"], mention["text"],
                        mention["confidence"], input_hash, now,
                    ),
                )
            for left_course, right_course in combinations(sorted(by_course), 2):
                left = sorted(by_course[left_course], key=lambda item: (-item["confidence"], item["start_ms"]))[0]
                right = sorted(by_course[right_course], key=lambda item: (-item["confidence"], item["start_ms"]))[0]
                evidence = [
                    {key: value for key, value in left.items() if key != "prerequisite"},
                    {key: value for key, value in right.items() if key != "prerequisite"},
                ]
                relation = "prerequisite" if left["prerequisite"] and not right["prerequisite"] else "related"
                if relation == "prerequisite":
                    from_course, to_course, from_item, to_item = left_course, right_course, left, right
                else:
                    from_course, to_course, from_item, to_item = left_course, right_course, left, right
                edge_id = hashlib.sha256(f"{concept_id}:{from_course}:{to_course}".encode()).hexdigest()[:32]
                touched_edge_ids.add(edge_id)
                edge_input_hash = hashlib.sha256(_json({"concept": key, "relation": relation, "evidence": evidence}).encode("utf-8")).hexdigest()
                confidence = round(min(float(left["confidence"]), float(right["confidence"])), 3)
                db.execute(
                    """INSERT INTO concept_edges_v3(
                        edge_id,concept_id,from_course_id,from_sub_id,to_course_id,to_sub_id,relation,
                        evidence_json,confidence,status,origin,extractor_version,input_hash,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(edge_id) DO UPDATE SET
                        from_sub_id=excluded.from_sub_id,to_sub_id=excluded.to_sub_id,
                        relation=CASE WHEN concept_edges_v3.origin='manual' THEN concept_edges_v3.relation ELSE excluded.relation END,
                        evidence_json=excluded.evidence_json,confidence=excluded.confidence,
                        status=CASE WHEN concept_edges_v3.status='dismissed' THEN 'dismissed' ELSE 'active' END,
                        extractor_version=excluded.extractor_version,input_hash=excluded.input_hash,updated_at=excluded.updated_at""",
                    (
                        edge_id, concept_id, from_course, from_item["sub_id"], to_course, to_item["sub_id"],
                        relation, _json(evidence), confidence, "active", "automatic",
                        CONCEPT_EXTRACTOR_VERSION, edge_input_hash, now, now,
                    ),
                )
                created_edges.append({"edge_id": edge_id, "concept_id": concept_id})
        if analyzed_courses:
            placeholders = ",".join("?" for _ in analyzed_courses)
            existing = db.execute(
                f"""SELECT edge_id FROM concept_edges_v3 WHERE origin='automatic'
                       AND from_course_id IN ({placeholders}) AND to_course_id IN ({placeholders})""",
                analyzed_courses + analyzed_courses,
            ).fetchall()
            for row in existing:
                if str(row[0]) not in touched_edge_ids:
                    db.execute(
                        "UPDATE concept_edges_v3 SET status='review_required',updated_at=? WHERE edge_id=? AND status<>'dismissed'",
                        (now, str(row[0])),
                    )
        db.commit()
    return {
        "extractor_version": CONCEPT_EXTRACTOR_VERSION,
        "course_count": len(analyzed_courses),
        "concept_count": len(cross_keys),
        "edge_count": len(created_edges),
        "status": "completed",
    }


def list_concept_graph(path: str | Path, *, course_id: str = "") -> dict[str, Any]:
    ensure_concept_schema(path)
    params: list[str] = []
    where = ""
    if course_id:
        where = " WHERE e.from_course_id=? OR e.to_course_id=?"
        params = [str(course_id), str(course_id)]
    with closing(connect_learning_db(path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT e.*,c.canonical_name,c.aliases_json
                 FROM concept_edges_v3 e JOIN concepts_v3 c ON c.concept_id=e.concept_id"""
            + where + " ORDER BY e.status='active' DESC,e.confidence DESC,c.canonical_name LIMIT 500",
            params,
        ).fetchall()
    edges = []
    concepts: dict[str, dict[str, Any]] = {}
    courses: set[str] = set()
    for row in rows:
        value = dict(row)
        value["evidence"] = json.loads(value.pop("evidence_json", "[]") or "[]")
        aliases = json.loads(value.pop("aliases_json", "[]") or "[]")
        concepts[value["concept_id"]] = {
            "concept_id": value["concept_id"], "name": value.pop("canonical_name"), "aliases": aliases,
        }
        courses.update((str(value["from_course_id"]), str(value["to_course_id"])))
        edges.append(value)
    return {
        "concepts": list(concepts.values()),
        "edges": edges,
        "course_ids": sorted(courses),
        "extractor_version": CONCEPT_EXTRACTOR_VERSION,
    }


def update_concept_edge(
    path: str | Path, *, edge_id: str, action: str, relation: str = ""
) -> dict[str, Any]:
    ensure_concept_schema(path)
    action = str(action)
    if action not in {"dismiss", "restore", "set_relation"}:
        raise ValueError("concept_edge_action_invalid")
    if action == "set_relation" and relation not in {"related", "prerequisite"}:
        raise ValueError("concept_relation_invalid")
    now = time.time()
    with closing(connect_learning_db(path)) as db:
        if action == "dismiss":
            cursor = db.execute("UPDATE concept_edges_v3 SET status='dismissed',updated_at=? WHERE edge_id=?", (now, str(edge_id)))
        elif action == "restore":
            cursor = db.execute("UPDATE concept_edges_v3 SET status='review_required',updated_at=? WHERE edge_id=?", (now, str(edge_id)))
        else:
            cursor = db.execute(
                "UPDATE concept_edges_v3 SET relation=?,origin='manual',status='active',updated_at=? WHERE edge_id=?",
                (str(relation), now, str(edge_id)),
            )
        if cursor.rowcount != 1:
            raise KeyError("concept_edge_not_found")
        db.commit()
    return next(item for item in list_concept_graph(path)["edges"] if item["edge_id"] == str(edge_id))


def mark_stale_edges(path: str | Path, hashes_by_sub: dict[str, set[str]]) -> int:
    ensure_concept_schema(path)
    graph = list_concept_graph(path)
    stale: list[str] = []
    for edge in graph["edges"]:
        if edge.get("status") == "dismissed":
            continue
        valid = True
        evidence = list(edge.get("evidence") or [])
        if len({str(item.get("course_id") or "") for item in evidence}) < 2:
            valid = False
        for item in evidence:
            if str(item.get("source_hash") or "") not in hashes_by_sub.get(str(item.get("sub_id") or ""), set()):
                valid = False
                break
        if not valid:
            stale.append(str(edge["edge_id"]))
    if not stale:
        return 0
    now = time.time()
    with closing(connect_learning_db(path)) as db:
        db.executemany(
            "UPDATE concept_edges_v3 SET status='review_required',updated_at=? WHERE edge_id=? AND status<>'dismissed'",
            [(now, edge_id) for edge_id in stale],
        )
        db.commit()
    return len(stale)


__all__ = [
    "CONCEPT_EXTRACTOR_VERSION", "analyze_concepts", "ensure_concept_schema",
    "list_concept_graph", "mark_stale_edges", "update_concept_edge",
]
