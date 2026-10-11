"""Read-only course-data inventory for the data management workspace.

Aggregates the user's local course data without ever reading content columns
or file contents: one GROUP BY pass per table in the two SQLite databases,
plus a bounded stat-only walk over the three closed-namespace artifact
directories (``documents/<document_id>/``, ``artifacts/subtitles/<key>/``,
``courseware/<lec-key>/``).  Output shapes follow the frozen
``courselens.course-data-summary.v1`` / ``courselens.course-data-lecture-page.v1``
/ ``courselens.course-data-action-result.v1`` key sets; unknown or empty
values are omitted, never fabricated.

Credentials, Fudan sessions, GitHub tokens, and DeepSeek keys are permanently
outside this domain and are never surfaced by this module.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any


SUMMARY_SCHEMA = "courselens.course-data-summary.v1"
LECTURE_PAGE_SCHEMA = "courselens.course-data-lecture-page.v1"
ACTION_RESULT_SCHEMA = "courselens.course-data-action-result.v1"

# Frozen category closed set (contract ledger §2).  ``orphan_artifacts`` is a
# file-namespace aggregate, not a per-course category, and is reported in its
# own summary key.
COURSE_DATA_CATEGORIES = (
    "progress", "transcript", "ppt", "artifacts", "documents", "references",
    "timeline", "search", "bookmarks", "quizzes", "review", "tasks", "automation",
)

# Frozen action closed set (contract ledger §2).
COURSE_DATA_ACTIONS = (
    "rebuild-search", "purge-derived", "remove-copies", "delete-records", "export",
    "release-stuck", "export-study-stats",
)

# Machine-readable lifecycle blocker codes; each maps to one existing closed
# store probe (find_active / active_remote_run_count /
# active_automation_import_count / migration_cleanup_pending_count /
# automation_course_rule_counts).
COURSE_DATA_BLOCKER_CODES = (
    "active_task", "active_remote_run", "active_automation_import",
    "automation_rule", "cleanup_pending",
)

# Whole-file byte accounting: SQLite file sizes include indexes and free
# pages; per-course byte values are text-volume proxies, not file sizes.
BYTE_BASIS = "sqlite_file_bytes_including_indexes_and_free_pages"

MAX_SCAN_DIRECTORIES = 4096
MAX_SUMMARY_PAGE_SIZE = 200
MAX_LECTURE_PAGE_SIZE = 50

# Local task kinds that occupy a lecture (application-level closed set).
ACTIVE_TASK_KINDS = ("subtitle", "summary")

# Task kinds recorded against a comma-joined multi-course id instead of one
# course (application-level closed set).  They are operation history, not
# per-course data, so their aggregates belong to the unattributed bucket and
# must never surface as a fabricated orphan course row.
CROSS_COURSE_TASK_KINDS = {"search_answer", "concept_analysis"}

_DOCUMENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ARTIFACT_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_COURSEWARE_KEY_RE = re.compile(r"^lec-[0-9a-f]{16}$")

_EMPTY_AGGREGATE = {"count": 0, "text_bytes": 0, "last_updated_at": 0.0}

FILE_NAMESPACES = ("documents", "subtitles", "courseware")


def subtitle_artifact_key(course_id: str, sub_id: str) -> str:
    """Forward-derive the on-disk subtitle artifact key for one lecture."""
    return hashlib.sha256(f"{course_id}:{sub_id}".encode("utf-8")).hexdigest()


def courseware_lecture_key(course_id: str, sub_id: str) -> str:
    """Forward-derive the on-disk courseware directory key for one lecture."""
    digest = hashlib.sha256(
        f"courseware-pdf.v1\0{course_id}\0{sub_id}".encode("utf-8")
    ).hexdigest()
    return f"lec-{digest[:16]}"


def _aggregate(count: int = 0, text_bytes: int = 0, last_updated_at: float = 0.0) -> dict[str, Any]:
    return {"count": int(count), "text_bytes": int(text_bytes), "last_updated_at": float(last_updated_at or 0.0)}


def _merge_aggregate(destination: dict[str, Any], source: dict[str, Any]) -> None:
    destination["count"] = int(destination.get("count") or 0) + int(source.get("count") or 0)
    destination["text_bytes"] = int(destination.get("text_bytes") or 0) + int(source.get("text_bytes") or 0)
    destination["last_updated_at"] = max(
        float(destination.get("last_updated_at") or 0.0),
        float(source.get("last_updated_at") or 0.0),
    )


class CourseDataInventory:
    """Compose read-only store aggregates into the frozen inventory shapes."""

    def __init__(self, *, learning_store, catalog_repository, task_store, data_root=None):
        self.learning_store = learning_store
        self.catalog_repository = catalog_repository
        self.task_store = task_store
        self.data_root = Path(data_root) if data_root is not None else Path(learning_store.path).parent

    # -- public shapes -------------------------------------------------------

    def summary(self, *, page: int = 1, page_size: int = 50, include_orphans: bool = False) -> dict[str, Any]:
        """Build one ``course-data-summary.v1`` payload (read-only)."""
        state = self._collect()
        rows = state["rows"]
        if not include_orphans:
            rows = [row for row in rows if row["in_catalog"]]
        total = len(rows)
        safe_page = max(1, int(page))
        safe_size = min(max(1, int(page_size)), MAX_SUMMARY_PAGE_SIZE)
        start = (safe_page - 1) * safe_size
        value: dict[str, Any] = {
            "schema": SUMMARY_SCHEMA,
            "generated_at": time.time(),
            "byte_basis": BYTE_BASIS,
            "database_bytes": self._database_bytes(),
            "orphan_artifacts": dict(state["file_stats"]["orphans"]),
            "page": {"page": safe_page, "page_size": safe_size, "total": total},
            "rows": rows[start:start + safe_size],
        }
        if state["unattributed"]:
            value["unattributed"] = {"categories": state["unattributed"]}
        if state["file_stats"].get("scan_truncated"):
            value["scan_truncated"] = True
        return value

    def lecture_page(self, course_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """Build one ``course-data-lecture-page.v1`` payload (read-only)."""
        state = self._collect()
        course_id = str(course_id or "")
        course_state = state["courses"].get(course_id, {})
        in_catalog = course_id in state["catalog_courses"]
        lectures = list(state["lectures_by_course"].get(course_id, []))
        total = len(lectures)
        safe_limit = min(max(1, int(limit)), MAX_LECTURE_PAGE_SIZE)
        safe_offset = max(0, int(offset))
        value: dict[str, Any] = {
            "schema": LECTURE_PAGE_SCHEMA,
            "course_id": course_id,
            "in_catalog": in_catalog,
            "total": total,
            "page": {"limit": safe_limit, "offset": safe_offset},
            "lectures": lectures[safe_offset:safe_offset + safe_limit],
        }
        categories = self._pruned_categories(course_state.get("categories", {}))
        if categories:
            value["categories"] = categories
        return value

    def lifecycle_blockers(
        self, *, sub_ids: tuple[str, ...] | list[str] = (), course_ids: tuple[str, ...] | list[str] = (),
    ) -> list[dict[str, Any]]:
        """Machine-readable blockers from the existing closed store probes."""
        blockers: list[dict[str, Any]] = []
        targeted_subs = list(dict.fromkeys(str(value) for value in sub_ids if str(value)))
        busy_subs = sorted({
            sub_id
            for sub_id in targeted_subs
            for kind in ACTIVE_TASK_KINDS
            if self.task_store.find_active(kind, sub_id) is not None
        })
        if busy_subs:
            blockers.append({"code": "active_task", "count": len(busy_subs), "sub_ids": busy_subs})
        remote_runs = int(self.task_store.active_remote_run_count() or 0)
        if remote_runs > 0:
            blockers.append({"code": "active_remote_run", "count": remote_runs})
        imports = int(self.task_store.active_automation_import_count() or 0)
        if imports > 0:
            blockers.append({"code": "active_automation_import", "count": imports})
        cleanup = int(self.task_store.migration_cleanup_pending_count() or 0)
        if cleanup > 0:
            blockers.append({"code": "cleanup_pending", "count": cleanup})
        targeted_courses = list(dict.fromkeys(str(value) for value in course_ids if str(value)))
        rules = self.task_store.automation_course_rule_counts()
        rule_courses = sorted(course for course in targeted_courses if rules.get(course))
        if rule_courses:
            blockers.append({
                "code": "automation_rule",
                "count": sum(int(rules[course]["count"]) for course in rule_courses),
                "course_ids": rule_courses,
            })
        return blockers

    def action_result(
        self,
        action: str,
        *,
        accepted: bool,
        operation_id: str,
        blockers: list[dict[str, Any]] | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Shape one ``course-data-action-result.v1`` payload (no execution)."""
        if action not in COURSE_DATA_ACTIONS:
            raise ValueError("unknown course-data action")
        value: dict[str, Any] = {
            "schema": ACTION_RESULT_SCHEMA,
            "action": action,
            "operation_id": str(operation_id or ""),
            "status": "accepted" if accepted else "rejected",
        }
        normalized = []
        for blocker in blockers or []:
            code = str((blocker or {}).get("code") or "")
            if code not in COURSE_DATA_BLOCKER_CODES:
                raise ValueError("unknown course-data blocker code")
            normalized.append({"code": code, "count": int((blocker or {}).get("count") or 0)})
        if normalized:
            value["blockers"] = normalized
        if result is not None:
            value["result"] = result
        return value

    # -- aggregation internals ----------------------------------------------

    def _database_bytes(self) -> dict[str, Any]:
        state = self._path_bytes(self.catalog_repository.path)
        learning = self._path_bytes(self.learning_store.path)
        return {
            "state_db": state,
            "learning_db": learning,
            "total": state + learning,
        }

    @staticmethod
    def _path_bytes(path: Path) -> int:
        total = 0
        for candidate in (Path(path), Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            try:
                total += int(candidate.stat().st_size)
            except OSError:
                continue
        return total

    def _collect(self) -> dict[str, Any]:
        """Run every read-only aggregate once and compose course/lecture rows."""
        learning = self.learning_store.course_data_category_aggregates()
        pairs = self.catalog_repository.lecture_course_pairs()
        courses_meta = {
            str(row.get("course_id") or ""): dict(row)
            for row in self.catalog_repository.courses()
            if str(row.get("course_id") or "")
        }
        task_aggregates = self.task_store.course_task_aggregates()
        cross_course_tasks = {
            pair: aggregate
            for pair, aggregate in task_aggregates.items()
            if str(aggregate.get("kind") or "") in CROSS_COURSE_TASK_KINDS
        }
        task_aggregates = {
            pair: aggregate
            for pair, aggregate in task_aggregates.items()
            if pair not in cross_course_tasks
        }
        automation_rules = self.task_store.automation_course_rule_counts()

        course_ids = set(courses_meta)
        course_ids.update(course for course, _ in learning["course_sub"])
        course_ids.update(course for course, _ in task_aggregates)
        course_ids.update(automation_rules)

        sub_course: dict[str, str] = {}
        for sub_id, lecture in pairs.items():
            sub_course.setdefault(str(sub_id), str(lecture.get("course_id") or ""))
        for course, sub in learning["course_sub"]:
            sub_course.setdefault(str(sub), str(course))
        for course, sub in task_aggregates:
            sub_course.setdefault(str(sub), str(course))

        courses: dict[str, dict[str, Any]] = {
            course_id: {"categories": {}, "sub_ids": set()} for course_id in course_ids
        }
        unattributed: dict[str, dict[str, Any]] = {}

        def bucket(course_id: str) -> dict[str, Any]:
            if not course_id:
                return {"categories": unattributed, "sub_ids": set()}
            return courses.setdefault(course_id, {"categories": {}, "sub_ids": set()})

        for (course_id, sub_id), categories in learning["course_sub"].items():
            target = bucket(str(course_id))
            target["sub_ids"].add(str(sub_id))
            for category, aggregate in categories.items():
                _merge_aggregate(target["categories"].setdefault(category, dict(_EMPTY_AGGREGATE)), aggregate)
        for sub_id, categories in learning["sub"].items():
            target = bucket(sub_course.get(str(sub_id), ""))
            target["sub_ids"].add(str(sub_id))
            for category, aggregate in categories.items():
                _merge_aggregate(target["categories"].setdefault(category, dict(_EMPTY_AGGREGATE)), aggregate)
        for category, aggregate in learning["global"].items():
            _merge_aggregate(unattributed.setdefault(category, dict(_EMPTY_AGGREGATE)), aggregate)
        for (course_id, sub_id), aggregate in task_aggregates.items():
            target = bucket(str(course_id))
            target["sub_ids"].add(str(sub_id))
            _merge_aggregate(target["categories"].setdefault("tasks", dict(_EMPTY_AGGREGATE)), aggregate)
        # Cross-course records carry a comma-joined course id; they merge into
        # the unattributed bucket beside review plans, never into a course row.
        for aggregate in cross_course_tasks.values():
            _merge_aggregate(unattributed.setdefault("tasks", dict(_EMPTY_AGGREGATE)), aggregate)
        for course_id, aggregate in automation_rules.items():
            target = bucket(str(course_id))
            _merge_aggregate(target["categories"].setdefault("automation", dict(_EMPTY_AGGREGATE)), aggregate)

        file_stats = self._file_stats(pairs, learning, task_aggregates)

        # POLISH-1 F8b（化身走查 F8b）：热点核对面——「没听懂」标记计数随行
        # 出场（additive 可选字段，不进冻结 categories 闭集）；>0 才写字段，
        # 前端「存在则上屏」。缺表/零行不出现该键，绝不虚构。
        not_understood = self.learning_store.not_understood_bookmark_counts()

        lecture_counts: dict[str, int] = {}
        for lecture in pairs.values():
            course_id = str(lecture.get("course_id") or "")
            if course_id:
                lecture_counts[course_id] = lecture_counts.get(course_id, 0) + 1

        lectures_by_course = self._lecture_rows(pairs, sub_course, learning, task_aggregates, file_stats)

        rows: list[dict[str, Any]] = []
        for course_id in sorted(courses):
            state = courses[course_id]
            in_catalog = course_id in courses_meta
            row: dict[str, Any] = {
                "course_id": course_id,
                "in_catalog": in_catalog,
                "categories": self._pruned_categories(state["categories"]),
                "file_bytes": self._course_file_bytes(file_stats, course_id),
            }
            row["total_file_bytes"] = int(sum(row["file_bytes"].values()))
            hotspot_count = int(not_understood.get(course_id, 0))
            if hotspot_count > 0:
                row["not_understood_count"] = hotspot_count
            # AVATAR-POLISH-1 UP-G1（化身走查 S1 观察升级项）：页头「数据 X」的
            # 逐课去向——随行补「本课文字存量」合计（与 categories 同源的
            # text_bytes 相加，additive 可选字段，不进冻结 categories 闭集）；
            # >0 才写字段，前端「存在则上屏」。口径=存进学习库的文字字节，与页头
            # sqlite 文件字节（含索引/空闲页，见 BYTE_BASIS）不同基，前端按
            # 「约」呈现，不声称与页头总数逐字对账。
            stored_text_bytes = sum(
                int(aggregate.get("text_bytes") or 0)
                for aggregate in state["categories"].values()
            )
            if stored_text_bytes > 0:
                row["stored_text_bytes"] = stored_text_bytes
            if in_catalog:
                meta = courses_meta[course_id]
                row["title"] = str(meta.get("title") or course_id)
                row["teacher"] = str(meta.get("teacher") or "")
                row["lecture_count"] = int(lecture_counts.get(course_id, 0))
            rows.append(row)
        rows.sort(key=lambda row: (not row["in_catalog"], row["course_id"]))
        return {
            "rows": rows,
            "courses": courses,
            "catalog_courses": courses_meta,
            "lectures_by_course": lectures_by_course,
            "unattributed": {
                category: aggregate
                for category, aggregate in unattributed.items()
                if int(aggregate.get("count") or 0) > 0
            },
            "file_stats": file_stats,
        }

    def _lecture_rows(
        self,
        pairs: dict[str, dict[str, str]],
        sub_course: dict[str, str],
        learning: dict[str, Any],
        task_aggregates: dict[tuple[str, str], dict[str, Any]],
        file_stats: dict[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        per_course: dict[str, list[dict[str, Any]]] = {}
        seen: set[tuple[str, str]] = set()
        for sub_id, lecture in sorted(pairs.items(), key=lambda item: (item[1].get("date") or "", item[0])):
            course_id = str(lecture.get("course_id") or "")
            if not course_id:
                continue
            seen.add((course_id, str(sub_id)))
            row: dict[str, Any] = {
                "sub_id": str(sub_id),
                "in_catalog": True,
                "title": str(lecture.get("sub_title") or ""),
                "date": str(lecture.get("date") or ""),
                "file_bytes": self._lecture_file_bytes(file_stats, course_id, str(sub_id)),
            }
            row["total_file_bytes"] = int(sum(row["file_bytes"].values()))
            per_course.setdefault(course_id, []).append(row)

        extra: dict[tuple[str, str], None] = {}
        for course_id, sub_id in learning["course_sub"]:
            if (course_id, sub_id) not in seen:
                extra[(course_id, sub_id)] = None
        for course_id, sub_id in task_aggregates:
            if (course_id, sub_id) not in seen:
                extra[(course_id, sub_id)] = None
        for sub_id, course_id in sub_course.items():
            if course_id and (course_id, sub_id) not in seen:
                extra[(course_id, sub_id)] = None
        for course_id, sub_id in sorted(extra):
            row = {
                "sub_id": str(sub_id),
                "in_catalog": False,
                "file_bytes": self._lecture_file_bytes(file_stats, course_id, str(sub_id)),
            }
            row["total_file_bytes"] = int(sum(row["file_bytes"].values()))
            per_course.setdefault(course_id, []).append(row)
        return per_course

    @staticmethod
    def _pruned_categories(categories: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Omit empty categories; keep the frozen three-field shape otherwise."""
        pruned: dict[str, dict[str, Any]] = {}
        for category in COURSE_DATA_CATEGORIES:
            aggregate = categories.get(category)
            if not aggregate or int(aggregate.get("count") or 0) <= 0:
                continue
            pruned[category] = _aggregate(
                count=int(aggregate.get("count") or 0),
                text_bytes=int(aggregate.get("text_bytes") or 0),
                last_updated_at=float(aggregate.get("last_updated_at") or 0.0),
            )
        return pruned

    @staticmethod
    def _course_file_bytes(file_stats: dict[str, Any], course_id: str) -> dict[str, int]:
        totals = {namespace: 0 for namespace in FILE_NAMESPACES}
        for (owner_course, _sub), namespaces in file_stats["by_course_sub"].items():
            if owner_course != course_id:
                continue
            for namespace, size in namespaces.items():
                totals[namespace] = totals.get(namespace, 0) + int(size)
        return {namespace: size for namespace, size in totals.items() if size > 0}

    @staticmethod
    def _lecture_file_bytes(file_stats: dict[str, Any], course_id: str, sub_id: str) -> dict[str, int]:
        namespaces = file_stats["by_course_sub"].get((course_id, sub_id), {})
        return {namespace: int(size) for namespace, size in namespaces.items() if int(size) > 0}

    def _file_stats(
        self,
        pairs: dict[str, dict[str, str]],
        learning: dict[str, Any],
        task_aggregates: dict[tuple[str, str], dict[str, Any]],
    ) -> dict[str, Any]:
        """Bounded stat-only walk over the three closed-namespace roots."""
        known_pairs: set[tuple[str, str]] = {
            (str(lecture.get("course_id") or ""), str(sub_id))
            for sub_id, lecture in pairs.items()
            if str(lecture.get("course_id") or "")
        }
        known_pairs.update(learning["course_sub"])
        known_pairs.update(task_aggregates)
        by_course_sub: dict[tuple[str, str], dict[str, int]] = {}
        orphans = {"directories": 0, "bytes": 0}
        truncated = False

        subtitle_owner = {
            subtitle_artifact_key(course, sub): (course, sub) for course, sub in known_pairs
        }
        courseware_owner = {
            courseware_lecture_key(course, sub): (course, sub) for course, sub in known_pairs
        }

        def record(owner: tuple[str, str] | None, namespace: str, size: int) -> None:
            if owner is None:
                orphans["directories"] += 1
                orphans["bytes"] += int(size)
                return
            buckets = by_course_sub.setdefault(owner, {})
            buckets[namespace] = buckets.get(namespace, 0) + int(size)

        namespaces = (
            (self.data_root / "documents", _DOCUMENT_ID_RE, "documents",
             lambda name: learning["document_ids"].get(name)),
            (self.data_root / "artifacts" / "subtitles", _ARTIFACT_KEY_RE, "subtitles",
             lambda name: subtitle_owner.get(name)),
            (self.data_root / "courseware", _COURSEWARE_KEY_RE, "courseware",
             lambda name: courseware_owner.get(name)),
        )
        for root, key_pattern, namespace, owner_of in namespaces:
            scanned = 0
            try:
                entries = list(os.scandir(root))
            except OSError:
                continue
            resolved_root = root.resolve()
            for entry in entries:
                scanned += 1
                if scanned > MAX_SCAN_DIRECTORIES:
                    truncated = True
                    break
                if not key_pattern.match(entry.name) or not entry.is_dir(follow_symlinks=False):
                    record(None, namespace, 0)
                    continue
                size, safe = self._directory_file_bytes(Path(entry.path), resolved_root)
                if not safe:
                    record(None, namespace, 0)
                else:
                    record(owner_of(entry.name), namespace, size)

        return {"by_course_sub": by_course_sub, "orphans": orphans, "scan_truncated": truncated}

    @staticmethod
    def _directory_file_bytes(directory: Path, namespace_root: Path) -> tuple[int, bool]:
        """Stat-only byte sum of one artifact directory's direct files."""
        try:
            resolved = directory.resolve()
            resolved.relative_to(namespace_root)
        except (ValueError, OSError):
            return 0, False
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return 0, False
        total = 0
        for entry in entries:
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                total += int(entry.stat(follow_symlinks=False).st_size)
            except OSError:
                continue
        return total, True


__all__ = [
    "ACTION_RESULT_SCHEMA",
    "ACTIVE_TASK_KINDS",
    "BYTE_BASIS",
    "COURSE_DATA_ACTIONS",
    "COURSE_DATA_BLOCKER_CODES",
    "COURSE_DATA_CATEGORIES",
    "CROSS_COURSE_TASK_KINDS",
    "FILE_NAMESPACES",
    "LECTURE_PAGE_SCHEMA",
    "MAX_LECTURE_PAGE_SIZE",
    "MAX_SUMMARY_PAGE_SIZE",
    "SUMMARY_SCHEMA",
    "CourseDataInventory",
    "courseware_lecture_key",
    "subtitle_artifact_key",
]
