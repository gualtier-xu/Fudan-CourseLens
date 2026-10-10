"""Small, versioned read/write contract for the local student client."""

from __future__ import annotations

from typing import Any
import re

from .course_data_inventory import (
    COURSE_DATA_ACTIONS,
    MAX_LECTURE_PAGE_SIZE,
    MAX_SUMMARY_PAGE_SIZE,
)
from .references import normalize_reference


API_VERSION = "v3"
SCHEDULE_OUTPUTS = {"subtitle", "ocr", "summary", "chapters", "quiz"}
SCHEDULE_PIPELINE_VERSION = "daily-schedule-v2"
COURSE_DATA_OPERATION_ID_PATTERN = r"[A-Za-z0-9._:-]{8,128}"
CLIENT_RESET_ACTIONS = ("reset",)
DATA_MIGRATION_ACTIONS = ("export", "import")
DATA_MIGRATION_PACKAGE_ID_PATTERN = r"[0-9a-f]{32}"
# 导出/导入密码与包同寿：只做形状闭集，不做强度武断（强度是学生的自由）。
DATA_MIGRATION_PASSWORD_MIN = 8
DATA_MIGRATION_PASSWORD_MAX = 256


class OnboardingGuideVersionError(RuntimeError):
    """Client guide version does not match the current onboarding guide constant."""


def envelope(data: Any, *, request_id: str = "") -> dict[str, Any]:
    return {"schema": "courselens.api.v3", "version": API_VERSION, "request_id": request_id, "data": data}


def catalog(service: Any, *, course_ids: list[str] | None = None, query: str = "") -> list[dict[str, Any]]:
    courses = service.catalog_repository.courses()
    wanted = {str(value) for value in (course_ids or []) if str(value)}
    result = []
    for course in courses:
        item = dict(course)
        course_id = str(item.get("course_id") or item.get("id") or "")
        if wanted and course_id not in wanted:
            continue
        text = f"{item.get('title', '')} {item.get('teacher', '')}".casefold()
        if query and query.casefold() not in text:
            continue
        result.append(item)
    return result


def tasks(service: Any) -> list[dict[str, Any]]:
    # CLIENT-STATE-R1：v3 tasks 契约=「最近窗口」读——最新优先 + 90 天终态
    # 保留窗（与任务抽屉同语义）。
    return service.task_store.list_tasks(limit=100, newest_first=True)


def validate_feature_update(body: dict[str, Any]) -> tuple[str, bool]:
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 80 or not all(ch.isalnum() or ch in "._-" for ch in name):
        raise ValueError("feature flag name is invalid")
    value = body.get("enabled")
    if not isinstance(value, bool):
        raise ValueError("feature flag enabled must be boolean")
    return name, value


def validate_reference_body(body: dict[str, Any]) -> dict[str, Any]:
    return normalize_reference(body).public()


def validate_schedule(body: dict[str, Any]) -> dict[str, Any]:
    at = str(body.get("time") or "07:30").strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at):
        raise ValueError("schedule time must use HH:MM")
    outputs = []
    for item in list(body.get("outputs") or ["subtitle"]):
        value = str(item).strip()
        if value not in SCHEDULE_OUTPUTS:
            raise ValueError("schedule output is invalid")
        if value not in outputs:
            outputs.append(value)
    if not outputs:
        raise ValueError("at least one schedule output is required")
    course_ids = []
    for item in list(body.get("course_ids") or []):
        value = str(item).strip()
        if not value.isdigit():
            raise ValueError("course_ids must contain numeric identifiers")
        if value not in course_ids:
            course_ids.append(value)
    enabled = bool(body.get("enabled", False))
    if enabled and not course_ids:
        raise ValueError("enabled schedules require at least one course")
    return {
        "enabled": enabled,
        "time": at,
        "course_ids": course_ids,
        "outputs": outputs,
        "catch_up": body.get("catch_up") is not False,
        "pipeline_version": SCHEDULE_PIPELINE_VERSION,
        "pending": [],
    }


