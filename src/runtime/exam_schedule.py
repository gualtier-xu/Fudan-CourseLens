"""Deterministic, read-only Fudan exam-arrangement context for review plans.

The exam context is an additive descriptor on the existing local review-plan
flow; it never creates a second review mode.  The parser understands sanitized
HTML shaped like the university exam-arrangement page
(``/student/for-std/exam-arrange/info/{studentId}``) with an independent
stdlib implementation.  Raw HTML, student ids, account identifiers, and row
notes are parsed at most in memory and are never persisted, logged, or
returned in artifacts.

State semantics (frozen contract):
    active only when ``0 <= exam_at - now <= 720h`` in Asia/Shanghai.
    The window is evaluated against the exam end instant so an exam that has
    already started stays ``active`` (rather than flapping to
    ``outside_window``) until it is ``passed``; the 720h gate itself is never
    widened.  For date precision the exam day counts as active until the end
    of that day because the source does not expose an exam time.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")

ACTIVE_WINDOW_HOURS = 720
ACTIVE_WINDOW = timedelta(hours=ACTIVE_WINDOW_HOURS)

EXAM_ARRANGE_URL = "https://fdjwgl.fudan.edu.cn/student/for-std/exam-arrange/info/{student_id}"

EXAM_CONTEXT_VERSION = "exam-context-v1"

EXAM_SOURCES = ("fudan_jwgl", "user_confirmed", "none")
EXAM_PRECISIONS = ("datetime", "date", "none")
EXAM_STATES = ("unavailable", "outside_window", "active", "passed")
DEADLINE_CONTEXTS = ("none", "active")
PLAN_STRATEGIES = ("coverage", "weak_first", "wrong_first", "mixed")

# Closed-set reasons recorded alongside ``exam_state``; never free-form text.
CONTEXT_DETAILS = (
    "matched", "user_confirmed", "session_unavailable", "fetch_failed",
    "no_rows", "no_match", "ambiguous", "malformed",
)

_STUDENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RANGE_RE = re.compile(r"^(\d{1,2}):(\d{2})~(\d{1,2}):(\d{2})$")

_MAX_TEXT = 200
_MAX_ROWS = 200
_SEMESTER_DAYS = 30 * 7


class ReviewPlanValidationError(ValueError):
    """Fail-closed review-plan validation with a closed-set error code."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = str(code)


def _text(value: Any) -> str:
    """NFKC-normalized, whitespace-collapsed text, bounded for storage."""
    collapsed = " ".join(str(value or "").split())
    return unicodedata.normalize("NFKC", collapsed)[:_MAX_TEXT]


def _normalize_identity(value: Any) -> str:
    return _text(value).casefold()


def _strip_full_width_parens(value: str) -> str:
    # NFKC normalization has already folded full-width parentheses to ASCII.
    if len(value) >= 2 and value[0] in "（(" and value[-1] in "）)":
        return value[1:-1].strip()
    return value


def _parse_time_range(value: str) -> tuple[time, time] | None:
    match = _TIME_RANGE_RE.match(str(value or ""))
    if not match:
        return None
    start_hour, start_minute, end_hour, end_minute = (int(group) for group in match.groups())
    try:
        start = time(start_hour, start_minute)
        end = time(end_hour, end_minute)
    except ValueError:
        return None
    if end <= start:
        return None
    return start, end


