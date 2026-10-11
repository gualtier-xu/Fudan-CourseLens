"""Identity-scoped Fudan timetable acquisition, normalization and export.

The implementation is intentionally local to the private desktop client.  It
uses the already authenticated WebVPN session and never sends timetable data
to the public Worker or Mailbox repositories.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
FRESH_SECONDS = 6 * 60 * 60
STALE_SECONDS = 30 * 24 * 60 * 60
MAX_WEEK = 30

UNDERGRADUATE_PAGE = "https://fdjwgl.fudan.edu.cn/student/for-std/course-table"
UNDERGRADUATE_DATA = (
    "https://fdjwgl.fudan.edu.cn/student/for-std/course-table/semester/{semester_id}/print-data"
)
POSTGRADUATE_DATA = (
    "http://yjsxktest.fudan.sh.cn/yjsxkapp/sys/xsxkappfudan/xsxkCourse/loadKbxx.do?_={timestamp}"
)

SLOT_STARTS = (
    "08:00", "08:55", "09:55", "10:50", "11:45",
    "13:30", "14:25", "15:25", "16:20", "17:15",
    "18:30", "19:25", "20:20", "21:15", "22:10",
)
SLOT_ENDS = (
    "08:45", "09:40", "10:40", "11:35", "12:30",
    "14:15", "15:10", "16:10", "17:05", "18:00",
    "19:15", "20:10", "21:05", "22:00", "22:55",
)


class TimetableError(RuntimeError):
    def __init__(self, code: str, message: str = "Timetable operation failed"):
        super().__init__(message)
        self.code = str(code)


@dataclass(frozen=True)
class Semester:
    semester_id: str
    label: str
    start_date: str
    source: str = "fudan_undergraduate"
    is_default: bool = False
    selectable: bool = True

    def public(self) -> dict[str, Any]:
        return {
            "semester_id": self.semester_id,
            "label": self.label,
            "start_date": self.start_date,
            "source": self.source,
            "is_default": self.is_default,
            "selectable": self.selectable,
        }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load_json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _normalized_title(value: Any) -> str:
    return re.sub(r"[\s\u3000]+", "", _text(value)).casefold()


def _teacher_values(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else re.split(r"[,，、/;；]+", _text(value))
    result: list[str] = []
    for item in values:
        teacher = re.sub(r"\s+", "", _text(item)).casefold()
        if teacher and teacher not in result:
            result.append(teacher)
    return result


def _semester_key(value: Any) -> str:
    text = _text(value).casefold()
    if not text:
        return ""
    years = re.findall(r"20\d{2}", text)
    season = ""
    if any(token in text for token in ("spring", "春", "第二学期", "第2学期")):
        season = "2"
    elif any(token in text for token in ("fall", "autumn", "秋", "第一学期", "第1学期")):
        season = "1"
    trailing = re.search(r"(?:^|[-_/\s])([12])(?:$|[-_/\s])", text)
    if not season and trailing:
        season = trailing.group(1)
    if years:
        return "-".join(years[:2] + ([season] if season else []))
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text)


def _parse_iso_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(_text(value)[:10])
    except ValueError:
        return None


def _semester_label(item: dict[str, Any]) -> str:
    for key in ("name", "label", "fullName", "schoolYear"):
        value = _text(item.get(key))
        if value:
            if key == "schoolYear":
                season = _text(item.get("name") or item.get("season") or item.get("code"))
                return f"{value} {season}".strip()
            return value
    calendar_year = _text(item.get("calendarYear"))
    season = _text(item.get("season"))
    return f"{calendar_year} {season}".strip() or _text(item.get("id"))


def _decode_script_string(value: str) -> str:
    """Decode one quoted JavaScript string without evaluating page code."""
    output: list[str] = []
    index = 0
    escapes = {
        "'": "'", '"': '"', "\\": "\\", "/": "/",
        "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
    }
    while index < len(value):
        character = value[index]
        if character != "\\":
            output.append(character)
            index += 1
            continue
        index += 1
        if index >= len(value):
            raise ValueError("unterminated script string escape")
        escaped = value[index]
        if escaped in escapes:
            output.append(escapes[escaped])
            index += 1
            continue
        if escaped == "u" and index + 4 < len(value):
            digits = value[index + 1:index + 5]
            if not re.fullmatch(r"[0-9A-Fa-f]{4}", digits):
                raise ValueError("invalid Unicode escape")
            output.append(chr(int(digits, 16)))
            index += 5
            continue
        if escaped in {"\r", "\n"}:
            if escaped == "\r" and index + 1 < len(value) and value[index + 1] == "\n":
                index += 1
            index += 1
            continue
        raise ValueError("unsupported script string escape")
    return "".join(output)


def _json_parse_assignment(page: str, names: tuple[str, ...]) -> Any | None:
    for name in names:
        pattern = (
            rf"(?<![$.\w])(?:(?:var|let|const)\s+)?{re.escape(name)}\s*=\s*"
            r"JSON\.parse\(\s*(?P<quote>['\"])(?P<encoded>(?:\\.|(?!(?P=quote))[\s\S])*)"
            r"(?P=quote)\s*\)\s*;?"
        )
        match = re.search(pattern, page)
        if not match:
            continue
        try:
            return json.loads(_decode_script_string(match.group("encoded")))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TimetableError("timetable_payload_invalid", f"{name} metadata is invalid") from exc
    return None


def parse_undergraduate_semesters(html: str) -> tuple[list[Semester], str]:
    page = str(html or "")
    raw = _json_parse_assignment(page, ("allSemesters", "semesters"))
    if raw is None:
        raise TimetableError("timetable_payload_invalid", "Semester metadata is missing")
    current = _json_parse_assignment(page, ("currentSemester",))
    default_id = _text(current.get("id") if isinstance(current, dict) else current)
    option_tags = re.findall(r"<option\b[^>]*>", page, re.I)
    selected_option = next((
        tag for tag in option_tags
        if re.search(r"\bselected(?:\s*=|\s|>)", tag, re.I)
    ), "")
    fallback_option = selected_option or (option_tags[0] if option_tags else "")
    option_value = re.search(r"\bvalue\s*=\s*[\"']([^\"']+)", fallback_option, re.I)
    if not default_id:
        default_id = _text(option_value.group(1) if option_value else "")
    semesters: list[Semester] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        semester_id = _text(item.get("id"))
        if not semester_id:
            continue
        start = _parse_iso_date(item.get("startDate"))
        if start is not None:
            start += timedelta(days=1)
        semesters.append(Semester(
            semester_id=semester_id,
            label=_semester_label(item),
            start_date=start.isoformat() if start else "",
            is_default=semester_id == default_id,
        ))
    if not semesters:
        raise TimetableError("timetable_payload_invalid", "No semester metadata was returned")
    if default_id and not any(item.semester_id == default_id for item in semesters):
        raise TimetableError("timetable_payload_invalid", "Current semester metadata is inconsistent")
    if not default_id:
        default_id = semesters[0].semester_id
        semesters[0] = Semester(**{**semesters[0].__dict__, "is_default": True})
    return semesters, default_id


def _course_id(source: str, semester_id: str, values: Iterable[Any]) -> str:
    digest = hashlib.sha256("\x1f".join(_text(value) for value in values).encode("utf-8")).hexdigest()[:20]
    return f"{source}:{semester_id}:{digest}"


def _valid_units(start: Any, end: Any | None = None) -> tuple[int, int]:
    try:
        first = int(start)
        last = int(end if end is not None else start)
    except (TypeError, ValueError) as exc:
        raise TimetableError("timetable_payload_invalid", "Course slot is invalid") from exc
    if first < 1 or last < first or last > len(SLOT_STARTS):
        raise TimetableError("timetable_payload_invalid", "Course slot is out of range")
    return first, last


def _valid_weekday(value: Any) -> int:
    try:
        weekday = int(value)
    except (TypeError, ValueError) as exc:
        raise TimetableError("timetable_payload_invalid", "Course weekday is invalid") from exc
    if weekday < 1 or weekday > 7:
        raise TimetableError("timetable_payload_invalid", "Course weekday is out of range")
    return weekday


def _weeks(value: Any) -> list[int]:
    values = value if isinstance(value, (list, tuple)) else []
    result = sorted({int(item) for item in values if str(item).isdigit() and 1 <= int(item) <= MAX_WEEK})
    return result


def parse_undergraduate_payload(payload: Any, semester: Semester) -> list[dict[str, Any]]:
    try:
        activities = payload["studentTableVms"][0]["activities"]
    except (KeyError, IndexError, TypeError) as exc:
        raise TimetableError("timetable_payload_invalid", "Undergraduate timetable schema is invalid") from exc
    if not isinstance(activities, list):
        # 夜10-C T16：activities=None 曾以裸 TypeError 逃逸闭集（调用方兜底把
        # 其误诊成 upstream_unavailable）；与研究生解析器同纪律，fail-closed。
        raise TimetableError("timetable_payload_invalid", "Undergraduate timetable schema is invalid")
    records: list[dict[str, Any]] = []
    for activity in activities:
        if not isinstance(activity, dict):
            continue
        weekday = _valid_weekday(activity.get("weekday"))
        start_unit, end_unit = _valid_units(activity.get("startUnit"), activity.get("endUnit"))
        week_indexes = _weeks(activity.get("weekIndexes"))
        title = _text(activity.get("courseName"))
        if not title or not week_indexes:
            continue
        lesson_id = _text(activity.get("lessonId"))
        course_code = _text(activity.get("lessonCode"))
        teachers = [_text(value) for value in activity.get("teachers", []) if _text(value)]
        room = _text(activity.get("room"))
        records.append({
            "timetable_course_id": _course_id("ug", semester.semester_id, (lesson_id, course_code, title)),
            "source": "fudan_undergraduate",
            "semester_id": semester.semester_id,
            "semester_label": semester.label,
            "semester_start_date": semester.start_date,
            "lesson_id": lesson_id,
            "course_code": course_code,
            "title": title,
            "teachers": teachers,
            "room": room,
            "week_indexes": week_indexes,
            "meetings": [{"weekday": weekday, "start_unit": start_unit, "end_unit": end_unit}],
        })
    return merge_course_records(records)


def _pg_weeks(value: Any) -> list[int]:
    bits = _text(value)
    return [index + 1 for index, flag in enumerate(bits[:MAX_WEEK]) if flag == "1"]


def parse_postgraduate_payload(
    payload: Any, *, semester_id: str, semester_label: str = "", start_date: str = ""
) -> list[dict[str, Any]]:
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise TimetableError("timetable_payload_invalid", "Postgraduate timetable schema is invalid")
    records: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        title = _text(item.get("KCMC"))
        week_indexes = _pg_weeks(item.get("ZCBH"))
        if not title or not week_indexes:
            continue
        weekday = _valid_weekday(item.get("XQ"))
        start_unit, end_unit = _valid_units(item.get("KSJCDM"))
        teacher = _text(item.get("JSXM"))
        room = _text(item.get("JASMC"))
        course_code = _text(item.get("KCH") or item.get("KCDM"))
        records.append({
            "timetable_course_id": _course_id("pg", semester_id, (course_code, title, teacher)),
            "source": "fudan_postgraduate",
            "semester_id": semester_id,
            "semester_label": semester_label or semester_id,
            "semester_start_date": start_date,
            "lesson_id": "",
            "course_code": course_code,
            "title": title,
            "teachers": [teacher] if teacher else [],
            "room": room,
            "week_indexes": week_indexes,
            "meetings": [{"weekday": weekday, "start_unit": start_unit, "end_unit": end_unit}],
        })
    return merge_course_records(records)


def merge_course_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in records:
        key = (
            record.get("source"), record.get("semester_id"), record.get("course_code"),
            _normalized_title(record.get("title")), tuple(_teacher_values(record.get("teachers"))),
            _text(record.get("room")), tuple(record.get("week_indexes") or []),
        )
        current = grouped.setdefault(key, {**record, "meetings": []})
        current["meetings"].extend(dict(value) for value in record.get("meetings") or [])
    output: list[dict[str, Any]] = []
    for current in grouped.values():
        meetings = sorted(current["meetings"], key=lambda item: (item["weekday"], item["start_unit"], item["end_unit"]))
        merged: list[dict[str, int]] = []
        for meeting in meetings:
            if merged and merged[-1]["weekday"] == meeting["weekday"] and merged[-1]["end_unit"] + 1 >= meeting["start_unit"]:
                merged[-1]["end_unit"] = max(merged[-1]["end_unit"], meeting["end_unit"])
            elif meeting not in merged:
                merged.append(meeting)
        current["meetings"] = merged
        output.append(current)
    output.sort(key=lambda item: (_normalized_title(item.get("title")), item.get("source", ""), item.get("timetable_course_id", "")))
    return output


class TimetableStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS timetable_snapshots (
                    identity_scope TEXT NOT NULL,
                    source TEXT NOT NULL,
                    semester_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY(identity_scope, source, semester_id)
                );
                CREATE INDEX IF NOT EXISTS idx_timetable_snapshots_identity
                    ON timetable_snapshots(identity_scope, observed_at DESC);
                CREATE TABLE IF NOT EXISTS timetable_preferences (
                    identity_scope TEXT PRIMARY KEY,
                    selected_semester_id TEXT NOT NULL DEFAULT '',
                    semester_start_dates_json TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL
                );
                """
            )

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def put(self, identity_scope: str, source: str, semester_id: str, payload: dict[str, Any]) -> None:
        now = float(payload.get("observed_at") or time.time())
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO timetable_snapshots(
                       identity_scope,source,semester_id,payload_json,observed_at,expires_at
                   ) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(identity_scope,source,semester_id) DO UPDATE SET
                       payload_json=excluded.payload_json,observed_at=excluded.observed_at,
                       expires_at=excluded.expires_at""",
                (identity_scope, source, semester_id, _json(payload), now, now + FRESH_SECONDS),
            )

    def list(self, identity_scope: str, semester_id: str = "") -> list[dict[str, Any]]:
        query = "SELECT * FROM timetable_snapshots WHERE identity_scope=?"
        params: list[Any] = [identity_scope]
        if semester_id:
            query += " AND semester_id=?"
            params.append(semester_id)
        query += " ORDER BY observed_at DESC,source"
        with self._lock, self._connect() as db:
            rows = db.execute(query, params).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["payload"] = _load_json(item.pop("payload_json", ""), {})
            output.append(item)
        return output

    def preferences(self, identity_scope: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM timetable_preferences WHERE identity_scope=?", (identity_scope,)
            ).fetchone()
        if row is None:
            return {"selected_semester_id": "", "semester_start_dates": {}}
        value = dict(row)
        value["semester_start_dates"] = _load_json(value.pop("semester_start_dates_json", ""), {})
        return value

    def update_preferences(
        self, identity_scope: str, *, selected_semester_id: str | None = None,
        semester_id: str = "", start_date: str = "",
    ) -> dict[str, Any]:
        value = self.preferences(identity_scope)
        if selected_semester_id is not None:
            value["selected_semester_id"] = _text(selected_semester_id)
        starts = dict(value.get("semester_start_dates") or {})
        if semester_id and start_date:
            starts[_text(semester_id)] = _text(start_date)
        value["semester_start_dates"] = starts
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO timetable_preferences(
                       identity_scope,selected_semester_id,semester_start_dates_json,updated_at
                   ) VALUES(?,?,?,?)
                   ON CONFLICT(identity_scope) DO UPDATE SET
                       selected_semester_id=excluded.selected_semester_id,
                       semester_start_dates_json=excluded.semester_start_dates_json,
                       updated_at=excluded.updated_at""",
                (identity_scope, value.get("selected_semester_id", ""), _json(starts), now),
            )
        return value


