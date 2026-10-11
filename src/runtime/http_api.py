"""Loopback-only HTTP adapter for Fudan CourseLens."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import math
import mimetypes
import os
import re
import sys
import threading
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse, urlsplit

from path_utils import PROJECT_ROOT
from src.services import CourseLensServices
from src.services.domains import (
    AutoConnectPreferenceError,
    ClientResetActionError,
    CourseDataActionError,
    CourseReviewActionError,
)
from src.remote.github_app import GitHubAppError
from src.runtime.subtitle_reader import (
    parse_subtitle_file,
    shape_display_cues_cached,
    split_long_cues,
)
from src.runtime.media_source import (
    MEDIA_STREAM_SUCCESS,
    MediaStreamFailureLedger,
    atrust_presence_snapshot,
    classify_open_failure,
)
from src.runtime.task_store import (
    PREDICTION_OUTCOME_SCHEMA,
    REMOTE_RUN_RECOVERABLE_STATES,
    USER_PAUSE_INTENT_KEY,
)
from src.runtime.timetable import SHANGHAI, STALE_SECONDS, TimetableError, _occurrences, merge_course_records
from src.runtime.exam_schedule import ReviewPlanValidationError
from src.runtime.live_room import DEFAULT_LIVE_VIEW, PLAYBACK_ABSOLUTE_TTL, LiveRoomError
from src.runtime.analytics import STUDY_EVENT_KINDS
from src.runtime.progress import ESTIMATE_BASIS_VALUES, QUEUE_BASIS_VALUES
from src.runtime.student_features import load_search_answer
from src.runtime.test_mode import fresh_operations_requested
from src.update import UpdateError
from src.runtime.api_v3 import (
    envelope as api_v3_envelope,
    validate_client_reset_action,
    validate_course_data_action,
    validate_data_migration_action,
    validate_course_data_lectures_query,
    validate_course_data_summary_query,
    validate_feature_update,
    validate_reference_body,
    validate_schedule,
    OnboardingGuideVersionError,
)
from src.runtime.course_data_inventory import CourseDataInventory
from src.runtime.data_map import data_map_snapshot
from src.runtime.assessment_radar import (
    AssessmentActionError,
    assessment_events_action,
    assessment_events_scan,
)
from src.runtime.materials_center import (
    MaterialsActionError,
    materials_center_delete,
    materials_center_file_path,
    materials_center_scan,
)
from src.runtime.data_migration import DataMigrationError


PROJECT_INSTANCE_ID = hashlib.sha256(str(PROJECT_ROOT).casefold().encode("utf-8")).hexdigest()[:16]


def _client_version() -> str:
    try:
        value = json.loads(
            (PROJECT_ROOT / "courselens-version.json").read_text(encoding="utf-8")
        )
    except (OSError, TypeError, ValueError):
        return ""
    if value.get("schema") != "courselens.client-version.v1":
        return ""
    return str(value.get("version") or "")


def _course_data_inventory(service: Any) -> CourseDataInventory:
    """Compose the read-only course-data inventory from the container's raw stores."""
    return CourseDataInventory(
        learning_store=service.learning.repository,
        catalog_repository=service.auth_catalog.catalog,
        task_store=service.tasks.repository,
    )


# greeting 课量尾句闭集键（AUTOLOGIN-LOCAL-FIRST-1①）：与 frontend/modules/greeting.js
# 课量尾句变体同源——none=今日无课；done=今日课毕；done-long=今日课毕，辛苦了；
# full=今日课满；full-half=今日课满，已过半；no-data=无本地数据/深夜档/课量语义留白。
_GREETING_TAIL_KEYS = frozenset({"none", "done", "done-long", "full", "full-half", "no-data"})
# 本地可信上下文（隐私放宽记档）：已保存账号且启用自动登录。rotation_required 仍代表
# 账号在本机保存（仅密码待更新），对零敏感的尾句键同样构成可信读据。
_GREETING_TRUSTED_ACCOUNT_STATUSES = frozenset({"ready", "rotation_required"})


def _greeting_auto_login_account(service: Any) -> dict:
    """已保存账号且启用自动登录的闭集判据（只读本地状态，零网络）。"""
    try:
        snapshot = service.settings.privacy_snapshot() or {}
    except Exception:
        return {}
    # SRC-SYNDROME-1 U2（第廿八案）形状修复：settings_privacy_snapshot 的
    # auto_connect 是 auto_connect_snapshot() 全量快照（enabled/status 在
    # .fudan 子字典下）——旧代码按平铺键读取，谓词在生产恒 False，三态门的
    # restoring-trusted 分支（含 greeting 尾句）从未生效。此处以生产嵌套形状
    # 为准，平铺形状保持兼容。
    raw = snapshot.get("auto_connect")
    if isinstance(raw, dict) and isinstance(raw.get("fudan"), dict):
        fudan = raw["fudan"]
    elif isinstance(raw, dict):
        fudan = raw
    else:
        return {}
    if fudan.get("enabled") and str(fudan.get("status") or "") in _GREETING_TRUSTED_ACCOUNT_STATUSES:
        return fudan
    return {}


def _auto_login_session_resumable(service: Any) -> bool:
    """自动登录可恢复（status ready）：旋转待定的保存密码无法恢复会话，
    不构成「正在登录」语境（与 greeting 尾句的可信判据相比更严一档）。"""
    fudan = _greeting_auto_login_account(service)
    return bool(fudan) and fudan.get("status") == "ready"


# U5（SRC-CLEANUP-1）：今日结束钟点按身份+上海日期做 60s 记忆化——每次
# greeting 请求重算要全 store 扫描+整学期展开（实测合成基准 ~20ms 起步，
# 生产行累积下 175-264ms）；钟点粒度的问候尾句 60s 陈旧无感。
_GREETING_ENDS_CACHE_TTL_SECONDS = 60.0
_GREETING_ENDS_CACHE: dict[str, tuple[str, float, list[str] | None]] = {}
_GREETING_ENDS_CACHE_LOCK = threading.Lock()


def _greeting_today_meeting_ends(service: Any) -> list[str] | None:
    """今日课程结束钟点（"HH:MM"）；None=本地缓存不可用。纯本地读，零学校网络。

    带 60s 记忆化（键=身份+上海日期，跨天即失效）；实际计算见
    :func:`_greeting_today_meeting_ends_uncached`。
    """
    try:
        identity = str(service.auth_catalog.identity_scope() or "")
        if not identity:
            return None
        now_monotonic = time.monotonic()
        today = datetime.now(SHANGHAI).date().isoformat()
        with _GREETING_ENDS_CACHE_LOCK:
            hit = _GREETING_ENDS_CACHE.get(identity)
        if hit and hit[0] == today and now_monotonic - hit[1] < _GREETING_ENDS_CACHE_TTL_SECONDS:
            return list(hit[2]) if hit[2] is not None else None
        ends = _greeting_today_meeting_ends_uncached(service)
        with _GREETING_ENDS_CACHE_LOCK:
            _GREETING_ENDS_CACHE[identity] = (today, now_monotonic, ends)
        return ends
    except Exception:
        return None


def _greeting_today_meeting_ends_uncached(service: Any) -> list[str] | None:
    """今日课程结束钟点的实际计算（与 TimetableRuntime.snapshot 同一 store、
    同一身份分区、同一新鲜度与学期选择语义、同一展开函数，不复制判定逻辑）；
    只输出结束钟点，绝不携带课程名/ID/数字等个人数据。
    """
    try:
        identity = str(service.auth_catalog.identity_scope() or "")
        if not identity:
            return None
        store = service.timetable.runtime.store
        preferences = store.preferences(identity)
        known: dict[str, dict[str, Any]] = {}
        for row in store.list(identity):
            payload = row.get("payload") or {}
            for item in payload.get("semesters") or []:
                semester_id = str(item.get("semester_id") or "").strip()
                if semester_id:
                    known[semester_id] = {**known.get(semester_id, {}), **item}
            item = dict(payload.get("semester") or {})
            semester_id = str(item.get("semester_id") or row.get("semester_id") or "").strip()
            if semester_id:
                known[semester_id] = {**known.get(semester_id, {}), **item}
        selected = str(preferences.get("selected_semester_id") or "").strip()
        if not selected and known:
            selected = next(
                (sid for sid, item in known.items() if item.get("is_default")),
                next(iter(known)),
            )
        rows = store.list(identity, selected) if selected else []
        now = time.time()
        visible = [row for row in rows if now - float(row.get("observed_at") or 0) <= STALE_SECONDS]
        if not visible:
            return None
        courses = merge_course_records(
            course for row in visible for course in row["payload"].get("courses") or []
        )
        if not courses:
            return None
        start_date = str(
            (known.get(selected) or {}).get("start_date")
            or (preferences.get("semester_start_dates") or {}).get(selected)
            or ""
        ).strip()
        if not start_date:
            return None  # 学期起点缺失：无法定位今日，诚实无数据
        courses = [
            {**course, "semester_start_date": course.get("semester_start_date") or start_date}
            for course in courses
        ]
        today = datetime.now(SHANGHAI).date().isoformat()
        return sorted(item["end_time"] for item in _occurrences(courses) if item.get("date") == today)
    except Exception:
        return None


def _greeting_tail_key_from_ends(ends: list[str], minutes: int) -> str:
    """闭集尾句键判定（纯函数）：与 greeting.js courseLoadTail 同一判定骨架。

    深夜档不叠加（凌晨「今日」指代歧义）；1–4 节未开始/进行中诚实留白。
    """
    if minutes < 300 or minutes >= 1440:
        return "no-data"
    if not ends:
        return "none"  # 缓存确知今日无课
    marks: list[int] = []
    for value in ends:
        parts = str(value).split(":")
        if len(parts) != 2:
            continue
        try:
            mark = int(parts[0]) * 60 + int(parts[1])
        except ValueError:
            continue
        if 0 <= mark < 1440:
            marks.append(mark)
    if not marks:
        return "no-data"  # 有课量记录而钟点全坏：异常退回，诚实留白
    total = len(marks)
    if minutes > max(marks):
        return "done-long" if total >= 5 else "done"
    if total <= 4:
        return "no-data"
    finished = sum(1 for mark in marks if mark < minutes)
    return "full-half" if finished > total / 2 else "full"


def _greeting_tail_key(service: Any) -> str:
    """greeting-context 端点输出：仅闭集键，零课程名/ID/数字/个人数据。"""
    if not _greeting_auto_login_account(service):
        return "no-data"  # 无保存账号/未启用自动登录：原硬门原样，诚实无数据
    ends = _greeting_today_meeting_ends(service)
    if ends is None:
        return "no-data"
    local = time.localtime()
    return _greeting_tail_key_from_ends(ends, local.tm_hour * 60 + local.tm_min)


TASK_ERROR_CODES = {
    "operation_already_running", "operation_failed", "task_not_found",
    "operation_id_conflict", "task_already_active",
    "fudan_login_required",
    "task_action_invalid", "task_action_rejected", "task_retry_not_supported",
    "task_retry_requires_failed_state", "task_cancel_requires_active_state",
    "task_cancel_not_supported", "timeline_transcript_unavailable",
    "timeline_classification_failed", "alignment_transcript_unavailable",
    "alignment_failed", "concept_analysis_requires_two_courses",
    "question_explanation_not_configured", "document_import_failed",
    "document_type_unsupported", "document_payload_invalid", "document_empty",
    "document_too_large", "document_format_invalid", "document_password_required",
    "document_payload_not_recoverable",
    "remote_recovery_material_unavailable",
    "remote_recovery_cancel_not_safe",
    "authorization_required", "authorization_revoked", "permission_denied",
    "cloud_setup_required",
    "rate_limited", "network_unavailable", "proxy_unavailable", "timeout",
    # ⑫（LOG1）：托管 runner 中途失联（步级 cancelled+run 级 failure 签名）
    "remote_runner_lost",
    # AS2（CLOSED-RESULT-IMPORT-1）U3：worker 树漂移签名码按码表归位，不再被
    # github 字样兜底吞成 remote_failed；U2 显式导入通道的两种诚实拒绝。
    # 前端 TASK_FAILURE_GUIDANCE 同笔带键（等集钉 test_automation 严校）。
    "worker_tree_drifted", "remote_import_unavailable", "remote_import_expired",
    "integrity_rejected", "remote_failed", "runtime_failed", "task_failed",
    "course_data_action_invalid", "course_data_page_invalid",
    "course_data_confirm_required", "course_data_target_invalid",
    # B3：worker 已归约的闭集码，客户端词汇与登录侧挑战家族对齐（两侧同 commit）
    "platform_challenge_required",
    # P11 质量抽检任务级闭集码（TASK_FAILURE_GUIDANCE 等集钉 test_automation
    # 严校——三键与前端文案同笔落地；HTTP 请求级拒绝码不在此表）。
    "worker_kind_unsupported", "judge_output_invalid", "quality_no_material",
    # APP500-CLOSE（MEDIA-RETRY-1 停车场同族收口）：enqueue 源上已抛显式码
    # lecture_not_found（application.py 目录守卫），但词表缺键时
    # _task_error_code 把它吞成通用 task_failed——闭集码到不了学生面前，
    # api.js「这门讲次不在当前课程目录里了。刷新目录即可同步。」的人话文案
    # 收不到该码。入表=显式码直通；自然语言消息形态仍归 task_failed
    # （启发式兜底不放宽，合同钉 test_unified_task_center 严校）。
    # 前端 tasks-drawer TASK_FAILURE_GUIDANCE 同键同笔（等集钉）。
    "lecture_not_found",
    # DEAD-TASK-PURGE：顽固残留任务根修的两个诚实死因码（A 启动僵尸清扫 /
    # B 用户面清除卡住任务；常量住 task_store，前端 TASK_FAILURE_GUIDANCE
    # 同笔带键，等集钉 test_automation 严校）。
    "zombie_session_cleanup", "user_cleared_stuck",
}


def _proxy_url_parseable(value: str) -> bool:
    """D8：代理地址闭集校验——scheme 必须 http/https 且主机非空。

    与 frontend/modules/settings.js 的 proxyUrlParseable 同规则（等集）；
    旧路径「缺 scheme 就静默补 http://」在保存入口就此关闭。"""
    if not value.startswith(("http://", "https://")):
        return False
    try:
        return bool(urlsplit(value).hostname)
    except ValueError:
        return False


