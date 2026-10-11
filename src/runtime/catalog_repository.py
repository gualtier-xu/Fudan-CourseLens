"""SQLite-backed authorized course catalog."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CATALOG_SCHEMA_VERSION = 2
RETIRED_LECTURE_FIELDS = {
    "status", "file_path", "error", "size_bytes", "progress_percent",
    "progress_label", "downloaded_bytes", "legacy_file_path",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CatalogRepository:
    """Thread-safe catalog repository using short-lived SQLite connections."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    def _initialize(self) -> None:
        with self._lock:
            db = self._connect()
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS catalog_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS catalog_courses (
                        course_id TEXT PRIMARY KEY, title TEXT NOT NULL,
                        teacher TEXT NOT NULL DEFAULT '', term TEXT NOT NULL DEFAULT '',
                        department TEXT NOT NULL DEFAULT '', authorization_state TEXT NOT NULL DEFAULT 'unknown',
                        updated_at TEXT NOT NULL
                    );
                    """
                )
                columns = {
                    str(row[1])
                    for row in db.execute("PRAGMA table_info(catalog_lectures)").fetchall()
                }
                if not columns:
                    self._create_lecture_table(db, "catalog_lectures")
                elif columns.intersection(RETIRED_LECTURE_FIELDS):
                    self._create_lecture_table(db, "catalog_lectures_v2")
                    metadata = "metadata_json" if "metadata_json" in columns else "'{}'"
                    db.execute(
                        f"""INSERT INTO catalog_lectures_v2
                        (sub_id,course_id,sub_title,date,has_playback,metadata_json,updated_at)
                        SELECT sub_id,course_id,sub_title,date,has_playback,{metadata},updated_at
                        FROM catalog_lectures"""
                    )
                    db.execute("DROP TABLE catalog_lectures")
                    db.execute("ALTER TABLE catalog_lectures_v2 RENAME TO catalog_lectures")
                db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_catalog_lectures_course "
                    "ON catalog_lectures(course_id,date,sub_title)"
                )
                db.execute(
                    "INSERT INTO catalog_meta(key,value) VALUES('schema_version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(CATALOG_SCHEMA_VERSION),),
                )
                db.commit()
            finally:
                db.close()

    @staticmethod
    def _create_lecture_table(db: sqlite3.Connection, name: str) -> None:
        if name not in {"catalog_lectures", "catalog_lectures_v2"}:
            raise ValueError("invalid catalog table name")
        db.execute(
            f"""CREATE TABLE {name} (
                sub_id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL REFERENCES catalog_courses(course_id) ON DELETE CASCADE,
                sub_title TEXT NOT NULL DEFAULT '', date TEXT NOT NULL DEFAULT '',
                has_playback INTEGER NOT NULL DEFAULT 1,
                metadata_json TEXT NOT NULL DEFAULT '{{}}', updated_at TEXT NOT NULL
            )"""
        )

    def schema_version(self) -> int:
        with self._lock:
            db = self._connect()
            try:
                row = db.execute("SELECT value FROM catalog_meta WHERE key='schema_version'").fetchone()
                return int(row[0]) if row else 0
            finally:
                db.close()

    @staticmethod
    def _upsert_course(db: sqlite3.Connection, course_id: str, item: dict[str, Any]) -> None:
        db.execute(
            """INSERT INTO catalog_courses
            (course_id,title,teacher,term,department,authorization_state,updated_at)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(course_id) DO UPDATE SET
            title=excluded.title,teacher=excluded.teacher,term=excluded.term,
            department=excluded.department,authorization_state=excluded.authorization_state,
            updated_at=excluded.updated_at""",
            (str(course_id), str(item.get("title") or course_id), str(item.get("teacher") or ""),
             str(item.get("term") or ""), str(item.get("department") or ""),
             str(item.get("authorization_state") or "unknown"), str(item.get("updated_at") or _now())),
        )

    def upsert_course(self, course_id: str, title: str, teacher: str = "", *, term: str = "",
                      department: str = "", authorization_state: str = "verified") -> None:
        with self._lock:
            db = self._connect()
            try:
                self._upsert_course(db, str(course_id), {"title": title, "teacher": teacher,
                    "term": term, "department": department, "authorization_state": authorization_state})
                db.commit()
            finally:
                db.close()

    @classmethod
    def _upsert_lecture(cls, db: sqlite3.Connection, course_id: str, lecture: dict[str, Any], **fields: Any) -> None:
        cid = str(course_id or lecture.get("course_id") or "")
        sub_id = str(lecture.get("sub_id") or "")
        if not cid or not sub_id:
            raise ValueError("course_id and sub_id are required")
        if db.execute("SELECT 1 FROM catalog_courses WHERE course_id=?", (cid,)).fetchone() is None:
            cls._upsert_course(db, cid, {"title": lecture.get("course_title") or cid})
        current = db.execute("SELECT * FROM catalog_lectures WHERE sub_id=?", (sub_id,)).fetchone()
        merged = dict(current) if current else {}
        merged.update(lecture)
        merged.update({key: value for key, value in fields.items() if value is not None})
        db.execute(
            """INSERT INTO catalog_lectures
            (sub_id,course_id,sub_title,date,has_playback,metadata_json,updated_at)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(sub_id) DO UPDATE SET
            course_id=excluded.course_id,sub_title=excluded.sub_title,date=excluded.date,
            has_playback=excluded.has_playback,metadata_json=excluded.metadata_json,
            updated_at=excluded.updated_at""",
            (sub_id, cid, str(merged.get("sub_title") or ""), str(merged.get("date") or ""),
             int(bool(merged.get("has_playback", True))),
             json.dumps(cls._extra_fields(merged), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
             str(merged.get("updated_at") or _now())),
        )

    @staticmethod
    def _extra_fields(value: dict[str, Any]) -> dict[str, Any]:
        known = {
            "sub_id", "course_id", "sub_title", "date", "has_playback",
            "updated_at", "metadata_json", *RETIRED_LECTURE_FIELDS,
        }
        result: dict[str, Any] = {}
        raw = value.get("metadata_json")
        if isinstance(raw, str):
            try:
                decoded = json.loads(raw)
                if isinstance(decoded, dict):
                    result.update(decoded)
            except json.JSONDecodeError:
                pass
        result.update({str(key): item for key, item in value.items() if key not in known})
        return result

    def upsert_lecture(self, course_id: str, lecture: dict[str, Any]) -> None:
        with self._lock:
            db = self._connect()
            try:
                self._upsert_lecture(db, course_id, lecture)
                db.commit()
            finally:
                db.close()

    def replace_authorized_catalog(self, courses: list[dict[str, Any]]) -> None:
        """Atomically replace the identity-scoped catalog after full verification."""
        if len(courses) > 100:
            raise ValueError("authorized catalog exceeds the course limit")
        normalized: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = []
        seen_courses: set[str] = set()
        seen_lectures: set[str] = set()
        for raw in courses:
            # 夜10-C T17：非 dict 条目曾以 dict() 内部消息逃逸闭集文案
            # （仍是 ValueError，调用面安全；与相邻分支同一句式收口）。
            if not isinstance(raw, dict):
                raise ValueError("authorized catalog contains an invalid or duplicate course")
            item = dict(raw)
            course_id = str(item.get("course_id") or "").strip()
            if not course_id or course_id in seen_courses:
                raise ValueError("authorized catalog contains an invalid or duplicate course")
            if str(item.get("authorization_state") or "") != "verified":
                raise ValueError("authorized catalog contains an unverified course")
            seen_courses.add(course_id)
            lectures: list[dict[str, Any]] = []
            for raw_lecture in item.get("lectures") or []:
                lecture = dict(raw_lecture or {})
                sub_id = str(lecture.get("sub_id") or "").strip()
                if not sub_id or sub_id in seen_lectures:
                    raise ValueError("authorized catalog contains an invalid or duplicate lecture")
                seen_lectures.add(sub_id)
                lectures.append(lecture)
            normalized.append((course_id, item, lectures))

        with self._lock:
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM catalog_lectures")
                db.execute("DELETE FROM catalog_courses")
                for course_id, item, lectures in normalized:
                    self._upsert_course(db, course_id, item)
                    for lecture in lectures:
                        self._upsert_lecture(db, course_id, lecture)
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["has_playback"] = bool(result.get("has_playback"))
        raw = result.pop("metadata_json", "{}")
        try:
            extra = json.loads(raw or "{}")
            if isinstance(extra, dict):
                for key, value in extra.items():
                    result.setdefault(str(key), value)
        except (TypeError, json.JSONDecodeError):
            pass
        return result

    def get_lecture(self, sub_id: str) -> dict[str, Any] | None:
        with self._lock:
            db = self._connect()
            try:
                return self._row(db.execute("SELECT * FROM catalog_lectures WHERE sub_id=?", (str(sub_id),)).fetchone())
            finally:
                db.close()

    def update_lecture_fields(self, sub_id: str, **fields: Any) -> None:
        with self._lock:
            db = self._connect()
            try:
                current = db.execute("SELECT * FROM catalog_lectures WHERE sub_id=?", (str(sub_id),)).fetchone()
                if current is None:
                    raise KeyError(f"Lecture {sub_id} is not in catalog")
                self._upsert_lecture(db, str(current["course_id"]), dict(current), **fields)
                db.commit()
            finally:
                db.close()

    def courses(self) -> list[dict[str, Any]]:
        with self._lock:
            db = self._connect()
            try:
                return [dict(row) for row in db.execute("SELECT * FROM catalog_courses ORDER BY course_id").fetchall()]
            finally:
                db.close()

    def courses_for_ids(self, course_ids: set[str]) -> list[dict[str, Any]]:
        """Load one bounded identity-scoped catalog with a single connection."""
        values = sorted({str(value) for value in course_ids if str(value)})
        if not values:
            return []
        if len(values) > 100:
            raise ValueError("authorized catalog exceeds the course limit")
        placeholders = ",".join("?" for _ in values)
        with self._lock:
            db = self._connect()
            try:
                courses = [dict(row) for row in db.execute(
                    f"SELECT * FROM catalog_courses WHERE course_id IN ({placeholders}) ORDER BY course_id",
                    values,
                ).fetchall()]
                lectures = [self._row(row) for row in db.execute(
                    f"SELECT * FROM catalog_lectures WHERE course_id IN ({placeholders}) "
                    "ORDER BY course_id,date,sub_title",
                    values,
                ).fetchall()]
            finally:
                db.close()
        by_course: dict[str, list[dict[str, Any]]] = {}
        for lecture in lectures:
            if lecture is not None:
                by_course.setdefault(str(lecture.get("course_id") or ""), []).append(lecture)
        return [
            {**course, "lectures": by_course.get(str(course.get("course_id") or ""), [])}
            for course in courses
        ]

    def lectures_for_course(self, course_id: str) -> list[dict[str, Any]]:
        with self._lock:
            db = self._connect()
            try:
                rows = db.execute("SELECT * FROM catalog_lectures WHERE course_id=? ORDER BY date,sub_title", (str(course_id),)).fetchall()
                return [self._row(row) for row in rows if row is not None]
            finally:
                db.close()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            db = self._connect()
            try:
                courses = [dict(row) for row in db.execute(
                    "SELECT * FROM catalog_courses ORDER BY course_id"
                ).fetchall()]
                lectures = [self._row(row) for row in db.execute(
                    "SELECT * FROM catalog_lectures ORDER BY course_id,date,sub_title"
                ).fetchall()]
            finally:
                db.close()
        return {"version": CATALOG_SCHEMA_VERSION, "storage_layout_version": CATALOG_SCHEMA_VERSION,
                "courses": {str(item["course_id"]): item for item in courses},
                "lectures": {str(item["sub_id"]): item for item in lectures if item is not None}}

    def lecture_course_pairs(self) -> dict[str, dict[str, str]]:
        """Read-only sub_id → lecture identity map for the course-data inventory."""
        with self._lock:
            db = self._connect()
            try:
                rows = db.execute(
                    "SELECT sub_id,course_id,sub_title,date FROM catalog_lectures"
                ).fetchall()
            finally:
                db.close()
        return {
            str(row["sub_id"]): {
                "course_id": str(row["course_id"]),
                "sub_title": str(row["sub_title"] or ""),
                "date": str(row["date"] or ""),
            }
            for row in rows
        }

    def close(self) -> None:
        """Kept for service symmetry; connections are transaction-scoped."""


__all__ = ["CATALOG_SCHEMA_VERSION", "CatalogRepository"]