def reconcile_courses(courses: list[dict[str, Any]], catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for course in courses:
        term_key = _semester_key(course.get("semester_label") or course.get("semester_id"))
        title_key = _normalized_title(course.get("title"))
        teachers = set(_teacher_values(course.get("teachers")))
        candidates = []
        if term_key and title_key and teachers:
            for item in catalog:
                catalog_term = _semester_key(item.get("term") or item.get("semester") or item.get("term_name"))
                if catalog_term != term_key or _normalized_title(item.get("title")) != title_key:
                    continue
                if teachers.intersection(_teacher_values(item.get("teacher"))):
                    candidates.append(item)
        linked = {
            "state": "linked" if len(candidates) == 1 else "ambiguous" if len(candidates) > 1 else "unlinked",
            "course_id": str(candidates[0].get("course_id") or "") if len(candidates) == 1 else "",
            "candidate_course_ids": sorted(str(item.get("course_id") or "") for item in candidates) if len(candidates) > 1 else [],
        }
        output.append({**course, "catalog_link": linked})
    return output


def _meeting_times(meeting: dict[str, Any]) -> tuple[str, str]:
    start = int(meeting["start_unit"])
    end = int(meeting["end_unit"])
    return SLOT_STARTS[start - 1], SLOT_ENDS[end - 1]


def _occurrences(courses: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for course in courses:
        start_date = _parse_iso_date(course.get("semester_start_date"))
        if start_date is None:
            continue
        for week in course.get("week_indexes") or []:
            for meeting in course.get("meetings") or []:
                day = start_date + timedelta(days=(int(week) - 1) * 7 + int(meeting["weekday"]) - 1)
                start_text, end_text = _meeting_times(meeting)
                starts_at = datetime.combine(day, datetime.strptime(start_text, "%H:%M").time(), SHANGHAI)
                ends_at = datetime.combine(day, datetime.strptime(end_text, "%H:%M").time(), SHANGHAI)
                output.append({
                    "meeting_id": hashlib.sha256(
                        f"{course['timetable_course_id']}:{week}:{meeting['weekday']}:{meeting['start_unit']}:{meeting['end_unit']}".encode()
                    ).hexdigest()[:24],
                    "timetable_course_id": course["timetable_course_id"],
                    "catalog_course_id": course.get("catalog_link", {}).get("course_id", ""),
                    "source": course.get("source", ""),
                    "semester_id": course.get("semester_id", ""),
                    "title": course.get("title", ""),
                    "teachers": list(course.get("teachers") or []),
                    "room": course.get("room", ""),
                    "course_code": course.get("course_code", ""),
                    "week": int(week),
                    "weekday": int(meeting["weekday"]),
                    "start_unit": int(meeting["start_unit"]),
                    "end_unit": int(meeting["end_unit"]),
                    "date": day.isoformat(),
                    "start_time": start_text,
                    "end_time": end_text,
                    "starts_at": starts_at.isoformat(),
                    "ends_at": ends_at.isoformat(),
                })
    output.sort(key=lambda item: (item["starts_at"], item["ends_at"], item["title"], item["meeting_id"]))
    return output


def _assign_conflicts(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_day: dict[str, list[dict[str, Any]]] = {}
    for value in values:
        by_day.setdefault(value["date"], []).append(value)
    for day_values in by_day.values():
        groups: list[list[dict[str, Any]]] = []
        for value in sorted(day_values, key=lambda item: (item["start_unit"], item["end_unit"], item["title"])):
            overlapping = [group for group in groups if any(
                value["start_unit"] <= other["end_unit"] and other["start_unit"] <= value["end_unit"]
                for other in group
            )]
            if overlapping:
                group = overlapping[0]
                group.append(value)
                for extra in overlapping[1:]:
                    group.extend(extra)
                    groups.remove(extra)
            else:
                groups.append([value])
        for index, group in enumerate(groups, start=1):
            count = len(group)
            group_id = f"{group[0]['date']}:{index}" if count > 1 else ""
            for lane, value in enumerate(sorted(group, key=lambda item: (item["start_unit"], item["title"]))):
                value["conflict_group"] = group_id
                value["conflict_lane"] = lane
                value["conflict_count"] = count
    return values


def _ics_escape(value: Any) -> str:
    return _text(value).replace("\\", "\\\\").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def render_ics(snapshot: dict[str, Any]) -> str:
    courses = list(snapshot.get("courses") or [])
    if not courses or any(not _parse_iso_date(course.get("semester_start_date")) for course in courses):
        raise TimetableError("semester_start_required", "Semester start date is required for calendar export")
    observed = datetime.fromtimestamp(float(snapshot.get("observed_at") or 0), SHANGHAI)
    stamp = observed.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//CourseLens//Student Timetable//ZH-CN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:CourseLens 课程表",
        "X-WR-TIMEZONE:Asia/Shanghai", "BEGIN:VTIMEZONE", "TZID:Asia/Shanghai",
        "BEGIN:STANDARD", "DTSTART:19700101T000000", "TZOFFSETFROM:+0800",
        "TZOFFSETTO:+0800", "TZNAME:CST", "END:STANDARD", "END:VTIMEZONE",
    ]
    for item in _occurrences(courses):
        start = datetime.fromisoformat(item["starts_at"]).strftime("%Y%m%dT%H%M%S")
        end = datetime.fromisoformat(item["ends_at"]).strftime("%Y%m%dT%H%M%S")
        uid = hashlib.sha256(
            f"{item['source']}:{item['semester_id']}:{item['meeting_id']}".encode("utf-8")
        ).hexdigest() + "@courselens.local"
        description = "教师：" + "、".join(item.get("teachers") or [])
        if item.get("course_code"):
            description += "\n课程代码：" + item["course_code"]
        lines.extend([
            "BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}",
            f"DTSTART;TZID=Asia/Shanghai:{start}", f"DTEND;TZID=Asia/Shanghai:{end}",
            f"SUMMARY:{_ics_escape(item['title'])}", f"LOCATION:{_ics_escape(item.get('room'))}",
            f"DESCRIPTION:{_ics_escape(description)}", "STATUS:CONFIRMED", "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


class TimetableRuntime:
    def __init__(
        self,
        db_path: str | Path,
        *,
        identity_getter: Callable[[], str],
        auth_getter: Callable[[], dict[str, Any]],
        vpn_getter: Callable[[], Any],
        catalog_getter: Callable[[], list[dict[str, Any]]],
    ):
        self.store = TimetableStore(db_path)
        self.identity_getter = identity_getter
        self.auth_getter = auth_getter
        self.vpn_getter = vpn_getter
        self.catalog_getter = catalog_getter
        self._refresh_lock = threading.RLock()

    def _identity(self) -> str:
        value = _text(self.identity_getter())
        if not value:
            raise TimetableError("timetable_login_required", "Fudan authentication is required")
        return value

    def _require_auth(self) -> str:
        if self.auth_getter().get("state") != "ready":
            raise TimetableError("timetable_login_required", "Fudan authentication is required")
        return self._identity()

    @staticmethod
    def _response_json(response: Any, source: str) -> Any:
        status = int(getattr(response, "status_code", 0) or 0)
        if status in {401, 403}:
            raise TimetableError("timetable_session_expired", f"{source} session expired")
        if status < 200 or status >= 300:
            raise TimetableError("timetable_upstream_unavailable", f"{source} returned status {status}")
        headers = getattr(response, "headers", {})
        content_type = str(
            headers.get("content-type", "") or headers.get("Content-Type", "") or ""
        ).casefold()
        if "text/html" in content_type:
            raise TimetableError("timetable_upstream_unavailable", f"{source} returned an HTML gateway page")
        try:
            return response.json()
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TimetableError("timetable_payload_invalid", f"{source} returned invalid JSON") from exc

    @staticmethod
    def _undergraduate_sso_handoff(page: str) -> str:
        expected = urlparse(UNDERGRADUATE_PAGE)
        for candidate in re.findall(r"https://[^\"'<>\s]+", html.unescape(str(page or ""))):
            try:
                parsed = urlparse(candidate)
                query = parse_qs(parsed.query, keep_blank_values=True)
            except ValueError:
                continue
            ticket = query.get("ticket") or []
            refer = query.get("refer") or []
            if (
                parsed.scheme == "https"
                and parsed.hostname == expected.hostname
                and parsed.path == "/student/sso/login"
                and parsed.username is None
                and parsed.password is None
                and not parsed.fragment
                and set(query) == {"refer", "ticket"}
                and len(ticket) == 1
                and len(refer) == 1
                and refer[0] == UNDERGRADUATE_PAGE
                and 16 <= len(ticket[0]) <= 512
                and not any(character in ticket[0] for character in "\r\n")
            ):
                return candidate
        raise TimetableError("timetable_payload_invalid", "Undergraduate SSO handoff is invalid")

    def refresh(self, semester_id: str = "") -> dict[str, Any]:
        identity = self._require_auth()
        with self._refresh_lock:
            vpn = self.vpn_getter()
            preferences = self.store.preferences(identity)
            selected = _text(semester_id or preferences.get("selected_semester_id"))
            failures: list[dict[str, str]] = []
            semesters: list[Semester] = []
            default_id = ""
            try:
                response = vpn.get_allowed(UNDERGRADUATE_PAGE, allow_redirects=True, timeout=30)
                if int(response.status_code) in {401, 403}:
                    raise TimetableError("timetable_session_expired")
                page = str(response.text or "")
                try:
                    semesters, default_id = parse_undergraduate_semesters(page)
                except TimetableError:
                    handoff = self._undergraduate_sso_handoff(page)
                    response = vpn.get_allowed(handoff, allow_redirects=True, timeout=30)
                    if int(response.status_code) in {401, 403}:
                        raise TimetableError("timetable_session_expired")
                    response = vpn.get_allowed(
                        UNDERGRADUATE_PAGE, allow_redirects=True, timeout=30
                    )
                    if int(response.status_code) in {401, 403}:
                        raise TimetableError("timetable_session_expired")
                    semesters, default_id = parse_undergraduate_semesters(response.text)
            except Exception as exc:
                failures.append({
                    "source": "fudan_undergraduate",
                    "code": str(getattr(exc, "code", "timetable_upstream_unavailable")),
                })
            if selected == "current" and default_id:
                selected = default_id
            elif not selected:
                selected = default_id or "current"
            semester = next((item for item in semesters if item.semester_id == selected), None)
            stored_start = _text((preferences.get("semester_start_dates") or {}).get(selected))
            if semester is None:
                semester = Semester(selected, selected, stored_start, source="local", is_default=True)
            elif stored_start and not semester.start_date:
                semester = Semester(**{**semester.__dict__, "start_date": stored_start})
            self.store.update_preferences(identity, selected_semester_id=selected)

            successes = 0
            if semesters and any(item.semester_id == selected for item in semesters):
                try:
                    response = vpn.get_allowed(
                        UNDERGRADUATE_DATA.format(semester_id=selected), allow_redirects=True, timeout=30
                    )
                    courses = parse_undergraduate_payload(
                        self._response_json(response, "fudan_undergraduate"), semester
                    )
                    observed = time.time()
                    self.store.put(identity, "fudan_undergraduate", selected, {
                        "source": "fudan_undergraduate", "semester": semester.public(),
                        "semesters": [item.public() for item in semesters], "courses": courses,
                        "observed_at": observed, "partial_failures": [],
                    })
                    successes += 1
                except Exception as exc:
                    failures.append({
                        "source": "fudan_undergraduate",
                        "code": str(getattr(exc, "code", "timetable_upstream_unavailable")),
                    })

            if selected in {default_id, "current"} or not semesters:
                try:
                    response = vpn.get_allowed(
                        POSTGRADUATE_DATA.format(timestamp=int(time.time() * 1000)),
                        allow_redirects=True, timeout=30,
                    )
                    courses = parse_postgraduate_payload(
                        self._response_json(response, "fudan_postgraduate"),
                        semester_id=selected,
                        semester_label=semester.label,
                        start_date=semester.start_date or stored_start,
                    )
                    observed = time.time()
                    self.store.put(identity, "fudan_postgraduate", selected, {
                        "source": "fudan_postgraduate", "semester": semester.public(),
                        "semesters": [semester.public()], "courses": courses,
                        "observed_at": observed, "partial_failures": [],
                    })
                    successes += 1
                except Exception as exc:
                    failures.append({
                        "source": "fudan_postgraduate",
                        "code": str(getattr(exc, "code", "timetable_upstream_unavailable")),
                    })
            if not successes and not self.store.list(identity, selected):
                code = failures[0]["code"] if failures else "timetable_upstream_unavailable"
                raise TimetableError(code)
            return self.snapshot(selected, 0, refresh_failures=failures)

    def set_semester_start(self, semester_id: str, start_date: str) -> dict[str, Any]:
        identity = self._require_auth()
        semester_id = _text(semester_id)
        parsed = _parse_iso_date(start_date)
        if not semester_id or parsed is None or parsed.weekday() != 0:
            raise TimetableError("semester_start_invalid", "Semester start must be a Monday")
        self.store.update_preferences(identity, selected_semester_id=semester_id, semester_id=semester_id, start_date=parsed.isoformat())
        for row in self.store.list(identity, semester_id):
            payload = dict(row["payload"])
            semester = dict(payload.get("semester") or {})
            semester["start_date"] = parsed.isoformat()
            payload["semester"] = semester
            payload["courses"] = [
                {**course, "semester_start_date": parsed.isoformat()}
                for course in payload.get("courses") or []
            ]
            payload["observed_at"] = float(row.get("observed_at") or time.time())
            self.store.put(identity, row["source"], semester_id, payload)
        return self.snapshot(semester_id, 0)

    def snapshot(
        self, semester_id: str = "", week: int = 0, *,
        refresh_failures: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        auth = self.auth_getter()
        now = time.time()
        if auth.get("state") != "ready":
            return {
                "state": "action_required", "source": "local", "observed_at": now,
                "expires_at": 0.0, "code": "timetable_login_required", "actions": ["login"],
                "semesters": [], "selected_semester": None, "selected_week": None,
                "days": [], "courses": [], "current_meeting": None, "next_meeting": None,
                "counts": {"courses": None, "meetings": None, "conflicts": None, "linked": None, "ambiguous": None, "unlinked": None},
            }
        identity = self._identity()
        preferences = self.store.preferences(identity)
        all_rows = self.store.list(identity)
        known_semesters: dict[str, dict[str, Any]] = {}
        for row in all_rows:
            for item in row["payload"].get("semesters") or []:
                sid = _text(item.get("semester_id"))
                if sid:
                    known_semesters[sid] = {**known_semesters.get(sid, {}), **item}
            item = dict(row["payload"].get("semester") or {})
            sid = _text(item.get("semester_id") or row.get("semester_id"))
            if sid:
                known_semesters[sid] = {**known_semesters.get(sid, {}), **item}
        selected = _text(semester_id or preferences.get("selected_semester_id"))
        if not selected and known_semesters:
            selected = next((sid for sid, item in known_semesters.items() if item.get("is_default")), next(iter(known_semesters)))
        rows = self.store.list(identity, selected) if selected else []
        visible_rows = [row for row in rows if now - float(row.get("observed_at") or 0) <= STALE_SECONDS]
        if not visible_rows:
            return {
                "state": "empty", "source": "local", "observed_at": now, "expires_at": 0.0,
                "code": "timetable_not_loaded", "actions": ["refresh"],
                "partial_failures": list(refresh_failures or []),
                "semesters": sorted(known_semesters.values(), key=lambda item: _text(item.get("label")), reverse=True),
                "selected_semester": known_semesters.get(selected), "selected_week": None,
                "days": [], "courses": [], "current_meeting": None, "next_meeting": None,
                "counts": {"courses": 0, "meetings": 0, "conflicts": 0, "linked": 0, "ambiguous": 0, "unlinked": 0},
            }
        observed_at = max(float(row.get("observed_at") or 0) for row in visible_rows)
        expires_at = min(float(row.get("expires_at") or 0) for row in visible_rows)
        courses = merge_course_records(
            course for row in visible_rows for course in row["payload"].get("courses") or []
        )
        starts = dict(preferences.get("semester_start_dates") or {})
        selected_semester = dict(known_semesters.get(selected) or {})
        start_date = _text(selected_semester.get("start_date") or starts.get(selected))
        if start_date:
            selected_semester["start_date"] = start_date
            courses = [{**course, "semester_start_date": course.get("semester_start_date") or start_date} for course in courses]
        courses = reconcile_courses(courses, list(self.catalog_getter() or []))
        occurrences = _occurrences(courses)
        now_dt = datetime.now(SHANGHAI)
        current = next((item for item in occurrences if datetime.fromisoformat(item["starts_at"]) <= now_dt < datetime.fromisoformat(item["ends_at"])), None)
        next_meeting = next((item for item in occurrences if datetime.fromisoformat(item["starts_at"]) > now_dt), None)
        start = _parse_iso_date(start_date)
        current_week = 1
        if start is not None:
            current_week = max(1, min(MAX_WEEK, ((now_dt.date() - start).days // 7) + 1))
        selected_week = int(week or current_week)
        if selected_week < 1 or selected_week > MAX_WEEK:
            raise TimetableError("timetable_week_invalid", "Week must be between 1 and 30")
        week_occurrences = _assign_conflicts([dict(item) for item in occurrences if item["week"] == selected_week])
        week_start = start + timedelta(days=(selected_week - 1) * 7) if start else None
        days = []
        for weekday in range(1, 8):
            day_date = week_start + timedelta(days=weekday - 1) if week_start else None
            days.append({
                "weekday": weekday,
                "date": day_date.isoformat() if day_date else "",
                "meetings": [item for item in week_occurrences if item["weekday"] == weekday],
            })
        failures = list(refresh_failures or [])
        stale = now > expires_at
        missing_start = bool(courses and not start)
        source_names = sorted({str(row.get("source") or "") for row in visible_rows if row.get("source")})
        state = "action_required" if missing_start else "degraded" if stale or failures else "ready"
        code = "semester_start_required" if missing_start else "timetable_stale" if stale else "timetable_partial" if failures else "timetable_verified"
        conflict_groups = {item["conflict_group"] for item in week_occurrences if item.get("conflict_group")}
        link_counts = {name: sum(1 for item in courses if item.get("catalog_link", {}).get("state") == name) for name in ("linked", "ambiguous", "unlinked")}
        return {
            "state": state, "source": "+".join(source_names) or "local",
            "observed_at": observed_at, "expires_at": expires_at, "code": code,
            "actions": ["set-semester-start", "refresh"] if missing_start else ["refresh", "export-ics"],
            "partial_failures": failures,
            "semesters": sorted(known_semesters.values(), key=lambda item: (_text(item.get("start_date")), _text(item.get("label"))), reverse=True),
            "selected_semester": selected_semester or {"semester_id": selected, "label": selected, "start_date": start_date},
            "current_week": current_week, "selected_week": selected_week,
            "week_start": week_start.isoformat() if week_start else "",
            "week_end": (week_start + timedelta(days=6)).isoformat() if week_start else "",
            "days": days, "courses": courses, "current_meeting": current, "next_meeting": next_meeting,
            "slots": [{"unit": index + 1, "start": SLOT_STARTS[index], "end": SLOT_ENDS[index]} for index in range(len(SLOT_STARTS))],
            "counts": {
                "courses": len(courses), "meetings": len(week_occurrences), "conflicts": len(conflict_groups),
                **link_counts,
            },
        }

    def enrich_catalog(self, courses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.auth_getter().get("state") != "ready":
            return courses
        try:
            snapshot = self.snapshot()
        except TimetableError:
            return courses
        linked_by_id: dict[str, list[dict[str, Any]]] = {}
        occurrences = _occurrences(snapshot.get("courses") or [])
        now = datetime.now(SHANGHAI)
        for timetable_course in snapshot.get("courses") or []:
            link = timetable_course.get("catalog_link") or {}
            course_id = _text(link.get("course_id"))
            if link.get("state") == "linked" and course_id:
                next_value = next((item for item in occurrences if item["catalog_course_id"] == course_id and datetime.fromisoformat(item["starts_at"]) > now), None)
                linked_by_id.setdefault(course_id, []).append({
                    "link_state": "linked", "course_code": timetable_course.get("course_code", ""),
                    "teachers": timetable_course.get("teachers", []), "room": timetable_course.get("room", ""),
                    "rooms": sorted({timetable_course.get("room", "")} - {""}),
                    "week_indexes": timetable_course.get("week_indexes", []),
                    "meetings": timetable_course.get("meetings", []), "next_meeting": next_value,
                })
        output = []
        for item in courses:
            matches = linked_by_id.get(_text(item.get("course_id")), [])
            if len(matches) == 1:
                output.append({**item, "timetable": matches[0]})
            elif len(matches) > 1:
                output.append({**item, "timetable": {"link_state": "ambiguous"}})
            else:
                output.append(item)
        return output

    def export_ics(self, semester_id: str = "") -> tuple[str, str]:
        self._require_auth()
        snapshot = self.snapshot(semester_id, 1)
        content = render_ics(snapshot)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", _text(snapshot.get("selected_semester", {}).get("semester_id"))) or "current"
        return f"CourseLens-{safe}.ics", content


__all__ = [
    "MAX_WEEK", "POSTGRADUATE_DATA", "SLOT_ENDS", "SLOT_STARTS",
    "UNDERGRADUATE_DATA", "UNDERGRADUATE_PAGE", "Semester", "TimetableError",
    "TimetableRuntime", "TimetableStore", "merge_course_records",
    "parse_postgraduate_payload", "parse_undergraduate_payload",
    "parse_undergraduate_semesters", "reconcile_courses", "render_ics",
]