def _task_error_code(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    explicit = str(getattr(value, "code", "") or "").strip()
    if explicit in TASK_ERROR_CODES:
        return explicit
    # LIVE-VALIDATE-1 LV1-3：连接组件的 authorization_missing（派发预检拒绝）
    # 与任务面 cloud_setup_required（授权/连接未完成）是同一条学生事实，按既有
    # 闭集码归位，不再让「github」兜底启发式贴成 remote_failed。
    if explicit == "authorization_missing":
        return "cloud_setup_required"
    normalized = re.sub(r"[^a-z0-9_]+", "_", raw.casefold()).strip("_")[:80]
    if normalized in TASK_ERROR_CODES:
        return normalized
    # AS2/U3：闭集码透传——错误文本内嵌已知闭集码（如
    # "GitHubAppError: worker_tree_drifted"）时按码表归位，不再落到含
    # github 字样的兜底启发式。逐词全等匹配既有码表，不发明新码、不放宽。
    for token in re.findall(r"[a-z0-9_]+", raw.casefold()):
        if token in TASK_ERROR_CODES:
            return token
    text = raw.casefold()
    if "429" in text or ("rate" in text and "limit" in text):
        return "rate_limited"
    if "401" in text or "authorization" in text or "credential" in text:
        return "authorization_required"
    if "403" in text or "permission" in text or "forbidden" in text:
        return "permission_denied"
    if "proxy" in text:
        return "proxy_unavailable"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "network" in text or "connection" in text or "dns" in text or "tls" in text:
        return "network_unavailable"
    if "signature" in text or "hash" in text or "integrity" in text or "mismatch" in text:
        return "integrity_rejected"
    if "github" in text or "runner" in text or "artifact" in text:
        return "remote_failed"
    if raw.split(":", 1)[0].strip().casefold().endswith("error"):
        return "runtime_failed"
    return "task_failed"


def _subtitle_excerpt(body: dict) -> dict:
    """Optional bounded media excerpt for subtitle enqueue requests."""
    excerpt: dict[str, float] = {}
    for name in ("start_seconds", "duration_seconds"):
        if body.get(name) is None:
            continue
        value = body.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number")
        value = float(value)
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{name} must be finite")
        if value < 0 or (name == "duration_seconds" and value <= 0):
            raise ValueError(f"{name} is out of range")
        excerpt[name] = value
    return excerpt


def _task_actions(task: dict) -> list[str]:
    state = str(task.get("state") or "")
    kind = str(task.get("kind") or "")
    if _task_error_code(task.get("error")) == "remote_recovery_material_unavailable":
        return ["cancel"] if state == "paused" else []
    if state == "queued":
        return ["pause", "cancel"]
    if state == "running":
        return ["pause", "cancel"] if kind in {"subtitle", "summary", "question"} else []
    if state == "pausing":
        return [] if bool(dict(task.get("payload") or {}).get("cancel_requested")) else ["resume"]
    if state == "paused":
        return ["resume", "cancel"]
    if state == "failed" and kind != "document_import":
        return ["retry"]
    return []


# AS2/U2：失败/暂停卡「导入远端结果」按钮的可见性闭集（保守可能集）。
# 密钥有无只有应用层知道，这里只依据任务行/远端行初筛；最终判定在点击后
# 的显式导入通道。窗口与 application.REMOTE_RESULT_IMPORT_WINDOW_SECONDS
# 同值、候选态与 application.REMOTE_RESULT_RECONCILE_STATES 同集——
# 等值/等集钉在 tests/test_closed_result_import.py。
_REMOTE_IMPORT_WINDOW_SECONDS = 7 * 24 * 3600
_REMOTE_IMPORT_CANDIDATE_STATES = tuple(REMOTE_RUN_RECOVERABLE_STATES) + ("failed",)


def _public_remote_import_group(task: dict, remote: dict | None, now: float) -> dict | None:
    state = str(task.get("state") or "")
    if state not in {"failed", "paused"}:
        return None
    if dict(task.get("payload") or {}).get(USER_PAUSE_INTENT_KEY) is True:
        return None  # 51 显式暂停语义：不自动重挂，也不诱导导入
    run = dict(remote or {})
    if not int(run.get("run_id") or 0):
        return None
    if str(run.get("remote_state") or "") not in _REMOTE_IMPORT_CANDIDATE_STATES:
        return None
    stamp = float(run.get("dispatched_at") or run.get("updated_at") or 0.0)
    if stamp and now - stamp > _REMOTE_IMPORT_WINDOW_SECONDS:
        return None
    return {"possible": True}


# Legacy persisted estimate bases (pre-closed-set rows) mapped onto the
# ``ESTIMATE_BASIS_VALUES`` closed set from src/runtime/progress.py.
_ESTIMATE_BASIS_ALIASES = {
    "duration-history": "history_median",
    "duration-live": "live_blend",
    "duration-bootstrap": "bootstrap",
    "history+live": "phase_history",
    "history": "phase_history",
    "live": "live_blend",
    "unknown": "insufficient_data",
}


def _public_estimate_basis(raw: object) -> str:
    value = str(raw or "")
    if value in ESTIMATE_BASIS_VALUES:
        return value
    return _ESTIMATE_BASIS_ALIASES.get(value, "insufficient_data")


# --- Stage 09-C additive public groups (closed schemas, omit-not-fabricate) --

# course-review/actions 的闭集动作族：refresh = 把缺摘要的讲次排进既有摘要队列；
# explain_assessment = 学生点选一道作业/真题后请求有证据的 AI 解答（绝不自动触发）；
# review_flashcard = 学生给一张闪卡记一次评分（本地 FSRS 调度，零模型零外呼）；
# confirm/dismiss_term_candidate = 学生确认/忽略一个术语修正候选（P10，一次性
# 终态，零模型零外呼——确认词才进字幕链热词）。
COURSE_REVIEW_ACTIONS = (
    "refresh", "explain_assessment", "review_flashcard",
    "confirm_term_candidate", "dismiss_term_candidate",
)
# 这些码表示「这门课 / 这道题 / 这个词对不在本机目录里」，与请求本身不合法分开报。
COURSE_REVIEW_ACTION_NOT_FOUND = (
    "course_review_course_unknown", "course_review_lecture_unknown",
    "assessment_item_unknown", "course_review_flashcard_unknown",
    "term_candidate_unknown",
)

_PROGRESS_SEGMENT_STATES = {
    "complete": "completed",
    "completed": "completed",
    "running": "active",
    "active": "active",
    "skipped": "skipped",
}


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _public_phases(progress: dict) -> dict | None:
    """Semantic phase rail from persisted v2+ progress segments.

    ``None`` when the progress model carries no segment list — legacy scalar
    rows never gain fabricated phases.
    """
    segments = progress.get("segments")
    if not isinstance(segments, list) or not segments:
        return None
    items: list[dict[str, str]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        phase_id = str(segment.get("id") or "")
        if not phase_id:
            continue
        state = _PROGRESS_SEGMENT_STATES.get(str(segment.get("state") or ""), "waiting")
        items.append({
            "id": phase_id,
            "label": str(segment.get("label") or ""),
            "state": state,
        })
    if not items:
        return None
    return {"items": items, "evidence_updated_at": _finite_number(progress.get("observed_at"))}


def _public_prediction_outcome(metadata: dict | None) -> dict | None:
    """Validated actual-vs-initial record from v3 metadata (closed keys)."""
    value = dict((metadata or {}).get("estimate") or {})
    if value.get("schema") != PREDICTION_OUTCOME_SCHEMA:
        return None
    try:
        initial = float(value["initial_minutes"])
        actual = float(value["actual_minutes"])
        delta = float(value["delta_minutes"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(initial) and initial > 0):
        return None
    if not (math.isfinite(actual) and actual >= 0 and math.isfinite(delta)):
        return None
    return {
        "initial_minutes": initial,
        "actual_minutes": actual,
        "delta_minutes": delta,
    }


def public_task(
    task: dict | None, task_store: Any = None, *, now: float | None = None,
    context: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> dict | None:
    """Return the task fields the browser needs without exposing its payload."""
    if not task:
        return None
    current = float(now or time.time())
    progress = dict(task.get("progress") or {})
    estimate = dict(task.get("estimate") or {})
    task_id = str(task.get("task_id") or "")
    metadata_reader = getattr(task_store, "get_v3_metadata", None)
    remote_reader = getattr(task_store, "get_remote_run", None)
    attempt_reader = getattr(task_store, "get_remote_attempt", None)
    metadata = (context or {}).get("metadata", {}).get(task_id)
    remote = (context or {}).get("remote", {}).get(task_id)
    attempt = (context or {}).get("attempt", {}).get(task_id)
    if metadata is None and context is None:
        metadata = metadata_reader(task_id) if callable(metadata_reader) else None
    if remote is None and context is None:
        remote = remote_reader(task_id) if callable(remote_reader) else None
    if attempt is None and context is None:
        attempt = (
            attempt_reader(task_id, max(1, int((remote or {}).get("attempt") or 1)))
            if callable(attempt_reader) and remote else None
        )
    observed_at = float(progress.get("observed_at") or task.get("updated_at") or 0.0)
    estimate_seen = float(estimate.get("updated_at") or 0.0)
    if estimate_seen > 10_000_000_000:
        estimate_seen /= 1000.0
    observed_at = max(observed_at, estimate_seen)
    if attempt:
        observed_at = max(
            observed_at,
            float(attempt.get("observed_at") or 0.0),
            float(attempt.get("last_heartbeat_at") or 0.0),
        )
    state = str(task.get("state") or "")
    error_code = _task_error_code(task.get("error"))
    terminal = state in {"completed", "failed", "canceled"}
    ttl = 20.0 if state in {"running", "pausing"} else 90.0
    expires_at = None if terminal else observed_at + ttl
    stale = bool(not terminal and (observed_at <= 0 or current > float(expires_at or 0)))
    completed = progress.get("processed_media_seconds")
    total = progress.get("media_duration_seconds")
    if completed is None:
        completed = progress.get("completed", (attempt or {}).get("completed"))
    if total is None:
        total = progress.get("total", (attempt or {}).get("total"))
    progress_unit = "items"
    if str(task.get("kind") or "") == "subtitle":
        media_duration = progress.get("media_duration_seconds")
        processed_media = progress.get("processed_media_seconds")
        if media_duration is not None:
            total = media_duration
            completed = processed_media
            progress_unit = "seconds"
    elapsed = estimate.get("elapsed_seconds")
    if elapsed is None:
        elapsed = progress.get("elapsed_active_seconds", progress.get("elapsed_seconds"))
    if elapsed is None and task.get("started_at"):
        ended = float(task.get("finished_at") or current)
        elapsed = max(0.0, ended - float(task.get("started_at") or ended))
    remaining = None if stale else estimate.get("remaining_seconds")
    percent = None if stale else progress.get("percent")
    # Legacy task rows may contain stage placeholders such as 20/50/90 even
    # though no measurable numerator/denominator ever existed. Keep the rows
    # for audit history, but never present those placeholders as real progress.
    has_measurable_total = (
        completed is not None
        and total is not None
        and float(total) > 0
    )
    is_v3_observation = int(progress.get("schema_version") or 0) >= 3
    if percent is not None and not has_measurable_total and not is_v3_observation:
        percent = None
    if terminal:
        remaining = 0.0 if state == "completed" else None
        percent = 100.0 if state == "completed" else None
    # Queue vs active anchors: before the task starts (or while it is still
    # queued) no processing ETA may be published — only the wait time.
    queued_pending = bool(
        state == "queued" or (not terminal and not task.get("started_at"))
    )
    if queued_pending and not terminal:
        remaining = None
    if remaining is None or terminal:
        remaining_lower = None
        remaining_upper = None
    else:
        try:
            remaining_lower = max(0.0, float(estimate.get("lower_seconds")))
        except (TypeError, ValueError):
            remaining_lower = None
        try:
            remaining_upper = max(0.0, float(estimate.get("upper_seconds")))
        except (TypeError, ValueError):
            remaining_upper = None
    if stale and not terminal:
        estimate_basis = "stale"
    elif queued_pending and not terminal:
        estimate_basis = "queue"
    else:
        estimate_basis = _public_estimate_basis(estimate.get("basis"))
    elapsed_queued_seconds = None
    try:
        created_at = float(task.get("created_at")) if task.get("created_at") else 0.0
        started_anchor = float(task.get("started_at")) if task.get("started_at") else 0.0
        if created_at > 0:
            # Frozen once the task started; while still queued it tracks the
            # current wait against the creation anchor. 终态任务同样冻结——
            # 锚在 finished_at（缺则 updated_at）：历史排队时长不再随墙上时钟
            # 虚走（终态卡从不显示「已等待」，此值此前只喂 SSE 投影漂移，
            # PF1：冻结后终态快照才稳定可去重）。
            if started_anchor > 0:
                anchor = started_anchor
            elif terminal:
                anchor = float(task.get("finished_at") or task.get("updated_at") or current)
            else:
                anchor = current
            elapsed_queued_seconds = round(max(0.0, anchor - created_at), 1)
    except (TypeError, ValueError):
        elapsed_queued_seconds = None
    stage = str(progress.get("stage") or (metadata or {}).get("stage") or state)
    cancel_requested = bool(dict(task.get("payload") or {}).get("cancel_requested"))
    if error_code == "remote_recovery_material_unavailable":
        display_state = "remote_recovery_material_unavailable"
    elif state == "running" and stage in {"preflight", "remote_queue", "awaiting_payload"}:
        display_state = "waiting_authorization"
    elif state == "running" and stage in {"remote_result", "remote_import", "importing"}:
        display_state = "importing"
    elif state == "running" and (remote or stage.startswith("remote_") or stage in {"asr", "ocr", "proofread", "summary", "result"}):
        display_state = "remote_running"
    elif state == "pausing" and cancel_requested:
        display_state = "canceling"
    else:
        display_state = state
    # --- Stage 09-C additive groups (closed schemas; omit, never fabricate) ---
    queue_group: dict[str, Any] | None = None
    processing_group: dict[str, Any] | None = None
    completion_group: dict[str, Any] | None = None
    if queued_pending and not terminal and not stale:
        queue_group = {}
        if elapsed_queued_seconds is not None:
            queue_group["elapsed_seconds"] = float(elapsed_queued_seconds)
        workflow = str((remote or {}).get("workflow") or "")
        forecast = None
        if workflow:
            forecast_index = (context or {}).get("queue_forecast")
            if isinstance(forecast_index, dict) and workflow in forecast_index:
                forecast = forecast_index.get(workflow)
            elif callable(getattr(task_store, "queue_forecast_for_workflow", None)):
                forecast = task_store.queue_forecast_for_workflow(workflow)
        lower = _finite_number((forecast or {}).get("lower_seconds"))
        upper = _finite_number((forecast or {}).get("upper_seconds"))
        center = _finite_number((forecast or {}).get("center_seconds"))
        sample_count = _finite_number((forecast or {}).get("sample_count"))
        confidence = str((forecast or {}).get("confidence") or "")
        basis = str((forecast or {}).get("basis") or "")
        if (
            basis in QUEUE_BASIS_VALUES and basis != "insufficient_data"
            and confidence in {"low", "medium", "high"}
            and None not in (lower, upper, center, sample_count)
        ):
            queue_group.update({
                "lower_seconds": float(lower),
                "upper_seconds": float(upper),
                "center_seconds": float(center),
                "sample_count": int(sample_count),
                "confidence": confidence,
                "basis": basis,
                "level": str((forecast or {}).get("level") or ""),
            })
        if queue_group:
            # Combined completion: queue wait + processing prior, each counted
            # exactly once.  Both components must be valid or nothing ships.
            prior_remaining = _finite_number(estimate.get("remaining_seconds"))
            prior_lower = _finite_number(estimate.get("lower_seconds"))
            prior_upper = _finite_number(estimate.get("upper_seconds"))
            if None not in (prior_remaining, prior_lower, prior_upper) and "lower_seconds" in queue_group:
                completion_group = {
                    "earliest_seconds": round(queue_group["lower_seconds"] + prior_lower, 1),
                    "likely_seconds": round(queue_group["center_seconds"] + prior_remaining, 1),
                    "latest_seconds": round(queue_group["upper_seconds"] + prior_upper, 1),
                }
        # While queued the processing group is a history prior, not live
        # evidence: elapsed is omitted and the phrase stays 「开始后预计」.
        if queued_pending and not terminal and not stale:
            prior_remaining = _finite_number(estimate.get("remaining_seconds"))
            if prior_remaining is not None:
                prior_lower = _finite_number(estimate.get("lower_seconds"))
                prior_upper = _finite_number(estimate.get("upper_seconds"))
                processing_group = {
                    "remaining_seconds": float(prior_remaining),
                    "lower_seconds": float(prior_lower) if prior_lower is not None else None,
                    "upper_seconds": float(prior_upper) if prior_upper is not None else None,
                    "confidence": str(estimate.get("confidence") or "low"),
                    "basis": _public_estimate_basis(estimate.get("basis")),
                }
                sample_count = _finite_number(estimate.get("sample_count"))
                if sample_count is not None and sample_count > 0:
                    processing_group["sample_count"] = int(sample_count)
                if estimate.get("recalibrating"):
                    processing_group["recalibrating"] = True
    elif not terminal and not stale:
        p_remaining = _finite_number(remaining)
        if p_remaining is not None:
            p_elapsed = _finite_number(elapsed)
            processing_group = {
                "elapsed_seconds": float(p_elapsed) if p_elapsed is not None else None,
                "remaining_seconds": float(p_remaining),
                "lower_seconds": float(remaining_lower) if remaining_lower is not None else None,
                "upper_seconds": float(remaining_upper) if remaining_upper is not None else None,
                "confidence": str(estimate.get("confidence") or "low"),
                "basis": estimate_basis,
            }
            sample_count = _finite_number(estimate.get("sample_count"))
            if sample_count is not None and sample_count > 0:
                processing_group["sample_count"] = int(sample_count)
            if estimate.get("recalibrating"):
                processing_group["recalibrating"] = True
            completion_group = {
                "earliest_seconds": processing_group["lower_seconds"],
                "likely_seconds": float(p_remaining),
                "latest_seconds": processing_group["upper_seconds"],
            }
            if None in completion_group.values():
                completion_group = None
    phases_group = _public_phases(progress)
    outcome_group = _public_prediction_outcome(metadata)
    remote_import_group = _public_remote_import_group(task, remote, current)
    public = {
        "task_id": task.get("task_id"),
        "kind": task.get("kind"),
        "course_id": task.get("course_id"),
        "sub_id": task.get("sub_id"),
        "state": state,
        "display_state": display_state,
        "resume_requested": bool(task.get("resume_requested")),
        "control_state": (
            "cancel_requested"
            if cancel_requested and state == "pausing"
            else ("pause_requested" if state == "pausing" else "")
        ),
        "stage": stage,
        "label": (
            "任务已完成" if state == "completed"
            else "任务已取消" if state == "canceled"
            else "任务失败" if state == "failed"
            else "重启后未自动恢复：本地恢复材料不可用"
            if error_code == "remote_recovery_material_unavailable"
            else str(progress.get("label") or "")
        ),
        "percent": percent,
        "completed": float(completed) if completed is not None else None,
        "total": float(total) if total is not None else None,
        "progress_unit": progress_unit,
        "elapsed_seconds": float(elapsed) if elapsed is not None else None,
        # AS6 消耗透镜：任务行落库的绝对 DeepSeek 消耗（NULL=无记录，前端
        # 不渲染不伪造；tokens=0 是诚实值=该任务无 LLM 参与）。
        "deepseek_tokens": (
            int(task["deepseek_tokens"]) if task.get("deepseek_tokens") is not None else None
        ),
        "remaining_seconds": float(remaining) if remaining is not None else None,
        "remaining_lower_seconds": float(remaining_lower) if remaining_lower is not None else None,
        "remaining_upper_seconds": float(remaining_upper) if remaining_upper is not None else None,
        "estimate_basis": estimate_basis,
        "elapsed_queued_seconds": elapsed_queued_seconds,
        "progress_confidence": (
            str(estimate.get("confidence") or "low") if remaining is not None else "unknown"
        ),
        "observed_at": observed_at,
        "expires_at": expires_at,
        "stale": stale,
        "input_hash": str(
            progress.get("input_hash") or (metadata or {}).get("input_hash")
            or (remote or {}).get("input_hash") or ""
        ),
        "result_version": str(
            progress.get("result_version") or (remote or {}).get("pipeline_version") or ""
        ),
        "result_notices": dict((metadata or {}).get("result_notices") or {}),
        "error_code": error_code,
        "actions": _task_actions(task),
        "created_at": task.get("created_at"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        "updated_at": task.get("updated_at"),
    }
    if queue_group:
        public["queue"] = queue_group
    if processing_group:
        public["processing"] = processing_group
    if completion_group:
        public["completion"] = completion_group
    if phases_group:
        public["phases"] = phases_group
    if outcome_group:
        public["prediction_outcome"] = outcome_group
    if remote_import_group:
        public["remote_import"] = remote_import_group
    clean_outputs = [str(item) for item in ((metadata or {}).get("requested_outputs") or []) if str(item)]
    if clean_outputs:
        public["requested_outputs"] = clean_outputs
    parent_id = str((metadata or {}).get("parent_id") or "")
    if parent_id:
        public["parent_id"] = parent_id
    payload = task.get("payload") or {}
    if task.get("kind") == "summary" or "include_ppt" in payload:
        public["payload"] = {
            "include_ppt": bool(payload.get("include_ppt", True)),
            "force": bool(payload.get("force", False)),
        }
    elif task.get("kind") == "subtitle":
        public["payload"] = {
            "subtitle_mode": str(payload.get("subtitle_mode") or "automatic"),
            "source_kind": str(payload.get("source_kind") or "client"),
        }
    return public


def public_tasks(task_store: Any, tasks: list[dict], *, now: float | None = None) -> list[dict]:
    context_reader = getattr(task_store, "public_task_context", None)
    context = context_reader(task.get("task_id") for task in tasks) if callable(context_reader) else None
    values = [public_task(task, task_store, now=now, context=context) for task in tasks]
    return [value for value in values if value is not None]


class FrontendSessionRegistry:
    """Stop an idle local service after the last browser page disappears.

    Browser timers are not a reliable indication that a long-running local
    task has ended: Windows can suspend a background tab, so a lost heartbeat
    only means "contact lost", not "page closed".  The lease therefore covers
    a typical background-tab round trip, and a work predicate protects the
    service from automatic shutdown until every running/queued worker has
    reached a safe terminal or paused state.  An explicit page close still
    takes the short fast-path grace: closing the tab means exiting the
    client.

    A launch that never sees a browser page at all (the managed ``--no-open``
    daily-schedule task) is covered by the headless idle window: once no work
    needs the process for ``headless_idle_seconds`` continuously, it shuts
    down too instead of squatting until the next reboot (P67 U3c).
    """

    def __init__(
        self,
        *,
        lease_seconds: float = 300.0,
        shutdown_grace_seconds: float = 60.0,
        close_grace_seconds: float = 15.0,
        headless_idle_seconds: float = 1800.0,
        poll_seconds: float = 0.25,
    ):
        self.lease_seconds = max(0.1, float(lease_seconds))
        self.shutdown_grace_seconds = max(0.0, float(shutdown_grace_seconds))
        self.close_grace_seconds = max(0.0, float(close_grace_seconds))
        self.headless_idle_seconds = max(0.0, float(headless_idle_seconds))
        self.poll_seconds = max(0.05, float(poll_seconds))
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._sessions: dict[str, float] = {}
        self._saw_frontend = False
        self._empty_since: float | None = None
        self._empty_grace_seconds = self.shutdown_grace_seconds
        self._headless_idle_since: float | None = None
        self._shutdown_started = False
        self._shutdown_callback: Callable[[], None] | None = None
        self._should_keep_alive: Callable[[], bool] | None = None
        self._thread: threading.Thread | None = None

    def set_shutdown_callback(
        self,
        callback: Callable[[], None],
        *,
        should_keep_alive: Callable[[], bool] | None = None,
    ) -> None:
        with self._lock:
            self._shutdown_callback = callback
            self._should_keep_alive = should_keep_alive
        # P67 U3c: the monitor used to start only on the first session update,
        # so a headless launch (daily schedule task) never had a monitor at
        # all and nothing could ever end it.
        self._ensure_monitor_locked()
        self._wake.set()

    def update(self, session_id: str, action: str) -> dict[str, object]:
        session_id = (session_id or "").strip()
        action = (action or "").strip().lower()
        if not session_id or len(session_id) > 128:
            raise ValueError("a valid frontend session_id is required")
        if action not in {"open", "heartbeat", "close"}:
            raise ValueError("action must be open, heartbeat, or close")

        now = time.monotonic()
        with self._lock:
            if action == "close":
                self._sessions.pop(session_id, None)
                if self._saw_frontend and not self._sessions and self._empty_since is None:
                    self._empty_since = now
                    # 显式 close 走短宽限快路径；失联（租约到期）才用长宽限。
                    self._empty_grace_seconds = self.close_grace_seconds
            else:
                self._sessions[session_id] = now
                self._saw_frontend = True
                self._empty_since = None
                self._ensure_monitor_locked()
            active = len(self._sessions)
            pending = bool(self._saw_frontend and not self._sessions)
        self._wake.set()
        return {"active_sessions": active, "shutdown_pending": pending}

    def close_all_for_native_window(self) -> None:
        """P68: the native shell window closed — every session departs now.

        The webview window is destroyed without a pagehide beacon, so the
        page's own frontend-session row would otherwise linger until the
        300s lease and delay the exit by minutes. This is the deterministic
        replacement for that beacon: every tracked session takes the same
        explicit-close fast path a browser close takes (close grace and the
        work predicate unchanged), and a genuinely live browser page simply
        re-registers itself on its next heartbeat.
        """
        with self._lock:
            self._sessions.clear()
            # The window counts as a frontend even if its page's open beacon
            # never arrived (WebView2 can destroy the page mid-launch).
            self._saw_frontend = True
            if self._empty_since is None:
                self._empty_since = time.monotonic()
                self._empty_grace_seconds = self.close_grace_seconds
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.poll_seconds * 4))

    def _ensure_monitor_locked(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._monitor,
            name="frontend-session-monitor",
            daemon=True,
        )
        self._thread.start()

    def _monitor(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.poll_seconds)
            self._wake.clear()
            now = time.monotonic()
            callback = None
            with self._lock:
                expired = [
                    session_id
                    for session_id, seen_at in self._sessions.items()
                    if now - seen_at >= self.lease_seconds
                ]
                for session_id in expired:
                    self._sessions.pop(session_id, None)
                if self._saw_frontend and not self._sessions:
                    try:
                        protected_by_work = bool(
                            self._should_keep_alive is not None
                            and self._should_keep_alive()
                        )
                    except Exception:
                        # A transient status-read failure must never kill a
                        # worker whose state we could not verify.
                        protected_by_work = True
                    if protected_by_work:
                        self._empty_since = None
                    else:
                        if self._empty_since is None:
                            self._empty_since = now
                            self._empty_grace_seconds = self.shutdown_grace_seconds
                        if (
                            not self._shutdown_started
                            and self._shutdown_callback is not None
                            and now - self._empty_since >= self._empty_grace_seconds
                        ):
                            self._shutdown_started = True
                            callback = self._shutdown_callback
                elif not self._saw_frontend:
                    # P67 U3c (DEF-2①): a managed --no-open launch (the daily
                    # schedule task) never sees a browser page, so the
                    # saw_frontend paths above can never fire. Once nothing
                    # needs the process for the bounded headless idle window,
                    # end it the same way a closed page would.
                    try:
                        protected_by_work = bool(
                            self._should_keep_alive is not None
                            and self._should_keep_alive()
                        )
                    except Exception:
                        # Same rule as above: an unreadable state protects.
                        protected_by_work = True
                    if protected_by_work or self._shutdown_callback is None:
                        self._headless_idle_since = None
                    elif self._headless_idle_since is None:
                        self._headless_idle_since = now
                    elif (
                        not self._shutdown_started
                        and now - self._headless_idle_since >= self.headless_idle_seconds
                    ):
                        self._shutdown_started = True
                        callback = self._shutdown_callback
                else:
                    self._empty_since = None
            if callback is not None:
                callback()
                return


# ---- C2-3FIX-1 大读面条件 gzip（契约保持） -----------------------------------
# 慢根因（PERF-N20 检查点③）：subtitles/segments 逐 cue 展示 JSON 26-33MB 裸传，
# 学生每次开讲都付全量线传+解析成本。gzip1 对该载荷 ≈8-12× 缩线；只在客户端
# 显式声明 Accept-Encoding: gzip 时启用（浏览器恒发；不带该头的测试/脚本拿到
# 逐字节原样响应，契约零变化），小响应压缩得不偿失故设门槛。zlib 压缩释放 GIL，
# 压缩窗媒体/其他请求照常并发（task_store SSE 自截止语义零关联，SSE 不走本门）。
_GZIP_MIN_BYTES = 256 * 1024


def _client_accepts_gzip(handler: BaseHTTPRequestHandler) -> bool:
    accepted = str(handler.headers.get("Accept-Encoding") or "")
    return "gzip" in accepted.lower()


def _gzip_body_if_accepted(handler: BaseHTTPRequestHandler, body: bytes) -> bytes | None:
    if len(body) < _GZIP_MIN_BYTES or not _client_accepts_gzip(handler):
        return None
    return gzip.compress(body, 1)


def json_response(handler: BaseHTTPRequestHandler, data: object, status: int = 200) -> None:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    wire = _gzip_body_if_accepted(handler, body)
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    if wire is not None:
        handler.send_header("Content-Encoding", "gzip")
    handler.send_header("Content-Length", str(len(wire if wire is not None else body)))
    handler.end_headers()
    handler.wfile.write(wire if wire is not None else body)


def json_response_headers(
    handler: BaseHTTPRequestHandler, data: object, *, status: int = 200,
    headers: dict[str, str] | None = None,
) -> None:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


CLIENT_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


def read_json(handler: BaseHTTPRequestHandler, *, max_bytes: int = 2 * 1024 * 1024) -> dict:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    if length > int(max_bytes):
        raise OverflowError("request_body_too_large")
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8"))


def parse_byte_range(value: str, size: int) -> tuple[int, int] | None:
    """Parse one RFC 7233 byte range, returning an inclusive interval."""
    value = str(value or "").strip()
    if not value:
        return None
    if size <= 0 or not value.startswith("bytes=") or "," in value:
        raise ValueError("unsupported byte range")
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if match is None or not any(match.groups()):
        raise ValueError("invalid byte range")
    start_text, end_text = match.groups()
    if not start_text:
        suffix = int(end_text)
        if suffix <= 0:
            raise ValueError("invalid suffix range")
        return max(0, size - suffix), size - 1
    start = int(start_text)
    if start >= size:
        raise ValueError("range starts after end of file")
    end = size - 1 if not end_text else min(int(end_text), size - 1)
    if end < start:
        raise ValueError("range end precedes start")
    return start, end


def srt_to_vtt_bytes(path: Path) -> bytes:
    """Convert the small SRT syntax differences required by HTML tracks."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    text = re.sub(
        r"(?m)^(\d{2}:\d{2}:\d{2}),(\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2}),(\d{3})",
        r"\1.\2 --> \3.\4",
        text,
    )
    return ("WEBVTT\n\n" + text.lstrip()).encode("utf-8")


def _vtt_timestamp(ms: int) -> str:
    hours, remainder = divmod(max(0, int(ms)), 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _vtt_bytes(segments: list[dict]) -> bytes:
    """Serialize parsed cue segments back to WebVTT (same units as the panel)."""
    lines = ["WEBVTT"]
    for segment in segments:
        lines.append("")
        lines.append(
            f"{_vtt_timestamp(int(segment['start_ms']))} --> {_vtt_timestamp(int(segment['end_ms']))}"
        )
        lines.append(str(segment.get("text") or ""))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _auth_keepalive_of(service: Any) -> Callable[[], Any] | None:
    """Resolve the application keepalive nudge through the auth boundary.

    处理器只持有服务边界对象；application 方法经由已绑定的
    auth_catalog.authentication_snapshot.__self__ 到达。测试桩没有绑定的
    application 时返回 None（保持纯遥测行为）。
    """
    snapshot = getattr(getattr(service, "auth_catalog", None), "authentication_snapshot", None)
    owner = getattr(snapshot, "__self__", None)
    nudge = getattr(owner, "nudge_client_verification", None)
    return nudge if callable(nudge) else None


def nudge_auth_keepalive(keepalive: Callable[[], Any] | None) -> None:
    """Heartbeat paths must never surface keepalive failures."""
    if keepalive is None:
        return
    try:
        keepalive()
    except Exception:
        pass


class MidRequestDisconnectLog:
    """SRC-SYNDROME-1 U3（第廿九案）：mid-request 断连行的节流单行日志。

    该行=客户端生命周期固有噪音（页面刷新/关闭瞬间打断在途 fetch、关窗时
    SSE+轮询同断），对会话状态零语义影响（U1 判定）。原样每断连一行会在
    关窗/刷新风暴时刷屏且无时间戳。此处：①每行带本地时间戳；②60s 窗口内
    至多一行，窗口内后续断连只计数，下一条行尾带抑制计数。闭集文本、零
    请求细节，线程安全。
    """

    INTERVAL_SECONDS = 60.0

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        writer: Callable[[str], None] | None = None,
    ) -> None:
        self._clock = clock
        self._writer = writer or (lambda line: (sys.stderr.write(line), sys.stderr.flush()))
        self._lock = threading.Lock()
        self._last_emitted = float("-inf")
        self._suppressed = 0

    def emit(self) -> None:
        now = self._clock()
        with self._lock:
            if now - self._last_emitted < self.INTERVAL_SECONDS:
                self._suppressed += 1
                return
            suppressed = self._suppressed
            self._last_emitted = now
            self._suppressed = 0
        suffix = f" (suppressed {suppressed} in previous {int(self.INTERVAL_SECONDS)}s)" if suppressed else ""
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._writer(f"{stamp} http: client connection ended mid-request{suffix}\n")


class HealthProbeTrace:
    """DISPATCH-HEALTH-1（第四十案）：健康探测到达留痕（闭集、节流、零载荷）。

    断连横幅由前端三次连续探测失败点亮（shell.js），而服务端对探测此前完全
    无痕：``log_message`` 关闭、每 10s 一次的探测不可能逐条记录。第四十案取证
    因此无法回答「探测究竟有没有到达服务端」，也就无法把「页面报断开、进程
    却健在」归因到客户端还是服务端。这里只记两类闭集行，60s 内至多一行：

    ① 静默恢复：距上次探测 ≥ ``SILENCE_SECONDS`` 才又有探测——那一段客户端
       根本没有探测（页面冻结/连接池饥饿/标签页被节流），横幅多半是假阴性；
    ② 非就绪：探测到达但生命周期不是 ready（如 draining）——横幅有据，但
       「请重启客户端」措辞失真。

    只记时刻与生命周期状态（闭集小写词，其余收敛为 unknown），零请求细节、
    零账号、零载荷；写失败绝不影响健康响应。
    """

    SILENCE_SECONDS = 60.0
    INTERVAL_SECONDS = 60.0

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        writer: Callable[[str], None] | None = None,
    ) -> None:
        self._clock = clock
        self._writer = writer or (lambda line: (sys.stderr.write(line), sys.stderr.flush()))
        self._lock = threading.Lock()
        self._last_seen: float | None = None
        self._last_emitted = float("-inf")

    def note(self, state: str) -> None:
        try:
            now = self._clock()
            with self._lock:
                gap = None if self._last_seen is None else now - self._last_seen
                self._last_seen = now
                if state != "ready":
                    label = state if re.fullmatch(r"[a-z_]{1,24}", str(state or "")) else "unknown"
                    detail = f"health probe served while lifecycle={label}"
                elif gap is not None and gap >= self.SILENCE_SECONDS:
                    detail = f"health probe resumed after {int(gap)}s of silence"
                else:
                    return
                if now - self._last_emitted < self.INTERVAL_SECONDS:
                    return
                self._last_emitted = now
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._writer(f"{stamp} http: {detail}\n")
        except Exception:
            # 留痕是诊断面，永远不得改变健康响应或抛出。
            pass


def make_handler(
    service: CourseLensServices,
    frontend_dir: Path,
    *,
    frontend_sessions: FrontendSessionRegistry | None = None,
    health_trace: HealthProbeTrace | None = None,
):
    """Build the loopback-only v3 HTTP request handler."""

    frontend_root = frontend_dir.resolve()
    auth_keepalive = _auth_keepalive_of(service)
    disconnect_log = MidRequestDisconnectLog()
    health_trace = health_trace or HealthProbeTrace()
    # MEDIA-001-20261001：媒体开流结局闭集账本（每服务实例一份，纯内存）。
    media_stream_ledger = MediaStreamFailureLedger()

    class Handler(BaseHTTPRequestHandler):
        # NIGHT2-W21：空闲连接 30 秒回收（异常标签页不再常驻后端线程）。
        # 媒体/流式路由在读到路由后显式 settimeout(None) 豁免——暂停播放的
        # keep-alive 媒体连接空闲超时被切会让学生恢复播放时断流。
        timeout = 30

        def log_message(self, fmt: str, *args) -> None:
            return

        def handle_one_request(self):
            """连接中止族单行脱敏（FRONTEND-SMOOTH-1 单元F）。

            学生在页面刷新/关闭瞬间打断本地请求时，请求行读取或响应写回会抛
            ConnectionResetError/ConnectionAbortedError（WinError 10053/10054），
            默认沿 socketserver 冒泡到 handle_error 打整段 traceback。这里就地
            收口为单行提示并标记 close_connection；未知错误仍向服务器
            handle_error 传播，保留全栈可诊断性。
            """
            try:
                super().handle_one_request()
            except CLIENT_DISCONNECT_ERRORS:
                self.close_connection = True
                # SRC-SYNDROME-1 U3：时间戳+60s 节流（U1 判定=生命周期固有噪音，
                # 零会话语义影响；保留单行闭集文本）。
                disconnect_log.emit()

        def end_headers(self) -> None:
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
                "font-src 'self'; object-src 'none'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'self'",
            )
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header(
                "Permissions-Policy",
                "camera=(), microphone=(), geolocation=()",
            )
            super().end_headers()

        def _request_context_allowed(self, *, state_changing: bool) -> bool:
            host = str(self.headers.get("Host") or "").strip()
            try:
                parsed_host = urlparse(f"//{host}")
                host_name = (parsed_host.hostname or "").casefold()
                host_port = parsed_host.port
            except ValueError:
                host_name = ""
                host_port = None
            server_port = int(self.server.server_address[1])
            if (
                host_name not in {"127.0.0.1", "localhost", "::1"}
                or parsed_host.username is not None
                or parsed_host.password is not None
                or (host_port is not None and host_port != server_port)
            ):
                json_response(self, {"error": "request host is not allowed"}, HTTPStatus.FORBIDDEN)
                return False

            fetch_site = str(self.headers.get("Sec-Fetch-Site") or "").strip().casefold()
            if fetch_site == "cross-site" and (
                state_changing or urlparse(self.path).path.startswith("/api/")
            ):
                json_response(self, {"error": "cross-site request is not allowed"}, HTTPStatus.FORBIDDEN)
                return False

            origin = str(self.headers.get("Origin") or "").strip()
            if state_changing and origin:
                try:
                    parsed_origin = urlparse(origin)
                    origin_name = (parsed_origin.hostname or "").casefold()
                    origin_port = parsed_origin.port
                except ValueError:
                    origin_name = ""
                    origin_port = None
                if (
                    parsed_origin.scheme != "http"
                    or origin_name not in {"127.0.0.1", "localhost", "::1"}
                    or parsed_origin.username is not None
                    or parsed_origin.password is not None
                    or parsed_origin.path not in {"", "/"}
                    or bool(parsed_origin.params or parsed_origin.query or parsed_origin.fragment)
                    or origin_port != server_port
                ):
                    json_response(self, {"error": "request origin is not allowed"}, HTTPStatus.FORBIDDEN)
                    return False
            return True

        def _course_session_ready(self) -> bool:
            return service.auth_catalog.authentication_snapshot().get("state") == "ready"

        def _course_session_state(self) -> str:
            """三态会话门（AUTOLOGIN-LOCAL-FIRST-1③，只读本地状态，零网络）。

            ready=现状；restoring-trusted=恢复期本地可信上下文（会话在途校验
            checking 且存在已保存账号并启用自动登录）——隐私放宽记档仅限此场景；
            rejected=现状拒绝（含 degraded 确认失败：不构成「正在登录」语境，
            既有 fudan_login_required 文案链原样）。
            """
            auth = service.auth_catalog.authentication_snapshot()
            if auth.get("state") == "ready":
                return "ready"
            if auth.get("state") == "checking" and _auto_login_session_resumable(service):
                return "restoring-trusted"
            return "rejected"

        def _reject_course_access(self, *, restoring: bool = False) -> None:
            if restoring:
                json_response(self, {
                    "error": "Fudan session is restoring",
                    "error_code": "fudan_session_restoring",
                    "actions": ["login"],
                }, HTTPStatus.UNAUTHORIZED)
                return
            json_response(self, {
                "error": "Fudan authentication is required",
                "error_code": "fudan_login_required",
                "actions": ["login"],
            }, HTTPStatus.UNAUTHORIZED)

        def _course_gate_envelope(self) -> dict:
            """Closed envelope for privileged inline routes while not ready.

            校验未决（checking，如复用窗口过期但客户端仍在、后台校验在途）必须
            如实报 checking，绝不伪报 fudan_login_required；确认失败仍 fail-closed。
            """
            auth = service.auth_catalog.authentication_snapshot()
            if auth.get("state") == "ready":
                return {}
            checking = auth.get("state") == "checking"
            actions = list(auth.get("actions") or [])
            if not actions and not checking:
                actions = ["login"]
            return {
                "state": "checking" if checking else "action_required",
                "source": "local",
                "observed_at": time.time(),
                "expires_at": 0,
                "code": str(auth.get("code") or "fudan_login_required"),
                "actions": actions,
            }

        def _lifecycle_allows_request(self) -> bool:
            lifecycle = getattr(service, "lifecycle", None)
            accepting = getattr(lifecycle, "accepting_requests", None)
            if lifecycle is None or accepting is None or accepting():
                return True
            snapshot = lifecycle.snapshot()
            json_response(self, {
                "error": "Local service is not accepting new work",
                "error_code": "LIFECYCLE_E_DRAINING",
                "lifecycle": snapshot,
            }, HTTPStatus.SERVICE_UNAVAILABLE)
            return False

        def do_GET(self) -> None:
            try:
                if not self._request_context_allowed(state_changing=False):
                    return
                parsed = urlparse(self.path)
                if parsed.path == "/api/health":
                    lifecycle_service = getattr(service, "lifecycle", None)
                    lifecycle_snapshot = getattr(lifecycle_service, "snapshot", None)
                    lifecycle = (
                        lifecycle_snapshot()
                        if callable(lifecycle_snapshot)
                        else {
                            "state": "ready",
                            "stage": "serving",
                            "error_code": "",
                            "accepting_requests": True,
                        }
                    )
                    health_state = str(lifecycle.get("state") or "")
                    health_trace.note(health_state)
                    json_response(self, {
                        "ok": health_state == "ready",
                        "service": "fudan-courselens",
                        "schema_version": 1,
                        "instance_id": PROJECT_INSTANCE_ID,
                        "pid": os.getpid(),
                        "version": _client_version(),
                        "lifecycle": lifecycle,
                    })
                    return
                if parsed.path.startswith("/api/") and not self._lifecycle_allows_request():
                    return
                if parsed.path in {"/api/v3/media", "/api/v3/subtitles/file", "/api/v3/courseware-pdf/file", "/api/v3/materials/file", "/api/v3/study-stats/file"}:
                    self._serve_learning_asset(parsed, head_only=False)
                    return
                if parsed.path == "/api/v3/data-migration/file":
                    self._serve_migration_download()
                    return
                if parsed.path.startswith("/api/v3/live-room/play/"):
                    self._serve_live_room_asset(parsed)
                    return
                if parsed.path.startswith("/api/v3/"):
                    self._handle_api_v3_get(parsed)
                    return
                if parsed.path.startswith("/api/"):
                    json_response(self, {"error": "route not found"}, HTTPStatus.NOT_FOUND)
                    return
                self._serve_static(parsed.path)
            except CLIENT_DISCONNECT_ERRORS:
                return
            except Exception:
                # D3（CUI-2）observability：宽 except 吞异常前先留痕（堆栈入本地日志，
                # 不打请求体/敏感值）——学生面仍恒 500 闭集码，排查不再靠盲猜。
                logging.exception("api request failed: %s", self.command)
                json_response(self, {
                    "error": "Internal server error",
                    "error_code": "runtime_failed",
                }, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_HEAD(self) -> None:
            try:
                if not self._request_context_allowed(state_changing=False):
                    return
                parsed = urlparse(self.path)
                if not self._lifecycle_allows_request():
                    return
                if parsed.path in {"/api/v3/media", "/api/v3/subtitles/file", "/api/v3/courseware-pdf/file", "/api/v3/materials/file", "/api/v3/study-stats/file"}:
                    self._serve_learning_asset(parsed, head_only=True)
                    return
                self.send_error(HTTPStatus.NOT_FOUND)
            except CLIENT_DISCONNECT_ERRORS:
                return
            except Exception:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Request failed")

        def do_POST(self) -> None:
            try:
                if not self._request_context_allowed(state_changing=True):
                    return
                parsed = urlparse(self.path)
                if not self._lifecycle_allows_request():
                    return
                if not parsed.path.startswith("/api/v3/"):
                    json_response(self, {"error": "route not found"}, HTTPStatus.NOT_FOUND)
                    return
                # 数据搬家包上传（D12 P0）：原始字节流式落盘，不走 read_json——
                # 包体可达 GB 级，JSON/base64 缓冲既慢又吃内存。
                if parsed.path == "/api/v3/data-migration/package":
                    self._handle_data_migration_upload()
                    return
                try:
                    body = read_json(
                        self,
                        max_bytes=46 * 1024 * 1024 if parsed.path == "/api/v3/documents" else 2 * 1024 * 1024,
                    )
                except OverflowError:
                    json_response(self, {
                        "error": "Request body is too large",
                        "error_code": "request_body_too_large",
                    }, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                    return
                except ValueError:
                    json_response(self, {
                        "error": "Request body is invalid",
                        "error_code": "request_body_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                if parsed.path == "/api/v3/lifecycle/shutdown":
                    service.lifecycle.request_shutdown("api_request")
                    json_response(self, {
                        "accepted": True,
                        "lifecycle": service.lifecycle.snapshot(),
                    }, HTTPStatus.ACCEPTED)
                    return
                self._handle_api_v3_post(parsed, body)
            except CLIENT_DISCONNECT_ERRORS:
                return
            except Exception:
                # D3（CUI-2）observability：宽 except 吞异常前先留痕（堆栈入本地日志，
                # 不打请求体/敏感值）——学生面仍恒 500 闭集码，排查不再靠盲猜。
                logging.exception("api request failed: %s", self.command)
                json_response(self, {
                    "error": "Internal server error",
                    "error_code": "runtime_failed",
                }, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_PUT(self) -> None:
            try:
                if not self._request_context_allowed(state_changing=True):
                    return
                parsed = urlparse(self.path)
                if not self._lifecycle_allows_request():
                    return
                if not parsed.path.startswith("/api/v3/"):
                    json_response(self, {"error": "route not found"}, HTTPStatus.NOT_FOUND)
                    return
                try:
                    body = read_json(self, max_bytes=2 * 1024 * 1024)
                except (OverflowError, ValueError):
                    json_response(self, {
                        "error": "Request body is invalid",
                        "error_code": "request_body_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                self._handle_api_v3_put(parsed, body)
            except CLIENT_DISCONNECT_ERRORS:
                return
            except Exception:
                # D3（CUI-2）observability：宽 except 吞异常前先留痕（堆栈入本地日志，
                # 不打请求体/敏感值）——学生面仍恒 500 闭集码，排查不再靠盲猜。
                logging.exception("api request failed: %s", self.command)
                json_response(self, {
                    "error": "Internal server error",
                    "error_code": "runtime_failed",
                }, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_DELETE(self) -> None:
            # PLAYER-UX-1④：删除族。门控与 POST/PUT 同一状态变更门（Host/跨站/
            # Origin 校验），路由闭集当前仅书签删除——纯本地数据，无学校会话依赖。
            try:
                if not self._request_context_allowed(state_changing=True):
                    return
                parsed = urlparse(self.path)
                if not self._lifecycle_allows_request():
                    return
                if not parsed.path.startswith("/api/v3/"):
                    json_response(self, {"error": "route not found"}, HTTPStatus.NOT_FOUND)
                    return
                try:
                    body = read_json(self, max_bytes=2 * 1024 * 1024)
                except (OverflowError, ValueError):
                    json_response(self, {
                        "error": "Request body is invalid",
                        "error_code": "request_body_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                self._handle_api_v3_delete(parsed, body)
            except CLIENT_DISCONNECT_ERRORS:
                return
            except Exception:
                logging.exception("api request failed: %s", self.command)
                json_response(self, {
                    "error": "Internal server error",
                    "error_code": "runtime_failed",
                }, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _serve_learning_asset(self, parsed, *, head_only: bool) -> bool:
            if parsed.path == "/api/v3/study-stats/file":
                # STUDY-STATS-M3 学习统计导出文件直下（materials 直交另存同法）。
                # 文件名闭集正则=唯一通行证：只服 study-stats-exports 目录内
                # 本动作产出的三个命名，任何路径穿越/任意文件读取面为零。
                if self._course_session_state() == "rejected":
                    self.send_error(HTTPStatus.UNAUTHORIZED, "Fudan login is required")
                    return True
                query = parse_qs(parsed.query)
                name = query.get("name", [""])[0].strip()
                if not re.fullmatch(
                    r"courselens-study-stats-\d{8}(\-daily|\-lectures)?\.(json|csv)", name
                ):
                    json_response(self, {"error": "study-stats export name is invalid",
                                         "error_code": "study_stats_export_unavailable"},
                                  HTTPStatus.NOT_FOUND)
                    return True
                learning_store = service.learning.repository
                candidate = Path(learning_store.path).parent / "study-stats-exports" / name
                try:
                    if not candidate.resolve().parent.samefile(
                        (Path(learning_store.path).parent / "study-stats-exports").resolve()
                    ) or not candidate.is_file():
                        raise FileNotFoundError
                except (FileNotFoundError, OSError):
                    json_response(self, {"error": "Study-stats export is unavailable",
                                         "error_code": "study_stats_export_unavailable"},
                                  HTTPStatus.NOT_FOUND)
                    return True
                content_type = "application/json" if name.endswith(".json") else "text/csv"
                self._stream_file(candidate, content_type, head_only=head_only, no_store=True)
                return True
            if parsed.path == "/api/v3/materials/file":
                # 统一文件中心导出（DATA-DELETE-REPAIR-1）：现有文件直交流式
                # 给另存对话框，绝不在应用目录造第二份副本；路径校验全部在
                # materials_center_file_path 内闭包完成。登录态 rejected 与
                # 其余课程面同门（无账号/未保存原硬门原样）。
                if self._course_session_state() == "rejected":
                    self.send_error(HTTPStatus.UNAUTHORIZED, "Fudan login is required")
                    return True
                query = parse_qs(parsed.query)
                kind = query.get("kind", [""])[0].strip()
                entry_id = query.get("id", [""])[0].strip()
                if not kind or not entry_id:
                    json_response(self, {"error": "kind and id are required"}, HTTPStatus.BAD_REQUEST)
                    return True
                learning_store = service.learning.repository
                try:
                    path, content_type = materials_center_file_path(
                        data_root=Path(learning_store.path).parent,
                        kind=kind, entry_id=entry_id,
                    )
                except (KeyError, FileNotFoundError):
                    self.send_error(HTTPStatus.NOT_FOUND, "Materials file is unavailable")
                    return True
                self._stream_file(path, content_type, head_only=head_only, no_store=True)
                return True
            if parsed.path == "/api/v3/courseware-pdf/file":
                query = parse_qs(parsed.query)
                sub_id = query.get("sub_id", [""])[0].strip()
                if not sub_id:
                    json_response(self, {"error": "sub_id is required"}, HTTPStatus.BAD_REQUEST)
                    return True
                try:
                    path = service.tasks.courseware_pdf_file_path(sub_id)
                except PermissionError:
                    self.send_error(HTTPStatus.UNAUTHORIZED, "Courseware authorization is required")
                    return True
                except FileNotFoundError:
                    self.send_error(HTTPStatus.NOT_FOUND, "Courseware PDF is unavailable")
                    return True
                self._stream_file(
                    path, "application/pdf", head_only=head_only, no_store=True,
                )
                return True
            if parsed.path not in {"/api/v3/media", "/api/v3/subtitles/file", "/api/v3/courseware-pdf/file"}:
                return False
            query = parse_qs(parsed.query)
            sub_id = query.get("sub_id", [""])[0].strip()
            if not sub_id:
                json_response(self, {"error": "sub_id is required"}, HTTPStatus.BAD_REQUEST)
                return True
            if parsed.path == "/api/v3/media":
                try:
                    stream = service.media_session.open_remote_media(
                        sub_id,
                        self.headers.get("Range", ""),
                        head_only=head_only,
                    )
                except ValueError:
                    # MEDIA-001-20261001：开流结局全记账（闭集码），错误卡据此细分
                    media_stream_ledger.note("invalid")
                    self.send_error(HTTPStatus.BAD_REQUEST, "Invalid media request")
                    return True
                except PermissionError:
                    media_stream_ledger.note("auth_required")
                    self.send_error(HTTPStatus.UNAUTHORIZED, "Media authorization is required")
                    return True
                except FileNotFoundError:
                    media_stream_ledger.note("missing")
                    self.send_error(HTTPStatus.NOT_FOUND, "Media is unavailable")
                    return True
                except Exception as exc:
                    # M1（LIVE-VALIDATE-2）：502 前落一行闭集证据，夜间上游不可用
                    # 不再零线索——异常类名足以路由归因（网络/认证刷新/上游拒绝）；
                    # 消息文本可能携带上游签名 URL，绝不落盘。与 [live] 行同款式，
                    # launcher tee 进 state/last-launch.log。
                    print(
                        f"[media] failure_code=media_upstream_unavailable route=open kind={type(exc).__name__}",
                        flush=True,
                    )
                    # MEDIA-001-20261001：同一结局闭集码交给错误卡细分
                    # （校外实测 ConnectionError→upstream_unreachable→校外场景卡）。
                    media_stream_ledger.note(classify_open_failure(exc))
                    self.send_error(HTTPStatus.BAD_GATEWAY, "Remote media is temporarily unavailable")
                    return True
                media_stream_ledger.note(MEDIA_STREAM_SUCCESS)
                self._stream_remote_media(stream, head_only=head_only)
                return True
            try:
                path = service.media_session.subtitle_file_path(sub_id)
            except FileNotFoundError:
                self.send_error(HTTPStatus.NOT_FOUND, "Subtitle is unavailable")
                return True
            if path is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return True
            # 与阅读面板同一 cue 单元：parse → 长句切分 → AS13 确定性整形 →
            # 序列化 VTT（进程内缓存，键=源文件身份；FileNotFoundError 404 分支不变）
            parsed_segments = split_long_cues(parse_subtitle_file(path))
            stat = path.stat()
            shaped = shape_display_cues_cached(
                (str(path), stat.st_mtime_ns, stat.st_size), parsed_segments,
            )
            self._send_bytes(
                _vtt_bytes(shaped),
                "text/vtt; charset=utf-8",
                head_only=head_only,
                compress_gzip=True,
            )
            return True

        def _handle_api_v3_get(self, parsed) -> None:
            route = parsed.path.removeprefix("/api/v3/").strip("/")
            query = parse_qs(parsed.query)
            if route == "greeting-context":
                # 问候课量尾句（AUTOLOGIN-LOCAL-FIRST-1①，本地优先）：显式豁免
                # 会话门——纯本地缓存读，零学校网络；输出仅闭集键，绝不携带
                # 课程名/ID/数字/个人数据；无保存账号=诚实无数据键（原硬门原样）。
                json_response(self, {"tail_key": _greeting_tail_key(service)})
                return
            if route in {
                "bookmarks", "quizzes", "review-plans", "documents", "smart-timeline",
                "schedules", "references", "subtitles/segments", "progress", "artifacts",
                "search", "search-index", "documents/preview", "courseware-pdf",
                "course-data", "course-data/lectures", "materials", "watch-events",
                # 课程复习读面（N7K）：只读本地课程知识快照与本地目录缓存，
                # 零学校网络，故并入本地优先三态门；刷新动作仍走严格 ready 门。
                "course-review", "course-review/lecture", "course-review/assessment",
                # 闪卡读面（RR-P4FSRS-1）：只读本地快照派生 + 本地 FSRS 状态，同门。
                "course-review/flashcards",
            }:
                # AUTOLOGIN-LOCAL-FIRST-1③ 三态门：ready=现状；restoring-trusted
                # 放行为纯本地缓存读（16 路由 handler 均只读本地存储/本地文件/
                # 本地索引，零学校网络）；rejected 现状拒绝（无账号/未保存原硬门
                # 原样）。
                if self._course_session_state() == "rejected":
                    self._reject_course_access()
                    return
            if route.startswith("timetable") and self._course_session_state() == "rejected":
                # SRC-SYNDROME-1 U2（第廿八案）：课表读族（GET snapshot/export.ics，
                # 纯本地 store 读，零学校网络）并入 AUTOLOGIN-LOCAL-FIRST-1③ 三态门
                # ——启动自动登录（checking+restoring-trusted）期间放行上次快照，
                # 消灭「先验证再显示」。rejected（真未登录）原样 401。
                # timetable/actions POST 仍是严格 ready 门（上游刷新动作不放宽）。
                json_response(self, {
                    "error": "Fudan authentication is required for timetable access",
                    "error_code": "timetable_login_required",
                    "actions": ["login"],
                }, HTTPStatus.UNAUTHORIZED)
                return
            if route == "media/stream-status":
                # MEDIA-001-20261001：媒体开流结局的闭集回读（纯内存账本，无
                # 课程/网络/凭据数据）。错误卡凭它把 code=4 细分成校外场景卡/
                # 授权卡/通用卡。诊断类读面，不设课程会话门（同 campus-diagnostics）。
                # MEDIA-VPN-1-20261001：aTrust 在位三态并入同一回读（纯本机探测，
                # 零外联；闭集键，无进程列表/路径/代理地址值）——upstream_unreachable
                # 细分卡据此再分「未装→引导安装 / 在位→确认接入+开关指路」。
                json_response(self, api_v3_envelope({
                    **media_stream_ledger.status(),
                    "atrust": atrust_presence_snapshot(),
                }))
                return
            if route == "events":
                self._serve_remote_events(query)
                return
            if route == "live-room/status":
                course_id = query.get("course_id", [""])[0].strip()
                if not course_id:
                    json_response(self, {
                        "error": "course_id is required", "error_code": "course_id_required",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope(service.live_room.status(course_id)))
                return
            if route == "transcript/segments":
                # 平台原生文稿增量读（直播二期乙1/LIVESTUDY-1 F4）：icourse 的
                # since_ms 含尾水位契约原样透传，缓存与会话生命周期一致、不落盘。
                # 授权门在 application.transcript_segments（与字幕文件路由同界）；
                # 会话门=严格 ready（本端点走学校网络，restoring-trusted 不放行）。
                sub_id = query.get("sub_id", [""])[0].strip()
                since_raw = query.get("since_ms", [""])[0].strip()
                if not sub_id:
                    json_response(self, {
                        "error": "sub_id is required", "error_code": "sub_id_required",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    since_ms = int(since_raw) if since_raw else None
                except ValueError:
                    json_response(self, {
                        "error": "since_ms must be an integer", "error_code": "transcript_since_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                if not self._course_session_ready():
                    self._reject_course_access(
                        restoring=self._course_session_state() == "restoring-trusted",
                    )
                    return
                try:
                    segments = service.media_session.transcript_segments(sub_id, since_ms)
                except FileNotFoundError:
                    json_response(self, {
                        "error": "Lecture is not in the authorized catalog",
                        "error_code": "transcript_lecture_unknown",
                    }, HTTPStatus.NOT_FOUND)
                    return
                if segments is None:
                    json_response(self, {
                        "error": "Platform transcript is temporarily unavailable",
                        "error_code": "transcript_unavailable",
                    }, HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                json_response(self, api_v3_envelope({
                    "available": True, "segments": segments,
                }))
                return
            if route == "health":
                json_response(self, api_v3_envelope({"ok": True, "api": "v3"}))
                return
            if route == "authentication":
                # 附加 courselens.vpn-connection.v1 快照（增量键）：15s 轮询即可
                # 驱动校园连接卡真实快照视图；消费端按 schema 识别，旧前端忽略。
                # 窄桩没有 connection_snapshot 时保持既有形状（诚实回退视图）。
                authentication = service.auth_catalog.authentication_snapshot()
                attach_connection = getattr(service.auth_catalog, "connection_snapshot", None)
                if callable(attach_connection):
                    authentication = {**authentication, "connection": attach_connection()}
                # SRC-SYNDROME-1 U2（第廿八案）：闭集增量键 restoring_trusted——
                # checking 且自动登录可恢复时为 True，前端课表据此开闸「缓存先行
                # 显示」。与 _course_session_state 的三态判定同一谓词，单一出处。
                if authentication.get("state") == "checking" and _auto_login_session_resumable(service):
                    authentication = {**authentication, "restoring_trusted": True}
                json_response(self, api_v3_envelope(authentication))
                return
            if route == "campus-diagnostics":
                # P1-D 按需诊断：只在显式请求时运行有界无凭据探测（无轮询）。
                json_response(self, api_v3_envelope(service.auth_catalog.campus_diagnostics()))
                return
            if route == "data-map":
                # DATAMAP-P1「你的数据在哪里」：三域静态声明（privacy-notice
                # v1.2 同口径）+ 本机只读类别计数（复用 course-data 只读聚合，
                # 不读内容列）+ 外联主机具名闭集 10 主机。零新采集、零网络、
                # 无凭据无课程标识；诊断类读面，不设课程会话门（同
                # campus-diagnostics），首跑 onboarding 尾页即可查看。
                json_response(self, api_v3_envelope(data_map_snapshot(
                    learning_store=service.learning.repository,
                    catalog_repository=service.auth_catalog.catalog,
                    task_store=service.tasks.repository,
                )))
                return
            if route == "accounts":
                json_response(self, api_v3_envelope({
                    "accounts": service.auth_catalog.credentials.list_accounts(),
                    "deepseek": {
                        "configured": bool(service.settings.has_deepseek_key()),
                        "saved": service.auth_catalog.credentials.has_deepseek_key(),
                        "requires_rotation": service.auth_catalog.credentials.deepseek_key_requires_rotation(),
                    },
                }))
                return
            if route == "settings":
                json_response(self, api_v3_envelope({
                    **service.settings.privacy_snapshot(),
                    "network": service.settings.network.snapshot(),
                    "onboarding": service.auth_catalog.onboarding_snapshot(),
                    # P3 深度问答 key 门读本路由（search-palette refreshDeepReadiness）；
                    # 三元组与 accounts 路由同式同源，只有布尔，无 key 物料。
                    "deepseek": {
                        "configured": bool(service.settings.has_deepseek_key()),
                        "saved": service.auth_catalog.credentials.has_deepseek_key(),
                        "requires_rotation": service.auth_catalog.credentials.deepseek_key_requires_rotation(),
                    },
                    "max_deepseek_tokens": service.max_deepseek_tokens_limit(),
                    "ai_usage_month": service.ai_usage_month(),
                    # AS6 消耗透镜：本月云端任务实际消耗（本机累计）。
                    "task_usage_month": service.task_usage_month(),
                }))
                return
            if route == "deepseek-balance":
                # AS6：设置页显式拉取（打开页至多一次，后端另有缓存 TTL）；
                # 无 key 零外联。只读，不含任何 key 物料。
                json_response(self, api_v3_envelope(service.deepseek_balance_snapshot()))
                return
            if route == "client-update":
                json_response(self, api_v3_envelope(service.client_update.snapshot()))
                return
            if route == "onboarding":
                json_response(self, api_v3_envelope({
                    **service.auth_catalog.onboarding_snapshot(),
                    "guide": service.auth_catalog.onboarding_guide_snapshot(),
                }))
                return
            if route == "app-shell":
                json_response(self, api_v3_envelope(service.auth_catalog.app_shell_snapshot()))
                return
            if route == "progress":
                sub_id = query.get("sub_id", [""])[0].strip()
                if not sub_id:
                    json_response(self, {
                        "error": "sub_id is required", "error_code": "sub_id_required",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    progress = service.media_session.watch_progress(sub_id)
                except FileNotFoundError:
                    # APP500-CLOSE（MEDIA-RETRY-1 停车场同族）：讲次不在授权
                    # 目录的 FileNotFoundError 曾穿透本出口（无 try），被顶层
                    # 兜底洗成 500 runtime_failed——学生看到生硬错误而非
                    # 「刷新目录即可同步」的人话指引。404 闭集码与
                    # courseware-pdf 读面同形，前端 api.js 码表既有同话文案。
                    json_response(self, {
                        "error": "Lecture is not in the authorized catalog",
                        "error_code": "lecture_not_found",
                    }, HTTPStatus.NOT_FOUND)
                    return
                json_response(self, api_v3_envelope({"progress": progress}))
                return
            if route == "artifacts":
                sub_id = query.get("sub_id", [""])[0].strip()
                kind = query.get("kind", ["lecture_summary"])[0].strip() or "lecture_summary"
                if not sub_id:
                    json_response(self, {
                        "error": "sub_id is required", "error_code": "sub_id_required",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    artifact = service.learning.ai_artifact(sub_id, kind)
                except FileNotFoundError:
                    # C3（PB-1）：「尚无产物」是常态而非错误——改 200+空载荷信封，
                    # 打开讲次不再往浏览器 console 记结构性错误噪音；
                    # artifact_not_found 码随本改造退役（前端码表同笔删除）。
                    json_response(self, api_v3_envelope({"artifact": None}))
                    return
                json_response(self, api_v3_envelope({"artifact": artifact}))
                return
            if route == "search":
                raw_query = query.get("q", [""])[0]
                # F1（FUZZ-INPUT-1 P2）：控制字符 query 曾穿透校验打穿检索深链
                # → 500 runtime_failed 闭集破口。边界消毒：Cc 控制符（保留
                # \t\n\r 有意义空白）与 DEL 一律 400 闭集码——垃圾输入永不进
                # 检索深链，与 emoji 超长同走 search_request_invalid 正确形态。
                if any((ord(ch) < 32 and ch not in "\t\n\r") or ord(ch) == 127 for ch in raw_query):
                    json_response(self, {
                        "error": "Search request is invalid",
                        "error_code": "search_request_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                course_ids = [
                    value.strip() for value in query.get("course_ids", [""])[0].split(",")
                    if value.strip()
                ]
                sources = [
                    value.strip() for value in query.get("sources", [""])[0].split(",")
                    if value.strip()
                ]
                try:
                    value = service.learning.search_learning(
                        query.get("q", [""])[0],
                        course_ids=course_ids,
                        sources=sources,
                        limit=int(query.get("limit", ["20"])[0]),
                        offset=int(query.get("offset", ["0"])[0]),
                    )
                except (TypeError, ValueError):
                    json_response(self, {
                        "error": "Search request is invalid",
                        "error_code": "search_request_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope(value))
                return
            if route == "search/answer":
                # P3 深度问答读出面（DEFECT-2 根修）：任务公共形状 + search_answers
                # 行同封。404 一律带 task_task_unknown（前端 search-palette.js:644
                # 按码分支出「任务不在了」人话）；路由就绪后不再有「路由未就绪」
                # 裸 404 降级窗口。answer=None=任务在而记录未落（前端如实呈
                # 「结果记录还没就绪」，绝不伪造答案）。
                task_id = str(query.get("task_id", [""])[0]).strip()
                if not task_id:
                    json_response(self, {
                        "error": "Search answer task id is required",
                        "error_code": "search_answer_task_required",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                task = service.tasks.repository.get_task(task_id)
                if task is None:
                    json_response(self, {
                        "error": "Task was not found",
                        "error_code": "task_task_unknown",
                    }, HTTPStatus.NOT_FOUND)
                    return
                answer = load_search_answer(service.learning.repository.path, task_id=task_id)
                values = public_tasks(service.tasks.repository, [task])
                json_response(self, api_v3_envelope({
                    "task": values[0] if values else None,
                    "answer": answer,
                }))
                return
            if route == "search-index":
                json_response(self, api_v3_envelope(service.learning.search_index.status()))
                return
            if route == "documents/preview":
                document_id = query.get("document_id", [""])[0].strip()
                if not document_id:
                    json_response(self, {
                        "error": "document_id is required",
                        "error_code": "document_id_required",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    path = service.learning.learning_document_preview_path(document_id)
                except (FileNotFoundError, KeyError, PermissionError):
                    self.send_error(HTTPStatus.NOT_FOUND, "Document preview is unavailable")
                    return
                self._stream_file(
                    path,
                    mimetypes.guess_type(str(path))[0] or "application/octet-stream",
                    head_only=False,
                    allow_range=False,
                    no_store=True,
                )
                return
            if route == "timetable":
                try:
                    raw_week = query.get("week", [""])[0].strip()
                    week = int(raw_week) if raw_week else 0
                    if week and not 1 <= week <= 30:
                        raise TimetableError("timetable_week_invalid", "Week must be between 1 and 30")
                    value = service.timetable.snapshot(
                        query.get("semester_id", [""])[0].strip(), week
                    )
                except (TimetableError, ValueError) as exc:
                    json_response(self, {
                        "schema": "courselens.api.error.v1",
                        "error": "Timetable request is invalid",
                        "error_code": str(getattr(exc, "code", "timetable_week_invalid")),
                    }, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope(value))
                return
            if route == "timetable/export.ics":
                try:
                    filename, content = service.timetable.ics(
                        query.get("semester_id", [""])[0].strip()
                    )
                except TimetableError as exc:
                    status = HTTPStatus.CONFLICT if exc.code == "semester_start_required" else HTTPStatus.BAD_REQUEST
                    json_response(self, {
                        "schema": "courselens.api.error.v1",
                        "error": "Timetable calendar is unavailable",
                        "error_code": exc.code,
                    }, status)
                    return
                payload = content.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/calendar; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if route == "automation":
                refresh = query.get("refresh", [""])[0].strip().lower() in {"1", "true", "yes"}
                json_response(self, api_v3_envelope(service.automation.snapshot(refresh=refresh)))
                return
            if route == "automation/runs":
                json_response(self, api_v3_envelope({
                    "runs": service.tasks.repository.list_automation_runs(limit=100),
                    "observed_at": time.time(),
                }))
                return
            if route == "automation/imports":
                json_response(self, api_v3_envelope({
                    "imports": service.tasks.repository.list_automation_imports(limit=200),
                    "observed_at": time.time(),
                }))
                return
            if route == "catalog":
                ids = [v for v in query.get("course_ids", [""])[0].split(",") if v]
                q = query.get("q", [""])[0]
                teacher = query.get("teacher", [""])[0].casefold()
                term = query.get("term", [""])[0].casefold()
                try:
                    page = int(query.get("page", ["1"])[0] or 1)
                    page_size = int(query.get("page_size", ["24"])[0] or 24)
                except ValueError:
                    json_response(self, {"error": "catalog pagination is invalid", "error_code": "catalog_pagination_invalid"}, HTTPStatus.BAD_REQUEST)
                    return
                value = service.auth_catalog.authorized_catalog_snapshot(
                    page=page,
                    page_size=page_size,
                    query=q,
                    teacher=teacher,
                    term=term,
                    department=query.get("department", [""])[0],
                    status=query.get("status", [""])[0],
                )
                if ids:
                    wanted = set(ids)
                    value["courses"] = [item for item in value.get("courses") or [] if str(item.get("course_id")) in wanted]
                json_response(self, api_v3_envelope(value))
                return
            if route == "course-data":
                try:
                    params = validate_course_data_summary_query(query)
                except ValueError:
                    json_response(self, {
                        "error": "Course data pagination is invalid",
                        "error_code": "course_data_page_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope(_course_data_inventory(service).summary(**params)))
                return
            if route == "course-data/lectures":
                try:
                    params = validate_course_data_lectures_query(query)
                except ValueError:
                    json_response(self, {
                        "error": "Course data lecture request is invalid",
                        "error_code": "course_data_page_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope(_course_data_inventory(service).lecture_page(**params)))
                return
            if route == "assessment":
                # 考核雷达台账（N5A-P1）：本地规则抽取的考核事件只读列表；
                # 确认/忽略走 assessment/actions。凭据/会话永不进入本路由。
                value = assessment_events_scan(
                    service.learning.repository,
                    service.auth_catalog.catalog,
                    course_id=str((query.get("course_id") or [""])[0]).strip(),
                )
                json_response(self, api_v3_envelope(value))
                return
            if route == "materials":
                # 统一文件中心（DATA-DELETE-REPAIR-1）：导入资料/课件 PDF/
                # AI 总结导出的三类条目聚合，只读有界扫描，闭包与数据页同
                # 一 pair universe；凭据/会话/令牌永不进入本路由。
                learning_store = service.learning.repository
                value = materials_center_scan(
                    data_root=Path(learning_store.path).parent,
                    catalog_repository=service.auth_catalog.catalog,
                    learning_store=learning_store,
                    task_store=service.tasks.repository,
                    course_id=str((query.get("course_id") or [""])[0]).strip(),
                )
                json_response(self, api_v3_envelope(value))
                return
            if route == "tasks":
                gate = self._course_gate_envelope()
                if gate:
                    json_response(self, api_v3_envelope({
                        **gate,
                        "tasks": [], "counts": {"active": None, "failed": None, "completed": None},
                    }))
                    return
                # CLIENT-STATE-R1：任务抽屉主数据源走「最近 200」窗口——
                # newest_first 让窗口取最新而非最旧，旧终态任务不再长驻。
                recent = service.tasks.repository.list_tasks(limit=200, newest_first=True)
                values = public_tasks(service.tasks.repository, recent)
                json_response(self, api_v3_envelope({
                    "tasks": values,
                    "counts": {
                        "active": sum(1 for item in values if item["state"] in {"queued", "running", "pausing", "paused"}),
                        "failed": sum(1 for item in values if item["state"] == "failed"),
                        "completed": sum(1 for item in values if item["state"] == "completed"),
                    },
                    # 「今日计算保护」每日预算已整体移除（无门可告警）；字段保留
                    # 恒 None 形状以兼容前端 tasks-drawer 的既有消费点。
                    "protection_alert": None,
                    "observed_at": time.time(),
                }))
                return
            if route == "features":
                json_response(self, api_v3_envelope({"flags": service.tasks.repository.get_feature_flags()}))
                return
            if route == "references":
                json_response(self, api_v3_envelope({"supported_sources": ["transcript", "ppt", "document", "summary", "quiz", "bookmark"]}))
                return
            if route == "schedules":
                json_response(self, api_v3_envelope({"schedule": service.tasks.repository.get_app_state("daily_schedule", {
                    "enabled": False,
                    "time": "07:30",
                    "course_ids": [],
                    "outputs": ["subtitle"],
                    "catch_up": True,
                })}))
                return
            if route == "remote-connection":
                # 等待窗新鲜语义（INIT-PATH-POLISH-1 单元A）：仅新增只读查询
                # 参数，动作形状不变；常规无参读完全不受影响（零参读模型——
                # 包括测试替身——保持原形，fresh 仅在有参时透传）。
                fresh = str(query.get("fresh", [""])[0]).strip().lower() in {"1", "true"}
                if fresh:
                    value = service.remote_compute.connection_snapshot(fresh=True)
                else:
                    value = service.remote_compute.connection_snapshot()
                json_response(self, api_v3_envelope(value))
                return
            if route == "remote-runs":
                json_response(self, api_v3_envelope({"runs": service.remote_compute.runs_snapshot()}))
                return
            if route == "course-review/flashcards":
                # 闪卡读面（RR-P4FSRS-1）：本地快照懒派生 + 本地 FSRS 队列，零学校网络。
                flash_course_id = query.get("course_id", [""])[0].strip()
                if not flash_course_id or len(flash_course_id) > 200:
                    json_response(self, {
                        "error": "course_id is required",
                        "error_code": "course_review_request_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    deck = service.learning.course_flashcards(flash_course_id)
                except CourseReviewActionError as exc:
                    json_response(self, {
                        "error": "Course review is unavailable for this course",
                        "error_code": str(getattr(exc, "code", "") or "course_review_failed"),
                    }, HTTPStatus.NOT_FOUND)
                    return
                json_response(self, api_v3_envelope(deck))
                return
            if route in {"course-review", "course-review/lecture", "course-review/assessment"}:
                course_id = query.get("course_id", [""])[0].strip()
                review_sub_id = query.get("sub_id", [""])[0].strip()
                if not course_id or len(course_id) > 200 or len(review_sub_id) > 200:
                    json_response(self, {
                        "error": "course_id is required",
                        "error_code": "course_review_request_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                if route == "course-review/lecture" and not review_sub_id:
                    json_response(self, {
                        "error": "sub_id is required for a lecture detail view",
                        "error_code": "course_review_request_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    payload = service.learning.course_review(
                        course_id,
                        # 题目工作台永远是课程级的：即使带了 sub_id 也按课程级取，
                        # 免得返回一个「讲次明细」里必然为空的题目面。
                        sub_id="" if route == "course-review/assessment" else review_sub_id,
                        include_personal_notes=str(query.get("include_notes", ["0"])[0]).strip()
                        in {"1", "true"},
                    )
                except CourseReviewActionError as exc:
                    code = str(getattr(exc, "code", "") or "course_review_failed")
                    json_response(self, {
                        "error": "Course review is unavailable for this course",
                        # 讲次不存在与课程不存在分开报：前端据此提示"这一讲还没进目录"。
                        "error_code": "lecture_not_found" if code == "course_review_lecture_unknown" else code,
                    }, HTTPStatus.NOT_FOUND)
                    return
                if route == "course-review/assessment":
                    workspace = payload.get("assessment_workspace")
                    if workspace is None:
                        workspace = service.learning.course_review(
                            course_id, include_personal_notes=payload.get("include_personal_notes", False)
                        ).get("assessment_workspace")
                    json_response(self, api_v3_envelope(workspace or {
                        "view": "assessment_workspace", "course_id": course_id,
                        "items": [], "counts": {"total": 0, "lectures": 0, "course_level": 0},
                        "updated_at": 0.0,
                    }))
                    return
                if route == "course-review/lecture":
                    json_response(self, api_v3_envelope(payload.get("view") or {}))
                    return
                json_response(self, api_v3_envelope(payload))
                return
            if route == "bookmarks":
                sub_id = query.get("sub_id", [""])[0]
                json_response(self, api_v3_envelope({"bookmarks": service.learning.list_question_bookmarks(sub_id)}))
                return
            if route == "watch-events":
                watch_sub = query.get("sub_id", [""])[0].strip()
                if not watch_sub:
                    json_response(self, {
                        "error": "sub_id is required",
                        "error_code": "watch_events_sub_id_required",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope({"events": service.learning.list_watch_events(watch_sub)}))
                return
            if route == "concepts":
                gate = self._course_gate_envelope()
                if gate:
                    json_response(self, api_v3_envelope({
                        **gate,
                        "concepts": [], "edges": [], "courses": [],
                    }))
                    return
                course_id = query.get("course_id", [""])[0].strip()
                json_response(self, api_v3_envelope(service.learning.cross_course_concepts(course_id)))
                return
            if route == "analytics":
                gate = self._course_gate_envelope()
                if gate:
                    json_response(self, api_v3_envelope({
                        **gate,
                        "settings": {"enabled": False}, "summary": None,
                    }))
                    return
                term = query.get("term", [""])[0].strip()
                json_response(self, api_v3_envelope(service.learning.learning_analytics(term)))
                return
            if route == "study/overview":
                # STUDY-STATS-M1 默认层读面：纯本地聚合（学习时长/到期待办/掌握度
                # 简版），零学校网络零遥测；会话未决时 gate 信封 + first_run 引导态
                # （卡片诚实降级，绝不阻塞着陆页）。
                gate = self._course_gate_envelope()
                if gate:
                    json_response(self, api_v3_envelope({
                        **gate,
                        "view": "study_overview", "first_run": True,
                        "week": {}, "due": {}, "mastery": {},
                    }))
                    return
                json_response(self, api_v3_envelope(service.learning.study_overview()))
                return
            if route == "study/detail":
                # STUDY-STATS-M2-b 展开层读面：全课程掌握度+逐讲明细+FSRS 7 日预测。
                # 与 study/overview 同门：会话未决时 gate 信封 + 空课程面（展开层
                # 只进不出，dialog 内诚实空态，绝不阻塞默认层）。
                gate = self._course_gate_envelope()
                if gate:
                    json_response(self, api_v3_envelope({
                        **gate,
                        "view": "study_detail", "forecast": [], "courses": [],
                    }))
                    return
                json_response(self, api_v3_envelope(service.learning.study_detail()))
                return
            if route == "analytics/study-summary":
                # AIRESEARCH H6 埋点读出（TELEMETRY-H64-1）：本地闭集计数。
                # 无会话门——遥测读出不依赖学校会话，也不属动作族；未开启时
                # summary=None+闭集 reason 由 analytics 层收口。
                json_response(self, api_v3_envelope(service.learning.study_telemetry_summary()))
                return
            if route == "quizzes":
                course_id = query.get("course_id", [""])[0]
                sub_id = query.get("sub_id", [""])[0]
                json_response(self, api_v3_envelope({"items": service.learning.list_quizzes(course_id, sub_id)}))
                return
            if route == "review-plans":
                json_response(self, api_v3_envelope({"plans": service.learning.list_review_plans()}))
                return
            if route == "documents":
                document_id = query.get("document_id", [""])[0].strip()
                try:
                    if document_id:
                        value = {"document": service.learning.learning_document(document_id)}
                    else:
                        course_id = query.get("course_id", [""])[0].strip()
                        sub_id = query.get("sub_id", [""])[0].strip()
                        value = {"documents": service.learning.list_learning_documents(course_id, sub_id)}
                except KeyError:
                    json_response(self, {
                        "error": "Document was not found",
                        "error_code": "document_not_found",
                    }, HTTPStatus.NOT_FOUND)
                    return
                json_response(self, api_v3_envelope(value))
                return
            if route == "alignments":
                document_id = query.get("document_id", [""])[0].strip()
                if not document_id:
                    json_response(self, {
                        "error": "document_id is required",
                        "error_code": "document_id_required",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    document = service.learning.learning_document(document_id)
                except KeyError:
                    json_response(self, {
                        "error": "Document was not found",
                        "error_code": "document_not_found",
                    }, HTTPStatus.NOT_FOUND)
                    return
                alignments = [
                    {
                        key: page.get(key)
                        for key in (
                            "alignment_id", "page_num", "start_ms", "end_ms",
                            "confidence", "status", "evidence", "alignment_updated_at",
                        )
                    }
                    for page in document.get("pages") or []
                    if page.get("alignment_id")
                ]
                json_response(self, api_v3_envelope({"document_id": document_id, "alignments": alignments}))
                return
            if route == "timeline":
                sub_id = query.get("sub_id", [""])[0].strip()
                if not sub_id:
                    json_response(self, {
                        "error": "sub_id is required",
                        "error_code": "timeline_sub_id_required",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope(service.learning.smart_timeline(sub_id)))
                return
            if route == "subtitles/segments":
                sub_id = query.get("sub_id", [""])[0].strip()
                if not sub_id:
                    json_response(self, {
                        "error": "sub_id is required",
                        "error_code": "sub_id_required",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope(service.media_session.subtitle_segments(sub_id)))
                return
            if route == "analytics":
                progress = service.learning.repository.list_watch_progress()
                json_response(self, api_v3_envelope({"courses": len({str(v.get('course_id') or '') for v in progress.values()}), "lectures": len(progress), "watched_seconds": round(sum(float(v.get("position_seconds") or 0) for v in progress.values()), 1)}))
                return
            if route == "courseware-pdf":
                sub_id = query.get("sub_id", [""])[0].strip()
                if not sub_id:
                    json_response(self, {
                        "error": "sub_id is required",
                        "error_code": "sub_id_required",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    status = service.tasks.courseware_pdf_status(sub_id)
                except FileNotFoundError:
                    json_response(self, {
                        "error": "Lecture is not in the authorized catalog",
                        "error_code": "lecture_not_found",
                    }, HTTPStatus.NOT_FOUND)
                    return
                json_response(self, api_v3_envelope(status))
                return
            json_response(self, {"error": "v3 route not found"}, HTTPStatus.NOT_FOUND)

        def _migration_error_response(self, exc: DataMigrationError) -> tuple[dict[str, Any], HTTPStatus]:
            code = str(getattr(exc, "code", "") or "MIGRATION_E_PACKAGE_INVALID")
            status = (
                HTTPStatus.CONFLICT
                if code in {"MIGRATION_E_BUSY", "MIGRATION_E_SCHEMA_TOO_NEW"}
                else HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                if code == "MIGRATION_E_PACKAGE_TOO_LARGE"
                else HTTPStatus.BAD_REQUEST
            )
            return {
                "schema": "courselens.api.error.v1",
                "error": "Data migration was not accepted",
                "error_code": code,
                # instruction 是引擎内写死的人话指引（闭集字符串，零敏感值）。
                "instruction": str(getattr(exc, "instruction", "") or ""),
            }, status

        def _handle_data_migration_upload(self) -> None:
            """数据搬家包上传（D12 P0）：Content-Length 界定大小，流式转写落盘。

            大小闭集在应用层 stage_upload（8 GiB）与 Content-Length 预检两层把
            关；正文永不进内存整包，也绝不进日志。成功回执只含 package_id/
            字节/哈希——都是非敏感形状。
            """
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = 0
            if length <= 0:
                json_response(self, {
                    "error": "Package body is required",
                    "error_code": "request_body_invalid",
                }, HTTPStatus.BAD_REQUEST)
                return
            if length > 8 * 1024 * 1024 * 1024:
                json_response(self, {
                    "error": "Request body is too large",
                    "error_code": "request_body_too_large",
                }, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            try:
                staged = service.settings.migration_stage_upload(self.rfile, expected_bytes=length)
            except DataMigrationError as exc:
                code = str(getattr(exc, "code", "") or "migration_package_invalid")
                json_response(self, {
                    "schema": "courselens.api.error.v1",
                    "error": "Migration package upload was not accepted",
                    "error_code": code,
                }, HTTPStatus.REQUEST_ENTITY_TOO_LARGE if code == "MIGRATION_E_PACKAGE_TOO_LARGE" else HTTPStatus.BAD_REQUEST)
                return
            except Exception:
                logging.exception("migration package upload failed")
                json_response(self, {
                    "error": "Internal server error",
                    "error_code": "runtime_failed",
                }, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            json_response(self, api_v3_envelope({
                "schema": "courselens.data-migration-upload.v1",
                **staged,
            }), HTTPStatus.CREATED)

        def _serve_migration_download(self) -> None:
            """导出包下载（D12 P0）：令牌单次有效；流出即删，不留第二份。"""
            query = parse_qs(urlparse(self.path).query)
            token = str(query.get("token", [""])[0] or "").strip()
            try:
                path, filename = service.settings.migration_download_file(token)
            except FileNotFoundError:
                json_response(self, {
                    "error": "Download link is no longer available",
                    "error_code": "migration_download_unavailable",
                }, HTTPStatus.GONE)
                return
            try:
                size = path.stat().st_size
            except OSError:
                json_response(self, {
                    "error": "Download link is no longer available",
                    "error_code": "migration_download_unavailable",
                }, HTTPStatus.GONE)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            # filename 由导出侧生成（courselens-data-<stamp>-<id>.clmig，ASCII），
            # 无引号无控制字符，头注入面不存在。
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                with path.open("rb") as source:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            finally:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

        def _handle_api_v3_post(self, parsed, body: dict[str, Any]) -> None:
            route = parsed.path.removeprefix("/api/v3/").strip("/")
            if route == "live-room/grants":
                try:
                    value = service.live_room.issue_grant(str(body.get("course_id") or ""))
                except LiveRoomError as exc:
                    json_response(self, {
                        "error": "Live room is unavailable", "error_code": exc.code,
                    }, exc.status)
                    return
                json_response(self, api_v3_envelope(value), HTTPStatus.CREATED)
                return
            if route == "live-room/sessions":
                try:
                    # 视角维度（直播二期乙1）：闭集校验在 consume_grant 内、
                    # grant 消费之前（live_view_unknown 400 可原地重试）。
                    value = dict(service.live_room.consume_grant(
                        str(body.get("grant") or ""),
                        view=str(body.get("view") or "").strip() or DEFAULT_LIVE_VIEW,
                    ))
                except LiveRoomError as exc:
                    json_response(self, {
                        "error": "Live room entry was rejected", "error_code": exc.code,
                    }, exc.status)
                    return
                session_id = str(value.pop("session_id"))
                cookie_path = str(value["manifest_path"]).split("/manifest/", 1)[0] + "/"
                # Cookie 寿命只对齐会话绝对上限；真实存活仍由 live_room 的
                # 滑动空闲窗裁决，浏览器侧不得早于服务端把凭据丢掉。
                json_response_headers(
                    self, api_v3_envelope(value), status=HTTPStatus.CREATED,
                    headers={"Set-Cookie": (
                        f"courselens_live={session_id}; Path={cookie_path}; "
                        f"HttpOnly; SameSite=Strict; Max-Age={int(PLAYBACK_ABSOLUTE_TTL)}"
                    )},
                )
                return
            if route in {
                "tasks/actions", "tasks/enqueue", "concepts/actions", "analytics/actions", "bookmarks", "bookmarks/actions",
                "quizzes/actions", "review-plans/actions", "documents/actions", "smart-timeline/actions",
                "progress", "search/answer", "search-index/actions", "courseware-pdf/actions",
                "course-data/actions", "client-reset/actions", "course-review/actions",
            } and not self._course_session_ready():
                # AUTOLOGIN-LOCAL-FIRST-1③：动作族维持拒绝（需学校网络/需会话）；
                # 恢复期本地可信上下文改用闭集新码 fudan_session_restoring（前端
                # 映射瞬态「正在登录」提示），其余现状码原样。
                self._reject_course_access(
                    restoring=self._course_session_state() == "restoring-trusted",
                )
                return
            if route == "timetable/actions" and not self._course_session_ready():
                json_response(self, {
                    "error": "Fudan authentication is required for timetable access",
                    "error_code": "timetable_login_required",
                    "actions": ["login"],
                }, HTTPStatus.UNAUTHORIZED)
                return
            if route == "frontend-session":
                try:
                    value = (
                        frontend_sessions.update(
                            str(body.get("session_id") or ""),
                            str(body.get("action") or ""),
                        )
                        if frontend_sessions is not None
                        else {"active_sessions": 0, "shutdown_pending": False}
                    )
                except ValueError:
                    json_response(self, {
                        "error": "Frontend session update is invalid",
                        "error_code": "frontend_session_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                # 打开/心跳顺带触发会话保活校验：任何异常静默，不得打断心跳响应
                if str(body.get("action") or "").strip().lower() in {"open", "heartbeat"}:
                    nudge_auth_keepalive(auth_keepalive)
                json_response(self, api_v3_envelope(value))
                return
            if route == "authentication/actions":
                action = str(body.get("action") or "").strip().lower()
                operation_id = str(body.get("operation_id") or "").strip()
                if (
                    action not in {"login", "use-saved", "logout", "refresh-catalog"}
                    or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", operation_id)
                ):
                    json_response(self, {
                        "error": "A supported authentication action and operation_id are required",
                        "error_code": "authentication_action_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                stored_id = f"authentication:{operation_id}"
                existing = service.tasks.repository.get_app_state(stored_id, None)
                if existing and not fresh_operations_requested():
                    json_response(self, api_v3_envelope(existing))
                    return
                try:
                    if action == "login":
                        service.auth_catalog.set_credentials(
                            str(body.get("student_id") or ""),
                            str(body.get("password") or ""),
                            remember=bool(body.get("remember")),
                        )
                        authentication = service.auth_catalog.refresh_authorized_catalog_async()
                    elif action == "use-saved":
                        service.auth_catalog.use_saved_credentials(str(body.get("student_id") or ""))
                        authentication = service.auth_catalog.refresh_authorized_catalog_async()
                    elif action == "refresh-catalog":
                        authentication = service.auth_catalog.refresh_authorized_catalog_async()
                    else:
                        authentication = service.auth_catalog.logout_fudan()
                except (KeyError, RuntimeError, ValueError) as exc:
                    code = "saved_account_unavailable" if action == "use-saved" else "authentication_request_invalid"
                    json_response(self, {
                        "error": "Authentication request could not be accepted",
                        "error_code": code,
                    }, HTTPStatus.BAD_REQUEST)
                    return
                result = {
                    "action": action,
                    "authentication": authentication,
                    "operation_id": operation_id,
                }
                service.tasks.repository.set_app_state(stored_id, result)
                status = HTTPStatus.ACCEPTED if action != "logout" else HTTPStatus.OK
                json_response(self, api_v3_envelope(result), status)
                return
            if route == "accounts/actions":
                action = str(body.get("action") or "").strip().lower()
                if action != "delete" or not str(body.get("student_id") or "").strip():
                    json_response(self, {
                        "error": "Account action is invalid",
                        "error_code": "account_action_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                deleted = service.auth_catalog.delete_saved_credentials(str(body.get("student_id") or ""))
                json_response(self, api_v3_envelope({"deleted": bool(deleted)}))
                return
            if route == "onboarding/actions":
                # 单机幂等偏好写入：不要求 operation_id，不触碰认证/目录/凭据协议
                action = body.get("action") if isinstance(body, dict) else None
                version = body.get("version") if isinstance(body, dict) else None
                if (
                    not isinstance(action, str)
                    or str(action or "").strip().lower() not in {"mark-opened", "dismiss", "complete"}
                    or not isinstance(version, str)
                ):
                    json_response(self, {
                        "error": "A supported onboarding action and version are required",
                        "error_code": "onboarding_action_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    guide = service.auth_catalog.onboarding_guide_action(
                        str(action).strip().lower(), version,
                    )
                except OnboardingGuideVersionError:
                    json_response(self, {
                        "error": "Onboarding guide version does not match the current version",
                        "error_code": "onboarding_version_conflict",
                        "guide": service.auth_catalog.onboarding_guide_snapshot(),
                    }, HTTPStatus.CONFLICT)
                    return
                json_response(self, api_v3_envelope({"guide": guide}))
                return
            if route == "secrets/actions":
                action = str(body.get("action") or "").strip().lower()
                try:
                    if action == "set-deepseek":
                        service.settings.set_deepseek_key(
                            str(body.get("api_key") or ""),
                            remember=bool(body.get("remember")),
                        )
                        deleted = False
                    elif action == "delete-deepseek":
                        deleted = service.settings.delete_deepseek_key()
                    else:
                        raise ValueError("unsupported secret action")
                except ValueError:
                    json_response(self, {
                        "error": "Secret action is invalid",
                        "error_code": "secret_action_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope({
                    "configured": bool(service.settings.has_deepseek_key()),
                    "saved": service.auth_catalog.credentials.has_deepseek_key(),
                    "deleted": bool(deleted),
                }))
                return
            if route == "settings/actions":
                action = str(body.get("action") or "").strip().lower()
                try:
                    if action == "update-network":
                        # D8：保存前闭集校验——手动代理必填且可解析；非手动模式带值
                        # 时同样要求可解析。脏值一律 proxy_url_invalid 拒绝（前端
                        # api.js 文案键同笔等集），不再「假成功」入库。
                        update_mode = str(body.get("mode") or "")
                        update_proxy = str(body.get("proxy_url") or "").strip()
                        if (update_mode == "manual" and not update_proxy) or (
                            update_proxy and not _proxy_url_parseable(update_proxy)
                        ):
                            json_response(self, {
                                "error": "Proxy URL is invalid",
                                "error_code": "proxy_url_invalid",
                            }, HTTPStatus.BAD_REQUEST)
                            return
                        value = service.settings.update_network_settings(update_mode, update_proxy)
                    elif action == "detect-proxy":
                        # 闭集只读检测（onboarding 代理卡）：不触代际、不写状态；
                        # 与 GET settings 同模式直接消费注入的 NetworkSettings。
                        # own_port=本服务监听端口：候选里的自家端口跳过，
                        # 杜绝把 CourseLens 自身响应误判为代理（N5FE-P4）。
                        value = service.settings.network.detect_proxy(
                            own_port=int(self.server.server_address[1]),
                        )
                    elif action == "diagnose-network":
                        value = service.settings.diagnose_network()
                    elif action == "set-max-deepseek-tokens":
                        value = service.set_max_deepseek_tokens(body.get("tokens"))
                    elif action == "update-consent":
                        value = service.settings.update_processing_consent(bool(body.get("accepted")))
                    elif action == "set-auto-connect":
                        value = service.settings.set_auto_connect_preference({
                            "fudan": body.get("fudan"),
                            "github": body.get("github"),
                        })
                    elif action == "set-update-background-checks":
                        value = service.settings.set_update_background_checks(body)
                    elif action == "set-media-stream-proxy":
                        # MEDIA-VPN-1：媒体流系统代理偏好（闭集动作，默认关；
                        # 脏请求经下方统一 settings_action_invalid 门）。
                        value = service.settings.set_media_stream_proxy(body)
                    else:
                        raise ValueError("unsupported settings action")
                except ValueError as exc:
                    if isinstance(exc, AutoConnectPreferenceError):
                        # 前置条件不满足：闭集可操作错误码，帮助用户完成准备。
                        json_response(self, {
                            "error": "Auto-connect preference was not accepted",
                            "error_code": str(getattr(exc, "code", "") or "auto_connect_request_invalid"),
                        }, HTTPStatus.CONFLICT)
                        return
                    json_response(self, {
                        "error": "Settings action is invalid",
                        "error_code": "settings_action_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope(value))
                return
            if route == "client-update/actions":
                try:
                    value = service.client_update.action(
                        str(body.get("action") or ""),
                        confirmed=bool(body.get("confirmed", False)),
                    )
                except UpdateError as exc:
                    code = str(getattr(exc, "code", "") or "update_action_failed")
                    allowed = {
                        "confirmation_required", "update_action_invalid", "update_busy",
                        "update_restart_blocked",
                    }
                    conflicted = code in {"update_busy", "update_restart_blocked"}
                    json_response(self, {
                        "schema": "courselens.api.error.v1",
                        "error": "Client update action was not accepted",
                        "error_code": code if code in allowed else "update_action_failed",
                        "retriable": conflicted,
                    }, HTTPStatus.CONFLICT if conflicted else HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope(value), HTTPStatus.ACCEPTED)
                # The 202 reply above is fully written once this handler line
                # runs; only then may the orchestrated restart begin.
                service.client_update.complete_restart()
                return
            if route == "assessment/actions":
                # 考核雷达用户动作（T5/T6）：confirm→confirmed / dismiss→dismissed。
                # 本地台账面动作（与 GET assessment 同族，凭据/会话永不进入，
                # 不加课程会话门）；此前误挂 GET 分发器致 POST 恒 404
                # （BROWSERWALK-3 F3-P1-3），整支挪入 POST 分发器。
                action = str(body.get("action") or "").strip().casefold()
                event_id = str(body.get("event_id") or "").strip()
                try:
                    value = assessment_events_action(
                        service.learning.repository, event_id=event_id, action=action,
                    )
                except AssessmentActionError:
                    json_response(self, {
                        "error": "Assessment action is invalid",
                        "error_code": "assessment_action_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                except KeyError:
                    json_response(self, {
                        "error": "Assessment event was not found",
                        "error_code": "assessment_event_missing",
                    }, HTTPStatus.NOT_FOUND)
                    return
                json_response(self, api_v3_envelope(value))
                return
            if route == "progress":
                try:
                    progress = service.media_session.save_watch_progress(
                        str(body.get("sub_id") or "").strip(),
                        position_seconds=float(body.get("position_seconds") or 0),
                        duration_seconds=float(body.get("duration_seconds") or 0),
                        playback_rate=float(body.get("playback_rate") or 1),
                        completed=bool(body.get("completed", False)),
                    )
                except FileNotFoundError:
                    # APP500-CLOSE 同族第二穿透：讲次不在授权目录曾逃过本出口
                    # 闭集（KeyError/TypeError/ValueError），被顶层兜底洗成 500。
                    # 404 闭集码与 GET progress 同形同码。
                    json_response(self, {
                        "error": "Lecture is not in the authorized catalog",
                        "error_code": "lecture_not_found",
                    }, HTTPStatus.NOT_FOUND)
                    return
                except (KeyError, TypeError, ValueError):
                    json_response(self, {
                        "error": "Progress update is invalid",
                        "error_code": "progress_update_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope({"progress": progress}))
                return
            if route == "search/answer":
                # P3 深度问答接线：mode 闭集 {"", "deep"} 由服务层裁决（缺席/
                # 空串=既有本地拼装逐字不变；"deep"=检索证据→云端问答链）。
                # 深度链 key 缺失经书签链同款闭集码回前端人话文案；快速路径
                # RuntimeError 保持原码，避免误导性 key 文案。
                answer_mode = str(body.get("mode") or "")
                try:
                    result = service.learning.answer_from_evidence(
                        str(body.get("query") or ""),
                        course_ids=[str(value) for value in body.get("course_ids") or []],
                        sub_id=str(body.get("sub_id") or ""),
                        mode=answer_mode,
                    )
                except RuntimeError:
                    if answer_mode == "deep":
                        json_response(self, {
                            "error": "Question explanation is not configured",
                            "error_code": "question_explanation_not_configured",
                            "retriable": True,
                        }, HTTPStatus.BAD_REQUEST)
                    else:
                        json_response(self, {
                            "error": "Grounded answer is unavailable",
                            "error_code": "grounded_answer_unavailable",
                        }, HTTPStatus.BAD_REQUEST)
                    return
                except (TypeError, ValueError):
                    json_response(self, {
                        "error": "Grounded answer is unavailable",
                        "error_code": "grounded_answer_unavailable",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope(result), HTTPStatus.ACCEPTED)
                return
            if route == "search-index/actions":
                if str(body.get("action") or "").strip().lower() != "retry":
                    json_response(self, {
                        "error": "Search index action is invalid",
                        "error_code": "search_index_action_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                service.learning.refresh_search_index()
                json_response(self, api_v3_envelope(service.learning.search_index.status()), HTTPStatus.ACCEPTED)
                return
            if route == "timetable/actions":
                action = str(body.get("action") or "").strip().lower()
                operation_id = str(body.get("operation_id") or "").strip()
                semester_id = str(body.get("semester_id") or "").strip()
                if (
                    action not in {"refresh", "set-semester-start"}
                    or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", operation_id)
                ):
                    json_response(self, {
                        "error": "A supported timetable action and operation_id are required",
                        "error_code": "timetable_action_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                scope = str(service.auth_catalog.identity_scope())
                stored_id = f"timetable-operation:{scope}:{operation_id}"
                existing = service.tasks.repository.get_app_state(stored_id, None)
                if existing and not fresh_operations_requested():
                    if existing.get("action") != action or existing.get("semester_id", "") != semester_id:
                        json_response(self, {
                            "error": "operation_id was already used for another timetable action",
                            "error_code": "operation_id_conflict",
                        }, HTTPStatus.CONFLICT)
                        return
                    json_response(self, api_v3_envelope(existing))
                    return
                try:
                    timetable = service.timetable.action(
                        action,
                        semester_id=semester_id,
                        start_date=str(body.get("start_date") or "").strip(),
                    )
                    result = {
                        "operation_id": operation_id,
                        "action": action,
                        "semester_id": semester_id,
                        "state": "completed",
                        "timetable": timetable,
                    }
                    service.tasks.repository.set_app_state(stored_id, result)
                except TimetableError as exc:
                    json_response(self, {
                        "schema": "courselens.api.error.v1",
                        "error": "Timetable operation failed",
                        "error_code": exc.code,
                        "retriable": exc.code in {
                            "timetable_upstream_unavailable", "timetable_session_expired",
                        },
                    }, HTTPStatus.BAD_GATEWAY if exc.code == "timetable_upstream_unavailable" else HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope(result))
                return
            if route == "automation/actions":
                try:
                    operation = service.automation.action(
                        str(body.get("action") or ""),
                        operation_id=str(body.get("operation_id") or ""),
                    )
                except (RuntimeError, ValueError) as exc:
                    code = str(getattr(exc, "code", "") or "automation_failed")
                    json_response(self, {
                        "schema": "courselens.api.error.v1",
                        "error": "Automation operation could not be accepted",
                        "error_code": code,
                        "retriable": code in {
                            "github_unreachable", "rate_limited", "timeout",
                            "cloud_cleanup_pending", "automation_failed",
                        },
                    }, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope({"operation": operation}), HTTPStatus.ACCEPTED)
                return
            if route == "remote-connection/actions":
                try:
                    operation = service.remote_compute.connection_action(
                        str(body.get("action") or ""),
                        operation_id=str(body.get("operation_id") or ""),
                        target_id=str(body.get("target_id") or ""),
                        force=bool(body.get("force")),
                    )
                except (KeyError, RuntimeError, ValueError) as exc:
                    code = str(getattr(exc, "code", "") or "operation_failed")
                    status = HTTPStatus.NOT_FOUND if isinstance(exc, KeyError) else HTTPStatus.BAD_REQUEST
                    json_response(self, {
                        "schema": "courselens.api.error.v1",
                        "error": "Remote operation could not be accepted",
                        "error_code": code,
                        "retriable": code in {
                            "github_unreachable", "rate_limited", "timeout",
                            "operation_failed", "remote_cleanup_pending",
                        },
                    }, status)
                    return
                json_response(self, api_v3_envelope({"operation": operation}), HTTPStatus.ACCEPTED)
                return
            if route == "tasks/actions":
                task_id = str(body.get("task_id") or "").strip()
                action = str(body.get("action") or "").strip().lower()
                operation_id = str(body.get("operation_id") or "").strip()
                if (
                    not task_id
                    or action not in {"pause", "resume", "retry", "cancel", "import_result"}
                    or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", operation_id)
                ):
                    json_response(self, {
                        "error": "A task, supported action and operation_id are required",
                        "error_code": "task_action_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                stored_id = f"task:{operation_id}"
                operation, created = service.tasks.repository.begin_remote_operation(stored_id, action, task_id)
                if not created and not fresh_operations_requested():
                    if (
                        str(operation.get("action") or "") != action
                        or str(operation.get("target_id") or "") != task_id
                    ):
                        json_response(self, {
                            "error": "operation_id was already used for another action",
                            "error_code": "operation_id_conflict",
                        }, HTTPStatus.CONFLICT)
                        return
                    task = service.tasks.repository.get_task(task_id)
                    json_response(self, api_v3_envelope({
                        "operation": {
                            "operation_id": operation_id, "action": action,
                            "state": operation.get("state"), "error_code": operation.get("error_code") or "",
                        },
                        "task": public_task(task, service.tasks.repository),
                    }))
                    return
                try:
                    result = service.tasks.control_task(task_id, action)
                    task = result.get("task")
                    operation = service.tasks.repository.finish_remote_operation(
                        stored_id, state="accepted",
                        result={"task_id": task_id, "accepted_action": action},
                    )
                except KeyError:
                    operation = service.tasks.repository.finish_remote_operation(
                        stored_id, state="failed", error_code="task_not_found"
                    )
                    json_response(self, {
                        "error": "Task was not found", "error_code": "task_not_found",
                    }, HTTPStatus.NOT_FOUND)
                    return
                except (RuntimeError, ValueError) as exc:
                    code = _task_error_code(exc) or "task_action_rejected"
                    service.tasks.repository.finish_remote_operation(
                        stored_id, state="failed", error_code=code
                    )
                    json_response(self, {
                        "error": "Task action was not accepted", "error_code": code,
                    }, HTTPStatus.CONFLICT)
                    return
                json_response(self, api_v3_envelope({
                    "operation": {
                        "operation_id": operation_id, "action": action,
                        "state": operation.get("state"), "error_code": operation.get("error_code") or "",
                    },
                    "task": public_task(task, service.tasks.repository),
                }), HTTPStatus.ACCEPTED)
                return
            if route == "tasks/delete":
                # U4 任务记录删除（用户显式动作，无自动清理）：只删记录行与其
                # 直接耦合行，绝不触产物文件；非终态拒绝（复用闭集码零扩表）。
                task_id = str(body.get("task_id") or "").strip()
                if not task_id:
                    json_response(self, {
                        "error": "task_id is required",
                        "error_code": "task_action_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    task = service.tasks.repository.delete_task(task_id)
                except KeyError:
                    json_response(self, {
                        "error": "Task was not found", "error_code": "task_not_found",
                    }, HTTPStatus.NOT_FOUND)
                    return
                except ValueError:
                    json_response(self, {
                        "error": "Only terminal task records can be deleted",
                        "error_code": "task_action_invalid",
                    }, HTTPStatus.CONFLICT)
                    return
                json_response(self, api_v3_envelope({
                    "deleted": True,
                    "task_id": task_id,
                    "state": str(task.get("state") or ""),
                }))
                return
            if route == "tasks/delete-failed":
                # U4 一键清理全部失败记录：确认文案由前端承载；后端只删记录行。
                deleted = service.tasks.repository.delete_failed_tasks()
                json_response(self, api_v3_envelope({"deleted": int(deleted)}))
                return
            if route == "tasks/clear-stuck":
                # DEAD-TASK-PURGE B：清除卡住的任务（用户显式动作，幂等）。
                # 判据双门在 store 层（30 分钟无进展 + 远端无活跃 run），
                # 宁漏勿杀；命中行标 failed（user_cleared_stuck）入 90 天
                # 终态保留窗；只动记录行，绝不触产物文件。
                cleared = service.tasks.repository.clear_stuck_tasks()
                json_response(self, api_v3_envelope({"cleared": int(len(cleared))}))
                return
            if route == "automation/runs-delete":
                # 第卅六案③：自动材料运行记录删除（用户显式动作）——只删记录行，
                # 绝不触已生成的产物；非终态拒绝（复用既有闭集码零扩表）。
                try:
                    result = service.tasks.repository.delete_automation_run(
                        str(body.get("run_key") or "")
                    )
                except KeyError:
                    json_response(self, {
                        "error": "Automation run was not found",
                        "error_code": "task_not_found",
                    }, HTTPStatus.NOT_FOUND)
                    return
                except ValueError:
                    json_response(self, {
                        "error": "Only terminal run records can be deleted",
                        "error_code": "task_action_invalid",
                    }, HTTPStatus.CONFLICT)
                    return
                json_response(self, api_v3_envelope(result))
                return
            if route == "tasks/enqueue":
                kind = str(body.get("kind") or "").strip().lower()
                course_id = str(body.get("course_id") or "").strip()
                sub_id = str(body.get("sub_id") or "").strip()
                if kind not in {"subtitle", "summary", "question"} or not course_id or not sub_id:
                    json_response(self, {
                        "error": "kind, course_id and sub_id are required",
                        "error_code": "task_enqueue_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    excerpt = _subtitle_excerpt(body) if kind == "subtitle" else {}
                except ValueError as exc:
                    json_response(self, {
                        "error": str(exc),
                        "error_code": "task_excerpt_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    if kind == "subtitle":
                        # 唯一 automatic 策略：行为由后端凭据配置选择，
                        # 请求体不再携带任何模式选择参数。
                        task = service.tasks.enqueue_subtitle(
                            course_id, sub_id,
                            **excerpt,
                        )
                    elif kind == "summary":
                        task = service.tasks.enqueue_summary(
                            course_id, sub_id,
                            include_ppt=bool(body.get("include_ppt", True)),
                            force=bool(body.get("force", False)),
                        )
                    else:
                        raise ValueError("question enqueue requires a bookmark")
                except (KeyError, RuntimeError, ValueError) as exc:
                    code = _task_error_code(exc) or "task_enqueue_failed"
                    json_response(self, {
                        "error": "Task could not be queued",
                        "error_code": code,
                        "retriable": code in {"network_unavailable", "remote_failed", "timeout"},
                    }, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope({"task": public_task(task, service.tasks.repository)}), HTTPStatus.ACCEPTED)
                return
            if route == "course-review/actions":
                course_id = str(body.get("course_id") or "").strip()
                action = str(body.get("action") or "").strip().lower()
                if (
                    not course_id
                    or len(course_id) > 200
                    or action not in COURSE_REVIEW_ACTIONS
                ):
                    json_response(self, {
                        "error": "course_id and a supported action are required",
                        "error_code": "course_review_action_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                if action == "refresh":
                    try:
                        receipt = service.learning.course_review_refresh(
                            course_id,
                            include_personal_notes=bool(body.get("include_personal_notes", False)),
                        )
                    except CourseReviewActionError as exc:
                        json_response(self, {
                            "error": "Course review is unavailable for this course",
                            "error_code": str(getattr(exc, "code", "") or "course_review_failed"),
                        }, HTTPStatus.NOT_FOUND)
                        return
                    json_response(self, api_v3_envelope(receipt), HTTPStatus.ACCEPTED)
                    return
                if action == "review_flashcard":
                    # 闪卡评分（RR-P4FSRS-1）：本地 FSRS 一步调度，同步返回下一间隔。
                    flashcard_id = str(body.get("card_id") or "").strip()
                    rating = body.get("rating")
                    # P2-11：rating 只收真整数——2.9→2/"3"→3/true→1 的强转会把
                    # 越界值洗进评分闭集（2.9 变成合法的 2），非整数一律 400，
                    # 不代为取整（宁拒勿猜）。
                    if not isinstance(rating, int) or isinstance(rating, bool):
                        json_response(self, {
                            "error": "card_id and rating are required",
                            "error_code": "course_review_action_invalid",
                        }, HTTPStatus.BAD_REQUEST)
                        return
                    try:
                        result = service.learning.course_flashcard_review(
                            course_id, flashcard_id, rating,
                        )
                    except CourseReviewActionError as exc:
                        code = str(getattr(exc, "code", "") or "course_review_action_invalid")
                        json_response(self, {
                            "error": "This flashcard could not be graded",
                            "error_code": code,
                        }, HTTPStatus.NOT_FOUND if code in COURSE_REVIEW_ACTION_NOT_FOUND else HTTPStatus.BAD_REQUEST)
                        return
                    except (TypeError, ValueError):
                        json_response(self, {
                            "error": "card_id and rating are required",
                            "error_code": "course_review_action_invalid",
                        }, HTTPStatus.BAD_REQUEST)
                        return
                    json_response(self, api_v3_envelope(result))
                    return
                if action in ("confirm_term_candidate", "dismiss_term_candidate"):
                    # P10 术语候选确认/忽略：轻量本地动作，同步返回台账状态；
                    # 资格门在 feedback 层（词对不在推导候选集 → 404 族）。
                    wrong = str(body.get("wrong") or "").strip()
                    right = str(body.get("right") or "").strip()
                    if not wrong or not right or len(wrong) > 64 or len(right) > 64:
                        json_response(self, {
                            "error": "wrong and right are required",
                            "error_code": "course_review_action_invalid",
                        }, HTTPStatus.BAD_REQUEST)
                        return
                    try:
                        result = service.learning.course_term_candidate_action(
                            course_id, action, wrong=wrong, right=right,
                        )
                    except CourseReviewActionError as exc:
                        code = str(getattr(exc, "code", "") or "course_review_action_invalid")
                        json_response(self, {
                            "error": "This term candidate could not be processed",
                            "error_code": code,
                        }, HTTPStatus.NOT_FOUND if code in COURSE_REVIEW_ACTION_NOT_FOUND else HTTPStatus.BAD_REQUEST)
                        return
                    json_response(self, api_v3_envelope(result))
                    return
                # explain_assessment：只有学生显式点题才走到这里；服务端再按
                # 课程闭集、题目身份、内容版本与证据充足度各自 fail-closed。
                item_id = str(body.get("item_id") or "").strip()
                if not item_id or len(item_id) > 128:
                    json_response(self, {
                        "error": "item_id is required for the explain_assessment action",
                        "error_code": "course_review_action_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    receipt = service.learning.explain_assessment_item(
                        course_id, item_id,
                        content_hash=str(body.get("content_hash") or "").strip()[:64],
                        retry=bool(body.get("retry", False)),
                    )
                except CourseReviewActionError as exc:
                    code = str(getattr(exc, "code", "") or "course_review_failed")
                    json_response(self, {
                        "error": "An AI answer is unavailable for this item",
                        "error_code": code,
                    }, HTTPStatus.NOT_FOUND if code in COURSE_REVIEW_ACTION_NOT_FOUND else HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope(receipt), HTTPStatus.ACCEPTED)
                return
            if route == "features":
                try:
                    name, enabled = validate_feature_update(body)
                    service.tasks.repository.set_feature_flag(name, enabled)
                except ValueError as exc:
                    json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope({"name": name, "enabled": enabled}))
                return
            if route == "references/validate":
                try:
                    reference = validate_reference_body(body)
                except ValueError as exc:
                    json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope({"reference": reference}))
                return
            if route == "schedules":
                try:
                    value = validate_schedule(body)
                    value = service.tasks.configure_daily_schedule(value)
                except (OSError, RuntimeError, ValueError) as exc:
                    json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope({"schedule": value}))
                return
            if route == "schedules/run":
                try:
                    result = service.tasks.run_daily_schedule(force=True)
                except (OSError, RuntimeError, ValueError) as exc:
                    json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope({"run": result}))
                return
            if route == "review-plans":
                try:
                    exam_at = float(body.get("exam_at") or 0)
                    if exam_at and exam_at <= time.time():
                        raise ValueError("exam_at must be in the future")
                    raw_scope = body.get("course_scope")
                    plan = service.learning.create_review_plan(
                        title=str(body.get("title") or "复习计划"),
                        exam_at=exam_at,
                        available_minutes=int(body.get("available_minutes") or 60),
                        course_id=str(body.get("course_id") or ""),
                        sub_id=str(body.get("sub_id") or ""),
                        daily_minutes=body.get("daily_minutes"),
                        strategy=str(body.get("strategy") or "coverage"),
                        course_scope=[str(value) for value in raw_scope] if isinstance(raw_scope, list) else None,
                    )
                except ReviewPlanValidationError as exc:
                    json_response(self, {
                        "error": "Review plan was rejected",
                        "error_code": str(exc.code),
                    }, HTTPStatus.BAD_REQUEST)
                    return
                except (KeyError, ValueError) as exc:
                    json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope({"plan": plan}))
                return
            if route == "quizzes":
                course_id = str(body.get("course_id") or "").strip()
                sub_id = str(body.get("sub_id") or "").strip()
                if not course_id or not sub_id:
                    json_response(self, {"error": "course_id and sub_id are required"}, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope({"items": service.tasks.generate_quiz(course_id, sub_id)}))
                return
            if route == "documents":
                action = str(body.get("action") or "import").strip().casefold()
                try:
                    if action == "delete":
                        document_id = str(body.get("document_id") or "").strip()
                        if not document_id:
                            raise ValueError("document_id_required")
                        service.learning.delete_learning_document(document_id)
                        json_response(self, api_v3_envelope({"deleted": True, "document_id": document_id}))
                        return
                    if action != "import":
                        raise ValueError("document_action_invalid")
                    document = service.learning.import_learning_document(
                        course_id=str(body.get("course_id") or ""),
                        sub_id=str(body.get("sub_id") or ""),
                        title=str(body.get("title") or ""),
                        original_name=str(body.get("original_name") or ""),
                        media_type=str(body.get("media_type") or ""),
                        content_base64=str(body.get("content_base64") or ""),
                        doc_type=str(body.get("doc_type") or "other"),
                        scope=str(body.get("scope") or "lecture"),
                    )
                except KeyError:
                    json_response(self, {
                        "error": "Document was not found",
                        "error_code": "document_not_found",
                    }, HTTPStatus.NOT_FOUND)
                    return
                except (OSError, RuntimeError, ValueError) as exc:
                    code = str(getattr(exc, "code", "") or str(exc) or "document_import_failed")
                    allowed_codes = {
                        "document_lecture_required", "document_type_unsupported",
                        "document_payload_invalid", "document_empty", "document_too_large",
                        "document_archive_too_large", "document_page_limit_exceeded",
                        "document_image_too_large", "document_format_invalid",
                        "document_password_required", "document_has_no_pages",
                        "document_text_encoding_invalid", "pdf_reader_unavailable",
                        "document_storage_invalid", "document_id_required",
                        "document_action_invalid", "document_import_failed",
                    }
                    if code not in allowed_codes:
                        code = "document_import_failed"
                    status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE if code == "document_too_large" else HTTPStatus.BAD_REQUEST
                    json_response(self, {
                        "error": "Document could not be imported",
                        "error_code": code,
                        "retriable": code in {"pdf_reader_unavailable", "document_import_failed"},
                    }, status)
                    return
                json_response(self, api_v3_envelope({"document": document}), HTTPStatus.CREATED)
                return
            if route == "materials/actions":
                # 统一文件中心单条动作（DATA-DELETE-REPAIR-1）：删除语义按类
                # 分流——文档=行+目录+索引刷新（既有绑定操作）；课件=有界
                # rmtree 该讲次 owned 目录（生成中拒绝）；总结=仅删导出 md，
                # learning_store 记录原样保留（时间轴/搜索不受影响）。
                action = str(body.get("action") or "").strip().casefold()
                kind = str(body.get("kind") or "").strip().casefold()
                entry_id = str(body.get("id") or "").strip()
                if action != "delete" or not kind or not entry_id:
                    json_response(self, {
                        "error": "Materials action is invalid",
                        "error_code": "materials_action_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                learning_store = service.learning.repository
                try:
                    if kind == "document":
                        service.learning.delete_learning_document(entry_id)
                        value = {"deleted": True, "kind": kind, "id": entry_id}
                    else:
                        value = materials_center_delete(
                            data_root=Path(learning_store.path).parent,
                            catalog_repository=service.auth_catalog.catalog,
                            learning_store=learning_store,
                            task_store=service.tasks.repository,
                            kind=kind, entry_id=entry_id,
                        )
                except MaterialsActionError as exc:
                    json_response(self, {
                        "error": "Materials action was not accepted",
                        "error_code": str(exc.code),
                    }, HTTPStatus.CONFLICT if exc.code == "materials_courseware_busy" else HTTPStatus.BAD_REQUEST)
                    return
                except (KeyError, FileNotFoundError):
                    json_response(self, {
                        "error": "Materials entry was not found",
                        "error_code": "materials_entry_missing",
                    }, HTTPStatus.NOT_FOUND)
                    return
                json_response(self, api_v3_envelope(value))
                return
            if route == "alignments":
                action = str(body.get("action") or "auto").strip().casefold()
                try:
                    if action == "auto":
                        document_id = str(body.get("document_id") or "").strip()
                        if not document_id:
                            raise ValueError("document_id_required")
                        result = service.learning.align_learning_document(document_id)
                        json_response(self, api_v3_envelope({"result": result}))
                        return
                    if action == "update":
                        alignment_id = str(body.get("alignment_id") or "").strip()
                        if not alignment_id:
                            raise ValueError("alignment_id_required")
                        alignment = service.learning.update_document_alignment(
                            alignment_id,
                            start_ms=int(body.get("start_ms") or 0),
                            end_ms=int(body.get("end_ms") or body.get("start_ms") or 0),
                            status=str(body.get("status") or "confirmed"),
                        )
                        json_response(self, api_v3_envelope({"alignment": alignment}))
                        return
                    raise ValueError("alignment_action_invalid")
                except KeyError as exc:
                    code = str(exc).strip("'") or "alignment_not_found"
                    json_response(self, {
                        "error": "Alignment target was not found",
                        "error_code": code,
                    }, HTTPStatus.NOT_FOUND)
                    return
                except (OSError, RuntimeError, ValueError) as exc:
                    code = str(getattr(exc, "code", "") or str(exc) or "alignment_failed")
                    if code not in {
                        "document_id_required", "alignment_id_required",
                        "alignment_action_invalid", "alignment_transcript_unavailable",
                        "alignment_status_invalid", "alignment_failed",
                    }:
                        code = "alignment_failed"
                    json_response(self, {
                        "error": "Document alignment could not be completed",
                        "error_code": code,
                        "retriable": code in {"alignment_transcript_unavailable"},
                    }, HTTPStatus.BAD_REQUEST)
                    return
            if route == "timeline/classify":
                course_id = str(body.get("course_id") or "").strip()
                sub_id = str(body.get("sub_id") or "").strip()
                if not course_id or not sub_id:
                    json_response(self, {
                        "error": "course_id and sub_id are required",
                        "error_code": "timeline_lecture_required",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    timeline = service.learning.classify_smart_timeline(course_id, sub_id)
                except (OSError, RuntimeError, ValueError) as exc:
                    code = str(exc) if str(exc) == "timeline_transcript_unavailable" else "timeline_classification_failed"
                    json_response(self, {
                        "error": "Timeline classification could not be completed",
                        "error_code": code,
                        "retriable": code == "timeline_transcript_unavailable",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope(timeline))
                return
            if route == "bookmarks":
                # RR-BOOKMARK-1：缺课程/讲次身份在边界即拒（同 DELETE 族 bookmark_id 惯法）；
                # 证据闭集码随 ValueError 分码返回，前端码表按码显人话。
                course_id = str(body.get("course_id") or "").strip()
                sub_id = str(body.get("sub_id") or "").strip()
                if not course_id or not sub_id:
                    json_response(self, {
                        "error": "course_id and sub_id are required",
                        "error_code": "bookmark_request_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    bookmark = service.learning.create_question_bookmark(
                        course_id,
                        sub_id,
                        int(body.get("start_ms") or 0),
                        int(body.get("end_ms") or body.get("start_ms") or 0),
                        str(body.get("note") or ""),
                    )
                except (KeyError, ValueError) as exc:
                    code = str(exc) if str(exc) == "bookmark_evidence_unavailable" else "bookmark_request_invalid"
                    json_response(self, {
                        "error": (
                            "This lecture has no transcript or material content to anchor a bookmark yet"
                            if code == "bookmark_evidence_unavailable" else "Bookmark request is invalid"
                        ),
                        "error_code": code,
                    }, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope({"bookmark": bookmark}), HTTPStatus.CREATED)
                return
            if route == "watch-events":
                events = body.get("events")
                if not isinstance(events, list):
                    json_response(self, {
                        "error": "events must be a list",
                        "error_code": "watch_events_batch_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                inserted = service.learning.record_watch_events(
                    str(body.get("course_id") or ""),
                    str(body.get("sub_id") or ""),
                    events,
                )
                json_response(self, api_v3_envelope({"inserted": inserted}))
                return
            if route == "watch-events/clear":
                cleared = service.learning.clear_watch_events(str(body.get("sub_id") or ""))
                json_response(self, api_v3_envelope({"deleted": cleared}))
                return
            if route == "study/heartbeat":
                # STUDY-STATS-M1 播放心跳（30s 一跳，零内容）：与 watch-events
                # 默认流同族同门（客户端 insight 开关门控，服务端闭集校验+聚合落库）；
                # 畸形跳=recorded=False 诚实回执，绝不挡播放主链。
                json_response(self, api_v3_envelope(service.learning.record_study_heartbeat(
                    str(body.get("course_id") or ""),
                    str(body.get("sub_id") or ""),
                    body.get("seconds"),
                )))
                return
            if route == "bookmarks/explain":
                bookmark_id = str(body.get("bookmark_id") or "").strip()
                if not bookmark_id:
                    json_response(self, {"error": "bookmark_id is required"}, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    result = service.learning.explain_question_bookmark(bookmark_id)
                except KeyError as exc:
                    json_response(self, {"error": str(exc)}, HTTPStatus.NOT_FOUND)
                    return
                except RuntimeError:
                    json_response(self, {
                        "error": "Question explanation is not configured",
                        "error_code": "question_explanation_not_configured",
                        "retriable": True,
                    }, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope({
                    "bookmark": result.get("bookmark"),
                    "task": public_task(result.get("task")),
                    "created": bool(result.get("created")),
                }))
                return
            if route == "quality-judge":
                # P11 质量抽检：按需触发，不进会话门集（只需本机数据+既有 key，
                # 不需要复旦会话）；闭集拒绝码照实映射（含 in_flight 409）。
                sub_id = str(body.get("sub_id") or "").strip()
                if not sub_id:
                    json_response(self, {
                        "error": "A lecture sub_id is required",
                        "error_code": "sub_id_required",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    result = service.learning.request_quality_judge(sub_id)
                except RuntimeError as exc:
                    code = str(getattr(exc, "code", "") or "").strip()
                    if code == "sub_id_required":
                        payload = {"error": "A lecture sub_id is required", "error_code": code}
                    elif code == "lecture_not_in_catalog":
                        payload = {
                            "error": "This lecture is not in the authorized catalog; refresh the catalog and try again",
                            "error_code": code,
                        }
                    elif code == "quality_no_material":
                        payload = {
                            "error": "This lecture has no transcript or summary to check yet; generate one first",
                            "error_code": code,
                        }
                    elif code == "deepseek_key_required":
                        payload = {
                            "error": "A DeepSeek API key is required for quality checks",
                            "error_code": code,
                        }
                    elif code == "remote_not_configured":
                        payload = {
                            "error": "Remote compute is not connected and verified yet; finish setup in settings",
                            "error_code": code,
                        }
                    elif code == "quality_task_in_flight":
                        payload = {
                            "error": "A quality check for this lecture is already running",
                            "error_code": code,
                        }
                    else:
                        # 未识别的内部故障：按 do_POST 同款闭集码诚实 500，
                        # 不发明无文案的私有码（error_code_audit 零缺口）。
                        payload = {
                            "error": "Quality check could not be started",
                            "error_code": "runtime_failed",
                        }
                        json_response(self, payload, HTTPStatus.INTERNAL_SERVER_ERROR)
                        return
                    json_response(self, payload, HTTPStatus(int(getattr(exc, "status", 400) or 400)))
                    return
                json_response(self, api_v3_envelope({
                    "task": public_task(result.get("task")),
                    "created": bool(result.get("created")),
                }))
                return
            if route == "bookmarks/actions":
                bookmark_id = str(body.get("bookmark_id") or "").strip()
                action = str(body.get("action") or "").strip().lower()
                if not bookmark_id or action not in {"explain", "retry", "resolve", "reopen", "cancel"}:
                    json_response(self, {
                        "error": "bookmark_id and a supported action are required",
                        "error_code": "bookmark_action_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    if action in {"explain", "retry"}:
                        result = service.learning.explain_question_bookmark(bookmark_id, retry=action == "retry")
                        value = {
                            "bookmark": result.get("bookmark"),
                            "task": public_task(result.get("task")),
                            "created": bool(result.get("created")),
                            "accepted_action": action,
                        }
                    elif action == "cancel":
                        result = service.learning.cancel_question_explanation(bookmark_id)
                        value = {
                            "bookmark": result.get("bookmark"),
                            "task": public_task(result.get("task")),
                            "accepted_action": action,
                        }
                    else:
                        bookmark = service.learning.set_question_bookmark_resolution(
                            bookmark_id, resolved=action == "resolve"
                        )
                        value = {"bookmark": bookmark, "task": None, "accepted_action": action}
                except KeyError:
                    json_response(self, {
                        "error": "Bookmark was not found",
                        "error_code": "bookmark_not_found",
                    }, HTTPStatus.NOT_FOUND)
                    return
                except RuntimeError:
                    json_response(self, {
                        "error": "Question explanation is not configured",
                        "error_code": "question_explanation_not_configured",
                        "retriable": True,
                    }, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope(value))
                return
            if route == "concepts/actions":
                action = str(body.get("action") or "").strip().lower()
                operation_id = str(body.get("operation_id") or "").strip()
                stored_id = ""
                if operation_id:
                    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", operation_id):
                        json_response(self, {"error": "operation_id is invalid", "error_code": "operation_id_invalid"}, HTTPStatus.BAD_REQUEST)
                        return
                    stored_id = f"concept:{operation_id}"
                    existing = service.tasks.repository.get_app_state(stored_id, None)
                    if existing:
                        if existing.get("accepted_action") != action:
                            json_response(self, {"error": "operation_id was already used for another action", "error_code": "operation_id_conflict"}, HTTPStatus.CONFLICT)
                            return
                        json_response(self, api_v3_envelope(existing))
                        return
                if action == "analyze":
                    course_ids = [str(value) for value in body.get("course_ids") or [] if str(value)]
                    try:
                        result = service.learning.analyze_cross_course_concepts(course_ids)
                    except ValueError as exc:
                        json_response(self, {
                            "error": "At least two courses with evidence are required",
                            "error_code": str(exc),
                        }, HTTPStatus.BAD_REQUEST)
                        return
                    value = {"analysis": result, "accepted_action": action, "operation_id": operation_id}
                    if stored_id:
                        service.tasks.repository.set_app_state(stored_id, value)
                    json_response(self, api_v3_envelope(value))
                    return
                edge_id = str(body.get("edge_id") or "").strip()
                if not edge_id or action not in {"dismiss", "restore", "set_relation"}:
                    json_response(self, {
                        "error": "edge_id and a supported action are required",
                        "error_code": "concept_action_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    edge = service.learning.update_cross_course_concept(
                        edge_id, action=action, relation=str(body.get("relation") or "")
                    )
                except KeyError:
                    json_response(self, {
                        "error": "Concept relationship was not found",
                        "error_code": "concept_edge_not_found",
                    }, HTTPStatus.NOT_FOUND)
                    return
                except ValueError as exc:
                    json_response(self, {
                        "error": "Concept relationship action is invalid",
                        "error_code": str(exc),
                    }, HTTPStatus.BAD_REQUEST)
                    return
                value = {"edge": edge, "accepted_action": action, "operation_id": operation_id}
                if stored_id:
                    service.tasks.repository.set_app_state(stored_id, value)
                json_response(self, api_v3_envelope(value))
                return
            if route == "analytics/study-events":
                # AIRESEARCH H6 埋点入口（TELEMETRY-H64-1）：闭集种类+字段
                # 校验全在此收口，遥测绝不挡主链——校验失败 400 闭集码，
                # analytics 未开启时 200 recorded=False（诚实回执，不落行）。
                kind = str(body.get("kind") or "").strip()
                if kind not in STUDY_EVENT_KINDS:
                    json_response(self, {
                        "error": "Study event kind is invalid",
                        "error_code": "study_event_kind_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                course_id = str(body.get("course_id") or "").strip()
                sub_id = str(body.get("sub_id") or "").strip()
                dwell_ms = body.get("dwell_ms", 0)
                if not course_id or not sub_id or len(course_id) > 190 or len(sub_id) > 190 \
                        or not isinstance(dwell_ms, int) or isinstance(dwell_ms, bool) or dwell_ms < 0:
                    json_response(self, {
                        "error": "Study event fields are invalid",
                        "error_code": "study_event_field_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    result = service.learning.record_study_event(
                        kind=kind, course_id=course_id, sub_id=sub_id, dwell_ms=dwell_ms,
                    )
                except ValueError as exc:
                    json_response(self, {
                        "error": "Study event is invalid",
                        "error_code": str(exc),
                    }, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, api_v3_envelope(result))
                return
            if route == "analytics/actions":
                action = str(body.get("action") or "").strip().lower()
                operation_id = str(body.get("operation_id") or "").strip()
                stored_id = ""
                if operation_id:
                    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", operation_id):
                        json_response(self, {"error": "operation_id is invalid", "error_code": "operation_id_invalid"}, HTTPStatus.BAD_REQUEST)
                        return
                    stored_id = f"settings:{operation_id}"
                    existing = service.tasks.repository.get_app_state(stored_id, None)
                    if existing:
                        if existing.get("accepted_action") != action:
                            json_response(self, {"error": "operation_id was already used for another action", "error_code": "operation_id_conflict"}, HTTPStatus.CONFLICT)
                            return
                        json_response(self, api_v3_envelope(existing))
                        return
                if action in {"enable", "disable"}:
                    try:
                        settings = service.learning.configure_learning_analytics(
                            action == "enable",
                            timezone_name=str(body.get("timezone") or "Asia/Shanghai"),
                        )
                    except ValueError as exc:
                        json_response(self, {
                            "error": "Analytics timezone is invalid",
                            "error_code": str(exc),
                        }, HTTPStatus.BAD_REQUEST)
                        return
                    result = {"settings": settings, "accepted_action": action, "operation_id": operation_id}
                    if stored_id:
                        service.tasks.repository.set_app_state(stored_id, result)
                    json_response(self, api_v3_envelope(result))
                    return
                if action == "delete_term":
                    term = str(body.get("term") or "").strip()
                    if not term:
                        json_response(self, {
                            "error": "term is required",
                            "error_code": "analytics_term_required",
                        }, HTTPStatus.BAD_REQUEST)
                        return
                    result = {**service.learning.delete_learning_analytics(term), "accepted_action": action, "operation_id": operation_id}
                    if stored_id:
                        service.tasks.repository.set_app_state(stored_id, result)
                    json_response(self, api_v3_envelope(result))
                    return
                if action == "delete_study_events":
                    # H6 计数可擦除（TELEMETRY-H64-1）：与 delete_term 同隐私
                    # 口径，走既有 operation_id 幂等框架。
                    result = {**service.learning.delete_study_events(), "accepted_action": action, "operation_id": operation_id}
                    if stored_id:
                        service.tasks.repository.set_app_state(stored_id, result)
                    json_response(self, api_v3_envelope(result))
                    return
                json_response(self, {
                    "error": "Analytics action is invalid",
                    "error_code": "analytics_action_invalid",
                }, HTTPStatus.BAD_REQUEST)
                return
            if route == "courseware-pdf/actions":
                action = str(body.get("action") or "").strip().lower()
                sub_id = str(body.get("sub_id") or "").strip()
                course_id = str(body.get("course_id") or "").strip()
                if action not in {"generate", "resume"} or not sub_id or not course_id:
                    json_response(self, {
                        "error": "Courseware PDF action is invalid",
                        "error_code": "courseware_pdf_action_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    task = service.tasks.enqueue_courseware_pdf(
                        course_id, sub_id, action=action,
                    )
                except FileNotFoundError:
                    json_response(self, {
                        "error": "Lecture is not in the authorized catalog",
                        "error_code": "lecture_not_found",
                    }, HTTPStatus.NOT_FOUND)
                    return
                except ValueError:
                    json_response(self, {
                        "error": "No paused courseware task is available to resume",
                        "error_code": "courseware_pdf_resume_unavailable",
                    }, HTTPStatus.CONFLICT)
                    return
                json_response(self, api_v3_envelope({
                    "task_id": str(task.get("task_id") or ""),
                    "state": str(task.get("state") or ""),
                }), HTTPStatus.ACCEPTED)
                return
            if route == "course-data/actions":
                try:
                    request = validate_course_data_action(body)
                except ValueError:
                    json_response(self, {
                        "error": "Course data action is invalid",
                        "error_code": "course_data_action_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                stored_id = f"course-data:{request['operation_id']}"
                existing = service.tasks.repository.get_app_state(stored_id, None)
                if existing:
                    if (
                        str(existing.get("action") or "") != request["action"]
                        or list(existing.get("course_ids") or []) != request["course_ids"]
                        or list(existing.get("sub_ids") or []) != request["sub_ids"]
                    ):
                        json_response(self, {
                            "error": "operation_id was already used for another course-data action",
                            "error_code": "operation_id_conflict",
                        }, HTTPStatus.CONFLICT)
                        return
                    json_response(self, api_v3_envelope(existing.get("receipt")))
                    return
                try:
                    receipt = service.learning.course_data_action(
                        request["action"],
                        operation_id=request["operation_id"],
                        course_ids=request["course_ids"],
                        sub_ids=request["sub_ids"],
                        confirm=request["confirm"],
                        confirm_typed=request["confirm_typed"],
                        include_orphans=request["include_orphans"],
                    )
                except CourseDataActionError as exc:
                    json_response(self, {
                        "error": "Course data action was not accepted",
                        "error_code": str(getattr(exc, "code", "") or "course_data_action_invalid"),
                    }, HTTPStatus.BAD_REQUEST)
                    return
                if str(receipt.get("status") or "") == "rejected":
                    json_response(self, api_v3_envelope(receipt), HTTPStatus.CONFLICT)
                    return
                service.tasks.repository.set_app_state(stored_id, {
                    "action": request["action"],
                    "course_ids": request["course_ids"],
                    "sub_ids": request["sub_ids"],
                    "receipt": receipt,
                })
                json_response(self, api_v3_envelope(receipt))
                return
            if route == "client-reset/actions":
                try:
                    request = validate_client_reset_action(body)
                except ValueError:
                    json_response(self, {
                        "error": "Client reset action is invalid",
                        "error_code": "reset_action_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                stored_id = f"client-reset:{request['operation_id']}"
                existing = service.tasks.repository.get_app_state(stored_id, None)
                if existing:
                    if (
                        bool(existing.get("delete_derived")) != request["delete_derived"]
                        or bool(existing.get("delete_github_repos")) != request["delete_github_repos"]
                    ):
                        json_response(self, {
                            "error": "operation_id was already used for another client reset",
                            "error_code": "operation_id_conflict",
                        }, HTTPStatus.CONFLICT)
                        return
                    json_response(self, api_v3_envelope(existing.get("receipt")))
                    return
                try:
                    receipt = service.settings.client_reset_action(
                        operation_id=request["operation_id"],
                        confirm_typed=request["confirm_typed"],
                        delete_derived=request["delete_derived"],
                        delete_github_repos=request["delete_github_repos"],
                    )
                except ClientResetActionError as exc:
                    code = str(getattr(exc, "code", "") or "reset_action_invalid")
                    json_response(self, {
                        "schema": "courselens.api.error.v1",
                        "error": "Client reset was not accepted",
                        "error_code": code,
                        "retriable": code == "reset_blocked",
                    }, HTTPStatus.CONFLICT if code == "reset_blocked" else HTTPStatus.BAD_REQUEST)
                    return
                except GitHubAppError as exc:
                    # Repo deletion failed before any local change: the reset
                    # did not run at all and may be retried once GitHub works.
                    json_response(self, {
                        "schema": "courselens.api.error.v1",
                        "error": "GitHub repository deletion did not complete; nothing was reset",
                        "error_code": str(getattr(exc, "code", "") or "github_error"),
                        "retriable": True,
                    }, HTTPStatus.BAD_GATEWAY)
                    return
                if str(receipt.get("status") or "") == "rejected":
                    json_response(self, {
                        "schema": "courselens.api.error.v1",
                        "error": "Client reset is blocked by active work",
                        "error_code": "reset_blocked",
                        "retriable": True,
                        "operation_id": request["operation_id"],
                        "blockers": list(receipt.get("blockers") or []),
                    }, HTTPStatus.CONFLICT)
                    return
                # Accepted receipts only: the ledger row lives in the freshly
                # rebuilt state.db so a replay never re-executes the reset.
                service.tasks.repository.set_app_state(stored_id, {
                    "action": request["action"],
                    "delete_derived": request["delete_derived"],
                    "delete_github_repos": request["delete_github_repos"],
                    "receipt": receipt,
                })
                json_response(self, api_v3_envelope(receipt))
                # The reply above is fully written once this handler line runs;
                # only then may the reset shutdown begin (no auto-restart).
                service.lifecycle.request_shutdown("reset")
                return
            if route == "data-migration/actions":
                # 安家向导/搬家包动作面（D12 P0）。刻意不进 course-session 门：
                # 新机导入发生在登录任何复旦账号之前，登录门会锁死安家路径。
                try:
                    request = validate_data_migration_action(body)
                except ValueError:
                    json_response(self, {
                        "error": "Data migration action is invalid",
                        "error_code": "migration_action_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                if request["action"] == "export":
                    try:
                        receipt = service.settings.migration_export_action(
                            operation_id=request["operation_id"],
                            password=request["password"],
                        )
                    except DataMigrationError as exc:
                        payload, status = self._migration_error_response(exc)
                        json_response(self, payload, status)
                        return
                    if str(receipt.get("status") or "") == "rejected":
                        json_response(self, api_v3_envelope(receipt), HTTPStatus.CONFLICT)
                        return
                    json_response(self, api_v3_envelope(receipt))
                    return
                try:
                    receipt = service.settings.migration_import_action(
                        operation_id=request["operation_id"],
                        package_id=request["package_id"],
                        password=request["password"],
                    )
                except DataMigrationError as exc:
                    payload, status = self._migration_error_response(exc)
                    json_response(self, payload, status)
                    return
                if str(receipt.get("status") or "") == "rejected":
                    # 远端运行在途等硬拒：诚实回执，导入未执行、可重试。
                    json_response(self, api_v3_envelope(receipt), HTTPStatus.CONFLICT)
                    return
                json_response(self, api_v3_envelope(receipt))
                # 与 client-reset 同法：回执写完才关停，学生重开应用即安家完成。
                service.lifecycle.request_shutdown("migration_import")
                return
            json_response(self, {"error": "v3 route not found"}, HTTPStatus.NOT_FOUND)

        def _handle_api_v3_put(self, parsed, body: dict[str, Any]) -> None:
            route = parsed.path.removeprefix("/api/v3/").strip("/")
            stored_id = ""
            try:
                operation_id = str(body.get("operation_id") or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", operation_id):
                    json_response(self, {
                        "error": "operation_id is required",
                        "error_code": "operation_id_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                if route == "automation/config":
                    stored_id = f"automation-config:{operation_id}"
                    operation, created = service.tasks.repository.begin_remote_operation(
                        stored_id, "update-config", "automation"
                    )
                    if created:
                        service.automation.update_config(body)
                        operation = service.tasks.repository.finish_remote_operation(
                            stored_id, state="accepted", result={"state": "configuring"}
                        )
                    json_response(self, api_v3_envelope({"operation": {
                        "operation_id": operation_id,
                        "state": str(operation.get("state") or "unknown"),
                        "error_code": str(operation.get("error_code") or ""),
                    }}), HTTPStatus.ACCEPTED)
                    return
                if route == "automation/cloud-secrets":
                    # The upload body carries only a saved-account identity plus
                    # the disclosure acknowledgement; secret values are loaded
                    # server-side inside the action and never accepted here.
                    if {key.casefold() for key in dict(body or {})} & {
                        "student_id", "password", "api_key", "deepseek_api_key",
                        "smtp_username", "smtp_password", "secret_value",
                    }:
                        json_response(self, {
                            "schema": "courselens.api.error.v1",
                            "error": "Secret values are never accepted by this route",
                            "error_code": "cloud_secret_value_rejected",
                        }, HTTPStatus.BAD_REQUEST)
                        return
                    stored_id = f"automation-secrets:{operation_id}"
                    operation, created = service.tasks.repository.begin_remote_operation(
                        stored_id, "upload-cloud-secrets", "automation"
                    )
                    if created:
                        value = service.automation.upload_secrets(body)
                        operation = service.tasks.repository.finish_remote_operation(
                            stored_id, state="accepted", result={"state": str(value.get("state") or "uploaded")}
                        )
                    json_response(self, api_v3_envelope({
                        "operation": {
                            "operation_id": operation_id,
                            "state": str(operation.get("state") or "unknown"),
                            "error_code": str(operation.get("error_code") or ""),
                        },
                    }), HTTPStatus.ACCEPTED)
                    return
            except (RuntimeError, ValueError) as exc:
                code = str(getattr(exc, "code", "") or "automation_failed")
                if stored_id:
                    try:
                        service.tasks.repository.finish_remote_operation(
                            stored_id, state="failed", error_code=code
                        )
                    except KeyError:
                        pass
                json_response(self, {
                    "schema": "courselens.api.error.v1",
                    "error": "Automation configuration was rejected",
                    "error_code": code,
                    "retriable": code in {"github_unreachable", "rate_limited", "timeout"},
                }, HTTPStatus.BAD_REQUEST)
                return
            json_response(self, {"error": "v3 route not found"}, HTTPStatus.NOT_FOUND)

        def _handle_api_v3_delete(self, parsed, body: dict[str, Any]) -> None:
            # PLAYER-UX-1④：删除族路由闭集（当前仅书签）。书签是纯本机学习数据，
            # 删除不依赖学校会话，也不进入 POST 动作族的会话就绪门。
            route = parsed.path.removeprefix("/api/v3/").strip("/")
            if route == "bookmarks":
                bookmark_id = str(body.get("bookmark_id") or "").strip()
                if not bookmark_id:
                    json_response(self, {
                        "error": "bookmark_id is required",
                        "error_code": "bookmark_request_invalid",
                    }, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    service.learning.delete_question_bookmark(bookmark_id)
                except KeyError:
                    json_response(self, {
                        "error": "Bookmark was not found",
                        "error_code": "bookmark_not_found",
                    }, HTTPStatus.NOT_FOUND)
                    return
                json_response(self, api_v3_envelope({"bookmark_id": bookmark_id, "deleted": True}))
                return
            json_response(self, {"error": "v3 route not found"}, HTTPStatus.NOT_FOUND)

        def _serve_remote_events(self, query: dict[str, list[str]]) -> None:
            allowed_topics = {"remote-connection", "remote-runs", "tasks", "automation"}
            requested = {
                item.strip() for item in query.get("topics", [""])[0].split(",")
                if item.strip() in allowed_topics
            } or allowed_topics
            try:
                after = int(
                    str(self.headers.get("Last-Event-ID") or query.get("after", ["0"])[0] or "0")
                )
            except ValueError:
                after = 0
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            deadline = time.monotonic() + 45.0
            next_heartbeat = 0.0
            next_task_snapshot = 0.0
            # PF1（2026-10-07）：每连接记忆上次快照投影内容——任务零变化时跳过
            # 序列化与下发，空闲连接只剩心跳；新连接/重连首拍必发，任务真变化
            # 下一拍（≤5s）必达。observed_at 不参与比对（每次必变、渲染零消费）。
            last_task_projection = ""
            while time.monotonic() < deadline:
                events = service.tasks.repository.list_remote_events(
                    after_sequence=after, topics=requested, limit=100
                )
                try:
                    for event in events:
                        after = max(after, int(event.get("sequence") or 0))
                        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                        self.wfile.write(f"id: {after}\nevent: {event.get('topic')}\ndata: {payload}\n\n".encode("utf-8"))
                    now = time.monotonic()
                    if "tasks" in requested and now >= next_task_snapshot:
                        # CLIENT-STATE-R1：SSE tasks 快照与抽屉同窗口语义（最新优先）。
                        recent = service.tasks.repository.list_tasks(limit=200, newest_first=True)
                        projected = public_tasks(service.tasks.repository, recent)
                        projection_text = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
                        if projection_text != last_task_projection:
                            payload = api_v3_envelope({
                                "tasks": projected,
                                "observed_at": time.time(),
                            })
                            self.wfile.write(
                                f"event: tasks\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")
                            )
                            last_task_projection = projection_text
                        next_task_snapshot = now + 5.0
                    if events or now >= next_heartbeat:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        next_heartbeat = now + 15.0
                except CLIENT_DISCONNECT_ERRORS:
                    return
                time.sleep(1.0)

        def _serve_live_room_asset(self, parsed) -> None:
            match = re.fullmatch(
                r"/api/v3/live-room/play/([A-Za-z0-9_-]{20,80})/"
                r"(?:manifest|resource)/([A-Za-z0-9_-]{16,80})",
                parsed.path,
            )
            if match is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            cookie = SimpleCookie()
            try:
                cookie.load(str(self.headers.get("Cookie") or ""))
            except Exception:
                cookie = SimpleCookie()
            session_id, resource_id = match.groups()
            live_cookie = cookie.get("courselens_live")
            # 媒体路由豁免空闲回收（NIGHT2-W21）：暂停播放的 keep-alive 连接必须活过空闲窗口
            self.connection.settimeout(None)
            try:
                result = service.live_room.fetch(
                    session_id, resource_id,
                    cookie_session=live_cookie.value if live_cookie else "",
                    range_header=str(self.headers.get("Range") or ""),
                )
            except LiveRoomError as exc:
                json_response(self, {
                    "error": "Live media request was rejected", "error_code": exc.code,
                }, exc.status)
                return
            self.send_response(result.status)
            self.send_header("Content-Type", result.content_type)
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("Content-Length", str(len(result.body)))
            if result.content_range:
                self.send_header("Content-Range", result.content_range)
            if result.accept_ranges:
                self.send_header("Accept-Ranges", result.accept_ranges)
            self.end_headers()
            self.wfile.write(result.body)

        def _stream_remote_media(self, stream, *, head_only: bool) -> None:
            resources = getattr(service, "resources", None)
            claim = resources.claim("interactive_stream") if resources is not None else nullcontext()
            # 媒体路由豁免空闲回收（NIGHT2-W21）
            self.connection.settimeout(None)
            with claim:
                if resources is not None:
                    resources.begin_player_stream()
                try:
                    self.send_response(stream.status)
                    self.send_header("Content-Type", stream.content_type or "video/mp4")
                    self.send_header("Content-Length", str(stream.content_length))
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Cache-Control", "private, no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    if stream.content_range:
                        self.send_header("Content-Range", stream.content_range)
                    self.end_headers()
                    if not head_only and stream.status != HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE:
                        try:
                            for chunk in stream.iter_bytes():
                                self.wfile.write(chunk)
                        except Exception:
                            # Connection and retry errors may contain a signed
                            # upstream URL.  The browser already sees a closed
                            # stream, so keep the server log deliberately silent.
                            pass
                finally:
                    stream.close()
                    if resources is not None:
                        resources.end_player_stream()

        def _send_common_file_headers(self, content_type: str, content_length: int) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(content_length))
            self.send_header("Cache-Control", "private, max-age=0, must-revalidate")
            self.send_header("X-Content-Type-Options", "nosniff")

        def _send_bytes(
            self,
            body: bytes,
            content_type: str,
            *,
            head_only: bool,
            compress_gzip: bool = False,
        ) -> None:
            # compress_gzip=True 仅为显式选择加入的大文本读面（字幕 VTT 轨文件）
            # 准备；仍只在客户端声明 gzip 能力且体量过门槛时才真正压缩——
            # 媒体流/range 面从不进本参数，字节语义零变化。
            wire = _gzip_body_if_accepted(self, body) if compress_gzip else None
            self.send_response(HTTPStatus.OK)
            self._send_common_file_headers(content_type, len(wire if wire is not None else body))
            if wire is not None:
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            if not head_only:
                self.wfile.write(wire if wire is not None else body)

        def _stream_file(
            self,
            path: Path,
            content_type: str,
            *,
            head_only: bool,
            allow_range: bool = True,
            no_store: bool = False,
        ) -> None:
            size = path.stat().st_size
            byte_range = None
            if allow_range:
                try:
                    byte_range = parse_byte_range(self.headers.get("Range", ""), size)
                except ValueError:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            start, end = byte_range or (0, max(0, size - 1))
            length = max(0, end - start + 1)
            self.send_response(HTTPStatus.PARTIAL_CONTENT if byte_range else HTTPStatus.OK)
            self._send_common_file_headers(content_type, length)
            if no_store:
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("Pragma", "no-cache")
            if allow_range:
                self.send_header("Accept-Ranges", "bytes")
            if byte_range:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if head_only or length <= 0:
                return
            try:
                with path.open("rb") as source:
                    source.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except CLIENT_DISCONNECT_ERRORS:
                return

        def _serve_static(self, url_path: str) -> None:
            relative = url_path.lstrip("/") or "index.html"
            path = (frontend_root / relative).resolve()
            try:
                path.relative_to(frontend_root)
            except ValueError:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            if not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = path.read_bytes()
            content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # 静态面零缓存指令会让浏览器落入启发式缓存；loopback 上强制再验证
            # 成本可忽略，却保证语料等新增静态文件不会被陈旧缓存永久遮蔽。
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

    return Handler


__all__ = [
    "FrontendSessionRegistry",
    "json_response",
    "make_handler",
    "parse_byte_range",
    "read_json",
    "srt_to_vtt_bytes",
]