class _ExamTableParser(HTMLParser):
    """Collect exam rows from ``table.exam-table`` shaped HTML.

    Row layout follows the university page semantics: the first cell holds a
    ``div.time`` whose text is ``YYYY-MM-DD HH:MM~HH:MM`` and whose cell spans
    carry the location (third span); the second cell's first ``div`` spans
    carry course name, course code, and the full-width-parenthesized test
    category, while its second ``div`` span carries the exam type; the third
    cell holds the note; the status cell is ignored.  Rows marked
    ``data-finished="true"`` and empty placeholder rows are skipped.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, Any]] = []
        # One flag per open table: True when inside (or nested inside) an
        # exam-table, so unrelated page tables cannot poison the depth.
        self._table_stack: list[bool] = []
        self._tbody_depth = 0
        self._row: dict[str, Any] | None = None
        # Per-cell capture state.
        self._cell_index = 0
        self._in_time_div = False
        self._time_div_text: list[str] = []
        self._cell_spans: list[str] = []
        self._cell_text: list[str] = []
        self._course_div_index = -1
        self._course_div_spans: list[list[str]] = []
        self._capture_target: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {str(key).casefold(): str(value or "") for key, value in attrs}
        classes = set(attributes.get("class", "").split())
        if tag == "table":
            inside = bool(self._table_stack and self._table_stack[-1]) or "exam-table" in classes
            self._table_stack.append(inside)
            return
        if not (self._table_stack and self._table_stack[-1]):
            return
        if tag == "tbody":
            self._tbody_depth += 1
            return
        if tag == "tr":
            if self._tbody_depth <= 0:
                return
            if "tr-empty" in classes or attributes.get("data-finished", "").casefold() == "true":
                self._row = None
                return
            self._row = {}
            self._cell_index = 0
            return
        if self._row is None:
            return
        if tag == "td":
            self._cell_index += 1
            self._in_time_div = False
            self._time_div_text = []
            self._cell_spans = []
            self._cell_text = []
            self._course_div_index = -1
            self._course_div_spans = []
            self._capture_target = None
            return
        if self._cell_index == 1 and tag == "div":
            if "time" in classes:
                self._in_time_div = True
            else:
                self._course_div_index += 1
                self._course_div_spans.append([])
            return
        if self._cell_index == 2 and tag == "div":
            self._course_div_index += 1
            self._course_div_spans.append([])
            return
        if tag == "span" and self._cell_index in (1, 2):
            if self._in_time_div or self._course_div_index < 0:
                self._capture_target = self._cell_spans
            else:
                self._capture_target = self._course_div_spans[self._course_div_index]
            self._capture_target.append("")
        return

    def handle_data(self, data: str) -> None:
        if self._row is None:
            return
        if self._capture_target is not None:
            self._capture_target[-1] += data
        elif self._in_time_div and self._cell_index == 1:
            self._time_div_text.append(data)
        elif self._cell_index >= 2:
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self._table_stack:
                self._table_stack.pop()
            return
        if not (self._table_stack and self._table_stack[-1]):
            return
        if tag == "tbody":
            self._tbody_depth = max(0, self._tbody_depth - 1)
            return
        if tag == "tr":
            if self._row is not None:
                self._finish_row()
            self._row = None
            return
        if self._row is None:
            return
        if tag == "span":
            self._capture_target = None
            return
        if tag == "div" and self._in_time_div:
            self._in_time_div = False
            return
        if tag == "td":
            self._finish_cell()

    def _finish_cell(self) -> None:
        if self._cell_index == 1:
            self._row["time_div_text"] = _text("".join(self._time_div_text))
            self._row["cell_spans"] = [_text(span) for span in self._cell_spans if _text(span)]
        elif self._cell_index == 2:
            self._row["course_div_spans"] = [
                [_text(span) for span in group if _text(span)]
                for group in self._course_div_spans
            ]
        elif self._cell_index == 3:
            self._row["note"] = _text("".join(self._cell_text))

    def _finish_row(self) -> None:
        row = self._row
        if not isinstance(row, dict):
            return
        time_text = str(row.get("time_div_text") or "")
        spans = [str(span) for span in row.get("cell_spans") or []]
        course_groups = [[str(span) for span in group] for group in row.get("course_div_spans") or []]
        first_div = course_groups[0] if course_groups else []
        second_div = course_groups[1] if len(course_groups) > 1 else []
        self.rows.append({
            "date": time_text,
            "location": spans[2] if len(spans) > 2 else "",
            "course_name": first_div[0] if len(first_div) > 0 else "",
            "course_code": first_div[1] if len(first_div) > 1 else "",
            "category": _strip_full_width_parens(first_div[2]) if len(first_div) > 2 else "",
            "exam_type": second_div[0] if len(second_div) > 0 else "",
            "note": str(row.get("note") or ""),
        })


def parse_exam_arrangement(html_text: str) -> list[dict[str, str]]:
    """Parse sanitized exam-arrangement HTML into bounded row dicts.

    Malformed rows (bad date or time shapes) are dropped entirely so they can
    never become fuzzy match candidates.  Notes are parsed for completeness
    but callers must not persist them.
    """
    parser = _ExamTableParser()
    try:
        parser.feed(str(html_text or ""))
        parser.close()
    except Exception:
        return []
    rows: list[dict[str, str]] = []
    for raw in parser.rows[:_MAX_ROWS]:
        date_and_time = str(raw.get("date") or "")
        parts = date_and_time.split()
        if not parts or not _DATE_RE.match(parts[0]):
            continue
        try:
            parsed_date = date.fromisoformat(parts[0])
        except ValueError:
            continue
        time_range = parts[1] if len(parts) > 1 else ""
        clocks = _parse_time_range(time_range) if time_range else None
        if time_range and clocks is None:
            continue
        start_text = f"{clocks[0].hour:02d}:{clocks[0].minute:02d}" if clocks else ""
        end_text = f"{clocks[1].hour:02d}:{clocks[1].minute:02d}" if clocks else ""
        rows.append({
            "date": parsed_date.isoformat(),
            "start_time": start_text,
            "end_time": end_text,
            "course_code": _text(raw.get("course_code")),
            "course_name": _text(raw.get("course_name")),
            "exam_type": _text(raw.get("exam_type")),
            "category": _text(raw.get("category")),
            "location": _text(raw.get("location")),
            "note": _text(raw.get("note")),
        })
    return rows


def exam_state_for_window(exam_start: datetime, exam_end: datetime, now: datetime) -> str:
    """Closed-set state for one exam interval relative to aware ``now``."""
    if now > exam_end:
        return "passed"
    if exam_start - now > ACTIVE_WINDOW:
        return "outside_window"
    return "active"


def unavailable_context(detail: str) -> dict[str, Any]:
    return {
        "exam_at": None,
        "exam_source": "none",
        "exam_precision": "none",
        "exam_state": "unavailable",
        "deadline_context": "none",
        "detail": detail if detail in CONTEXT_DETAILS else "no_match",
    }


def context_from_exam(
    row: dict[str, Any],
    *,
    now: datetime,
    source: str = "fudan_jwgl",
) -> dict[str, Any] | None:
    """Build the frozen-shape context for one matched exam row.

    Returns None when the row cannot yield a trustworthy instant.
    """
    try:
        exam_date = date.fromisoformat(str(row.get("date") or ""))
    except ValueError:
        return None
    start_text = str(row.get("start_time") or "")
    end_text = str(row.get("end_time") or "")
    if start_text and end_text:
        start_clock = _parse_time_range(f"{start_text}~{end_text}")
        if start_clock is None:
            return None
        start = datetime.combine(exam_date, start_clock[0], tzinfo=SHANGHAI)
        end = datetime.combine(exam_date, start_clock[1], tzinfo=SHANGHAI)
        precision = "datetime"
        exam_at = start.isoformat()
    else:
        start = datetime.combine(exam_date, time(0, 0), tzinfo=SHANGHAI)
        end = datetime.combine(exam_date, time(23, 59, 59), tzinfo=SHANGHAI)
        precision = "date"
        exam_at = exam_date.isoformat()
    state = exam_state_for_window(start, end, now)
    return {
        "exam_at": exam_at,
        "exam_source": source if source in ("fudan_jwgl", "user_confirmed") else "none",
        "exam_precision": precision,
        "exam_state": state,
        "deadline_context": "active" if state == "active" else "none",
        "detail": "matched" if source == "fudan_jwgl" else "user_confirmed",
    }


def user_confirmed_context(exam_at_seconds: float, *, now: float) -> dict[str, Any]:
    """Additive context for a user-entered exam time (legacy float epoch)."""
    try:
        moment = float(exam_at_seconds)
    except (TypeError, ValueError):
        return unavailable_context("malformed")
    if moment <= 0:
        return unavailable_context("malformed")
    start = datetime.fromtimestamp(moment, tz=SHANGHAI)
    state = exam_state_for_window(start, start, datetime.fromtimestamp(now, tz=SHANGHAI))
    return {
        "exam_at": start.isoformat(),
        "exam_source": "user_confirmed",
        "exam_precision": "datetime",
        "exam_state": state,
        "deadline_context": "active" if state == "active" else "none",
        "detail": "user_confirmed",
    }


def match_exam_row(
    rows: list[dict[str, Any]],
    *,
    course_codes: list[str],
    course_name: str,
    semester_range: tuple[date, date] | None,
) -> tuple[dict[str, Any] | None, str]:
    """Match one course to at most one exam row; never pick a fuzzy candidate.

    Exact course-code matching wins.  Course-name matching is accepted only
    when a semester range is available and the exam date falls inside it, and
    only when it produces exactly one candidate.  Returns ``(row, detail)``
    with detail ``matched`` / ``no_match`` / ``ambiguous``.
    """
    normalized_codes = {
        _normalize_identity(code) for code in (course_codes or []) if _normalize_identity(code)
    }
    if normalized_codes:
        code_matches = [
            row for row in rows
            if _normalize_identity(row.get("course_code")) in normalized_codes
        ]
        if len(code_matches) == 1:
            return code_matches[0], "matched"
        if len(code_matches) > 1:
            return None, "ambiguous"
    name_key = _normalize_identity(course_name)
    if name_key and semester_range is not None:
        range_start, range_end = semester_range
        name_matches = []
        for row in rows:
            if _normalize_identity(row.get("course_name")) != name_key:
                continue
            try:
                exam_date = date.fromisoformat(str(row.get("date") or ""))
            except ValueError:
                continue
            if range_start <= exam_date <= range_end:
                name_matches.append(row)
        if len(name_matches) == 1:
            return name_matches[0], "matched"
        if len(name_matches) > 1:
            return None, "ambiguous"
    return None, "no_match"


def semester_range_from_start(start: Any) -> tuple[date, date] | None:
    """Bounded semester window used to gate name-based exam matching."""
    try:
        start_date = date.fromisoformat(str(start or ""))
    except ValueError:
        return None
    return start_date, start_date + timedelta(days=_SEMESTER_DAYS)


def fetch_exam_rows(vpn: Any, student_id: str, *, timeout: int = 20) -> list[dict[str, str]] | None:
    """Read-only fetch of the exam-arrangement page for one student.

    Returns parsed rows, or None when the session, network, or payload is
    unavailable.  Never raises and never exposes the raw HTML.
    """
    identity = str(student_id or "").strip()
    if vpn is None or not _STUDENT_ID_RE.match(identity):
        return None
    try:
        response = vpn.get_allowed(
            EXAM_ARRANGE_URL.format(student_id=identity),
            allow_redirects=True,
            timeout=int(timeout),
        )
        status = int(getattr(response, "status_code", 0) or 0)
        if status < 200 or status >= 300:
            return None
        rows = parse_exam_arrangement(str(getattr(response, "text", "") or ""))
    except Exception:
        return None
    return rows


def exam_start_epoch(context: Any) -> float | None:
    """Epoch seconds of a context's exam start instant, or None."""
    if not isinstance(context, dict):
        return None
    pair = _parse_context_instant(str(context.get("exam_at") or ""))
    if pair is None:
        return None
    return pair[0].timestamp()