def _course_data_query_int(query: dict[str, list[str]], name: str, default: int, *, minimum: int, maximum: int | None = None) -> int:
    raw = str(query.get(name, [""])[0] or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer")
    if value < minimum:
        return minimum
    if maximum is not None and value > maximum:
        return maximum
    return value


def _course_data_query_flag(query: dict[str, list[str]], name: str) -> bool:
    return str(query.get(name, [""])[0] or "").strip().casefold() in {"1", "true", "yes"}


def validate_course_data_summary_query(query: dict[str, list[str]]) -> dict[str, Any]:
    """Closed pagination bounds for ``GET /api/v3/course-data`` (page_size ≤ 200)."""
    return {
        "page": _course_data_query_int(query, "page", 1, minimum=1),
        "page_size": _course_data_query_int(query, "page_size", 50, minimum=1, maximum=MAX_SUMMARY_PAGE_SIZE),
        "include_orphans": _course_data_query_flag(query, "include_orphans"),
    }


def validate_course_data_lectures_query(query: dict[str, list[str]]) -> dict[str, Any]:
    """Closed pagination bounds for ``GET /api/v3/course-data/lectures`` (limit ≤ 50)."""
    course_id = str(query.get("course_id", [""])[0] or "").strip()
    if not course_id:
        raise ValueError("course_id is required")
    return {
        "course_id": course_id,
        "limit": _course_data_query_int(query, "limit", 50, minimum=1, maximum=MAX_LECTURE_PAGE_SIZE),
        "offset": _course_data_query_int(query, "offset", 0, minimum=0),
    }


def validate_course_data_action(body: dict[str, Any]) -> dict[str, Any]:
    """Validate one ``POST /api/v3/course-data/actions`` request body.

    Syntax only: action closed set, operation_id shape, list/bool/string
    types.  Semantic checks (blockers, confirmation tiers, typed course-name
    receipts) belong to the application-layer action engine.
    """
    if not isinstance(body, dict):
        raise ValueError("course data action body is invalid")
    action = str(body.get("action") or "").strip().casefold()
    if action not in COURSE_DATA_ACTIONS:
        raise ValueError("course data action is invalid")
    operation_id = str(body.get("operation_id") or "").strip()
    if not re.fullmatch(COURSE_DATA_OPERATION_ID_PATTERN, operation_id):
        raise ValueError("operation_id is required")

    def identifiers(name: str) -> list[str]:
        value = body.get(name)
        if value is None:
            return []
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"{name} must be a list of identifiers")
        unique: list[str] = []
        for item in value:
            stripped = item.strip()
            if stripped and stripped not in unique:
                unique.append(stripped)
        return unique

    confirm = body.get("confirm", False)
    if not isinstance(confirm, bool):
        raise ValueError("confirm must be boolean")
    confirm_typed = body.get("confirm_typed", "")
    if not isinstance(confirm_typed, str):
        raise ValueError("confirm_typed must be a string")
    include_orphans = body.get("include_orphans", False)
    if not isinstance(include_orphans, bool):
        raise ValueError("include_orphans must be boolean")
    return {
        "action": action,
        "operation_id": operation_id,
        "course_ids": identifiers("course_ids"),
        "sub_ids": identifiers("sub_ids"),
        "confirm": confirm,
        "confirm_typed": confirm_typed,
        "include_orphans": include_orphans,
    }


def validate_client_reset_action(body: dict[str, Any]) -> dict[str, Any]:
    """Validate one ``POST /api/v3/client-reset/actions`` request body.

    Syntax only: single-action closed set, operation_id shape, strict
    string/bool types.  The semantic checks (typed 「重置」 receipt, lifecycle
    blockers) belong to the application-layer reset engine.
    """
    if not isinstance(body, dict):
        raise ValueError("client reset action body is invalid")
    action = str(body.get("action") or "").strip().casefold()
    if action not in CLIENT_RESET_ACTIONS:
        raise ValueError("client reset action is invalid")
    operation_id = str(body.get("operation_id") or "").strip()
    if not re.fullmatch(COURSE_DATA_OPERATION_ID_PATTERN, operation_id):
        raise ValueError("operation_id is required")
    confirm_typed = body.get("confirm_typed", "")
    if not isinstance(confirm_typed, str):
        raise ValueError("confirm_typed must be a string")
    delete_derived = body.get("delete_derived", False)
    if not isinstance(delete_derived, bool):
        raise ValueError("delete_derived must be boolean")
    delete_github_repos = body.get("delete_github_repos", False)
    if not isinstance(delete_github_repos, bool):
        raise ValueError("delete_github_repos must be boolean")
    return {
        "action": action,
        "operation_id": operation_id,
        "confirm_typed": confirm_typed,
        "delete_derived": delete_derived,
        "delete_github_repos": delete_github_repos,
    }


def validate_data_migration_action(body: dict[str, Any]) -> dict[str, Any]:
    """Validate one ``POST /api/v3/data-migration/actions`` request body.

    Syntax only: two-action closed set, operation_id shape, password string
    bounds, package_id shape.  Semantic checks (staged package existence,
    single-flight, schema versions) belong to the application-layer engine.
    """
    if not isinstance(body, dict):
        raise ValueError("data migration action body is invalid")
    action = str(body.get("action") or "").strip().casefold()
    if action not in DATA_MIGRATION_ACTIONS:
        raise ValueError("data migration action is invalid")
    operation_id = str(body.get("operation_id") or "").strip()
    if not re.fullmatch(COURSE_DATA_OPERATION_ID_PATTERN, operation_id):
        raise ValueError("operation_id is required")
    password = body.get("password", "")
    if not isinstance(password, str):
        raise ValueError("password must be a string")
    if not DATA_MIGRATION_PASSWORD_MIN <= len(password) <= DATA_MIGRATION_PASSWORD_MAX:
        raise ValueError(
            f"password must be {DATA_MIGRATION_PASSWORD_MIN}-{DATA_MIGRATION_PASSWORD_MAX} characters"
        )
    package_id = ""
    if action == "import":
        package_id = str(body.get("package_id") or "").strip().casefold()
        if not re.fullmatch(DATA_MIGRATION_PACKAGE_ID_PATTERN, package_id):
            raise ValueError("package_id is required")
    return {
        "action": action,
        "operation_id": operation_id,
        "password": password,
        "package_id": package_id,
    }


__all__ = [
    "API_VERSION",
    "CLIENT_RESET_ACTIONS",
    "COURSE_DATA_OPERATION_ID_PATTERN",
    "DATA_MIGRATION_ACTIONS",
    "catalog",
    "envelope",
    "tasks",
    "validate_client_reset_action",
    "validate_course_data_action",
    "validate_data_migration_action",
    "validate_course_data_lectures_query",
    "validate_course_data_summary_query",
    "validate_feature_update",
    "validate_reference_body",
    "validate_schedule",
]