def refresh_context_state(context: Any, *, now: float) -> dict[str, Any]:
    """Re-evaluate ``exam_state``/``deadline_context`` from a stored context.

    Listing must describe the exam relative to the current moment, so the
    stored ``exam_at`` instant is re-evaluated on read.  Datetime instants
    keep their identity; date-only values span their whole day.  Anything
    unreadable degrades to ``unavailable`` without raising.
    """
    base = dict(context) if isinstance(context, dict) else dict(unavailable_context("malformed"))
    source = base.get("exam_source")
    exam_at = str(base.get("exam_at") or "")
    if source not in ("fudan_jwgl", "user_confirmed") or not exam_at:
        base.update({"exam_state": "unavailable", "deadline_context": "none"})
        base["exam_source"] = source if source in EXAM_SOURCES else "none"
        base["exam_precision"] = base.get("exam_precision") if base.get("exam_precision") in EXAM_PRECISIONS else "none"
        return base
    moment = _parse_context_instant(exam_at)
    if moment is None:
        base.update({
            "exam_at": None, "exam_precision": "none", "exam_state": "unavailable",
            "deadline_context": "none", "detail": "malformed",
        })
        return base
    start, end = moment
    state = exam_state_for_window(start, end, datetime.fromtimestamp(now, tz=SHANGHAI))
    base["exam_state"] = state
    base["deadline_context"] = "active" if state == "active" else "none"
    return base


def _parse_context_instant(exam_at: str) -> tuple[datetime, datetime] | None:
    text = str(exam_at or "").strip()
    if _DATE_RE.match(text):
        try:
            exam_date = date.fromisoformat(text)
        except ValueError:
            return None
        return (
            datetime.combine(exam_date, time(0, 0), tzinfo=SHANGHAI),
            datetime.combine(exam_date, time(23, 59, 59), tzinfo=SHANGHAI),
        )
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=SHANGHAI)
    return moment, moment


__all__ = [
    "ACTIVE_WINDOW", "ACTIVE_WINDOW_HOURS", "CONTEXT_DETAILS",
    "DEADLINE_CONTEXTS", "EXAM_ARRANGE_URL", "EXAM_CONTEXT_VERSION",
    "EXAM_PRECISIONS", "EXAM_SOURCES", "EXAM_STATES", "PLAN_STRATEGIES",
    "ReviewPlanValidationError", "context_from_exam",
    "exam_start_epoch", "exam_state_for_window", "fetch_exam_rows", "match_exam_row",
    "parse_exam_arrangement", "refresh_context_state", "semester_range_from_start",
    "unavailable_context", "user_confirmed_context",
]
