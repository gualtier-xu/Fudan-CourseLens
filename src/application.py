"""CLI and local web service for Fudan CourseLens."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import sys
import statistics
import threading
import time
import urllib.request
import uuid
import unicodedata
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - supported Python includes zoneinfo
    ZoneInfo = None  # type: ignore[assignment,misc]

from src.runtime.encoding import configure_stdio

configure_stdio()

from credentials import CredentialStore
from path_utils import (
    DEFAULT_DATA_DIR,
    PROJECT_ROOT,
    file_size,
    relative_to_project,
)
from src.runtime.estimator import estimate_from_progress
from src.runtime.api_v3 import OnboardingGuideVersionError
from src.runtime.learning_store import LearningStore
from src.runtime.test_mode import ensure_egress_allowed
from src.runtime.course_data_inventory import (
    COURSE_DATA_ACTIONS,
    MAX_SUMMARY_PAGE_SIZE,
    CourseDataInventory,
    courseware_lecture_key,
    subtitle_artifact_key,
)
from src.runtime.materials_center import (
    _course_data_known_pairs,
    _course_data_rmtree_owned,
    courseware_pdf_download_name,
    write_summary_export,
)
from src.runtime.sqlite_utils import connect_learning_db
from src.runtime.data_migration import (
    PACKAGE_SUFFIX,
    DataMigrationError,
    ERROR_PACKAGE_INVALID,
    ERROR_PACKAGE_TOO_LARGE,
    build_migration_package,
    import_migration_package,
)
from src.runtime.assessment_radar import CATEGORIES, title_norm, upsert_events, rescan_lecture, confirmed_schedule_events
from src.runtime.assessment_ir import delete_assessment_items, refresh_assessment_items
from src.runtime.exam_paper_split import refresh_exam_questions
from src.runtime.learning_schema import ensure_assessment_schema, initialize_learning_schema
from src.services.domains import (
    ClientResetActionError,
    CourseDataActionError,
    CourseReviewActionError,
)
from src.runtime.catalog_repository import CATALOG_SCHEMA_VERSION, CatalogRepository
from src.runtime.media_source import (
    MEDIA_STREAM_UPSTREAM_UNREACHABLE,
    RemoteMediaGateway,
    RemoteMediaStream,
    _ConfirmedServiceResponse,
    bounded_range_header,
    classify_open_failure,
    configure_media_stream_proxy_provider,
    detect_windows_system_proxy,
    media_stream_proxy_enabled,
    media_stream_proxy_snapshot,
    parse_upstream_range,
    set_media_stream_proxy_setting,
)
from src.runtime.network import (
    NetworkSettings,
    VPN_CONNECTION_SCHEMA,
    validate_vpn_connection_snapshot,
)
from src.runtime.query_relaxation import extract_content_terms, or_tier_terms
from src.runtime.search_index import LearningSearchIndex, normalize_search_text
from src.runtime.subtitle_reader import (
    display_cache_path,
    load_display_cache,
    parse_subtitle_file,
    shape_display_cues_cached,
    split_long_cues,
    store_display_cache,
)
from src.runtime.resources import ResourceCoordinator, device_fingerprint
from src.runtime.progress import (
    SUBTITLE_SERIAL_TAIL_PHASES,
    ProgressTracker,
    phase_plan,
    queued_progress,
    upgrade_progress,
)
from src.runtime.courseware_pdf import MANIFEST_SCHEMA, CoursewarePdfRun
from shared import course_knowledge_contract
from src.runtime.course_memory import (
    COURSE_MEMORY_MAX_EXAMPLES,
    course_memory_count,
    load_course_examples,
    sink_course_examples,
)
from src.runtime.course_memory_feedback import (
    TermCandidateUnknown,
    auto_confirmed_term_rows,
    confirm_term_mapping,
    confirmed_memory_terms,
    confirmed_term_list,
    course_memory_terms,
    dismiss_term_mapping,
    import_boundary_rulings,
    judge_annotation_rows,
    judge_boundary_pairs,
    judge_confirmed_term_rows,
    memory_annotation,
    record_product_deviations,
    term_candidates,
)
from src.runtime.course_knowledge import (
    CourseKnowledgeError,
    PACKET_ITEM_CHARS,
    PACKET_TOTAL_CHARS,
    build_evidence_packet,
    course_context,
    course_review as course_review_document,
    refresh_plan,
    save_course_knowledge,
)
from src.runtime.flashcards import (
    RATINGS as FLASHCARD_RATINGS,
    derive_flashcards,
    flashcard_deck,
    last_derived_input_hash,
    mark_derived,
    review_flashcard,
)
from src.runtime.task_store import (
    ACTIVE_STATES,
    QUEUE_OBSERVATION_SCHEMA,
    REMOTE_RUN_LIFECYCLE_STATES,
    REMOTE_RUN_RECOVERABLE_STATES,
    REMOTE_RUN_STARTUP_RECOVERY_STATES,
    REMOTE_BUSY_STATE_KEY_PREFIX,
    TERMINAL_STATES,
    USER_PAUSE_INTENT_KEY,
    ZOMBIE_SESSION_CLEANUP_REASON,
    ZOMBIE_STARTUP_STALE_SECONDS,
    TaskStore,
    queue_profile_keys,
    trusted_queue_observation,
)
from src.runtime.timetable import TimetableError, TimetableRuntime
from src.runtime.exam_schedule import (
    PLAN_STRATEGIES,
    SHANGHAI,
    ReviewPlanValidationError,
    context_from_exam,
    exam_start_epoch,
    fetch_exam_rows,
    match_exam_row,
    semester_range_from_start,
    unavailable_context,
    user_confirmed_context,
)
from src.runtime.student_features import (
    ensure_student_feature_schema,
    create_bookmark,
    build_review_steps,
    build_quiz_items,
    delete_bookmark,
    evidence_packet,
    evidence_answer,
    list_bookmarks,
    list_quiz_items,
    list_review_plans,
    local_practice_view,
    quiz_attempt_stats,
    save_quiz_items,
    save_review_plan,
    set_bookmark_resolution,
    update_bookmark_explanation,
    update_bookmark_task,
    validate_grounded_answer,
    clear_watch_events,
    insert_watch_events,
    list_watch_events,
)
from src.runtime.document_alignment import (
    align_document,
    delete_document,
    document_search_pages,
    document_storage_path,
    ensure_document_schema,
    get_document,
    list_documents,
    register_document,
    update_alignment,
)
from src.runtime.smart_playback import (
    classify_segments as classify_timeline_segments,
    ensure_smart_playback_schema,
    list_timeline,
    save_timeline,
    timeline_input_hash,
)
from src.runtime.concepts import (
    analyze_concepts,
    ensure_concept_schema,
    list_concept_graph,
    mark_stale_edges,
    update_concept_edge,
)
from src.runtime.analytics import (
    analytics_settings,
    analytics_summary,
    delete_analytics_term,
    delete_study_events,
    ensure_analytics_schema,
    estimate_fun_metrics,
    record_study_event,
    record_watch_event,
    set_analytics_enabled,
    study_telemetry_summary,
)
from src.runtime.study_stats import (
    STUDY_STATS_EXPORT_DIRNAME,
    STUDY_STATS_EXPORT_RETENTION_SECONDS,
    build_study_stats_export,
    clear_study_daily_seconds,
    ensure_study_stats_schema,
    record_study_heartbeat,
    study_detail,
    study_overview,
)

CLIENT_REUSE_SECONDS = 300
# SRC-SYNDROME-1 U2（第廿七案）：复用窗过期但保活补验在途时保持 ready 形态的
# 收敛上限。补验本体 check_alive timeout=10s（icourse.py），45s 覆盖最坏串行；
# 超限回 honest checking，绝不让「验证中」被无限遮蔽。
REVALIDATE_READY_GRACE_SECONDS = 45.0
# SRC-SYNDROME-1 U2（第廿六案）：degraded 自愈看门狗——recoverable 类登录失败
# 后台有界重试；身份源终态闭集永不重试（唯一动作是用户介入）。
SESSION_SELF_HEAL_TICK_SECONDS = 30.0
SESSION_SELF_HEAL_MAX_BACKOFF_SECONDS = 600.0
_SESSION_SELF_HEAL_SKIP_CODES = frozenset({
    "fudan_credentials_rejected",
    "fudan_challenge_required",
    "fudan_account_locked",
    "fudan_redirect_unsafe",
    "fudan_ticket_rejected",
})
CATALOG_REFRESH_DEADLINE_SECONDS = 90
CATALOG_ROUTE_ATTEMPT_SECONDS = 30
DAILY_SCHEDULE_PIPELINE_VERSION = "daily-schedule-v2"
BOOKMARK_ANSWER_PIPELINE_VERSION = "bookmark-answer-v1"
# RR-QWIN-1 Q2（AI-RESEARCH-1 P5 客户端面）：提问链出站载荷闭集预检——超限
# 不再裸奔上云空跑一整轮（分钟级 Actions 延迟换一个必败 LLM 调用）。帽值远
# 高于合法面（考核证据组装自带 6 条/4000 字硬帽；书签证据是有界窗口）：
# query 超长=安全降级（只截出站文本，书签笔记本体不动）；证据超限=本地
# fail-fast 闭集码，绝不静默改学生证据。
QUESTION_QUERY_MAX_CHARS = 4000
QUESTION_EVIDENCE_MAX_ITEMS = 200
QUESTION_EVIDENCE_MAX_CHARS = 60_000
QUESTION_PAYLOAD_OVERSIZED = "question_payload_oversized"
# RR-QWIN-1 Q2：提问链瞬态失败闭集码（LLM 坏形状/云侧超时）——自动重试恰
# 一次；429 限流与授权/ setup 类显式码绝不进本集合（不 hammer、不越权重试）。
_QUESTION_TRANSIENT_RETRY_CODES = frozenset({
    "remote_answer_failed", "deepseek_answer_failed", "remote_answer_timeout",
})


def _question_payload_preflight(query: str, evidence: list) -> tuple[str, str]:
    """提问链出站预检。返回 (出站 query, 失败闭集码)；码非空=本地 fail fast。

    证据口径与 worker answer_question 的准入过滤一致（citation_id+text 非空
    才计入），预检数字才对得上真正会发出去的载荷。"""
    text = str(query or "").strip()[:QUESTION_QUERY_MAX_CHARS]
    items = [
        item for item in (evidence or [])
        if isinstance(item, dict) and str(item.get("citation_id") or "") and str(item.get("text") or "")
    ]
    if len(items) > QUESTION_EVIDENCE_MAX_ITEMS:
        return text, QUESTION_PAYLOAD_OVERSIZED
    if sum(len(str(item.get("text") or "")) for item in items) > QUESTION_EVIDENCE_MAX_CHARS:
        return text, QUESTION_PAYLOAD_OVERSIZED
    return text, ""


# P11-CONTRACT-1：质量抽检（LLM-as-judge 离线质检器）客户端面。采样算式、
# input_hash 公式、findings 闭集与数量帽均为合同冻结字段——发现冲突回总控
# 重开合同，不得就地改。
QUALITY_JUDGE_PIPELINE_VERSION = "quality-judge-v1"
_QUALITY_SAMPLE_RATE = 0.03
_QUALITY_SAMPLE_MIN = 8
_QUALITY_SAMPLE_MAX = 40
_QUALITY_FINDINGS_CAP = 24
_QUALITY_SUBTITLE_TARGETS = frozenset({"segment"})
_QUALITY_SUMMARY_TARGETS = frozenset({"chapter", "takeaway", "body"})
_QUALITY_SUBTITLE_DIMENSIONS = frozenset({"term_fidelity", "readability"})
_QUALITY_SUMMARY_DIMENSIONS = frozenset({"factuality", "alignment", "readability"})
_QUALITY_SUBTITLE_CODES = frozenset({
    "glossary_violation", "homophone_suspect", "term_inconsistent", "broken_flow", "garbled",
})
_QUALITY_SUMMARY_CODES = frozenset({
    "no_source_support", "contradicts_source", "chapter_mislabel", "duplicate_content", "empty_section",
})
_QUALITY_SEVERITIES = frozenset({"warn", "info"})


class QualityJudgeRequestError(RuntimeError):
    """质量抽检请求的闭集拒绝（携带闭集码与 HTTP 状态，路由层照实映射）。"""

    def __init__(self, code: str, *, status: int = 400):
        super().__init__(code)
        self.code = str(code)
        self.status = int(status)


def _quality_sample_indices(total: int) -> list[int]:
    """合同冻结采样算式：k=min(40, max(8, ceil(0.03*N)))，stride=max(1, N//k)。

    确定性 stride（非随机）：同输入恒同样本，重跑幂等、报告可复现；不设
    env、不做自定义样本量。"""
    if total <= 0:
        return []
    k = min(
        _QUALITY_SAMPLE_MAX,
        max(_QUALITY_SAMPLE_MIN, math.ceil(_QUALITY_SAMPLE_RATE * total)),
    )
    stride = max(1, total // k)
    return list(range(0, total, stride))[:k]


def _quality_canonical_segment(segment: dict) -> dict:
    """契约段形状（index/start_ms/end_ms/text），canonical JSON 与 payload 共用。"""
    return {
        "index": int(segment.get("index") or 0),
        "start_ms": int(segment.get("start_ms") or 0),
        "end_ms": int(segment.get("end_ms") or 0),
        "text": str(segment.get("text") or ""),
    }


def _quality_transcript_digest(segments: list[dict]) -> str:
    encoded = json.dumps(
        [_quality_canonical_segment(segment) for segment in segments],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _quality_input_hash(
    sub_id: str, segment_indices: list[int], transcript_digest: str, summary_input_hash: str
) -> str:
    """合同冻结 input_hash 算式（_bookmark_input_hash 同族 canonical JSON）。

    transcript_digest 使重字幕后自动可重检；summary_input_hash 缺席=""。"""
    payload = {
        "schema": "quality-judge-v1",
        "sub_id": str(sub_id),
        "segment_indices": [int(index) for index in segment_indices],
        "transcript_digest": str(transcript_digest),
        "summary_input_hash": str(summary_input_hash or ""),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _quality_normalize_findings(
    raw_findings: list,
    *,
    targets: frozenset,
    dimensions: frozenset,
    codes: frozenset,
    bounds: dict,
) -> tuple[list[dict], int]:
    """合同 §② 校验纪律：target/dimension/code/severity 逐项对闭集、position
    越界丢弃并计数；数量帽截断保 warn 优先（稳定排序，同级保序）。零自由
    文本——任何 note/quote 字段直接丢弃（不进 findings 形状）。"""
    cleaned: list[dict] = []
    dropped = 0
    for item in raw_findings or []:
        if not isinstance(item, dict):
            dropped += 1
            continue
        target = item.get("target")
        dimension = item.get("dimension")
        code = item.get("code")
        severity = item.get("severity")
        position = item.get("position")
        bound = bounds.get(target)
        if (
            bound is None
            or dimension not in dimensions
            or code not in codes
            or severity not in _QUALITY_SEVERITIES
            or isinstance(position, bool)
            or not isinstance(position, int)
            or not 0 <= position < bound
        ):
            dropped += 1
            continue
        cleaned.append({
            "target": target,
            "position": position,
            "dimension": dimension,
            "code": code,
            "severity": severity,
        })
    cleaned.sort(key=lambda item: 0 if item["severity"] == "warn" else 1)
    return cleaned[:_QUALITY_FINDINGS_CAP], dropped


def _normalize_quality_judge_report(
    report: dict,
    *,
    sample_size: int,
    summary_chapter_count: int,
    summary_takeaway_count: int,
) -> tuple[dict, dict]:
    """合同 §② import 侧双层校验：返回 (规范化报告, 校验计数)。

    mode 非 dict / findings 非 list → 整 mode 置 null + judge_mode_invalid；
    双 mode 均 null → fail-closed 抛 judge_output_invalid（诚实失败，re-POST
    即重试）。sample_size/targets_total 取客户端分派时冻结的真值，不回传
    worker 自报数。"""
    counts = {"judge_mode_invalid": 0, "findings_invalid_dropped": 0}
    report = report if isinstance(report, dict) else {}

    subtitle = None
    subtitle_raw = report.get("subtitle")
    if subtitle_raw is not None:
        if not isinstance(subtitle_raw, dict) or not isinstance(subtitle_raw.get("findings"), list):
            counts["judge_mode_invalid"] += 1
        else:
            findings, dropped = _quality_normalize_findings(
                subtitle_raw.get("findings"),
                targets=_QUALITY_SUBTITLE_TARGETS,
                dimensions=_QUALITY_SUBTITLE_DIMENSIONS,
                codes=_QUALITY_SUBTITLE_CODES,
                bounds={"segment": max(0, int(sample_size))},
            )
            counts["findings_invalid_dropped"] += dropped
            subtitle = {
                "sample_size": max(0, int(sample_size)),
                "targets_total": max(0, int(sample_size)),
                "findings": findings,
            }

    summary = None
    summary_raw = report.get("summary")
    if summary_raw is not None:
        if not isinstance(summary_raw, dict) or not isinstance(summary_raw.get("findings"), list):
            counts["judge_mode_invalid"] += 1
        else:
            findings, dropped = _quality_normalize_findings(
                summary_raw.get("findings"),
                targets=_QUALITY_SUMMARY_TARGETS,
                dimensions=_QUALITY_SUMMARY_DIMENSIONS,
                codes=_QUALITY_SUMMARY_CODES,
                bounds={
                    "chapter": max(0, int(summary_chapter_count)),
                    "takeaway": max(0, int(summary_takeaway_count)),
                    "body": 1,
                },
            )
            counts["findings_invalid_dropped"] += dropped
            summary = {
                "targets_total": max(0, int(summary_chapter_count)) + max(0, int(summary_takeaway_count)) + 1,
                "findings": findings,
            }

    if subtitle is None and summary is None:
        raise QualityJudgeRequestError("judge_output_invalid")
    return {"schema_version": 1, "subtitle": subtitle, "summary": summary}, counts


def _memory_terms_count(result: dict) -> int:
    """RR-P6MEM-1：生成侧实报的记忆注入数（缺失/畸形=0，宁缺毋滥）。"""
    try:
        return max(0, int((result.get("metrics") or {}).get("course_memory_terms") or 0))
    except (TypeError, ValueError):
        return 0

RECORDED_LOCAL_TASK_KINDS = {
    "search_answer", "quiz", "review_plan", "document_import",
    "document_alignment", "timeline_classification", "concept_analysis",
}
ONBOARDING_GUIDE_VERSION = "student-onboarding.v1"
ONBOARDING_GUIDE_SCHEMA = "courselens.onboarding-guide.v1"
ONBOARDING_GUIDE_STATE_KEY = "student_onboarding_guide"
ONBOARDING_GUIDE_DISPOSITIONS = frozenset({"new", "dismissed", "completed"})
ONBOARDING_GUIDE_ACTIONS = frozenset({"mark-opened", "dismiss", "complete"})
AUTO_CONNECT_SCHEMA = "courselens.auto-connect.v1"
AUTO_CONNECT_STATE_KEY = "auto_connect"
UPDATE_BACKGROUND_CHECKS_SCHEMA = "courselens.update-background-checks.v1"
UPDATE_BACKGROUND_CHECKS_STATE_KEY = "update_background_checks"
# MEDIAWEBVPN-3-20261002：校外媒体 WebVPN 中转（方案 A）。直连优先不变；
# 直连开流失败且失败账本判 upstream_unreachable 时，恰一次改写经 WebVPN
# 重取。偏好默认开（校外可看是可用性底线；privacy 面零新增外联主机——
# webvpn.fudan.edu.cn 本就是闭集内唯一 cookie 检查点主机）。
# WEBVPN-AUTO-1：用户设定面已移除（设置动作后端本就无分发分支），
# app-state 仅作无 UI 兜底开关；本域无 schema 常量。
MEDIA_WEBVPN_RELAY_STATE_KEY = "media_webvpn_relay"
# 改写目标闭集：只放行平台媒体主机（R-1 调研：签名 URL 的 host 字段值仓库
# 内不可见，E1 实测收口；直连腿不受此集约束，本集只闸改写面，fail-closed）。
MEDIA_WEBVPN_RELAY_ALLOWED_HOSTS = frozenset({"icourse.fudan.edu.cn"})
# ⑨（C⑧ 修订；AS3 增「不限」档）：日 token 总量上限（学生自己的 DeepSeek
# key，KV 存储；10 万-100 万、步进 10 万，哨兵 0=不限。UI 在 DeepSeek key 配置面）
MAX_DEEPSEEK_TOKENS_STATE_KEY = "max_deepseek_tokens.v1"
MAX_DEEPSEEK_TOKENS_DEFAULT = 100_000
MAX_DEEPSEEK_TOKENS_MIN = 100_000
MAX_DEEPSEEK_TOKENS_MAX = 1_000_000
MAX_DEEPSEEK_TOKENS_STEP = 100_000
# AS6（第四十七案）：DeepSeek 官方唯一余额接口（只读；api.deepseek.com 是
# worker LLM 既有外联主机，客户端本次只读新增，零新增主机）。key 只进内存
# Bearer 头：禁落盘、禁日志、禁异常文本。失败态短缓存，绝不重试轰炸。
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
DEEPSEEK_BALANCE_SCHEMA = "courselens.deepseek-balance.v1"
DEEPSEEK_BALANCE_OK_TTL_SECONDS = 300.0
DEEPSEEK_BALANCE_FAIL_TTL_SECONDS = 60.0

# ⑩（RUNLOCK-1）：云端在飞并发上限（app_state 既有 automation 配置族键；
# 默认 5、可调至 10，超出上限的云任务在客户端排队而非同时压向 runner）。
CLOUD_RUN_LIMIT_STATE_KEY = "cloud_run_limit"
CLOUD_RUN_LIMIT_DEFAULT = 5
CLOUD_RUN_LIMIT_MAX = 10

# P2-A：受保护动作前按需无凭据预检的短 TTL（与路由决策 300s TTL 互相独立：
# 预检 TTL 只去重同一代际内的连续航班，探测本身仍走 refresh_route_decision）。
CAMPUS_PREFLIGHT_TTL_SECONDS = 60.0
# P2-E：去标识计数器（闭集名称、纯计数、本地 app_state；无账号/课程/网络数据）。
CAMPUS_METRICS_STATE_KEY = "campus_recovery_metrics.v1"

# 第四十二案（LOST-RESULT-RECOVERY-1）：远端真值核对与结果追补导入。
# 客户端侧闸门（Worker 树未受信、连接证据过期…）只说明「此刻不能下发媒体
# 授权」，它不构成远端结论——run 在 GitHub 上活过了本地进程。只要 run 仍在
# 窗口内且一次性结果密钥在手，本地就无权把任务判死，只该按真值收敛。
REMOTE_RESULT_IMPORT_WINDOW_SECONDS = 7 * 24 * 3600
REMOTE_RESULT_WATCH_INTERVAL_SECONDS = 45.0
# 每轮启动对账的真值读取上限（按 updated_at 倒序，最近的在最前）。
REMOTE_RESULT_RECONCILE_MAX_RUNS = 10
# 追补导入的有界重试次数：结果确实在远端，导入失败先重试；用满后如实上报
# 本地导入失败（而不是把已经生成的字幕永远挂在「可能还在跑」）。
REMOTE_RESULT_IMPORT_MAX_ATTEMPTS = 5
# 可参与真值核对的远端行：全部未终态 + 客户端侧放弃型 failed（派发阶段写下的
# 失败）；imported / canceled 已无待办，不再追。
REMOTE_RESULT_RECONCILE_STATES = REMOTE_RUN_RECOVERABLE_STATES + ("failed",)
# 远端自身结论非成功时的默认上报码；有 worker 签名码时优先用它。
REMOTE_RESULT_FAILURE_CODE = "remote_failed"
# 真值未定时给学生的说法：不催他重做，也不承诺结果一定在。
REMOTE_TRUTH_PENDING_LABEL = "云端任务可能仍在运行或已完成：结果会自动核对并导入，无需重新生成"

# The typed closed-code error lives at the service-domain boundary
# (src.services.domains); it is re-exported here for existing importers.
from src.services.domains import AutoConnectPreferenceError  # noqa: E402


def _course_knowledge_error_code(error: BaseException) -> str:
    """闭集错误码：合同错误原样透出，其余归一到 course_knowledge_failed。"""
    code = str(getattr(error, "code", "") or "")
    if isinstance(error, course_knowledge_contract.CourseKnowledgeContractError) or isinstance(error, CourseKnowledgeError):
        return code or "course_knowledge_failed"
    return "course_knowledge_failed"


def _summary_block_code(error: BaseException) -> str:
    """把摘要入队失败归一到人话可解释的闭集原因。

    R3-12：入队路径已改抛显式码异常，映射器优先读 code（措辞漂移不再
    静默降级）；关键词嗅探保留一个版本作兼容回退。"""
    explicit = str(getattr(error, "code", "") or "")
    if explicit in {
        "lecture_not_found", "transcript_missing", "ai_key_missing", "fudan_login_required",
    }:
        return "lecture_not_in_catalog" if explicit == "lecture_not_found" else explicit
    if isinstance(error, FileNotFoundError):
        return "lecture_not_in_catalog"
    message = str(error or "")
    if "subtitle" in message.lower() or "字幕" in message:
        return "transcript_missing"
    if "API key" in message or "DeepSeek" in message:
        return "ai_key_missing"
    if "credentials" in message.lower():
        return "fudan_login_required"
    return "summary_enqueue_failed"


def _auto_connect_resume_outcome(value: object) -> dict:
    """Reduce one resume outcome to its closed-set, non-secret shape."""
    record = value if isinstance(value, dict) else {}
    return {
        "state": str(record.get("state") or "")[:24],
        "code": str(record.get("code") or "")[:64],
    }


def _shanghai_now() -> datetime:
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo("Asia/Shanghai"))
    return datetime.now(timezone.utc).astimezone(timezone.utc)


# --- Subtitle media-duration bands (Stage 09-C, shared with the live ETA) ----
# Finer prior buckets: media-duration bands refine the kind/mode history.  A
# bucket answers only once it has enough samples; sparse buckets fall back to
# the existing kind/mode profile.
SUBTITLE_DURATION_BAND_BOUNDS = (1200.0, 2700.0, 5400.0)


def subtitle_duration_band(duration_seconds: float) -> str:
    """Closed duration-band bucket id ('' when the duration is unknown)."""
    try:
        value = float(duration_seconds or 0.0)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(value) or value <= 0:
        return ""
    for index, bound in enumerate(SUBTITLE_DURATION_BAND_BOUNDS):
        if value < bound:
            return f"band{index + 1}"
    return f"band{len(SUBTITLE_DURATION_BAND_BOUNDS) + 1}"


def _resolved_public_source_ip(url: str) -> str:
    """Resolve one public address for an encrypted remote job hint.

    Some authorized media hosts are resolvable from the student's network but
    not from a GitHub-hosted runner.  The Worker treats this value only as a
    hint: it independently rejects non-global addresses and still validates
    the original hostname with TLS/SNI.
    """
    try:
        parsed = urlsplit(str(url or "").strip())
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            return ""
        if parsed.port not in (None, 443):
            return ""
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        return ""
    ipv4_candidates: list[str] = []
    ipv6_candidates: list[str] = []
    for family, _, _, _, socket_address in addresses:
        try:
            address = ipaddress.ip_address(socket_address[0])
        except ValueError:
            continue
        if address.is_global:
            target = ipv4_candidates if family == socket.AF_INET else ipv6_candidates
            value = str(address)
            if value not in target:
                target.append(value)
    return (ipv4_candidates or ipv6_candidates or [""])[0]


# ---- MEDIAWEBVPN-3-20261002：媒体流三级取流层级 ----
# 直连 → WebVPN 中转（恰一次）→ （系统代理偏好开时）系统代理。
# 系统代理供给语义自 MEDIA-VPN-1 的「首跳即走」收窄为「仅第三级」：前两级
# 一律显式直连（校外直连不到才值得改写；WebVPN 腿再骑用户代理会同时叠加
# 两跳变数，且 env/系统代理正是既有证据里打断学校认证链的成因）。
# 供给器按线程读位：每个媒体请求在自身线程内跑完整层级，threading.local
# 让并发请求互不见对方的第三级状态。
_media_system_proxy_leg_state = threading.local()


def _media_system_proxy_leg_active() -> bool:
    return bool(getattr(_media_system_proxy_leg_state, "active", False))


def _media_webvpn_relay_target(media_url: str) -> str | None:
    """Return the WebVPN-rewritten media URL, or None when the target is out of set.

    签名 URL 的验签输入=pathname+身份+时间戳（icourse.sign_video_url），
    改写只换 scheme+host 段、path+query 原样透传（webvpn.get_vpn_url）——
    源站验签输入逐字节不变。闭集外/scheme 非 https/带 userinfo 一律 None
    （调用方保持直连失败原样上抛，绝不放宽）。
    """
    try:
        parsed = urlsplit(str(media_url or "").strip())
        hostname = (parsed.hostname or "").casefold()
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or hostname not in MEDIA_WEBVPN_RELAY_ALLOWED_HOSTS:
        return None
    if parsed.username or parsed.password:
        return None
    from src.api.webvpn import get_vpn_url

    return get_vpn_url(media_url)


def _media_webvpn_relay_headers() -> dict[str, str]:
    """Minimal header set for the WebVPN media hop（webvpn jar 接管 Cookie）.

    手拼 Cookie 串（get_stream_params 的直连腿头集）不进本腿：会话 jar 按
    域自动附带 webvpn cookie，重复头既多余又扩大凭据外泄面（R-1 §(c)）。
    """
    from src.runtime import config

    return {
        "User-Agent": config.USER_AGENT,
        "Referer": config.ICOURSE_BASE + "/",
        "Accept": "*/*",
    }


# ---- RANGE-FIX-1-20261002：WebVPN 中转腿「start=0 大段 Range」瞬态 403 缓解 ----
# E1 实测（MEDIAWEBVPN-3 停车场项）：WebVPN 对「Range start=0 且窗长>1KiB」
# 返回 403 拦截页（~1.3KB），约 15 分钟级自愈；start=0 ≤1KiB 与 start>0 的
# Range 恒 206。缓解=两段式取流：0-1023 探针 + 1024- 续段（两段均非拦截
# 形状），合成单支流给播放器。判别闭集=中转腿 + GET + 恰 HTTP 403（网关
# refresh 恰一次后仍 403=签名新鲜度已重建，排除会话失效）+ 请求形状；命中
# 即切两段式并记一段拦截窗（monotonic TTL）：窗内后续 start=0 大窗直接探针
# 先行，不再发送已知被拦形状、也避开网关 403→重建会话的代价（SOAK-F1 教
# 训）。两段式自身失败按既有分类器走原三层回退与错误卡闭集；零新外联主机
# （两段都走同一 WebVPN 改写面，探针 1KiB）。跨线程状态竞态=多一次探针，
# 零正确性影响，不加锁。
_MEDIA_RELAY_TWOSTAGE_PROBE_END = 1023
_MEDIA_RELAY_BLOCK_WINDOW_SECONDS = 900.0
_media_relay_block_window = {"until": 0.0}


def _media_relay_block_window_active() -> bool:
    return time.monotonic() < _media_relay_block_window["until"]


def _media_relay_note_block_window() -> None:
    _media_relay_block_window["until"] = (
        time.monotonic() + _MEDIA_RELAY_BLOCK_WINDOW_SECONDS
    )


def _media_relay_clear_block_window() -> None:
    _media_relay_block_window["until"] = 0.0


def _media_relay_twostage_shape(
    range_header: str, *, head_only: bool
) -> tuple[int, int] | None:
    """瞬态 403 请求形状判别（闭集）：返回 bounded 有效窗 (start, end)。

    仅 GET 且 start=0 且窗长>探针（E1 拦截形状）需要两段式；HEAD、后缀
    Range、start>0、小窗、非法 Range 一律 None（走原单发路径）。
    """
    if head_only:
        return None
    try:
        effective = bounded_range_header(range_header)
        parsed = parse_upstream_range(effective)
    except ValueError:
        return None
    if not parsed:
        return None
    start, end = parsed
    if start != 0 or end is None or end <= _MEDIA_RELAY_TWOSTAGE_PROBE_END:
        return None
    return start, end


def _media_relay_transient_block_failure(
    failure: BaseException, shape: tuple[int, int] | None
) -> bool:
    """观测到的 403 是否构成 E1 瞬态拦截证据（闭集判别，零上游文本）。"""
    if shape is None or not isinstance(failure, _ConfirmedServiceResponse):
        return False
    return str(failure).strip() == "HTTP 403"


class _TwoStageMediaBody:
    """两段式合成体：探针段 + 续段顺序拼接（iter_content 合同同 requests）。"""

    def __init__(self, parts: list[RemoteMediaStream]):
        self._parts = list(parts)

    def iter_content(self, chunk_size: int):
        for part in self._parts:
            for chunk in part.iter_bytes(chunk_size):
                yield chunk

    def close(self) -> None:
        for part in self._parts:
            part.close()


def _open_media_stream_twostage(
    relay_gateway: RemoteMediaGateway, shape: tuple[int, int]
) -> RemoteMediaStream:
    """两段式取流：0-1023 探针 + 续段，合成与单发逐字段等价的响应流。

    探针已覆盖全窗（total≤探针长，或 416）时直接返回探针流——与单发在该
    total 下产出的响应等价。续段起点/total/etag 校验失败=上游对象在两段
    之间变化，闭集文案上抛（归 unknown → 错误卡）。断流续传复用网关既有
    _resume（起点>0 的 Range 恒非拦截形状）。
    """
    _, window_end = shape
    probe = relay_gateway.open(f"bytes=0-{_MEDIA_RELAY_TWOSTAGE_PROBE_END}")
    if probe.status != 206 or probe.end < _MEDIA_RELAY_TWOSTAGE_PROBE_END:
        return probe
    cont = None
    try:
        cont = relay_gateway.open(f"bytes={probe.end + 1}-{window_end}")
        if cont.status != 206 or cont.start != probe.end + 1:
            raise RuntimeError("upstream media changed during two-stage media open")
        if (
            probe.total_length and cont.total_length
            and probe.total_length != cont.total_length
        ):
            raise RuntimeError("upstream media changed during two-stage media open")
        if probe.etag and cont.etag and probe.etag != cont.etag:
            raise RuntimeError("upstream media changed during two-stage media open")
    except Exception:
        probe.close()
        if cont is not None:
            cont.close()
        raise
    total = probe.total_length or cont.total_length
    return RemoteMediaStream(
        status=206,
        content_type=probe.content_type,
        content_length=cont.end + 1,
        content_range=f"bytes 0-{cont.end}/{total}" if total else f"bytes 0-{cont.end}/*",
        total_length=total,
        start=0,
        end=cont.end,
        etag=probe.etag,
        _response=_TwoStageMediaBody([probe, cont]),
        _resume=relay_gateway._resume_stream,
    )


class TaskPaused(RuntimeError):
    """Internal cooperative-cancellation signal; never reported as a failure."""


class CatalogRefreshError(RuntimeError):
    """Closed catalog failure that is safe to expose without upstream details."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SummaryIncompleteError(RuntimeError):
    """Remote summary came back without a usable body; closed-set code.

    SUMMARY-FIX-1（FINALWRAP-C2 立案）：远端/本地总结结果缺正文时的闭集失败
    码 ``summary_incomplete``——诊断面稳定可查（任务账与脱敏 type:code 形态），
    取代原先的裸英文句 RuntimeError；学生面文案走任务失败码表兜底（同
    CloudSetupRequired 的 code 约定）。
    """

    def __init__(self, code: str = "summary_incomplete"):
        super().__init__(code)
        self.code = code


class CloudSetupRequired(RuntimeError):
    """Remote dispatch before GitHub authorization finished; closed-set code.

    code 固定 ``cloud_setup_required``（授权/连接未完成）。CLOUD-CONSENT-AUTO-1
    U1 起云端处理默认允许：拒绝环节只剩「连接/授权没就绪」，``cloud_disabled``
    （开关没开）随开关一并退役。
    """

    def __init__(self, message: str = "", *, code: str = "cloud_setup_required"):
        super().__init__(message)
        self.code = code


_CATALOG_ERROR_CODES = frozenset({
    "catalog_timeout",
    "catalog_route_unavailable",
    "catalog_session_expired",
    "catalog_additional_verification_required",
    "catalog_bearer_missing",
    "catalog_identity_invalid",
    "catalog_identity_mismatch",
    "catalog_payload_invalid",
    "catalog_detail_partial",
    "catalog_target_invalid",
    # LV1-4（LIVE-VALIDATE-1）：无凭据刷新如实按 login_required 归位（前端
    # EVIDENCE_DETAILS 已有该码的人话文案），不再误标 catalog_payload_invalid。
    "fudan_login_required",
})


def _catalog_error_code(exc: BaseException) -> str:
    explicit = str(getattr(exc, "code", "") or "").strip()
    aliases = {
        "timeout": "catalog_timeout",
        "network_unavailable": "catalog_route_unavailable",
        "catalog_auth_rejected": "catalog_additional_verification_required",
    }
    code = aliases.get(explicit, explicit)
    if code in _CATALOG_ERROR_CODES:
        return code
    from requests import RequestException, Timeout

    if _exception_chain_contains(exc, Timeout):
        return "catalog_timeout"
    if _exception_chain_contains(exc, RequestException):
        return "catalog_route_unavailable"
    return "catalog_payload_invalid"


# 闭集目录诊断：只允许计数、布尔与既定标签，绝不允许字段值/URL/正文。
_CATALOG_DIAGNOSTIC_KEYS = frozenset({
    "route_class", "code", "exception_class", "reason", "elapsed_ms",
    "months_ok", "months_total", "rows",
    "candidates", "detail_attempted", "detail_ok", "detail_failed",
    "detail_fail_transport_timeout", "detail_fail_transport",
    "detail_fail_code_rejected", "detail_fail_json", "detail_fail_http",
    "detail_fail_shape", "detail_fail_other",
    "code_kind_zero", "code_kind_200", "code_kind_other",
})
_CATALOG_DIAGNOSTIC_ROUTE_CLASSES = frozenset({"webvpn", "proxy", "direct"})
_CATALOG_DIAGNOSTIC_EXCEPTIONS = frozenset({
    "RuntimeError", "ValueError", "JSONDecodeError", "AttributeError",
    "TypeError", "KeyError", "IndexError", "Timeout", "ReadTimeout",
    "ConnectionError", "HTTPError", "RequestException", "DirectICourseError",
    "BearerUnavailableError",
})
_CATALOG_DIAGNOSTIC_LABEL_KEYS = frozenset({
    "route_class", "code", "exception_class", "reason",
})


def _closed_catalog_diagnostics(raw: object) -> dict[str, object]:
    """Keep only closed-set diagnostic keys with clamped values."""
    from src.api.icourse import CATALOG_DIAGNOSTIC_REASONS

    if not isinstance(raw, dict):
        return {}
    result: dict[str, object] = {}
    for key, value in raw.items():
        key = str(key)
        if key not in _CATALOG_DIAGNOSTIC_KEYS:
            continue
        if isinstance(value, bool):
            result[key] = value
        elif isinstance(value, int):
            result[key] = max(0, min(int(value), 1_000_000))
        elif isinstance(value, str) and key in _CATALOG_DIAGNOSTIC_LABEL_KEYS:
            if key == "route_class":
                result[key] = value if value in _CATALOG_DIAGNOSTIC_ROUTE_CLASSES else ""
            elif key == "exception_class":
                result[key] = value if value in _CATALOG_DIAGNOSTIC_EXCEPTIONS else "other"
            elif key == "reason":
                result[key] = value if value in CATALOG_DIAGNOSTIC_REASONS else ""
            elif value in _CATALOG_ERROR_CODES:
                result[key] = value
    return result


def _catalog_actions(code: str) -> list[str]:
    if code in {
        "catalog_session_expired",
        "catalog_identity_invalid",
        "catalog_identity_mismatch",
        "catalog_additional_verification_required",
    }:
        return ["login"]
    if code in {"catalog_timeout", "catalog_route_unavailable"}:
        return ["refresh-catalog", "diagnose-network"]
    return ["refresh-catalog"]








def _safe_error_message(exc: Exception) -> str:
    code = str(getattr(exc, "code", "") or "").strip()
    return f"{type(exc).__name__}:{code}" if code else type(exc).__name__


# B3 词汇对齐：worker 已归约的闭集码里，客户端已配人话文案、可直接上屏的
# 子集。任务失败时优先透传这些码（前端按码给可行动指引），其余失败仍走
# 脱敏 type:code 形态；集合必须保持为 TASK_ERROR_CODES 的子集（有钉）。
_REMOTE_WORKER_GUIDANCE_CODES = frozenset({
    "platform_challenge_required",
})


def _task_failure_message(exc: Exception) -> str:
    """Prefer a closed-set worker reason; otherwise the redacted type:code form."""
    code = str(getattr(exc, "code", "") or "").strip()
    if code in _REMOTE_WORKER_GUIDANCE_CODES:
        return code
    return _safe_error_message(exc)


def _is_supervisor_busy(exc: BaseException) -> bool:
    """True when another live supervisor already owns this task's remote run.

    The worker treats this as "queue, don't fail": the task stays queued for
    the active supervisor instead of being marked failed.
    """
    return str(getattr(exc, "code", "") or "") == "remote_supervisor_busy"


# AS10（第五十案）：remote_supervisor_busy 的有界处置——自愈恰一次，之后
# 30/60/120s 封顶退避，第二个连续周期起转诚实排队态，绝不回到 5s 永动重排。
# 键前缀 REMOTE_BUSY_STATE_KEY_PREFIX 住 task_store（见顶部 import；mark_terminal
# 终态清键需要），此处仅消费。
REMOTE_BUSY_BACKOFF_SECONDS = (30.0, 60.0, 120.0)
REMOTE_BUSY_HONEST_STAGE = 2


def _exception_chain_contains(exc: BaseException, target: type[BaseException]) -> bool:
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        if isinstance(current, target):
            return True
        visited.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _remote_dispatch_workflow(payload: dict, *, job_kind: str = "") -> str:
    """N1-ROUTING：按任务种类与载荷媒体面派发工作流（f08b6c0 llm.yml 快路径）。

    媒体面缺席的纯 LLM 任务（总结/章节、质量抽检）改派 llm.yml 快路径：
    六钉最小装机，无 apt/ffmpeg/模型缓存，免付 ~195s/次的环境税；载荷带
    ``media`` 键=真实媒体腿（现役唯一=字幕任务的音视频取流），维持
    process.yml 全媒体环境字节不变。例外白名单（D-20261009-13）：worker
    N20 profile 合同（runner ``_MEDIA_PROFILE_REQUIRED_KINDS``）要求
    learning_pack 必须跑在 process-v1（问答链要读转写/证据基建语境），
    误派 llm.yml 会被 fail-closed 拒跑（任务 06b42931 实证 115.4s 死于
    WorkerError）——故 learning_pack 显式白名单回 process.yml，载荷形状
    （query/evidence 无 media）不再单独决定其归属。
    双向 fail-closed 兜底不变：媒体任务误入 llm.yml 由 worker 既有闭集
    语义拒跑（无模型根环境）；纯 LLM 任务误留 process.yml 只是多付一次
    媒体环境税，不产生任何错误行为。
    """
    if job_kind == "learning_pack":
        return "process.yml"
    return "process.yml" if "media" in payload else "llm.yml"






def _headers_to_dict(headers: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in headers.splitlines():
        key, sep, value = raw.partition(":")
        if sep and key.strip():
            result[key.strip()] = value.strip()
    return result


def _client_alive_verdict(client) -> bool | None:
    """SOAK-F1-R：探活三态收编——True/False 权威，None=不可判（保活不重建）。

    ac0c7be（SUP1-F1）同族纪律的验证边界版：传输类失败与网关 5xx 对
    「会话是否有效」零证据，判死会触发全量重建 = 一次全量 WebVPN 重登录
    （夜间抖动/502 窗口被验证边界放大成重登录风暴）。权威死亡证据
    （401/403、登录页、目录会话过期）仍照旧判死重建。
    """
    import requests

    from src.api.icourse import SessionUnverifiable

    try:
        return bool(client.check_alive())
    except (SessionUnverifiable, requests.RequestException):
        # 真实 ICourseClient 已在 check_alive 内把传输类失败收编为
        # SessionUnverifiable；RequestException 兜底覆盖变体探活实现。
        return None


# Git 忽略的 runtime/cache 子目录：登录阶段遥测 artifact（严格脱敏 JSONL）。
# 目录名用语义名（无日期）；2026-09-06 立项批次的历史目录名由
# LoginStageTelemetry 首写前一次性整目录改名迁移（只搬家不改内容）。
LOGIN_STAGE_CACHE_DIRNAME = "fudan-login-stage-telemetry"
LOGIN_STAGE_LEGACY_CACHE_DIRNAME = "fudan-login-stability-20260906"

# 进程内一次性迁移标记：迁移成功或确认无需迁移后置 True；失败保持 False，
# 留待下一实例重试（与遥测吞错纪律一致）。
_LOGIN_STAGE_MIGRATED = False

# 进程内 last-success 记忆：只影响下一次登录的候选排序。值为闭集类别
# （route ∈ {"direct","proxy"}、transport ∈ curl 类别），不含任何账号数据。
# 启动装配时从 webvpn_ticket_memory app-state 惰性 hydrate（代际匹配才生效），
# 登录成功后尽力回写；持久层只存类别，绝不落代理 URL/凭据。跨 attempt/
# 跨账户的会话与 cookie 隔离不受它影响。
_LAST_TICKET_SUCCESS: dict[str, str | None] = {"route": None, "transport": None}

# V5 会话检查点恢复航班的总时限：只包含两个 portal 探针（各 (3,5)s 超时），
# 不含任何凭据提交；超时即按失败处理，丢弃检查点退回普通登录。
_CHECKPOINT_RESTORE_DEADLINE_SECONDS = 20.0

# P3.1 宿主休眠恢复的惰性判定阈值：只在既有调用点（受保护动作/连接快照/
# 按需诊断）比较上次观察，绝无后台轮询。墙钟大幅超前单调钟 = 单调钟不含
# 挂起时段（Linux/macOS）；同进程调用点之间长时间静默则兜底覆盖单调钟
# 计入挂起的平台。两条阈值都远宽于 NTP 校时噪声，误触发代价仅一次代际
# 递增（下次受保护动作多一次重建，本就健康路径上无感）。
_HOST_RESUME_DRIFT_SECONDS = 120.0
_HOST_RESUME_IDLE_SECONDS = 1800.0

# 登录航班的终止性失败闭集（P0.2）：凭据拒绝、交互式挑战、不安全跳转、
# 票链已消费后的确认拒绝/传输失败。命中即终止本航班——绝不换路由、绝不重试、
# 绝不重放 ticket。票前传输失败仍按既有梯子重试（H2/H1 预热回退保留在
# WebVPNSession.prepare_ticket_transport 内部，发生在任何 ticket 产生之前）。
_LOGIN_TERMINAL_CODES = frozenset({
    "fudan_credentials_rejected",
    "fudan_challenge_required",
    "fudan_account_locked",
    "fudan_service_maintenance",
    "fudan_redirect_unsafe",
    "fudan_ticket_rejected",
    "webvpn_ticket_transport_failed",
    "icourse_ticket_transport_failed",
})

_LOGIN_TERMINAL_MESSAGES = {
    "fudan_credentials_rejected": "登录失败，请检查账号、密码或网络状态",
    "fudan_challenge_required": "需要在复旦页面完成安全验证，请重新登录并完成验证",
    "fudan_account_locked": "账号已被锁定或冻结，请先在复旦账号服务解除锁定后再登录",
    "fudan_service_maintenance": "校园服务暂时维护，请稍后重试",
    "fudan_redirect_unsafe": "登录跳转校验未通过，请重新登录",
    "fudan_ticket_rejected": "校园会话建立未确认，请重新登录",
    "webvpn_ticket_transport_failed": "校园会话建立未确认，请重新登录",
    "icourse_ticket_transport_failed": "校园会话建立未确认，请重新登录",
}

# 目录路由循环的终止性失败闭集：身份不匹配/不安全跳转与路径无关，
# 换路径重试无意义且不安全。
_CATALOG_TERMINAL_CODES = frozenset({
    "catalog_target_invalid",
    "catalog_identity_mismatch",
})

# 连接动作分步进度闭集（FRONTEND-SMOOTH-1 单元D）：动作执行期间连接快照
# 附带 action_progress {action, stage, label, updated_at}，前端轮询展示。
# 阶段与文案全闭集——未知阶段不进入快照，前端按其自身闭集回退。
REMOTE_ACTION_STAGE_LABELS: dict[str, str] = {
    "creating_repositories": "正在创建仓库",
    "syncing_documents": "正在同步文档",
    "verifying_worker": "正在校验 Worker",
    "repairing_worker": "正在修复 Worker",
    "configuring_secrets": "正在配置密钥",
    "rotating_keys": "正在配置密钥",
    "testing_channel": "正在校验加密通道",
    "verifying_connection": "正在确认连接状态",
}

# 首跑 bootstrap 瞬态自愈闭集（GH-UX-REWORK-1）：bootstrap 幂等边界上的
# 瞬态失败码——退避重试而非把一次网络抖动变成用户可见的「初始化失败」。
# 非瞬态（权限/冲突/名称占用）绝不重试，保持既有闭集报错一次上抛。
BOOTSTRAP_TRANSIENT_ERROR_CODES = frozenset({
    "github_unreachable", "rate_limited", "github_service_error",
})
BOOTSTRAP_SELFHEAL_RETRY_DELAYS_SECONDS = (3.0, 8.0)


class LoginStageTelemetry:
    """登录阶段遥测写入器（严格脱敏 JSONL，落 Git 忽略的 runtime/cache）。

    只记录：闭集阶段名、duration_ms、闭集 outcome、attempt 序号、
    operation_id_sha256_12（本批恒为 null：operation_id 位于本 allowlist 之外的
    HTTP 层，由静态 reason 字段说明）、ts、可选的纯数字 status。
    绝不记录 URL、query、headers、cookies、正文、账号或凭据。
    两种既有回调形态都被接受并归一化：WebVPNSession 的 (step, details) 与
    DirectICourseSession 的 (step)。任何写入失败都被吞掉：遥测永不影响登录。
    首次写入前懒执行一次有界保留：artifact 只保留最近 20 个且龄期不超过
    30 天，失败静默；同处先做旧批次名缓存目录的一次性整目录迁移。
    """

    CLOSED_STAGES = frozenset({
        # 票腿（既有 _report_step 发射；curl_cffi 传输，仅总时长可见）
        "webvpn_ticket_complete",
        "webvpn_ticket_error",
        "icourse_ticket_complete",
        "icourse_ticket_error",
        # ticket transport 创建时的无凭据传输预热（webvpn.py _warm_ticket_transport）
        "webvpn_ticket_warmup",
        # 直连 catalog 会话的既有 marker 发射
        "catalog_context",
        "catalog_challenge",
        "catalog_credentials",
        "catalog_ticket",
        "catalog_identity",
        # requests 传输层每请求 elapsed（_DeadlineSession.send + 显式阶段标签）
        "webvpn_auth_context",
        "webvpn_auth_methods",
        "webvpn_public_key",
        "webvpn_auth_execute",
        "webvpn_cas_ticket",
        "webvpn_portal_probe",
        "icourse_preflight_probe",
        "icourse_casapi",
        "icourse_auth_methods",
        "icourse_public_key",
        "icourse_auth_execute",
        "icourse_cas_ticket",
        "icourse_portal_probe",
    })
    CLOSED_OUTCOMES = frozenset({
        "ok",
        "recovered",
        "timeout",
        "connection_error",
        "request_error",
        "marker",
    })
    CLOSED_KEYS = frozenset({
        "stage",
        "duration_ms",
        "outcome",
        "attempt",
        "operation_id_sha256_12",
        "reason",
        "ts",
        "http_status",
        "route",
        "transport",
    })
    # 闭集值：route/transport 类别字段只接受这些值，未知值整字段丢弃。
    CLOSED_ROUTES = frozenset({"direct", "proxy"})
    CLOSED_TRANSPORTS = frozenset({
        "curl_h2",
        "curl_h1",
    })
    # 有界保留：artifact 文件只保留最近 20 个（prune 每实例至多执行一次）
    RETENTION_KEEP = 20
    # 龄期上界：mtime 超过 30 天的 artifact 一律清除（容量与陈旧双上界；
    # 30 天 ≈ 一个月学期诊断窗口，容量上界仍约 21×3.3KB≈70KB/副本）。
    RETENTION_MAX_AGE_SECONDS = 30 * 86400
    # 迁移留痕文件：一行 JSONL 记一次成功迁移；.log 后缀不匹配 prune glob。
    MIGRATION_LOG_BASENAME = "login-stage-migration.log"

    def __init__(self, directory: str | Path | None = None, *, attempt: int = 1):
        if directory is None:
            directory = PROJECT_ROOT / "runtime" / "cache" / LOGIN_STAGE_CACHE_DIRNAME
        self._directory = Path(directory)
        # MAC-NIGHT-1（round4 探针 mac 实证）：纯毫秒名在同毫秒双实例下
        # 互覆（fast arm64 上稳定复现）——加 8 位随机尾，glob/排序语义不变。
        self._path = self._directory / (
            f"login-stage-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.jsonl"
        )
        self.attempt = max(1, int(attempt))
        self._pruned = False  # 有界保留懒执行标记：每实例至多 prune 一次

    def set_attempt(self, attempt: int) -> None:
        try:
            self.attempt = max(1, int(attempt))
        except Exception:
            pass

    def __call__(self, step, details=None) -> None:
        """接受 (step, details) 与 (step) 两种既有回调形态并归一化。"""
        try:
            stage = str(step or "")
            if stage not in self.CLOSED_STAGES:
                return  # 闭集之外的 step 名一律丢弃
            record = self._normalize(
                stage, details if isinstance(details, dict) else {}
            )
            if record is not None:
                self._append(record)
        except Exception:
            pass  # swallow-all：遥测失败绝不影响登录

    def _normalize(self, stage: str, details: dict) -> dict:
        outcome = details.get("outcome")
        if outcome in self.CLOSED_OUTCOMES:
            # _DeadlineSession 每请求 elapsed 记录：duration_ms 已是毫秒
            duration_ms = self._int_or_none(details.get("duration_ms"))
        elif stage.endswith("_complete"):
            outcome = "recovered" if details.get("recovered_after") else "ok"
            duration_ms = self._seconds_to_ms(details.get("elapsed_seconds"))
        elif stage.endswith("_error"):
            error_type = str(details.get("error_type") or "")
            if "Timeout" in error_type:
                outcome = "timeout"
            elif "Connection" in error_type:
                outcome = "connection_error"
            else:
                outcome = "request_error"
            duration_ms = self._seconds_to_ms(details.get("elapsed_seconds"))
        else:
            outcome = "marker"
            duration_ms = None
        record = {
            "stage": stage,
            "duration_ms": duration_ms,
            "outcome": outcome,
            "attempt": int(self.attempt),
            "operation_id_sha256_12": None,
            "reason": "http-layer",
            "ts": int(time.time() * 1000),
        }
        status = details.get("status")
        if isinstance(status, int) and not isinstance(status, bool):
            record["http_status"] = status
        route = details.get("route")
        if route in self.CLOSED_ROUTES:
            record["route"] = route
        transport = details.get("transport")
        if transport in self.CLOSED_TRANSPORTS:
            record["transport"] = transport
        return record

    @staticmethod
    def _int_or_none(value) -> int | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
        return None

    @classmethod
    def _seconds_to_ms(cls, value) -> int | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(round(value * 1000))
        return None

    def _append(self, record: dict) -> None:
        self._prune_once()  # 首次成功写入前懒执行一次有界保留
        self._directory.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _prune_once(self) -> None:
        """有界保留（每实例至多一次）：先做旧目录一次性迁移，再按
        「保最新 20 个 + 龄期 30 天」清理，失败静默。"""
        if self._pruned:
            return
        self._pruned = True
        self._migrate_legacy_directory()
        try:
            # 仅匹配本类 artifact 文件名：不递归、不触碰父/兄弟目录，
            # 同目录的 benchmark 资产等非匹配条目一律不动。
            entries = sorted(
                self._directory.glob("login-stage-*.jsonl"),
                key=lambda entry: entry.stat().st_mtime,
                reverse=True,
            )
            cutoff = time.time() - self.RETENTION_MAX_AGE_SECONDS
            kept = 0
            for entry in entries:  # 最新在前：龄期与限额双闸
                if not entry.is_file():  # 同名模式的目录必须跳过
                    continue
                if entry.stat().st_mtime < cutoff:  # 龄期上界优先清除
                    entry.unlink()
                    continue
                kept += 1
                if kept > self.RETENTION_KEEP:
                    entry.unlink()
        except Exception:
            pass  # glob/排序/unlink 全链失败一律静默（如目录被文件占用）

    def _migrate_legacy_directory(self) -> None:
        """一次性迁移：历史批次名目录整目录改名为当前目录（只搬家不改内容）。

        幂等：仅当「旧目录存在且新目录不存在」才 Path.rename（同卷原子，
        文件内容与 mtime 原样，prune 语义不变）；两目录并存属异常态，保守
        不动。成功记一行迁移日志；失败静默留待下一实例重试。绝不影响登录。
        """
        global _LOGIN_STAGE_MIGRATED
        if _LOGIN_STAGE_MIGRATED:
            return
        try:
            legacy = self._directory.parent / LOGIN_STAGE_LEGACY_CACHE_DIRNAME
            if not legacy.is_dir() or self._directory.exists():
                _LOGIN_STAGE_MIGRATED = True  # 无旧目录/新目录已在：本进程免重查
                return
            file_count = sum(1 for entry in legacy.iterdir() if entry.is_file())
            legacy.rename(self._directory)
            _LOGIN_STAGE_MIGRATED = True
            with open(
                self._directory / self.MIGRATION_LOG_BASENAME, "a", encoding="utf-8"
            ) as handle:
                handle.write(
                    json.dumps(
                        {
                            "event": "legacy_cache_dir_migrated",
                            "from": LOGIN_STAGE_LEGACY_CACHE_DIRNAME,
                            "to": LOGIN_STAGE_CACHE_DIRNAME,
                            "files": file_count,
                            "ts": int(time.time() * 1000),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception:
            pass  # 迁移失败静默：留待下一实例重试，遥测永不影响登录


class CourseLensApplication:
    """Coordinates the authenticated online client and remote Worker runtime."""

    def __init__(self, output_dir: str | Path = DEFAULT_DATA_DIR):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.catalog_repository = CatalogRepository(self.output_dir / "state.db")
        self.credentials = CredentialStore(self.output_dir / "credentials.json")
        self.task_store = TaskStore(self.output_dir / "state.db")
        # 夜10-C：生产服务启用可写连接按线程池化（读热面 1.30ms→0.005ms/次；
        # 测试/工具默认一次一连，保持临时目录生命周期契约）。
        self.task_store.enable_connection_pool()
        self.learning_store = LearningStore(self.output_dir / "learning.db")
        ensure_student_feature_schema(self.output_dir / "learning.db")
        ensure_document_schema(self.output_dir / "learning.db")
        ensure_smart_playback_schema(self.output_dir / "learning.db")
        ensure_concept_schema(self.output_dir / "learning.db")
        ensure_analytics_schema(self.output_dir / "learning.db")
        # STUDY-STATS-M1：学习统计 v2 默认层表（study_daily_seconds）随 boot
        # ensure 族建表；旧库零行=诚实空态，零 backfill。
        ensure_study_stats_schema(self.output_dir / "learning.db")
        # night14-R2 Fix-D：assessment_events 建表随 boot 五连 ensure 一并执行
        # （learning_schema 自证「幂等随 ensure 族调用」，此前仅挂首份总结工件
        # 钩子与 client-reset 两处——fresh 安装首次进播放桌必撞 no such table
        # 500，R2 真机+进程内双实证）。
        ensure_assessment_schema(self.output_dir / "learning.db")
        # 数据搬家包（D12 P0）：导出包下载令牌登记（token→文件，单次有效）。
        self._migration_download_lock = threading.Lock()
        self._migration_downloads: dict[str, dict] = {}
        self.network = NetworkSettings(self.task_store)
        # MEDIA-VPN-1-20261001：媒体流可选系统代理——网关开流时经此供给读
        # 持久化偏好（默认关，注册一次随生命周期）。仅媒体字节流面受它影响，
        # 授权链/目录/WebVPN 面零改动。
        # MEDIAWEBVPN-3-20261002 语义收窄：供给器改为读线程local 三级位——
        # 系统代理偏好不再作用于首跳（直连/WebVPN 中转两级恒显式直连），
        # 只在取流层级的第三级（open_remote_media）按线程置位生效。
        configure_media_stream_proxy_provider(_media_system_proxy_leg_active)
        from src.remote.github_app import GitHubAppClient
        self.github_app = GitHubAppClient(
            self.credentials,
            proxy_url=self.network.github_proxy(),
        )
        self._github_device_lock = threading.RLock()
        self._github_device_authorization = None
        self._github_device_last_poll = 0.0
        from src.remote.coordinator import RemoteCoordinator
        # 云端处理默认允许（CLOUD-CONSENT-AUTO-1 U1）：本地开关不再决定协调器
        # 是否建立——连接/授权/密钥齐备即可用，缺件由 validate 如实拒绝。
        self.remote_settings = self._load_remote_settings()
        self.remote_coordinator = None
        try:
            self.remote_coordinator = RemoteCoordinator(
                self.remote_settings,
                self.task_store,
                self.credentials,
            )
        except RuntimeError:
            # An expired Device Flow token must not prevent the local UI
            # from starting so the user can authorize again.
            self.remote_coordinator = None
        self.resources = ResourceCoordinator()
        self._normalize_task_progress_models()
        # DEAD-TASK-PURGE A（取证先于改写）：上会话崩溃残留的在途僵尸必须在
        # recover_for_startup 把它们改写成 paused、刷新 updated_at 之前取证，
        # 否则与「正常关机遗留的合法暂停行」永久不可区分（checkpoint_for_shutdown
        # 与 recover_for_startup 产物同形）。落账在启动链 _reconcile_zombie_tasks_on_startup。
        self._startup_zombie_task_ids = self._collect_startup_zombie_task_ids()
        self.task_store.recover_for_startup()
        self._normalize_recorded_operation_recovery()
        self._vpn: WebVPNSession | None = None
        self._client: ICourseClient | None = None
        self._client_last_verified_at = 0.0
        self._client_nudge_in_flight = False
        self._lock = threading.RLock()
        # ⑩：云端在飞并发闸（自动化与手动共用，见 _cloud_run_slot）。
        self._cloud_run_condition = threading.Condition()
        self._cloud_runs_active = 0
        self._remote_echo_thread: threading.Thread | None = None
        self._remote_action_stage: dict[str, object] = {}
        self._remote_echo_status: dict[str, object] = {
            "state": "idle",
            "task_id": "",
            "run_id": 0,
            "stage": "",
            "percent": None,
            "message": "尚未运行本次启动后的连接测试",
            "updated_at": time.time(),
        }
        self._client_refresh_lock = threading.RLock()
        # P0.3/P0.4：路由代际、单航班失败共享与每服务路由证据。
        self._route_generation = self.network.route_generation()
        self._client_generation = self._route_generation
        # 登录 last-success 候选记忆跨重启 hydrate（仅空值 + 代际匹配时生效）。
        self._hydrate_ticket_memory()
        # P3.1：宿主恢复惰性判定的上一次观察点（单调钟, 墙钟）。
        self._host_activity_observed = (time.monotonic(), time.time())
        self._refresh_waiters = 0
        self._refresh_failure: tuple[Exception, int] | None = None
        self._webvpn_route = ""
        self._icourse_route_class = ""
        self._icourse_route_path = ""
        self._session_ever_connected = False
        self._connection_probe_started = False
        self._connection_probe_in_flight = False
        self._connection_probe_stop = threading.Event()
        self._connection_probe_thread: threading.Thread | None = None
        # P2-A：预检去重状态 (route_generation, monotonic)；P2-E：TUN 提示迁移去重。
        self._preflight_campus_state: tuple[int, float] | None = None
        self._tun_hint_published = False
        self._catalog_refresh_thread: threading.Thread | None = None
        self._auto_connect_started = False
        self._auto_connect_resume_stop = threading.Event()
        self._auto_connect_resume_thread: threading.Thread | None = None
        # P63-T1：worker-auto-sync 与 probe/resume 同族，close 须可置停可 join。
        self._worker_auto_sync_stop = threading.Event()
        self._worker_auto_sync_thread: threading.Thread | None = None
        # SRC-SYNDROME-1 U2：保活补验开始时刻（防抖收敛用）+ 自愈看门狗一次性旗标。
        self._client_revalidate_started_at = 0.0
        self._session_self_heal_started = False
        # N9-H（P63 停车场销项）：自愈看门狗与 probe/resume/sync 同族，close 须可置停可 join。
        self._session_self_heal_stop = threading.Event()
        self._session_self_heal_thread: threading.Thread | None = None
        # REALRUN-1 旅程6（2026-10-08 真测）：自愈退避落实例态——恢复事件
        # （宿主恢复/校园路径恢复探测）要能复位退避，否则断网期放大到封顶的
        # 旧退避让恢复后一次重验最长要等 10 分钟，degraded 挂满窗。
        self._session_self_heal_backoff = SESSION_SELF_HEAL_TICK_SECONDS
        self._subtitle_queue: list[dict[str, object]] = []
        self._subtitle_current: dict | None = None
        self._summary_queue: list[dict[str, object]] = []
        self._summary_worker: threading.Thread | None = None
        self._summary_current: dict | None = None
        self._restore_prime_started = False
        self._courseware_pdf_queue: list[str] = []
        self._courseware_pdf_worker: threading.Thread | None = None
        self._question_queue: list[dict[str, object]] = []
        self._question_worker: threading.Thread | None = None
        self._question_current: dict | None = None
        # P11：质量抽检线程族（镜像 question worker；不进 pause 全家桶——
        # 运行中质检不可取消，queued 态走 generic cancel 分支）。
        self._quality_queue: list[dict[str, object]] = []
        self._quality_worker: threading.Thread | None = None
        self._quality_current: dict | None = None
        self._paused = False
        if self.task_store.global_paused:
            self.task_store.set_global_paused(False)
        self._current: dict | None = None
        # SRC-SYNDROME-1 U5 并行派发：字幕族取消事件与活跃集合按任务记（键=task_id），
        # 取消/暂停不再跨任务连坐；消费者池至多 cloud_run_limit 路。
        self._subtitle_cancel_events: dict[str, threading.Event] = {}
        self._summary_cancel = threading.Event()
        self._question_cancel = threading.Event()
        # WP-3（N9-H close 链全景第三例，P63 家族）：summary/question/pdf 三个
        # 本地生成 worker 的停机置停事件——close() 置停后循环顶退场，杜绝停机
        # 途中在途完成项写已关 store。
        self._generation_workers_stop = threading.Event()
        self._active_subtitle_task_ids: set[str] = set()
        self._active_summary_task_id = ""
        self._active_question_task_id = ""
        self._subtitle_workers = 0
        self._subtitle_worker_seq = 0
        self._progress_trackers: dict[str, ProgressTracker] = {}
        self._credentials: dict[str, str] = {
            "student_id": os.environ.get("StuId", ""),
            "password": os.environ.get("UISPsw", ""),
        }
        self._deepseek_api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        # AS6 余额读数缓存：(取数实钟, 快照)；成功/失败两档 TTL，见常量处。
        self._deepseek_balance_cache: tuple[float, dict] | None = None
        configured = bool(self._credentials["student_id"] and self._credentials["password"])
        self._login_status: dict[str, object] = {
            "state": "configured" if configured else "idle",
            "step": "credentials",
            "attempt": 0,
            "max_attempts": 3,
            "course_index": 0,
            "course_total": 0,
            "connected": False,
            "message": "登录信息已就绪" if configured else "等待登录",
            "updated_at": int(time.time() * 1000),
        }
        self._catalog_status: dict[str, object] = {
            "state": "idle",
            "code": "fudan_login_required",
            "actions": ["login"],
            "route_class": "",
            "updated_at": int(time.time() * 1000),
        }
        self._pending_catalog_diagnostics: dict[str, object] = {}
        self._catalog_generation = 0
        self.timetable = TimetableRuntime(
            self.task_store.path,
            identity_getter=self._identity_scope,
            auth_getter=self.authentication_snapshot,
            vpn_getter=self._timetable_vpn,
            catalog_getter=self._authorized_catalog_courses_raw,
        )
        self.search_index = LearningSearchIndex(
            self.learning_store,
            self.public_catalog_snapshot,
            self.subtitle_segments,
            self.resources,
        )
        self._search_index_started = False
        from src.remote.connection import RemoteConnectionSupervisor
        self.remote_connection = RemoteConnectionSupervisor(
            self.task_store,
            self.credentials,
            lambda: self.github_app,
            active_work=self.has_active_work,
        )
        from src.runtime.automation import AutomationService
        self.automation = AutomationService(
            self.task_store,
            self.credentials,
            lambda: self.github_app,
            local_schedule_getter=lambda: dict(
                self.task_store.get_app_state("daily_schedule", {}) or {}
            ),
            local_schedule_setter=self.configure_daily_schedule,
            result_importer=self._import_automation_result,
            verified_catalog_getter=self._automation_verified_catalog,
        )
        self._automation_stop = threading.Event()
        self._automation_thread: threading.Thread | None = None
        if (
            self.github_app.snapshot().get("bootstrapped")
            and os.environ.get("COURSELENS_DISABLE_AUTOMATION_MONITOR") != "1"
        ):
            self._automation_thread = threading.Thread(
                target=self._automation_monitor_loop,
                name="courselens-automation-monitor",
                daemon=True,
            )
            self._automation_thread.start()

    def onboarding_snapshot(self) -> dict:
        consent = dict(self.task_store.get_app_state("student_processing_consent", {}) or {})
        recorded_approvals = dict(
            self.task_store.get_app_state("external_service_approvals", {}) or {}
        )
        approvals = {
            "github_terms": bool(
                os.environ.get("COURSELENS_GITHUB_TERMS_APPROVED") == "1"
                or recorded_approvals.get("github_terms") is True
            ),
            "fudan_processing": bool(
                os.environ.get("COURSELENS_FUDAN_PROCESSING_APPROVED") == "1"
                or recorded_approvals.get("fudan_processing") is True
            ),
        }
        return {
            "network": self.network.snapshot(),
            "github": self.github_app.snapshot(),
            "fudan": {
                "configured": bool(self._credentials.get("student_id")),
                "connected": bool(self._login_status.get("connected")),
            },
            "ai": {"configured": bool(self._deepseek_key())},
            "consent": {
                "accepted": bool(consent.get("accepted")),
                "version": str(consent.get("version") or ""),
                "accepted_at": consent.get("accepted_at"),
            },
            "approvals": approvals,
            "approvals_confirmed_at": recorded_approvals.get("confirmed_at"),
            "release_mode": "student-pilot" if all(approvals.values()) else "developer-validation",
        }

    def update_processing_consent(self, accepted: bool) -> dict:
        value = {
            "accepted": bool(accepted),
            "version": "processing-consent.v1",
            "accepted_at": time.time() if accepted else None,
        }
        self.task_store.set_app_state("student_processing_consent", value)
        return self.onboarding_snapshot()

    # ---- 新手引导记录：教程状态与服务 readiness 分离（合同 §2.2/§2.3） ----

    def _onboarding_guide_view(
        self, *, disposition: str, auto_opened: bool, source: str,
        persistence: str, updated_at: float,
    ) -> dict:
        return {
            "schema": ONBOARDING_GUIDE_SCHEMA,
            "version": ONBOARDING_GUIDE_VERSION,
            "disposition": disposition,
            "auto_opened": auto_opened,
            "persistence": persistence,
            "source": source,
            "updated_at": float(updated_at),
        }

    def _has_existing_user_evidence(self) -> bool:
        """旧用户证据：已保存账号 / 已持久化授权目录 / 历史任务，全部为本地只读检查。"""
        if self.credentials.list_accounts():
            return True
        cache_key = self._catalog_cache_key()
        if cache_key and self.task_store.get_app_state(cache_key, None) is not None:
            return True
        # 旧用户证据=任意历史任务（CLIENT-STATE-R1：显式关保留窗，90 天前
        # 的任务行同样是「用过产品」的证据）。
        return bool(self.task_store.list_tasks(limit=1, terminal_retention_days=None))

    def onboarding_guide_snapshot(self) -> dict:
        """只读引导记录视图；读取路径绝不写 app state。"""
        record = self.task_store.get_app_state(ONBOARDING_GUIDE_STATE_KEY, None)
        if record is None:
            if self._has_existing_user_evidence():
                # 版本上线不得打断既有用户：缺记录 + 旧用户证据 → 视为已跳过
                return self._onboarding_guide_view(
                    disposition="dismissed", auto_opened=True,
                    source="legacy_existing_user", persistence="ready", updated_at=0.0,
                )
            return self._onboarding_guide_view(
                disposition="new", auto_opened=False,
                source="default", persistence="ready", updated_at=0.0,
            )
        if not isinstance(record, dict):
            return self._corrupt_onboarding_guide_view()
        if record.get("version") != ONBOARDING_GUIDE_VERSION:
            # 旧版本记录不得冒充新版本的完成/跳过状态：按当前版本重新开始
            return self._onboarding_guide_view(
                disposition="new", auto_opened=False,
                source="default", persistence="ready", updated_at=0.0,
            )
        disposition = record.get("disposition")
        auto_opened = record.get("auto_opened")
        updated_at = record.get("updated_at")
        invalid = False
        if disposition not in ONBOARDING_GUIDE_DISPOSITIONS:
            invalid = True
            disposition = "new"
        if not isinstance(auto_opened, bool):
            invalid = True
            auto_opened = False
        if isinstance(updated_at, bool) or not isinstance(updated_at, (int, float)) or updated_at < 0:
            invalid = True
            updated_at = 0.0
        return self._onboarding_guide_view(
            disposition=disposition, auto_opened=auto_opened,
            source="stored", persistence="invalid" if invalid else "ready",
            updated_at=float(updated_at),
        )

    def _corrupt_onboarding_guide_view(self) -> dict:
        return self._onboarding_guide_view(
            disposition="new", auto_opened=False,
            source="stored", persistence="invalid", updated_at=0.0,
        )

    def onboarding_guide_action(self, action: str, version: str) -> dict:
        """本地幂等引导偏好写入；写入完整有效记录（对损坏/隐式旧用户记录是用户显式修复）。"""
        action = str(action or "").strip().lower()
        if action not in ONBOARDING_GUIDE_ACTIONS:
            raise ValueError("onboarding guide action is invalid")
        if version != ONBOARDING_GUIDE_VERSION:
            raise OnboardingGuideVersionError(
                f"onboarding guide version mismatch: {version!r} != {ONBOARDING_GUIDE_VERSION!r}"
            )
        current = self.onboarding_guide_snapshot()
        if action == "mark-opened":
            keep_disposition = (
                current.get("persistence") == "ready"
                and current.get("disposition") in {"dismissed", "completed"}
            )
            disposition = current["disposition"] if keep_disposition else "new"
        elif action == "dismiss":
            disposition = "dismissed"
        else:
            disposition = "completed"
        self.task_store.set_app_state(ONBOARDING_GUIDE_STATE_KEY, {
            "schema": ONBOARDING_GUIDE_SCHEMA,
            "version": ONBOARDING_GUIDE_VERSION,
            "disposition": disposition,
            "auto_opened": True,
            "updated_at": time.time(),
        })
        return self.onboarding_guide_snapshot()

    def update_network_settings(self, mode: str, proxy_url: str = "") -> dict:
        snapshot = self.network.update(mode, proxy_url)
        with self._lock:
            # P0.3：路由代际推进——只把当前客户端标记为过期，绝不中断在途
            # 请求；下一个受保护动作将按新代际重新验证/重建（client()）。
            self._route_generation = self.network.route_generation()
            self._client_last_verified_at = 0.0
        proxy = self.network.github_proxy()
        if proxy:
            self.credentials.save_secret("network_github_proxy", proxy)
        else:
            self.credentials.delete_secret("network_github_proxy")
        from src.remote.github_app import GitHubAppClient
        self.github_app = GitHubAppClient(
            self.credentials,
            proxy_url=proxy,
        )
        if getattr(self, "remote_connection", None) is not None:
            self.remote_connection.request_probe()
        return snapshot

    def diagnose_network(self) -> dict:
        result = self.network.diagnose()
        proxy = self.network.github_proxy()
        if proxy:
            self.credentials.save_secret("network_github_proxy", proxy)
        else:
            self.credentials.delete_secret("network_github_proxy")
        from src.remote.github_app import GitHubAppClient
        self.github_app = GitHubAppClient(self.credentials, proxy_url=proxy)
        if getattr(self, "remote_connection", None) is not None:
            self.remote_connection.request_probe()
        return result

    def start_github_device_authorization(self, *, force: bool = False) -> dict:
        with self._github_device_lock:
            if force:
                self.github_app.clear_user_authorization()
                self._github_device_authorization = None
                self._github_device_last_poll = 0.0
            snapshot = self.github_app.snapshot()
            if snapshot.get("authorized"):
                return {"state": "authorized", "reused": True, **snapshot}

            authorization = self._github_device_authorization
            if authorization is not None and time.time() < authorization.expires_at:
                return {"state": "pending", "reused": True, **authorization.public()}

            authorization = self.github_app.start_device_authorization()
            self._github_device_authorization = authorization
            self._github_device_last_poll = 0.0
            return {"state": "pending", "reused": False, **authorization.public()}

    def poll_github_device_authorization(self) -> dict:
        with self._github_device_lock:
            authorization = self._github_device_authorization
            if authorization is None:
                snapshot = self.github_app.snapshot()
                if not snapshot.get("authorized"):
                    return {"state": "idle"}
                # 已同意的首跑流程必须跨会话收口：device 对象可能因页面刷新
                # 丢失，或验证码已被另一窗口消费；此时快照仍指向“已授权未建仓”，
                # 继续现有幂等 bootstrap，而不是停在该状态。
                result = {"state": "authorized", **snapshot}
                result = {**result, **self._continue_first_run_setup()}
                if getattr(self, "remote_connection", None) is not None:
                    self.remote_connection.request_probe()
                return result
            if time.time() - self._github_device_last_poll < authorization.interval:
                return {"state": "pending", **authorization.public()}
            self._github_device_last_poll = time.time()
            result = self.github_app.poll_device_authorization(authorization)
            if result.get("state") in {"authorized", "expired", "error"}:
                self._github_device_authorization = None
            if result.get("state") == "authorized":
                # GH-UX-REWORK-1（MF-1 状态空窗）：授权确认即刻回包，初始化改由
                # 前端在收到 authorized 后自动发起（remote-connection/actions
                # bootstrap，异步反馈+分步阶段+自愈重试俱全）——不再在同一
                # poll POST 内联跑可能持续数分钟的 bootstrap，让 UI 停留在
                # 「等待 GitHub 授权确认」。setup_state=pending_bootstrap 是
                # 闭集中间态：仅表示「已授权、初始化待自动续跑」。
                result = {**result, "setup_state": "pending_bootstrap"}
                if getattr(self, "remote_connection", None) is not None:
                    self.remote_connection.request_probe()
            return {**authorization.public(), **result}

    def _bootstrap_with_selfheal(self) -> dict:
        """Run the idempotent first-run bootstrap with live stage progress.

        The stage channel (action_progress) is armed for the whole call so a
        concurrent connection snapshot always shows which closed-set step is
        running.  Closed-set transient failures (unreachable / rate limited /
        GitHub 5xx) are retried with bounded backoff — every bootstrap
        boundary is idempotent, so a retry never double-creates anything.
        Non-transient failures surface once, unchanged.
        """

        def report(stage: str) -> None:
            self._remote_action_stage_set("bootstrap", stage)

        attempts = 1 + len(BOOTSTRAP_SELFHEAL_RETRY_DELAYS_SECONDS)
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return self.github_app.bootstrap_student_repositories(progress=report)
            except Exception as exc:
                code = str(getattr(exc, "code", "") or "")
                last_error = exc
                if code not in BOOTSTRAP_TRANSIENT_ERROR_CODES or attempt >= attempts - 1:
                    raise
                time.sleep(BOOTSTRAP_SELFHEAL_RETRY_DELAYS_SECONDS[attempt])
        raise last_error  # pragma: no cover - loop always returns or raises

    def _continue_first_run_setup(self) -> dict:
        """Continue the explicitly consented first-run flow after authorization.

        Runs the existing idempotent bootstrap at most once per authorization
        completion.  A bootstrap failure must not read as an authorization
        failure: the closed-set continuation code is reported and the connection
        evidence then points at the correct next primary action.
        """
        try:
            if self.github_app.snapshot().get("bootstrapped"):
                return {}
            try:
                return dict(self._bootstrap_with_selfheal())
            finally:
                self._remote_action_stage_clear()
        except Exception as exc:
            return {
                "setup_state": "bootstrap_failed",
                "setup_error_code": str(getattr(exc, "code", "") or "operation_failed"),
            }

    def bootstrap_github(self) -> dict:
        # 分步进度（单元D）：建仓串行链（仓库→文档→完整性→环境→密钥）经
        # progress 回调逐段布防（GH-UX-REWORK-1 R2），收尾对账折叠为
        # 「正在确认连接状态」
        self._remote_action_stage_set("bootstrap", "creating_repositories")
        try:
            result = self._bootstrap_with_selfheal()
        finally:
            self._remote_action_stage_clear()
        self._reload_remote_coordinator()
        self._remote_action_stage_set("bootstrap", "verifying_connection")
        if getattr(self, "remote_connection", None) is not None:
            self.remote_connection.request_probe()
        self._remote_action_stage_clear()
        return result

    def verify_github_worker(self) -> dict:
        result = self.github_app.check_worker_integrity()
        if getattr(self, "remote_connection", None) is not None:
            self.remote_connection.request_probe()
        return result

    def repair_github_worker(self) -> dict:
        was_bootstrapped = bool(self.github_app.snapshot().get("bootstrapped"))
        result = self.github_app.repair_worker()
        if not result.get("trusted"):
            return result
        # Do not rotate the existing box/signing keys merely because code was
        # repaired.  A partially completed first bootstrap still finishes its
        # environment setup, but an established client keeps its key identity.
        snapshot = (
            self.github_app.snapshot()
            if was_bootstrapped
            else self.github_app.bootstrap_student_repositories()
        )
        if was_bootstrapped:
            self.github_app.sync_managed_mailbox_documents()
        self._reload_remote_coordinator()
        if getattr(self, "remote_connection", None) is not None:
            self.remote_connection.request_probe()
        return {**snapshot, "repaired": True}

    def disconnect_github(self) -> dict:
        active_remote = any(
            str(item.get("remote_state") or "") in {
                "created", "queued", "awaiting_payload", "running", "canceling",
                "artifact_ready", "downloading_result",
            }
            for item in self.task_store.list_remote_runs(limit=100)
        )
        if self.task_store.list_remote_token_leases() or active_remote:
            raise RuntimeError("仍有远程任务持有临时授权，不能断开 GitHub")
        with self._github_device_lock:
            self._github_device_authorization = None
            self._github_device_last_poll = 0.0
        try:
            self.github_app.delete_job_token()
        except Exception as exc:
            self.credentials.save_secret("github_job_token_cleanup_pending", "1")
            raise RuntimeError("临时 GitHub Secret 尚未确认删除，请先重试清理") from exc
        self.github_app.disconnect()
        self._reload_remote_coordinator()
        if getattr(self, "remote_connection", None) is not None:
            self.remote_connection.request_probe()
        return self.github_app.snapshot()

    def _load_remote_settings(self):
        """云端处理默认允许（CLOUD-CONSENT-AUTO-1 U1）：本地开关（remote_enabled）
        退出全部判定链——托管配置一律以启用态交给协调器，缺凭据/密钥由
        RemoteSettings.validate 如实报错（域外 coordinator.py 保持只读）。"""
        from src.remote.coordinator import RemoteSettings
        return replace(RemoteSettings.load(self.credentials), enabled=True)

    def _reload_remote_coordinator(self) -> None:
        from src.remote.coordinator import RemoteCoordinator
        settings = self._load_remote_settings()
        coordinator = None
        try:
            coordinator = RemoteCoordinator(settings, self.task_store, self.credentials)
        except RuntimeError:
            coordinator = None
        self.remote_settings = settings
        self.remote_coordinator = coordinator

    def _prepare_remote_coordinator(self):
        """入队门=连接就绪（CLOUD-CONSENT-AUTO-1 U1）：协调器建得起来即放行，
        与云端处理开关无关；连接/授权/密钥任一缺件仍按既有闭集拒绝。"""
        if getattr(self, "remote_connection", None) is not None:
            self.remote_connection.preflight(maximum_age_seconds=30.0)
        if self.github_app.snapshot().get("bootstrapped"):
            self._reload_remote_coordinator()
        if self.remote_coordinator is None:
            raise CloudSetupRequired("在线计算尚未完成 GitHub 授权")
        return self.remote_coordinator

    @contextmanager
    def _leased_remote_coordinator(self, task_id: str, *, workflow: str | None = None):
        """Refresh the transient token before constructing its API client.

        workflow 缺省=共享协调器的 process.yml 现状（字节不变）；N1-ROUTING
        传入路由结果（如 llm.yml）时以独立实例携带工作流——echo.yml 先例
        （start_remote_echo 的 replace 形态）同款，共享协调器与其设置零触碰，
        并发任务互不影响。等值请求不重建，零额外开销。
        """
        with self.github_app.job_token_lease(
            task_id=task_id, task_store=self.task_store
        ):
            coordinator = self._prepare_remote_coordinator()
            if workflow is not None and workflow != coordinator.settings.workflow:
                from src.remote.coordinator import RemoteCoordinator

                coordinator = RemoteCoordinator(
                    replace(coordinator.settings, workflow=workflow),
                    coordinator.task_store,
                    coordinator.credentials,
                    github=coordinator.github,
                )
            yield coordinator

    def _cloud_run_limit(self) -> int:
        raw = self.task_store.get_app_state(
            CLOUD_RUN_LIMIT_STATE_KEY, CLOUD_RUN_LIMIT_DEFAULT
        )
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return CLOUD_RUN_LIMIT_DEFAULT
        return max(1, min(CLOUD_RUN_LIMIT_MAX, value))

    @staticmethod
    def _cloud_queue_label(ahead: int) -> str:
        return f"前面还有 {ahead} 个任务在云端跑，这个已排入"

    @contextmanager
    def _cloud_run_slot(self, task_id: str, *, cancel_requested=None, on_wait=None):
        """One client-side in-flight slot for a cloud runner task.

        Subtitle, summary and question dispatches — manual or automation-fed —
        share this ceiling, so the client never pushes more runner jobs than
        the configured limit.  Extra tasks queue here with an honest label and
        stay cancelable instead of dispatching a doomed run.
        """
        from src.remote.coordinator import RemoteTaskPaused
        notified = False
        with self._cloud_run_condition:
            while self._cloud_runs_active >= self._cloud_run_limit():
                if not notified:
                    notified = True
                    if on_wait is not None:
                        on_wait(self._cloud_runs_active)
                if cancel_requested is not None and cancel_requested():
                    raise RemoteTaskPaused("cloud run canceled while queued")
                self._cloud_run_condition.wait(timeout=0.25)
            self._cloud_runs_active += 1
        try:
            yield
        finally:
            with self._cloud_run_condition:
                self._cloud_runs_active -= 1
                self._cloud_run_condition.notify_all()

    def start_search_index(self) -> dict:
        """Start the derived search worker when the local HTTP service starts."""
        with self._lock:
            if not self._search_index_started:
                self._search_index_started = True
                self.search_index.start()
        return self.search_index.status()

    def has_active_work(self) -> bool:
        """Return whether automatic UI shutdown must keep workers alive."""
        try:
            active_remote = self.task_store.has_active_remote_run(REMOTE_RUN_LIFECYCLE_STATES)
        except Exception:
            # A failed local-state read must not stop a recoverable remote run.
            active_remote = True
        return bool(
            self.task_store.count(states=("queued", "running", "pausing"))
            or self._active_subtitle_task_ids
            or self._active_summary_task_id
            or self._active_question_task_id
            or bool(self._remote_echo_thread and self._remote_echo_thread.is_alive())
            or active_remote
        )

    def _remote_configured(self) -> bool:
        app = self.github_app.snapshot()
        return bool(app.get("authorized") and app.get("bootstrapped")) or all(
            self.credentials.has_secret(name)
            for name in (
                "github_remote_token",
                "worker_box_public_key",
                "worker_signing_public_key",
            )
        )

    def remote_compute_snapshot(self) -> dict:
        connection = (
            self.remote_connection.snapshot()
            if getattr(self, "remote_connection", None) is not None
            else None
        )
        recent = self.task_store.list_remote_runs(limit=6)
        verified_run = next(
            (
                run for run in self.task_store.list_remote_runs(limit=1, workflow="echo.yml")
                if run.get("remote_state") == "imported"
            ),
            None,
        )
        with self._lock:
            echo = dict(self._remote_echo_status)
        channel = dict((connection or {}).get("channel_test") or {})
        # CLOUD-CONSENT-AUTO-1 U1：快照不再带 enabled/switch_locked 两个开关字段
        # ——云端处理默认允许，真值只剩连接面（configured 已配置 / verified 通道
        # 通过），前端不再有开关可开可关（消费面证据见结果文件 §5.2）。
        return {
            "configured": self._remote_configured(),
            "verified": channel.get("state") == "ready" if connection else bool(verified_run),
            "public_repo": self.remote_settings.public_repo,
            "private_repo": self.remote_settings.private_repo,
            "workflow": self.remote_settings.workflow,
            "ref": self.remote_settings.ref,
            "mode": "online-service",
            "local_functions": ["playback", "database", "search", "derived-content"],
            "remote_functions": ["subtitle", "ocr", "proofread", "summary", "chapters"],
            "student_service": True,
            "echo": echo,
            "last_verified_at": (verified_run or {}).get("imported_at"),
            "recent_runs": [
                {
                    "task_id": str(run.get("task_id") or ""),
                    "workflow": str(run.get("workflow") or ""),
                    "run_id": int(run.get("run_id") or 0),
                    "attempt": int(run.get("attempt") or 1),
                    "state": str(run.get("remote_state") or ""),
                    "updated_at": float(run.get("updated_at") or 0),
                }
                for run in recent
            ],
            "connection": connection,
        }

    def recover_remote_runs_on_startup(self) -> int:
        """Requeue only remote tasks with durable recovery material."""
        # 第四十二案：先核远端真值再落账。已完结成功的 run 走导入收口，
        # 而不是先被本地闸门重判一次；无法即时导入的交给结果核对者。
        self.reconcile_remote_results_on_startup()
        # AS2/U1：终态 failed 行补扫（加性）——51 的对账按远端行驱动且只看
        # 最近 REMOTE_RESULT_RECONCILE_MAX_RUNS 条，启动窗真值读取失败后也
        # 无人重排；这里按任务行把「有可核远端 run 的终态 failed 行」纳入
        # 同一条核真值链，判据零放宽。
        self._reconcile_terminal_failed_tasks_on_startup()
        # DEAD-TASK-PURGE A：上会话崩溃残留僵尸的启动落账（取证在 __init__，
        # 判据与互斥说明见 _reconcile_zombie_tasks_on_startup）。
        self._reconcile_zombie_tasks_on_startup()
        recovered = 0
        for task in self.task_store.list_tasks(states=("paused",)):
            task_id = str(task.get("task_id") or "")
            if dict(task.get("payload") or {}).get(USER_PAUSE_INTENT_KEY) is True:
                continue
            remote = self.task_store.get_remote_run(task_id) or {}
            state = str(remote.get("remote_state") or "")
            if state not in REMOTE_RUN_STARTUP_RECOVERY_STATES:
                continue
            if not self.credentials.has_secret(f"remote_result_private:{task_id}"):
                self.task_store.update_task(
                    task_id,
                    error="remote_recovery_material_unavailable",
                )
                continue
            resumed = self.task_store.resume_task(task_id, clear_global_pause=True)
            if not resumed or resumed.get("state") != "queued":
                continue
            with self._lock:
                self._queue_persisted_task(resumed)
                if resumed.get("kind") == "subtitle":
                    self._ensure_subtitle_worker()
                elif resumed.get("kind") == "summary":
                    self._summary_cancel.clear()
                    self._ensure_summary_worker()
            recovered += 1
        return recovered

    # ------------------------------------------------------------------
    # 第四十二案（LOST-RESULT-RECOVERY-1）：远端真值核对 + 结果追补导入
    #
    # 一条 GitHub run 的生命周期跨进程：客户端退出、崩溃、本地闸门失败都改变
    # 不了远端事实。本地因此只有两种合法动作——按真值收敛，或继续等真值。
    # 下面这组方法负责「等」与「收敛」，且不放宽任何既有信任判据：追补导入走
    # 的仍是协调者的原生通道（artifact 名绑定 task_id、一次性密钥解密、
    # Worker 签名验签、input_hash 绑定），只是不再要求那条只在「下发新任务」
    # 时才需要的媒体授权闸门。
    # ------------------------------------------------------------------

    @staticmethod
    def _remote_result_secret_name(task_id: str) -> str:
        return f"remote_result_private:{str(task_id)}"

    def _remote_run_awaiting_truth(self, task_id: str) -> dict[str, Any] | None:
        """Return the durable remote row when a real result may still be pending.

        Three conditions, all necessary: a recorded run id (the run outlives
        this process), a remote state that is neither imported nor canceled,
        and the one-time result key that alone makes decryption possible.  An
        explicit user pause keeps its 38-case meaning — no automatic
        re-attachment — so it is never a reconcile candidate either.
        """
        task_id = str(task_id or "")
        if not task_id:
            return None
        task = self.task_store.get_task(task_id) or {}
        if not task or str(task.get("state") or "") == "canceled":
            return None
        if dict(task.get("payload") or {}).get(USER_PAUSE_INTENT_KEY) is True:
            return None
        run = self.task_store.get_remote_run(task_id) or {}
        if not int(run.get("run_id") or 0):
            return None
        if str(run.get("remote_state") or "") not in REMOTE_RESULT_RECONCILE_STATES:
            return None
        if not self.credentials.has_secret(self._remote_result_secret_name(task_id)):
            return None
        return run

    def _remote_run_truth(self, run: dict[str, Any]) -> dict[str, str] | None:
        """Read the raw GitHub truth (status/conclusion); ``None`` if unreadable.

        Classification only: the import itself keeps every artifact-name,
        signature and input-hash check, so a raw read here never relaxes the
        trust boundary — it only decides whether to keep waiting.
        """
        coordinator = getattr(self, "remote_coordinator", None)
        if coordinator is None:
            return None
        try:
            payload = coordinator.github.get_run(
                coordinator.settings.public_repo, int(run.get("run_id") or 0)
            )
        except Exception:
            return None
        return {
            "status": str(payload.get("status") or ""),
            "conclusion": str(payload.get("conclusion") or ""),
        }

    def reconcile_remote_results_on_startup(self) -> dict[str, int]:
        """Converge every recoverable remote run against GitHub truth first.

        Runs before the requeue sweep so a finished result is never lost to a
        local gate: success → import the verified artifact into the course,
        failure → record it honestly, still in flight → keep waiting.  Every
        failure mode is silent and bounded; an unreachable GitHub must never
        block startup.
        """
        outcomes: dict[str, int] = {}
        if getattr(self, "remote_coordinator", None) is None:
            return outcomes
        task_ids = [
            str(run.get("task_id") or "")
            for run in self.task_store.list_remote_runs(
                limit=REMOTE_RESULT_RECONCILE_MAX_RUNS
            )
        ]
        for task_id in task_ids:
            if not task_id:
                continue
            try:
                outcome = self._reconcile_remote_task_result(task_id)
            except Exception:
                outcome = "retry"
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        return outcomes

    def _reconcile_terminal_failed_tasks_on_startup(self) -> dict[str, int]:
        """AS2/U1：终态 failed 行的启动核真值回流（加性扩展，复用 51 原链）。

        只补 51 对账的两处残余盲区：远端行被挤出近
        ``REMOTE_RESULT_RECONCILE_MAX_RUNS`` 条窗口的存量 failed 行，以及
        启动窗真值读取失败（retry）后无人重排的行。每行仍先过
        ``_remote_run_awaiting_truth`` 的全部条件（run_id、remote_state 白
        名单、一次性密钥；取消与显式暂停原样排除），再走
        ``_reconcile_remote_task_result`` 的三分支收敛与 7 天窗——判据零放
        宽。success→验签导入转成功；远端实败保持 failed 如实记账；真值未定
        交给 51 的有界核对者。全链幂等：导入成功的行转 completed，不再进
        本扫。
        """
        outcomes: dict[str, int] = {}
        if getattr(self, "remote_coordinator", None) is None:
            return outcomes
        # CLIENT-STATE-R1：存量 failed 行可能远超 90 天（正是本扫的收口
        # 对象）——显式关终态保留窗，核真语义不变。
        for task in self.task_store.list_tasks(
            states=("failed",), limit=200, terminal_retention_days=None,
        ):
            task_id = str(task.get("task_id") or "")
            if not task_id:
                continue
            if self._remote_run_awaiting_truth(task_id) is None:
                continue
            try:
                outcome = self._reconcile_remote_task_result(task_id)
            except Exception:
                outcome = "retry"
            if outcome == "retry":
                # 启动窗抖动不等于终局：交给既有 45s 有界核对者自行收敛
                self._schedule_remote_result_watch(task_id)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        return outcomes

    def _collect_startup_zombie_task_ids(self) -> list[str]:
        """DEAD-TASK-PURGE A（取证）：识别上会话崩溃残留的在途僵尸。

        崩溃（没走到 checkpoint_for_shutdown）会在库里遗留 queued/running/
        pausing 行——本地进程已死，状态却仍宣称在途。判据三条同时成立：
        ①在途态；②updated_at 距此当下 >5 分钟无任何进展（快速重启窗口内
        不动，交给既有恢复扫描；progress/checkpoint 写入都会刷新
        updated_at，真活跃到不了这条线）；③远端无活跃 run（remote_state
        在 REMOTE_RUN_LIFECYCLE_STATES 白名单外）——远端仍活的行由核真链
        与 paused 恢复扫描收敛，绝不在这里误杀。与 recover_for_startup 的
        paused 恢复扫描状态条件互斥：僵尸行无活跃 remote run，恢复扫描永
        不相中。只读取证，不落账。
        """
        now = time.time()
        zombie_ids: list[str] = []
        for task in self.task_store.list_tasks(
            states=("queued", "running", "pausing"), limit=500,
        ):
            updated_at = float(task.get("updated_at") or 0.0)
            if updated_at and now - updated_at <= ZOMBIE_STARTUP_STALE_SECONDS:
                continue
            remote = self.task_store.get_remote_run(str(task.get("task_id") or "")) or {}
            if str(remote.get("remote_state") or "") in REMOTE_RUN_LIFECYCLE_STATES:
                continue
            zombie_ids.append(str(task.get("task_id") or ""))
        return zombie_ids

    def _reconcile_zombie_tasks_on_startup(self) -> int:
        """DEAD-TASK-PURGE A（落账）：把开工取证的上会话崩溃残留僵尸标记为
        failed（closed-set 原因 zombie_session_cleanup），此后自然落入 90 天
        终态保留窗，用户面「清理全部失败记录 / 逐条删除」皆可收口——顽固
        残留的启动根治。幂等：已在终态（或已不存在）的行原样跳过，重复调
        用零改写。用户显式暂停（USER_PAUSE_INTENT）与全局暂停的合法待恢复
        行不在取证集里（它们本就是 paused，不是崩溃残留的在途态）。
        """
        cleared = 0
        for task_id in list(getattr(self, "_startup_zombie_task_ids", None) or []):
            task = self.task_store.get_task(task_id) or {}
            if str(task.get("state") or "") in TERMINAL_STATES:
                continue
            marked = self.task_store.mark_terminal(
                task_id, "failed", error=ZOMBIE_SESSION_CLEANUP_REASON,
            )
            if marked is not None and str(marked.get("state") or "") == "failed":
                cleared += 1
        return cleared

    def import_remote_task_result(self, task_id: str) -> dict[str, Any]:
        """AS2/U2：学生在失败/暂停卡上点「导入远端结果」——显式核真值+导入。

        与启动核真值完全同通道：51 的真值门、原生验签导入
        （allow_dispatch=False）、7 天窗、一次性密钥判据逐字复用，零放宽。
        显式暂停行被 ``_remote_run_awaiting_truth`` 原样排除（51 合同）；
        无可核 run、超窗、真值读不到分别给闭集诚实码；绝不自动重跑。
        """
        task_id = str(task_id or "")
        task = self.task_store.get_task(task_id)
        if task is None:
            raise KeyError("task not found")
        if str(task.get("state") or "") not in {"failed", "paused"}:
            raise ValueError("task_action_invalid")
        run = self._remote_run_awaiting_truth(task_id)
        if run is None:
            raise ValueError("remote_import_unavailable")
        stamp = float(run.get("dispatched_at") or run.get("updated_at") or 0.0)
        if stamp and time.time() - stamp > REMOTE_RESULT_IMPORT_WINDOW_SECONDS:
            raise ValueError("remote_import_expired")
        outcome = self._reconcile_remote_task_result(task_id)
        if outcome == "retry":
            # 真值此刻读不到：区分「连不上云端」与「已有监督者在导入」
            if self._remote_run_truth(run) is None:
                raise ValueError("network_unavailable")
            raise ValueError("operation_already_running")
        if outcome == "expired":
            raise ValueError("remote_import_expired")
        if outcome in {"unrecoverable", "unsupported"}:
            raise ValueError("remote_import_unavailable")
        return {
            "task": self.task_store.get_task(task_id),
            "global_paused": bool(self.task_store.global_paused),
            "accepted_action": "import_result",
            "outcome": outcome,
        }

    def _reconcile_remote_task_result(self, task_id: str) -> str:
        """Classify one task's remote truth and converge whatever is concluded.

        Closed-set outcomes: ``imported`` / ``failed`` / ``reopened`` /
        ``running`` / ``deferred`` / ``expired`` / ``unrecoverable`` /
        ``retry``.  Only a concluded remote failure ever terminalizes a task
        here — never a client-side condition.
        """
        task = self.task_store.get_task(str(task_id))
        if task is None:
            return "unrecoverable"
        run = self._remote_run_awaiting_truth(str(task_id))
        if run is None:
            return "unrecoverable"
        stamp = float(run.get("dispatched_at") or run.get("updated_at") or 0.0)
        if stamp and time.time() - stamp > REMOTE_RESULT_IMPORT_WINDOW_SECONDS:
            # 超期记档：陈旧 run 不再起线程、不再导入（边界=一个导入窗）
            print(
                f"[FudanCourseLens] Remote run outside the import window: {task_id}",
                flush=True,
            )
            return "expired"
        truth = self._remote_run_truth(run)
        if truth is None:
            return "retry"
        if truth["status"] != "completed":
            # 远端仍在飞：本地终态必须让位给真值，否则结果无处可落
            return "reopened" if self._reopen_remote_task_for_truth(task, run) else "running"
        conclusion = truth["conclusion"]
        if conclusion in {"cancelled", "canceled"}:
            # 取消语义归既有 worker 通道，本函数不新造
            return "unrecoverable"
        if conclusion != "success":
            return self._fail_concluded_remote_result(task, run, conclusion)
        return self._import_concluded_remote_result(task, run)

    def _reopen_remote_task_for_truth(self, task: dict[str, Any], run: dict[str, Any]) -> bool:
        """Return a failed-but-live task to the recoverable lane.

        Only the local record moves: the run keeps going and the existing
        recovery chain (or the result watcher) owns it from here.  A task the
        user canceled is never reopened.
        """
        task_id = str(task.get("task_id") or "")
        if str(task.get("state") or "") != "failed":
            return False
        label = REMOTE_TRUTH_PENDING_LABEL
        self.task_store.update_task(
            task_id,
            state="paused",
            resume_requested=0,
            error="",
            progress={
                **dict(task.get("progress") or {}),
                "stage": "paused",
                "percent": None,
                "indeterminate": True,
                "label": label,
                "observed_at": time.time(),
            },
        )
        self._announce_remote_truth_pending(task, label)
        self._schedule_remote_result_watch(task_id)
        print(
            f"[FudanCourseLens] Remote task reopened for truth: {task_id}",
            flush=True,
        )
        return True

    def _announce_remote_truth_pending(self, task: dict[str, Any], label: str) -> None:
        """Say "the cloud result is still undecided" on the visible surface."""
        kind = str(task.get("kind") or "")
        sub_id = str(task.get("sub_id") or "")
        if kind == "subtitle" and sub_id:
            self.catalog_repository.update_lecture_fields(
                sub_id,
                subtitle_status="paused",
                subtitle_error="",
                subtitle_progress_label=label,
            )
        elif kind == "summary" and sub_id:
            self.catalog_repository.update_lecture_fields(
                sub_id,
                summary_status="paused",
                summary_error="",
                summary_progress_label=label,
            )
        elif kind == "question":
            bookmark_id = str(dict(task.get("payload") or {}).get("bookmark_id") or "")
            if bookmark_id:
                try:
                    update_bookmark_task(
                        self.learning_store.path,
                        bookmark_id=bookmark_id,
                        task_id=str(task.get("task_id") or ""),
                        explanation_state="paused",
                    )
                except Exception:
                    # 可见面诚实化是尽力而为，绝不能让恢复路径失败
                    pass

    def _remote_result_importer(self, task: dict[str, Any]):
        """Return the verified-result callback for one task kind, or ``None``.

        The callbacks are the very ones a live run uses, so a recovered import
        lands in the course exactly like an unimpeded one.
        """
        kind = str(task.get("kind") or "")
        course_id = str(task.get("course_id") or "")
        sub_id = str(task.get("sub_id") or "")
        if kind == "subtitle":
            return lambda result: self._import_remote_subtitle(
                course_id, sub_id, result,
                task_id=str(task.get("task_id") or ""),
            )
        if kind == "summary":
            return lambda result: self._import_remote_summary_result(
                course_id, sub_id, result,
                task_id=str(task.get("task_id") or ""),
            )
        return None

    def _import_concluded_remote_result(self, task: dict[str, Any], run: dict[str, Any]) -> str:
        """Import one concluded-success run through the existing verified path.

        The coordinator is driven directly — no job-token lease, no dispatch,
        no connection preflight — because a concluded run needs no media
        authorization, and because those gates are exactly what stranded a
        finished result.  Dispatch stays disabled for this path.
        """
        coordinator = getattr(self, "remote_coordinator", None)
        if coordinator is None:
            return "retry"
        task_id = str(task.get("task_id") or "")
        import_result = self._remote_result_importer(task)
        if import_result is None:
            # 无成品导入面的 kind：交回既有恢复链接手，绝不终态化
            return "reopened" if self._reopen_remote_task_for_truth(task, run) else "unsupported"
        try:
            coordinator.execute(
                task_id=task_id,
                build_job=lambda _public_key: {},
                import_result=import_result,
                cancel_requested=lambda: False,
                progress=lambda *_args, **_kwargs: None,
                allow_dispatch=False,
            )
        except Exception as exc:
            if str(getattr(exc, "code", "") or "") == "remote_supervisor_busy":
                # 在任监督者正在处理它：让位，不重判
                return "retry"
            latest = self.task_store.get_remote_run(task_id) or {}
            latest_state = str(latest.get("remote_state") or "")
            if latest_state == "imported":
                return self._complete_imported_remote_task(task)
            if latest_state == "failed":
                # 协调者把结论落成失败了：那是远端自身的失败，如实上报
                return self._fail_concluded_remote_result(task, latest, "failure")
            if self._remote_import_attempts(task_id) > REMOTE_RESULT_IMPORT_MAX_ATTEMPTS:
                # 结果确实存在，是本地导入反复失败：如实上报，不再无限重试
                return self._fail_remote_result_import(task, exc)
            return "deferred"
        return self._complete_imported_remote_task(task)

    def _remote_import_attempts(self, task_id: str) -> int:
        """Count追补导入 attempts for one task in this process (bounded retries)."""
        with self._lock:
            counters = getattr(self, "_remote_result_import_counts", None)
            if counters is None:
                counters = {}
                self._remote_result_import_counts = counters
            counters[task_id] = int(counters.get(task_id) or 0) + 1
            return counters[task_id]

    def _fail_remote_result_import(self, task: dict[str, Any], exc: Exception) -> str:
        """Bounded retries spent: report the local import failure as it is."""
        task_id = str(task.get("task_id") or "")
        kind = str(task.get("kind") or "")
        sub_id = str(task.get("sub_id") or "")
        message = _task_failure_message(exc)
        if kind == "subtitle" and sub_id:
            self.catalog_repository.update_lecture_fields(
                sub_id,
                subtitle_status="failed",
                subtitle_step="Failed",
                subtitle_step_percent=0.0,
                subtitle_error=message,
                subtitle_progress_label="字幕结果导入失败，可重新点击生成字幕",
            )
        elif kind == "summary" and sub_id:
            for artifact_kind in ("timestamp_summary", "review_views"):
                self.learning_store.interrupt_ai_artifacts(sub_id, artifact_kind, message)
            self.catalog_repository.update_lecture_fields(
                sub_id,
                summary_status="failed",
                summary_progress_label="AI 课程笔记结果导入失败，可重新生成",
                summary_error=message,
            )
        self.task_store.mark_terminal(task_id, "failed", error=message)
        print(
            f"[FudanCourseLens] Remote result import failed: {task_id} ({message})",
            flush=True,
        )
        return "failed"

    def _complete_imported_remote_task(self, task: dict[str, Any]) -> str:
        """Close a task whose verified remote result just landed in the course."""
        task_id = str(task.get("task_id") or "")
        kind = str(task.get("kind") or "")
        sub_id = str(task.get("sub_id") or "")
        latest = self.task_store.get_task(task_id) or task
        self.task_store.update_task(
            task_id,
            progress={
                **dict(latest.get("progress") or {}),
                "stage": "done",
                "percent": 100.0,
                "indeterminate": False,
                "label": "字幕已生成" if kind == "subtitle" else "AI 时间戳总结已完成",
                "observed_at": time.time(),
            },
        )
        self.task_store.mark_terminal(task_id, "completed")
        if sub_id:
            self._request_search_refresh([sub_id])
            if kind == "summary":
                self.catalog_repository.update_lecture_fields(
                    sub_id,
                    summary_status="done",
                    summary_percent=100.0,
                    summary_progress_label="AI 时间戳总结已完成",
                    summary_error="",
                )
        print(
            f"[FudanCourseLens] Remote result imported without a new run: {task_id} ({kind})",
            flush=True,
        )
        return "imported"

    def _fail_concluded_remote_result(
        self, task: dict[str, Any], run: dict[str, Any], conclusion: str
    ) -> str:
        """Record a remote conclusion of failure exactly as it is."""
        task_id = str(task.get("task_id") or "")
        kind = str(task.get("kind") or "")
        sub_id = str(task.get("sub_id") or "")
        attempt = max(1, int(run.get("attempt") or 1))
        signed = str(
            (self.task_store.get_remote_attempt(task_id, attempt) or {}).get("error_code") or ""
        ).strip()
        code = signed if signed in _REMOTE_WORKER_GUIDANCE_CODES else REMOTE_RESULT_FAILURE_CODE
        settings = getattr(self, "remote_settings", None)
        repository = str(run.get("repository") or getattr(settings, "public_repo", "") or "")
        workflow = str(run.get("workflow") or "")
        if repository and workflow:
            # 行写入要求 repo+workflow 齐全；缺一（异常行）只收敛任务，不让恢复
            # 路径炸在非关键字段上。
            self.task_store.upsert_remote_run(
                task_id,
                repository=repository,
                workflow=workflow,
                run_id=int(run.get("run_id") or 0),
                attempt=attempt,
                issue_number=int(run.get("issue_number") or 0) or None,
                remote_state="failed",
                finished_at=time.time(),
                last_error=f"remote run concluded {conclusion}"[:300],
            )
        if kind == "subtitle" and sub_id:
            self.catalog_repository.update_lecture_fields(
                sub_id,
                subtitle_status="failed",
                subtitle_step="Failed",
                subtitle_step_percent=0.0,
                subtitle_error=code,
                subtitle_progress_label="字幕生成失败，可重新点击生成字幕",
            )
        elif kind == "summary" and sub_id:
            for artifact_kind in ("timestamp_summary", "review_views"):
                self.learning_store.interrupt_ai_artifacts(sub_id, artifact_kind, code)
            self.catalog_repository.update_lecture_fields(
                sub_id,
                summary_status="failed",
                summary_progress_label="AI 课程笔记生成失败，可重新生成",
                summary_error=code,
            )
        self.task_store.mark_terminal(task_id, "failed", error=code)
        print(
            f"[FudanCourseLens] Remote run concluded {conclusion}: {task_id} ({code})",
            flush=True,
        )
        return "failed"

    def _defer_remote_task_failure(self, task_id: str, *, kind: str, sub_id: str) -> bool:
        """Keep a remote task recoverable while the remote truth is still open.

        A client-side gate failure — untrusted Worker tree, stale connection
        evidence, withheld media authorization — is evaluated before the
        supervisor ever reads the run, so it says nothing about the run's
        outcome.  Terminalizing there is what stranded a finished result, so
        while a durable run and its one-time key exist the honest local state
        is "awaiting remote truth".
        """
        run = self._remote_run_awaiting_truth(task_id)
        if run is None:
            return False
        if str(run.get("remote_state") or "") not in REMOTE_RUN_RECOVERABLE_STATES:
            # 协调者已把远端结论落账（failed/imported）：那不是「真值未定」，
            # 照既有兜底路径如实终态化。
            return False
        task = self.task_store.get_task(str(task_id)) or {}
        label = REMOTE_TRUTH_PENDING_LABEL
        self.task_store.update_task(
            task_id,
            state="paused",
            resume_requested=0,
            error="",
            progress={
                **dict(task.get("progress") or {}),
                "stage": "paused",
                "percent": None,
                "indeterminate": True,
                "label": label,
                "observed_at": time.time(),
            },
        )
        self._announce_remote_truth_pending(
            {**task, "task_id": str(task_id), "kind": kind, "sub_id": sub_id}, label
        )
        self._schedule_remote_result_watch(str(task_id))
        print(
            f"[FudanCourseLens] {kind} task {task_id}: kept recoverable, awaiting remote truth",
            flush=True,
        )
        return True

    def _schedule_remote_result_watch(self, task_id: str) -> None:
        """Follow one open remote run until its truth lands (bounded, single-flight)."""
        task_id = str(task_id or "")
        if not task_id:
            return
        with self._lock:
            watches = getattr(self, "_remote_result_watches", None)
            if watches is None:
                watches = {}
                self._remote_result_watches = watches
            running = watches.get(task_id)
            if running is not None and running.is_alive():
                return
            thread = threading.Thread(
                target=self._remote_result_watch_loop,
                args=(task_id,),
                name=f"remote-result-watch-{task_id[:8]}",
                daemon=True,
            )
            watches[task_id] = thread
        thread.start()

    def _remote_result_watch_loop(self, task_id: str) -> None:
        """Poll one task's remote truth until it is decided.

        The loop never blocks on the run itself: each cycle reads status and
        returns, so an open run costs one tiny request per interval and never
        occupies a worker.  Bounded by the same import window, and it stops
        with the rest of the background work when the client closes.
        """
        stop = getattr(self, "_automation_stop", None)
        if getattr(self, "remote_coordinator", None) is None:
            # 没有协调者就无从观察真值；下次启动的对账会接手
            return
        deadline = time.time() + REMOTE_RESULT_IMPORT_WINDOW_SECONDS
        while True:
            try:
                outcome = self._reconcile_remote_task_result(task_id)
            except Exception:
                outcome = "retry"
            if outcome not in {"running", "retry", "deferred"}:
                return
            if time.time() >= deadline:
                return
            if stop is None:
                time.sleep(REMOTE_RESULT_WATCH_INTERVAL_SECONDS)
            elif stop.wait(REMOTE_RESULT_WATCH_INTERVAL_SECONDS):
                return

    def automation_snapshot(self, *, refresh: bool = False) -> dict:
        return self.automation.snapshot(refresh=refresh)

    def _automation_monitor_loop(self) -> None:
        while not self._automation_stop.is_set():
            try:
                self.automation.reconcile(force=True)
            except Exception:
                # The public snapshot remains unknown/stale; exception text may
                # contain external request details and must never enter logs.
                pass
            try:
                self._capture_queue_observations()
            except Exception:
                # Queue evidence is additive; any failure must never disturb
                # the automation monitor cadence.
                pass
            if self._automation_stop.wait(60):
                break

    # Trusted managed-Worker workflows whose sanitized run views feed the
    # queue-timing history.  Membership is closed; unknown workflows are
    # never polled and never sampled.  llm.yml joins once N1-ROUTING made it
    # a live dispatch target (pure observation alignment; dispatch routing
    # itself is unchanged).
    QUEUE_OBSERVATION_WORKFLOWS = ("process.yml", "llm.yml", "echo.yml", "cloud-verify.yml", "cloud-daily.yml")

    def _capture_queue_observations(self) -> int:
        """Record trusted Worker queue waits from sanitized workflow evidence.

        Each monitor cycle lists the recent runs of the closed managed
        workflow set and converts completed, head-pinned runs into
        ``queued_seconds`` samples (created_at -> run_started_at) stored under
        the hierarchical queue profiles.  Run ids already recorded are never
        sampled twice, so retries cannot duplicate observations.  Returns the
        number of newly recorded samples; every failure mode is silent and
        bounded because evidence capture must never break the monitor.
        """
        expected = str(
            getattr(getattr(self, "remote_settings", None), "expected_worker_commit", "") or ""
        ).strip().lower()
        if len(expected) != 40:
            return 0
        credentials = getattr(self, "credentials", None)
        if credentials is None or not credentials.has_secret("github_worker_repo"):
            return 0
        github = getattr(self, "github_app", None)
        if github is None:
            return 0
        from src.remote.protocol import PROTOCOL_VERSION

        recorded_state = dict(self.task_store.get_app_state("queue_observation_runs", {}) or {})
        if recorded_state.get("schema") not in (None, "", QUEUE_OBSERVATION_SCHEMA):
            recorded_state = {}
        seen = {int(value) for value in (recorded_state.get("run_ids") or []) if int(value or 0) > 0}
        recorded = 0
        for workflow in self.QUEUE_OBSERVATION_WORKFLOWS:
            try:
                runs = github.list_workflow_runs(workflow, limit=20)
            except Exception:
                continue
            for run in runs:
                sample = trusted_queue_observation(
                    run, workflow=workflow, pipeline=PROTOCOL_VERSION, expected_head=expected,
                )
                if sample is None:
                    continue
                run_id = int(sample["run_id"])
                if run_id in seen:
                    continue
                if self.task_store.record_queue_sample(
                    queue_profile_keys(workflow, pipeline=PROTOCOL_VERSION),
                    sample["queued_seconds"],
                ):
                    seen.add(run_id)
                    recorded += 1
        if recorded:
            # Bounded dedupe ledger: keep the newest run ids only.
            kept = sorted(seen)[-400:]
            self.task_store.set_app_state(
                "queue_observation_runs",
                {"schema": QUEUE_OBSERVATION_SCHEMA, "run_ids": kept},
            )
        return recorded

    def update_automation_config(self, body: dict) -> dict:
        return self.automation.update_config(body)

    def upload_automation_secrets(self, body: dict) -> dict:
        return self.automation.upload_cloud_secrets(body)

    def automation_action(self, action: str, *, operation_id: str) -> dict:
        return self.automation.action(action, operation_id=operation_id)

    def _automation_verified_catalog(self) -> dict:
        """Backend verified playable-lecture catalog for selection baselines.

        Bounded opaque identifiers only: course_id -> sorted playable sub_ids
        from the locally persisted verified catalog (authorization_state ==
        'verified'); no titles, payloads, URLs, or history leave this shape.
        """
        verified = {
            str(item.get("course_id") or ""): item
            for item in self.catalog_repository.courses()
            if str(item.get("authorization_state") or "") == "verified"
            and str(item.get("course_id") or "").strip()
        }
        catalog: dict[str, list[str]] = {}
        for course_id in verified:
            playable = sorted({
                str(row.get("sub_id") or "").strip()
                for row in self.catalog_repository.lectures_for_course(course_id)
                if row.get("has_playback") and str(row.get("sub_id") or "").strip()
            })
            catalog[course_id] = playable
        return catalog

    @staticmethod
    def _courseware_plan_digest(plan: dict) -> str:
        return hashlib.sha256(json.dumps(
            plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    def _verify_courseware_plan(self, result: dict, outputs: dict, course_id: str, sub_id: str) -> tuple:
        """Validate the imported courseware_plan binding and digest.

        A plan bound to another course/lecture or another pipeline version,
        or whose digest does not match the worker's committed digest, rejects
        the whole import (fail closed, retryable) instead of queueing work.
        """
        plan = outputs.get("courseware_plan")
        if not isinstance(plan, dict):
            raise ValueError("cloud result courseware plan is missing or invalid")
        if str(plan.get("schema") or "") != "courseware_plan.v1":
            raise ValueError("cloud result courseware plan schema is unsupported")
        if str(plan.get("course_id") or "") != course_id or str(plan.get("sub_id") or "") != sub_id:
            raise ValueError("cloud courseware plan is bound to another course or lecture")
        from src.runtime.automation import CLOUD_PROTOCOL_VERSION
        if str(plan.get("pipeline") or "") != str(CLOUD_PROTOCOL_VERSION):
            raise ValueError("cloud courseware plan comes from another pipeline version")
        digest = self._courseware_plan_digest(plan)
        if digest != str(outputs.get("courseware_plan_digest") or ""):
            raise ValueError("cloud courseware plan digest mismatch")
        return plan, digest

    def _enqueue_plan_courseware(self, course_id: str, sub_id: str, plan: dict, digest: str) -> None:
        """Queue or reuse one local courseware_pdf task carrying the plan.

        Import success never depends on the PDF: an enqueue failure is
        recorded as bounded pending state for the next import instead of
        rolling back the already-persisted learning output.  An existing
        valid PDF artifact stays untouched.
        """
        try:
            status = self.courseware_pdf_status(sub_id)
            artifact = dict(status.get("artifact") or {})
            if artifact.get("ready"):
                return
            self.enqueue_courseware_pdf(
                course_id, sub_id, plan=plan, plan_digest=digest,
            )
        except Exception as exc:
            pending = dict(
                self.task_store.get_app_state("automation_courseware_pending", {}) or {}
            )
            pending[sub_id] = {
                "course_id": str(course_id),
                "sub_id": str(sub_id),
                "error_code": str(getattr(exc, "code", "") or "courseware_enqueue_failed"),
                "observed_at": time.time(),
            }
            # Bounded: keep only the newest records when lectures pile up.
            if len(pending) > 50:
                pending = dict(
                    sorted(pending.items(), key=lambda item: float(
                        (item[1] or {}).get("observed_at") or 0,
                    ))[-50:]
                )
            self.task_store.set_app_state("automation_courseware_pending", pending)

    def _import_automation_result(self, result: dict) -> None:
        outputs = dict(result.get("outputs") or {})
        catalog = dict(outputs.get("cloud_catalog") or {})
        course_id = str(catalog.get("course_id") or "").strip()
        lecture = dict(catalog.get("lecture") or {})
        sub_id = str(lecture.get("sub_id") or "").strip()
        if not course_id or not sub_id:
            raise ValueError("cloud catalog result is incomplete")
        self.catalog_repository.upsert_course(
            course_id,
            str(catalog.get("title") or course_id),
            str(catalog.get("teacher") or ""),
            term=str(catalog.get("term") or ""),
            department=str(catalog.get("department") or ""),
            authorization_state="verified",
        )
        self.catalog_repository.upsert_lecture(course_id, lecture)
        subtitle = dict(outputs.get("subtitle") or {})
        if subtitle:
            # AS6 相邻缺陷修复：此处原以 4 个位置参数调用（course_id, sub_id,
            # mode, result），而 _import_remote_subtitle 只收 3 参——automation
            # 结果带字幕载荷时导入必 TypeError。automation 无任务行，不传 task_id。
            self._import_remote_subtitle(course_id, sub_id, result)
        summary = dict(outputs.get("summary") or {})
        chapters = list(outputs.get("chapters") or summary.get("chapters") or [])
        markdown = str(summary.get("markdown") or "").strip()
        ppt_pages = list(outputs.get("ppt_pages") or [])
        if markdown or chapters or ppt_pages:
            self.learning_store.import_remote_summary(
                course_id=course_id,
                sub_id=sub_id,
                input_hash=str(result.get("input_hash") or ""),
                model=str(summary.get("model") or "deepseek-flash"),
                markdown=markdown,
                chapters=chapters,
                ppt_pages=ppt_pages,
                metrics=dict(result.get("metrics") or {}),
                key_takeaways=[str(item or "") for item in (summary.get("key_takeaways") or [])],
                # RR-ANCHORFE-1：takeaway 时间戳锚原样透传（归一化与对齐在 store 收口）
                takeaway_anchors=list(summary.get("takeaway_anchors") or []),
                # P2-CONTRACT-1 ④：automation 路径与任务漏斗同权（守门在 store）。
                review_views=summary.get("review_views"),
            )
            self._export_summary_markdown(course_id, sub_id, markdown=markdown, chapters=chapters)
            self._run_assessment_radar(
                course_id, sub_id, markdown=markdown,
                llm_events=list(summary.get("assessment_events") or []),
            )
            # RR-P6MEM-1 反哺：automation 导入体同样过本地一致性检查（此链无
            # 记忆注入数实报，不加可见标注——宁缺毋滥）。
            self._memory_feedback_sink(course_id, markdown, source="summary_automation", sub_id=sub_id)
            # THINK-LADDER-1 自动抽检：总结落库后同内容至多自动一次（幂等+
            # 预算门+try/except 收口，绝不挡导入）。
            self._auto_quality_judge_after_summary(sub_id)
        # Additive Lecture IR import: deterministic evidence view, no model call.
        self.learning_store.import_lecture_ir(
            course_id=course_id,
            sub_id=sub_id,
            input_hash=str(result.get("input_hash") or ""),
            view=outputs.get("lecture_ir") if isinstance(outputs.get("lecture_ir"), dict) else None,
            metrics=dict(result.get("metrics") or {}),
        )
        # v3 hybrid courseware: the verified plan rides inside the encrypted
        # result.  Binding/digest validation happens before the local PDF
        # task is queued, and a plan-driven enqueue can never fail the import.
        if isinstance(outputs.get("courseware_plan"), dict):
            plan, digest = self._verify_courseware_plan(result, outputs, course_id, sub_id)
            self._enqueue_plan_courseware(course_id, sub_id, plan, digest)
        metrics = dict(result.get("metrics") or {})
        elapsed = float(metrics.get("elapsed_seconds") or 0)
        tokens = int(metrics.get("deepseek_tokens") or 0)
        budget_date = _shanghai_now().date().isoformat()
        cloud_state = dict(
            self.task_store.get_app_state("automation_last_cloud_state", {}) or {}
        )
        if (
            (subtitle or summary or chapters or ppt_pages)
            and str(cloud_state.get("budget_date") or "") != budget_date
        ):
            self.task_store.update_automation_budget(
                budget_date,
                lectures=1,
                runner_minutes=max(0.0, elapsed / 60.0),
                deepseek_tokens=max(0, tokens),
            )
        rule = next(
            (
                value for value in self.task_store.list_automation_rules()
                if str(value.get("course_id") or "") == course_id
            ),
            {},
        )
        if rule.get("quiz_after_import"):
            # 本地出题（字幕 → 回忆题）全程零云、零 LLM，不依赖 DeepSeek Key：
            # 之前按 Key 分流的 action_required 是假阻塞，已改成一律排队。
            # 这一处放宽只针对本地确定性生成，其他云任务的连接/授权边界不动。
            pending = dict(self.task_store.get_app_state("automation_quiz_pending", {}) or {})
            pending[sub_id] = {
                "course_id": course_id,
                "sub_id": sub_id,
                "state": "queued",
                "error_code": "",
                "observed_at": time.time(),
            }
            self.task_store.set_app_state("automation_quiz_pending", pending)

            def generate() -> None:
                try:
                    self.generate_quiz(course_id, sub_id)
                    latest = dict(self.task_store.get_app_state("automation_quiz_pending", {}) or {})
                    latest[sub_id] = {**latest.get(sub_id, {}), "state": "completed", "observed_at": time.time()}
                    self.task_store.set_app_state("automation_quiz_pending", latest)
                except Exception:
                    latest = dict(self.task_store.get_app_state("automation_quiz_pending", {}) or {})
                    latest[sub_id] = {
                        **latest.get(sub_id, {}), "state": "failed",
                        "error_code": "quiz_generation_failed", "observed_at": time.time(),
                    }
                    self.task_store.set_app_state("automation_quiz_pending", latest)

            threading.Thread(target=generate, name="courselens-local-quiz", daemon=True).start()
        self._request_search_refresh([sub_id])

    def app_shell_snapshot(self) -> dict:
        now = time.time()
        authentication = self.authentication_snapshot()
        catalog = self.authorized_catalog_snapshot(page=1, page_size=1)
        courses_visible = authentication["state"] == "ready"
        # CLIENT-STATE-R1：开机壳快照与抽屉同窗口语义——最新优先，旧终态
        # 任务不进壳窗口。
        tasks = self.task_store.list_tasks(limit=200, newest_first=True)
        active_states = {"queued", "running", "pausing", "paused"}
        automation = self.automation.snapshot(refresh=False)
        remote = self.remote_connection_snapshot()
        def observed(value: object) -> float:
            if isinstance(value, (int, float)):
                return float(value)
            text = str(value or "").strip()
            if not text:
                return 0.0
            try:
                return float(text)
            except ValueError:
                try:
                    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    return 0.0
        login_observed = float(self._login_status.get("updated_at") or 0)
        if login_observed > 10_000_000_000:
            login_observed /= 1000.0
        return {
            "schema": "courselens.app-shell.v2",
            "session": authentication,
            "authentication": authentication,
            "catalog": {
                key: catalog.get(key) for key in (
                    "state", "source", "observed_at", "expires_at", "code", "actions",
                    "refreshing", "partial_failures", "course_count", "lecture_count",
                )
            },
            "remote": remote,
            "tasks": {
                "active": sum(1 for task in tasks if task.get("state") in active_states) if courses_visible else None,
                "failed": sum(1 for task in tasks if task.get("state") == "failed") if courses_visible else None,
                "pending_imports": sum(1 for item in automation.get("imports", []) if item.get("state") in {"available", "downloading", "verifying", "importing", "cleanup_pending"}) if courses_visible else None,
                "observed_at": max((observed(task.get("updated_at")) for task in tasks), default=0) if courses_visible else 0,
            },
            "automation": {
                key: automation.get(key) for key in (
                    "state", "mode", "schedule", "budget", "verification", "observed_at",
                    "expires_at", "stale", "actions",
                )
            },
            "settings": self.settings_privacy_snapshot(),
            "connection": self.connection_snapshot(),
            "tutorials": self.tutorials_snapshot(),
            "features": self.task_store.get_feature_flags(),
            "observed_at": now,
            "expires_at": now + 20,
        }

    def _identity_scope(self) -> str:
        student_id = str(self._credentials.get("student_id") or "").strip()
        if not student_id:
            return ""
        return hashlib.sha256(f"courselens-course-cache-v1:{student_id}".encode("utf-8")).hexdigest()

    def identity_scope(self) -> str:
        """Return the opaque identity partition used by idempotent operations."""
        return self._identity_scope()

    def _catalog_cache_key(self) -> str:
        scope = self._identity_scope()
        return f"authorized_catalog:{scope}" if scope else ""

    def authentication_snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            login = dict(self._login_status)
            client = self._client
            verified_monotonic = float(self._client_last_verified_at or 0.0)
            configured = bool(self._credentials.get("student_id") and self._credentials.get("password"))
        updated_at = float(login.get("updated_at") or 0.0)
        if updated_at > 10_000_000_000:
            updated_at /= 1000.0
        remaining = CLIENT_REUSE_SECONDS - max(0.0, time.monotonic() - verified_monotonic)
        verified = bool(login.get("connected") and client is not None and remaining > 0)
        revalidating_flag = False
        if verified:
            state = "ready"
            code = "fudan_session_verified"
            actions = ["refresh-catalog", "logout"]
            expires_at = now + remaining
        elif login.get("state") in {"connecting", "retrying", "loading"}:
            state = "checking"
            code = "fudan_session_checking"
            actions = []
            expires_at = 0.0
        elif login.get("state") == "error":
            state = "degraded"
            code = str(login.get("error_code") or "fudan_login_failed")
            actions = ["login"]
            expires_at = 0.0
        elif client is not None and login.get("connected"):
            # 复用窗口已过期但已连接客户端仍在：这是未决校验（校验在途或待补验），
            # 不是确认失败——绝不能在这里报 fudan_login_required。
            self._nudge_lapsed_verification()
            with self._lock:
                revalidating = bool(self._client_nudge_in_flight)
                revalidate_started = float(self._client_revalidate_started_at or 0.0)
            revalidate_elapsed = (
                max(0.0, time.monotonic() - revalidate_started) if revalidate_started else 0.0
            )
            if revalidating and revalidate_elapsed <= REVALIDATE_READY_GRACE_SECONDS:
                # SRC-SYNDROME-1 U2（第廿七案）stale-while-revalidate 防抖：补验在途
                # 且未超收敛上限时保持 ready 形态，前端不再每 5 分钟闪「验证中」；
                # revalidating=True 为诚实披露。超限回 checking（收敛），真掉线重登
                # 期间 connected 已被 _set_login_status 重置，不会进入本分支。
                state = "ready"
                code = "fudan_session_verified"
                actions = ["refresh-catalog", "logout"]
                expires_at = 0.0
                revalidating_flag = True
            else:
                state = "checking"
                code = "fudan_session_checking"
                actions = []
                expires_at = 0.0
                revalidating_flag = False
        elif configured:
            state = "action_required"
            code = "fudan_login_required"
            actions = ["login"]
            expires_at = 0.0
            revalidating_flag = False
        else:
            state = "action_required"
            code = "fudan_credentials_missing"
            actions = ["login"]
            expires_at = 0.0
            revalidating_flag = False
        snapshot = {
            "state": state,
            "source": "platform" if verified or login.get("state") == "error" else "local",
            "observed_at": updated_at if verified or login.get("state") == "error" else now,
            "expires_at": expires_at,
            "code": code,
            "actions": actions,
            "connected": verified,
            "configured": configured,
        }
        if revalidating_flag:
            snapshot["revalidating"] = True
        # 检查中快照附加进度（纯增量键）：登录 dialog 渲染「第 N/M 次 · 步骤」
        if login.get("state") in {"connecting", "retrying", "loading"}:
            for key in ("step", "attempt", "max_attempts"):
                if key in login:
                    snapshot[key] = login[key]
        return snapshot

    # --- courselens.vpn-connection.v1 生产侧（P0.1 后端半） -------------------

    def connection_snapshot(self) -> dict:
        """Build the frozen v1 campus-connection snapshot (closed set only).

        绝不含 URL、headers、cookies、账号标识、消息文本或响应正文。
        服务级 verified 只来自本快照内已确认的认证读（会话验证/目录验证），
        门户探测成功绝不充当身份证明（计划原则 4：不制造假确定性）。
        """
        # P3.1：复用 15s 状态轮询作恢复判定的观察点（纯钟表比较，零探测）。
        self._maybe_note_host_resume()
        now = int(time.time())
        auth = self.authentication_snapshot()
        with self._lock:
            generation = max(0, int(self._route_generation))
            webvpn_route = str(self._webvpn_route or "")
            icourse_route = str(self._icourse_route_path or "")
            icourse_catalog_verified = bool(self._icourse_route_class)
            login_state = str(self._login_status.get("state") or "")
            error_code = str(self._login_status.get("error_code") or "")
            session_ever_connected = bool(self._session_ever_connected)
        webvpn_evidence = self.network.route_evidence("webvpn")
        icourse_evidence = self.network.route_evidence("icourse")
        in_flight = login_state in {"connecting", "retrying", "loading"}
        client_connected = bool(auth.get("connected"))

        def _evidence_paths(evidence: dict) -> tuple[bool, bool]:
            return evidence.get("direct_ok"), evidence.get("proxy_ok")

        def _both_failed(evidence: dict) -> bool:
            direct_ok, proxy_ok = _evidence_paths(evidence)
            return direct_ok is False and proxy_ok is False

        def _any_path_ok(evidence: dict) -> bool:
            direct_ok, proxy_ok = _evidence_paths(evidence)
            return bool(direct_ok or proxy_ok)

        tun_evidence = any(
            direct_ok is False and proxy_ok is True
            for evidence in (webvpn_evidence, icourse_evidence)
            for direct_ok, proxy_ok in (_evidence_paths(evidence),)
        )

        webvpn_verified = client_connected
        icourse_verified = client_connected and icourse_catalog_verified
        webvpn_service_state = (
            "ready" if webvpn_verified
            else "checking" if in_flight
            else "unavailable" if _both_failed(webvpn_evidence)
            else "unknown"
        )
        icourse_service_state = (
            "ready" if icourse_verified
            else "checking" if in_flight
            else "unavailable" if _both_failed(icourse_evidence)
            else "unknown"
        )
        webvpn_service_route = (
            webvpn_route
            or (webvpn_evidence.get("decision") or "")
            or "unknown"
        )
        icourse_service_route = (
            icourse_route
            or (icourse_evidence.get("decision") or "")
            or "unknown"
        )
        if webvpn_service_route not in {"direct", "local_proxy"}:
            webvpn_service_route = "unknown"
        if icourse_service_route not in {"direct", "local_proxy"}:
            icourse_service_route = "unknown"

        school_terms: set[str] = set()
        if webvpn_service_route != "unknown":
            school_terms.add("webvpn")
        if icourse_service_route != "unknown":
            school_terms.add(
                "webvpn" if icourse_service_route == webvpn_service_route and webvpn_service_route != "unknown"
                else "icourse_direct"
            )
        # iCourse 走 WebVPN 会话时其 route 字段即 webvpn 服务同路径
        if icourse_route == "webvpn":
            school_terms.add("webvpn")
            school_terms.discard("icourse_direct")
        if len(school_terms) > 1:
            school_route = "mixed"
        elif school_terms:
            school_route = next(iter(school_terms))
        else:
            school_route = "unknown"
        network_path = webvpn_service_route if webvpn_service_route != "unknown" else (
            icourse_service_route if icourse_service_route != "unknown" else "unknown"
        )

        if error_code == "fudan_challenge_required":
            state = "challenge_required"
        elif auth.get("state") == "ready":
            if webvpn_verified and icourse_verified:
                state = "ready"
            elif webvpn_verified or icourse_verified:
                state = "degraded"
            else:
                state = "checking"
        elif in_flight:
            state = "reauthenticating" if session_ever_connected else "checking"
        elif auth.get("state") == "degraded" or login_state == "error":
            if error_code == "fudan_session_expired":
                state = "expired"
            elif error_code == "fudan_service_maintenance":
                # 服务端确认的维护态：与网络路径故障分离——复检网络毫无意义。
                state = "network_unavailable"
            elif error_code in {"timeout", "network_unavailable", "webvpn_ticket_transport_failed", "icourse_ticket_transport_failed"}:
                state = "network_unavailable"
            else:
                # 凭据拒绝与账号锁定同属身份源拒绝（聚合态一致）；细分文案由
                # 闭集 error_code 驱动（锁定 → 解锁后再登录，唯一动作是打开设置）。
                state = "login_required"
        elif auth.get("state") == "action_required":
            # 无会话可校验：探测证据说两条允许路径都不可达时，网络不可达
            # 比登录要求更诚实（此时登录也必然失败）。
            if _both_failed(webvpn_evidence) or _both_failed(icourse_evidence):
                state = "network_unavailable"
            else:
                state = "login_required"
        elif _both_failed(webvpn_evidence) or _both_failed(icourse_evidence):
            state = "network_unavailable"
        elif _any_path_ok(webvpn_evidence) or _any_path_ok(icourse_evidence):
            state = "login_required"
        else:
            state = "off"

        if state == "ready":
            reason = "proxy_fallback" if network_path == "local_proxy" else "direct_ok"
        elif state == "degraded":
            reason = "proxy_fallback" if network_path == "local_proxy" else "direct_ok"
        elif state == "expired":
            reason = "session_expired"
        elif state == "challenge_required":
            reason = "challenge"
        elif state == "network_unavailable":
            # 服务端确认的维护态例外：维护事实优先于任何本地路径证据。
            reason = (
                "possible_tun_interference"
                if tun_evidence and error_code != "fudan_service_maintenance"
                else "service_unavailable"
            )
        elif state == "login_required":
            reason = (
                "credentials_rejected"
                if error_code in {"fudan_credentials_rejected", "fudan_account_locked"}
                else "unknown"
            )
        elif state == "off":
            reason = "cold_start"
        else:
            reason = "unknown"
        if tun_evidence and state not in {"ready", "checking", "reauthenticating", "off"} and error_code != "fudan_service_maintenance":
            # 证据支持时才允许 TUN 提示覆盖中性措辞（绝不把探测失败说成 TUN 事实）；
            # 服务端确认的维护态例外——维护事实优先于任何本地路径证据。
            reason = "possible_tun_interference"
        # P2-E：只对提示的出现/消失迁移计数（轮询热路径零常态写入）。
        tun_hint_now = reason == "possible_tun_interference"
        with self._lock:
            tun_hint_prev = self._tun_hint_published
            self._tun_hint_published = tun_hint_now
        if tun_hint_now != tun_hint_prev:
            self._bump_campus_metrics(
                tun_hint_transitions=1 if tun_hint_now else 0,
                tun_hint_clears=0 if tun_hint_now else 1,
            )

        if state == "challenge_required":
            actions = ["login"]
        elif state == "login_required":
            if error_code == "fudan_account_locked":
                # 锁定态唯一有用动作：打开设置核对账号；重试登录只会再次失败。
                actions = ["open-settings"]
            else:
                actions = ["login"]
                if tun_evidence:
                    actions.append("close-tun-and-retry")
        elif state == "expired":
            actions = ["login"]
        elif state == "network_unavailable":
            if error_code == "fudan_service_maintenance":
                # 维护态唯一动作：稍后重试；检查网络/换路径都不解决问题。
                actions = ["retry"]
            else:
                actions = (
                    ["check-network", "close-tun-and-retry"]
                    if tun_evidence
                    else ["check-network", "retry"]
                )
        elif state == "degraded":
            actions = ["reauthenticate", "check-network"]
        elif state == "off":
            actions = ["check-network"]
        else:
            actions = []

        expires_at = None
        if state in {"ready", "degraded"}:
            raw_expires = float(auth.get("expires_at") or 0.0)
            expires_at = int(max(raw_expires, now + 1))
        snapshot = {
            "schema": VPN_CONNECTION_SCHEMA,
            "state": state,
            "network_path": network_path,
            "school_route": school_route,
            "reason": reason,
            "observed_at": now,
            "expires_at": expires_at,
            "retry_after": None,
            "actions": actions,
            "generation": generation,
            "services": {
                "webvpn": {
                    "state": webvpn_service_state,
                    "route": webvpn_service_route,
                    "verified": webvpn_verified,
                },
                "icourse": {
                    "state": icourse_service_state,
                    "route": icourse_service_route,
                    "verified": icourse_verified,
                },
            },
        }
        violations = validate_vpn_connection_snapshot(snapshot)
        if violations:
            # 生产侧构造器的自检兜底：任何违例都以闭集 off/unknown 形态降级发布，
            # 绝不让内部状态错误把不合契约的快照发给前端。
            snapshot = {
                "schema": VPN_CONNECTION_SCHEMA,
                "state": "off",
                "network_path": "unknown",
                "school_route": "unknown",
                "reason": "unknown",
                "observed_at": now,
                "expires_at": None,
                "retry_after": None,
                "actions": [],
                "generation": generation,
                "services": {
                    "webvpn": {"state": "unknown", "route": "unknown", "verified": False},
                    "icourse": {"state": "unknown", "route": "unknown", "verified": False},
                },
            }
        return snapshot

    # --- P1-D 按需校园路径诊断（courselens.campus-diagnostics.v1） -------------

    CAMPUS_DIAGNOSTICS_SCHEMA = "courselens.campus-diagnostics.v1"
    CAMPUS_DIAGNOSTIC_SERVICES = ("webvpn", "icourse")

    @classmethod
    def _campus_latency_band(cls, healthy: bool, elapsed_seconds: float) -> str:
        """Coarse closed-set band only; raw latency numbers never leave the process."""
        if not healthy:
            return "unavailable"
        if elapsed_seconds < 2.0:
            return "fast"
        if elapsed_seconds < 5.0:
            return "normal"
        return "slow"

    def campus_diagnostics(self) -> dict:
        """On-demand campus path diagnostics (P1.3); bounded, closed set, no credentials.

        只做有界的无凭据成对探测（auto 模式两条允许路径；direct/manual 模式
        refresh_route_decision 不探测并返回空证据），绝不携带凭据；结果只含
        闭集路由/延迟档/回退结论，绝不含代理地址、URL、账号或课程数据。
        诊断只在用户显式触发时运行：健康会话没有任何轮询或后台探测。
        """
        # P3.1：按需诊断也是恢复判定的观察点之一（显式触发，非轮询）。
        self._maybe_note_host_resume()
        services: dict[str, dict] = {}
        for name in self.CAMPUS_DIAGNOSTIC_SERVICES:
            started = time.monotonic()
            try:
                evidence = self.network.refresh_route_decision(name)
            except Exception:
                evidence = {"decision": "unknown", "direct_ok": None, "proxy_ok": None}
            elapsed = time.monotonic() - started
            decision = str(evidence.get("decision") or "unknown")
            direct_ok = evidence.get("direct_ok")
            proxy_ok = evidence.get("proxy_ok")
            healthy = bool(direct_ok or proxy_ok)
            services[name] = {
                "route": decision,
                "direct_ok": direct_ok,
                "proxy_ok": proxy_ok,
                "fallback_used": bool(
                    decision == "proxy" and direct_ok is False and proxy_ok is True
                ),
                "latency_band": self._campus_latency_band(healthy, elapsed),
            }
        snapshot = self.connection_snapshot()
        actions = [
            action for action in (snapshot.get("actions") or []) if isinstance(action, str)
        ]
        return {
            "schema": self.CAMPUS_DIAGNOSTICS_SCHEMA,
            "checked_at": int(time.time()),
            "mode": str(self.network.snapshot().get("mode") or "auto"),
            "services": services,
            "state": str(snapshot.get("state") or "unknown"),
            "next_action": actions[0] if actions else "",
            # P2-E：去标识计数器随按需诊断一并返回（纯计数闭集，无任何身份/网络数据）。
            "counters": self.campus_metric_counters(),
        }

    # --- P2-A 按需预检 + P2-E 去标识计数器 ------------------------------------

    CAMPUS_METRIC_NAMES = (
        "auth_flights_total",       # 登录航班总数（含检查点恢复失败的落回）
        "reauth_flights",           # 本进程已有会话后的再认证航班
        "route_fallback_attempts",  # 航班内一次换路尝试
        "route_flap_suppressed",    # P3.1 冷却窗口内被抑制的重复路径翻转
        "checkpoint_restores_ok",   # 加密会话检查点恢复成功
        "checkpoint_restores_fail", # 恢复失败落回普通登录
        "duplicate_flight_waiters", # 由同一航班结果唤醒的排队等待者
        "tun_hint_transitions",     # 证据支持的「可能受 TUN 影响」提示出现次数
        "tun_hint_clears",          # 提示随后消失（含无干预自行恢复；仅作局部信号）
    )

    def campus_metric_counters(self) -> dict:
        """De-identified integer counters only; names are a closed set."""
        try:
            raw = self.task_store.get_app_state(CAMPUS_METRICS_STATE_KEY, {}) or {}
        except Exception:
            raw = {}
        raw = raw if isinstance(raw, dict) else {}
        return {name: max(0, int(raw.get(name) or 0)) for name in self.CAMPUS_METRIC_NAMES}

    def _bump_campus_metrics(self, **counts) -> None:
        try:
            current = self.campus_metric_counters()
            for name, delta in counts.items():
                if name in self.CAMPUS_METRIC_NAMES and isinstance(delta, int) and delta:
                    current[name] += delta
            self.task_store.set_app_state(CAMPUS_METRICS_STATE_KEY, current)
        except Exception:
            pass  # 计数器绝不影响连接/登录路径

    def _preflight_campus_route(self) -> None:
        """Non-blocking no-credential route preflight before a rebuild (P2-A).

        只在 client() 重建路径触发，且只在本进程已有过验证会话之后（再认证/
        复验校准；冷启动首航由启动探测与航班内一次换路覆盖，绝不给首次登录
        增加延迟）。探测在守护线程中运行：UI 启动与当前航班都永不等待探测
        （探测同时在所选路径上完成一次 TCP+TLS 级预热；套接字级预热仍由
        webvpn 的 _warm_ticket_transport 负责）。代际 + 短 TTL 先占位再探测，
        并发航班只探测一次；direct/manual 模式 refresh_route_decision 不探测
        ——用户已显式决定路径。失败静默：本轮无路由证据时按既有候选顺序。
        """
        with self._lock:
            if not self._session_ever_connected:
                return
            generation = int(self._route_generation)
            state = self._preflight_campus_state
            now = time.monotonic()
            if (
                state is not None
                and state[0] == generation
                and (now - state[1]) < CAMPUS_PREFLIGHT_TTL_SECONDS
            ):
                return
            self._preflight_campus_state = (generation, now)
        if self.network.route_decision_fresh("webvpn") and self.network.route_decision_fresh("icourse"):
            return

        def _probe() -> None:
            try:
                self.network.refresh_route_decision("webvpn")
                self.network.refresh_route_decision("icourse")
            except Exception:
                pass  # 预检失败只意味着本轮无路由证据；绝不打断任何路径

        threading.Thread(target=_probe, name="campus-route-preflight", daemon=True).start()

    def _store_authorized_catalog(self, courses: list[dict]) -> dict:
        key = self._catalog_cache_key()
        now = time.time()
        value = {
            "identity_scope": self._identity_scope(),
            "course_ids": sorted({str(item.get("course_id") or "") for item in courses if item.get("course_id")}),
            "observed_at": now,
            "expires_at": now + 15 * 60,
            "partial_failures": [],
        }
        if key:
            self.task_store.set_app_state(key, value)
        return value

    def _authorized_catalog_cache(self) -> dict:
        key = self._catalog_cache_key()
        if not key:
            return {}
        value = dict(self.task_store.get_app_state(key, {}) or {})
        if value.get("identity_scope") != self._identity_scope():
            return {}
        return value

    def _authorized_catalog_courses_raw(self) -> list[dict]:
        auth = self.authentication_snapshot()
        if auth.get("state") != "ready":
            return []
        cache = self._authorized_catalog_cache()
        allowed = {str(value) for value in cache.get("course_ids") or [] if str(value)}
        courses: list[dict] = []
        try:
            rows = self.catalog_repository.courses_for_ids(allowed)
        except ValueError:
            return []
        for item in rows:
            course_id = str(item.get("course_id") or "")
            value = dict(item)
            value["lectures"] = [
                self._public_lecture(course_id, lecture, resolve_stored=False)
                for lecture in item.get("lectures") or []
            ]
            courses.append(value)
        return courses

    def _authorized_catalog_stale_courses_raw(self) -> list[dict]:
        """Identity-scoped verified cache without requiring a ready auth (P2-C).

        只服务当前身份命中的目录缓存：身份不匹配时 `_authorized_catalog_cache`
        返回空，显式登出/删除账号已把缓存清空——陈旧视图绝不跨账号泄漏，
        也不包含任何需要新授权的平台读取（只回放已验证过的课程 ID）。
        """
        cache = self._authorized_catalog_cache()
        allowed = {str(value) for value in cache.get("course_ids") or [] if str(value)}
        if not allowed:
            return []
        courses: list[dict] = []
        try:
            rows = self.catalog_repository.courses_for_ids(allowed)
        except ValueError:
            return []
        for item in rows:
            course_id = str(item.get("course_id") or "")
            value = dict(item)
            value["lectures"] = [
                self._public_lecture(course_id, lecture, resolve_stored=False)
                for lecture in item.get("lectures") or []
            ]
            courses.append(value)
        return courses

    def authorized_catalog_snapshot(
        self, *, page: int = 1, page_size: int = 24, query: str = "",
        teacher: str = "", term: str = "", department: str = "", status: str = "",
    ) -> dict:
        auth = self.authentication_snapshot()
        now = time.time()
        with self._lock:
            catalog_status = dict(self._catalog_status)
        # P2-C 陈而可用：平台暂时不可达（会话过期/网络失败/复验在途）时，当前
        # 身份上次验证的目录缓存继续可见并显式标注；登出/删除账号已清除缓存，
        # 身份不匹配时缓存读取直接返回空——陈旧视图绝不跨账号泄漏。
        stale_continuity = auth["state"] != "ready"
        if stale_continuity:
            courses = self._authorized_catalog_stale_courses_raw()
            stale_continuity = bool(courses)
            if not stale_continuity:
                auth_checking = auth["state"] == "checking"
                return {
                    "state": "checking" if auth_checking else "action_required",
                    "source": "local",
                    "observed_at": auth.get("observed_at", now),
                    "expires_at": 0.0,
                    "code": str(auth.get("code") or "fudan_login_required"),
                    "actions": list(auth.get("actions") or ([] if auth_checking else ["login"])),
                    "refreshing": auth_checking,
                    "stale": False,
                    "partial_failures": [],
                    "course_count": None,
                    "lecture_count": None,
                    "courses": [],
                    "page": 1,
                    "page_size": max(1, min(100, int(page_size or 24))),
                    "page_count": None,
                }
        else:
            courses = self._authorized_catalog_courses_raw()
        cache = self._authorized_catalog_cache()
        query_text = str(query or "").strip().casefold()
        teacher_text = str(teacher or "").strip().casefold()
        term_text = str(term or "").strip().casefold()
        department_text = str(department or "").strip().casefold()
        status_text = str(status or "").strip().casefold()
        def matches(item: dict) -> bool:
            if query_text and query_text not in f"{item.get('title', '')} {item.get('teacher', '')}".casefold():
                return False
            if teacher_text and teacher_text != str(item.get("teacher") or "").casefold():
                return False
            if term_text and term_text != str(item.get("term") or item.get("semester") or "").casefold():
                return False
            if department_text and department_text != str(item.get("department") or item.get("faculty") or "").casefold():
                return False
            if status_text and status_text != str(item.get("learning_status") or "new").casefold():
                return False
            return True
        filtered = [item for item in courses if matches(item)]
        filtered = self.timetable.enrich_catalog(filtered)
        filtered.sort(key=lambda item: (str(item.get("term") or item.get("semester") or ""), str(item.get("title") or "")), reverse=True)
        size = max(1, min(100, int(page_size or 24)))
        page_count = max(1, (len(filtered) + size - 1) // size) if filtered else 1
        current_page = max(1, min(page_count, int(page or 1)))
        start = (current_page - 1) * size
        observed_at = float(cache.get("observed_at") or 0.0)
        expires_at = float(cache.get("expires_at") or 0.0)
        stale = not observed_at or now > expires_at
        catalog_state = str(catalog_status.get("state") or "idle")
        refreshing = catalog_state == "checking"
        failed = catalog_state == "degraded"
        if stale_continuity:
            # P2-C：陈旧视图有唯一诚实的状态对——复验在途 → checking；否则
            # degraded + authorized_catalog_stale（既有闭集码：正在显示同一账号
            # 上次验证的课程）。需要新授权的动作只保留登录入口。
            auth_checking = auth["state"] == "checking"
            state = "checking" if auth_checking else "degraded"
            code = "fudan_session_checking" if auth_checking else "authorized_catalog_stale"
            actions = list(auth.get("actions") or ([] if auth_checking else ["login"]))
            refreshing = auth_checking
        elif failed:
            state = "degraded"
            code = str(catalog_status.get("code") or "catalog_payload_invalid")
            actions = list(catalog_status.get("actions") or _catalog_actions(code))
        elif refreshing:
            state = "checking"
            code = "authorized_catalog_refreshing"
            actions = []
        elif stale:
            state = "degraded"
            code = "authorized_catalog_stale"
            actions = ["refresh-catalog"]
        else:
            state = "ready"
            code = "authorized_catalog_verified"
            actions = ["refresh-catalog"]
        return {
            "state": state,
            "source": "platform" if state == "ready" else "local",
            "observed_at": observed_at,
            "expires_at": expires_at,
            "code": code,
            "actions": actions,
            "refreshing": refreshing,
            "stale": bool(stale_continuity or (state == "degraded" and code == "authorized_catalog_stale")),
            "diagnostics": _closed_catalog_diagnostics(
                catalog_status.get("diagnostics")
            ),
            "partial_failures": list(dict.fromkeys([
                *(cache.get("partial_failures") or []),
                *(catalog_status.get("partial_failures") or []),
            ]))[:8],
            "course_count": len(courses),
            "filtered_count": len(filtered),
            "lecture_count": sum(len(item.get("lectures") or []) for item in courses),
            "courses": filtered[start:start + size],
            "page": current_page,
            "page_size": size,
            "page_count": page_count,
            "facets": {
                "terms": sorted({str(item.get("term") or item.get("semester") or "") for item in courses if item.get("term") or item.get("semester")}),
                "departments": sorted({str(item.get("department") or item.get("faculty") or "") for item in courses if item.get("department") or item.get("faculty")}),
                "teachers": sorted({str(item.get("teacher") or "") for item in courses if item.get("teacher")}),
            },
        }

    def public_catalog_snapshot(self) -> dict:
        auth = self.authentication_snapshot()
        cache = self._authorized_catalog_cache()
        if auth["state"] != "ready" or not cache:
            return {
                "authorization_state": auth["state"],
                "courses": {},
                "lectures": {},
            }
        courses: dict[str, dict] = {}
        lectures: dict[str, dict] = {}
        for item in self._authorized_catalog_courses_raw():
            course = dict(item)
            course_lectures = list(course.pop("lectures", []) or [])
            course_id = str(course.get("course_id") or "")
            if not course_id:
                continue
            courses[course_id] = course
            for lecture in course_lectures:
                sub_id = str(lecture.get("sub_id") or "")
                if sub_id:
                    lectures[sub_id] = lecture
        return {
            "version": CATALOG_SCHEMA_VERSION,
            "storage_layout_version": CATALOG_SCHEMA_VERSION,
            "authorization_state": "ready",
            "courses": courses,
            "lectures": lectures,
        }

    def _discard_platform_client(self) -> None:
        with self._lock:
            current = self._client
            self._client = None
            self._vpn = None
            self._client_last_verified_at = 0.0
            # 会话证据随客户端一起失效：连接快照的服务级 verified 必须回落。
            self._webvpn_route = ""
            self._icourse_route_class = ""
            self._icourse_route_path = ""
        # P3.1：身份/账号边界事件——路由健康记忆随旧身份一起失效。
        try:
            self.network.clear_route_health()
        except Exception:
            pass
        if current is not None:
            try:
                current.close()
            except (AttributeError, OSError):
                pass

    def logout_fudan(self) -> dict:
        self._discard_platform_client()
        try:
            # 显式登出丢弃全部会话检查点：会话状态对下一次登录不再可信。
            self.credentials.clear_all_session_checkpoints()
        except Exception:
            pass  # 检查点清理绝不阻断登出本身
        with self._lock:
            # 显式登出重置“本进程曾有会话”记忆：其后的登录是初次认证，
            # 不是静默重认证。
            self._session_ever_connected = False
        # P2-C：显式登出立即清除该身份的陈旧目录视图（身份边界事件）。
        self._discard_authorized_catalog_cache()
        self._reset_catalog_status()
        self._set_login_status("configured", "credentials", "已退出当前 iCourse 会话")
        return self.authentication_snapshot()

    def _discard_authorized_catalog_cache(self) -> None:
        """Clear only the current identity's verified-catalog cache (P2-C)."""
        key = self._catalog_cache_key()
        if not key:
            return
        try:
            self.task_store.set_app_state(key, {})
        except Exception:
            pass  # 缓存清理绝不阻断登出/删除账号本身

    def _timetable_vpn(self):
        return self.client().vpn

    def timetable_snapshot(self, semester_id: str = "", week: int = 0) -> dict:
        return self.timetable.snapshot(semester_id, week)

    def timetable_action(
        self, action: str, *, semester_id: str = "", start_date: str = "",
    ) -> dict:
        action = str(action or "").strip().lower()
        if action == "refresh":
            return self.timetable.refresh(semester_id)
        if action == "set-semester-start":
            return self.timetable.set_semester_start(semester_id, start_date)
        raise TimetableError("timetable_action_invalid", "Timetable action is invalid")

    def timetable_ics(self, semester_id: str = "") -> tuple[str, str]:
        return self.timetable.export_ics(semester_id)

    def settings_privacy_snapshot(self) -> dict:
        now = time.time()
        analytics = analytics_summary(self.learning_store.path, term="")
        remote = self.remote_connection_snapshot()
        automation = self.automation.snapshot(refresh=False)
        auth = self.authentication_snapshot()
        return {
            "state": "ready",
            "source": "local",
            "observed_at": now,
            "expires_at": now + 20,
            "code": "settings_snapshot_ready",
            "actions": ["diagnose", "test-channel", "start-authorization", "revoke-cloud-credentials", "disable-analytics", "delete-analytics"],
            "authentication": auth,
            "local_backend": {"state": "ready", "source": "local", "observed_at": now, "expires_at": now + 20, "code": "local_backend_ready", "actions": []},
            "remote": remote,
            "automation": {key: automation.get(key) for key in ("state", "source", "observed_at", "expires_at", "code", "actions", "stale")},
            "credentials": {
                "state": "ready" if self.credentials.list_accounts() else "action_required",
                "source": "local", "observed_at": now, "expires_at": now + 20,
                "code": "local_credentials_saved" if self.credentials.list_accounts() else "local_credentials_missing",
                "actions": [], "saved_account_count": len(self.credentials.list_accounts()),
            },
            "analytics": {
                "state": "ready" if analytics.get("settings", {}).get("enabled") else "action_required",
                "source": "local", "observed_at": now, "expires_at": now + 20,
                "code": "analytics_enabled" if analytics.get("settings", {}).get("enabled") else "analytics_disabled",
                "actions": ["disable-analytics", "delete-analytics"] if analytics.get("settings", {}).get("enabled") else ["enable-analytics"],
                "enabled": bool(analytics.get("settings", {}).get("enabled")),
            },
            "auto_connect": self.auto_connect_snapshot(),
            "update_background_checks": self.update_background_checks_enabled(),
            # MEDIA-VPN-1：媒体流系统代理偏好（闭集快照，无代理地址值）。
            "media_stream_proxy": self.media_stream_proxy_snapshot(),
        }

    def update_background_checks_enabled(self) -> bool:
        """Persisted low-frequency update-check preference; default on."""
        raw = self.task_store.get_app_state(UPDATE_BACKGROUND_CHECKS_STATE_KEY, {}) or {}
        raw = raw if isinstance(raw, dict) else {}
        if "enabled" not in raw:
            return True
        return bool(raw.get("enabled"))

    def media_webvpn_relay_enabled(self) -> bool:
        """Persisted off-campus media WebVPN relay preference; default on."""
        raw = self.task_store.get_app_state(MEDIA_WEBVPN_RELAY_STATE_KEY, {}) or {}
        raw = raw if isinstance(raw, dict) else {}
        if "enabled" not in raw:
            return True
        return bool(raw.get("enabled"))

    def set_update_background_checks(self, request: object) -> dict:
        """Persist the ``update_background_checks`` preference toggle."""
        if not isinstance(request, dict) or not isinstance(request.get("enabled"), bool):
            raise ValueError("update_background_checks_request_invalid")
        self.task_store.set_app_state(UPDATE_BACKGROUND_CHECKS_STATE_KEY, {
            "schema": UPDATE_BACKGROUND_CHECKS_SCHEMA,
            "enabled": request["enabled"],
        })
        return {"update_background_checks": request["enabled"]}

    def media_stream_proxy_snapshot(self) -> dict:
        """MEDIA-VPN-1：媒体流系统代理偏好+检测面（闭集，无地址值）。"""
        return media_stream_proxy_snapshot(self.task_store)

    def set_media_stream_proxy(self, request: object) -> dict:
        """Persist the ``media_stream_proxy`` preference toggle (default off)."""
        return set_media_stream_proxy_setting(self.task_store, request)

    def _auto_connect_preference(self) -> dict:
        """Load the versioned, non-secret auto-connect preference."""
        raw = self.task_store.get_app_state(AUTO_CONNECT_STATE_KEY, {}) or {}
        raw = raw if isinstance(raw, dict) else {}
        fudan = raw.get("fudan") if isinstance(raw.get("fudan"), dict) else {}
        github = raw.get("github") if isinstance(raw.get("github"), dict) else {}
        return {
            "schema": AUTO_CONNECT_SCHEMA,
            "fudan": {
                "enabled": bool(fudan.get("enabled")),
                "account_id": str(fudan.get("account_id") or "")[:64],
            },
            "github": {"enabled": bool(github.get("enabled"))},
            "last_resume": raw.get("last_resume") if isinstance(raw.get("last_resume"), dict) else {},
        }

    def _github_user_grant_stored(self) -> bool:
        return bool(self.credentials.has_secret("github_app_access_token"))

    def auto_connect_snapshot(self) -> dict:
        """Preference plus closed-set status only: no secret, cookie, or raw payload."""
        preference = self._auto_connect_preference()
        accounts = {
            str(item.get("student_id") or ""): item
            for item in self.credentials.list_accounts()
            if isinstance(item, dict)
        }
        fudan = preference["fudan"]
        if not fudan["enabled"]:
            fudan_status = "off"
        else:
            account = accounts.get(fudan["account_id"])
            if account is None:
                fudan_status = "account_missing"
            elif bool(account.get("requires_rotation")):
                fudan_status = "rotation_required"
            else:
                fudan_status = "ready"
        github = preference["github"]
        if not github["enabled"]:
            github_status = "off"
        elif self._github_user_grant_stored():
            github_status = "ready"
        else:
            github_status = "grant_missing"
        raw_resume = preference["last_resume"] if isinstance(preference["last_resume"], dict) else {}
        return {
            "schema": AUTO_CONNECT_SCHEMA,
            "fudan": {
                "enabled": fudan["enabled"],
                "account_id": fudan["account_id"] if fudan["enabled"] else "",
                "status": fudan_status,
            },
            "github": {"enabled": github["enabled"], "status": github_status},
            "last_resume": {
                "observed_at": float(raw_resume.get("observed_at") or 0.0),
                "fudan": _auto_connect_resume_outcome(raw_resume.get("fudan")),
                "github": _auto_connect_resume_outcome(raw_resume.get("github")),
            },
        }

    def set_auto_connect_preference(self, request: object) -> dict:
        """Validate and persist one auto-connect preference change.

        Enabling requires an explicit saved Fudan account (not marked for
        rotation) or a stored GitHub user grant.  Disabling never deletes
        credentials and never logs the session out.
        """
        if not isinstance(request, dict):
            raise AutoConnectPreferenceError("auto_connect_request_invalid")
        preference = self._auto_connect_preference()
        next_fudan = dict(preference["fudan"])
        next_github = dict(preference["github"])
        fudan = request.get("fudan")
        if isinstance(fudan, dict):
            enabled = bool(fudan.get("enabled"))
            account_id = str(fudan.get("account_id") or next_fudan["account_id"] or "").strip()
            if enabled:
                if not account_id:
                    raise AutoConnectPreferenceError("auto_connect_account_required")
                accounts = {
                    str(item.get("student_id") or ""): item
                    for item in self.credentials.list_accounts()
                    if isinstance(item, dict)
                }
                account = accounts.get(account_id)
                if account is None:
                    raise AutoConnectPreferenceError("auto_connect_account_missing")
                if bool(account.get("requires_rotation")):
                    raise AutoConnectPreferenceError("auto_connect_account_rotation_required")
            next_fudan = {"enabled": enabled, "account_id": account_id}
        github = request.get("github")
        if isinstance(github, dict):
            enabled = bool(github.get("enabled"))
            if enabled and not self._github_user_grant_stored():
                raise AutoConnectPreferenceError("auto_connect_github_grant_missing")
            next_github = {"enabled": enabled}
        value = {
            "schema": AUTO_CONNECT_SCHEMA,
            "fudan": next_fudan,
            "github": next_github,
        }
        previous_resume = preference["last_resume"]
        if isinstance(previous_resume, dict) and previous_resume:
            value["last_resume"] = previous_resume
        self.task_store.set_app_state(AUTO_CONNECT_STATE_KEY, value)
        return self.auto_connect_snapshot()

    def start_connection_probe(self) -> dict:
        """Start at most one bounded no-credential path probe per process (P0.2).

        永不阻塞调用方：loopback UI 先就绪，探测只在后台线程运行；auto 模式外
        不探测（direct/manual 的路径由用户显式决定）。失败静默——探测只产出
        路由证据，绝不影响登录或会话状态。
        """
        def _probe() -> None:
            try:
                if self._connection_probe_stop.is_set():
                    return
                self.network.refresh_route_decision("webvpn")
                if self._connection_probe_stop.is_set():
                    return
                self.network.refresh_route_decision("icourse")
            except Exception:
                pass  # 探测失败只意味着无证据；绝不打断启动或登录
            finally:
                with self._lock:
                    self._connection_probe_in_flight = False

        with self._lock:
            if self._connection_probe_started:
                return {"state": "already_started"}
            self._connection_probe_started = True
            self._connection_probe_in_flight = True
            thread = threading.Thread(target=_probe, name="connection-path-probe", daemon=True)
            self._connection_probe_thread = thread
            thread.start()
        return {"state": "started"}

    def start_auto_connect_resume(self) -> dict:
        """Start at most one bounded background auto-connect resume per process.

        Never blocks the caller: the loopback UI is ready before any resume
        work begins, and a second call in the same process is a no-op.
        """
        self.start_connection_probe()
        # SRC-SYNDROME-1 U2：自愈看门狗与自动登录偏好无关——显式登录的会话
        # 同样可能 degraded，必须在「off」早退之前挂上（每进程至多一次）。
        self._start_session_self_heal()
        with self._lock:
            if self._auto_connect_started:
                return {"state": "already_started"}
            self._auto_connect_started = True
        # P59-WORKER-AUTOREPAIR-1：启动后台 best-effort Worker 对账（与自动
        # 连接偏好无关；每进程至多一次——唯一调用点受上方 once 门约束）。
        self._start_worker_auto_sync()
        preference = self._auto_connect_preference()
        fudan_enabled = preference["fudan"]["enabled"]
        github_enabled = preference["github"]["enabled"]
        if not fudan_enabled and not github_enabled:
            return {"state": "off"}
        thread = threading.Thread(
            target=self._run_auto_connect_resume,
            args=(preference,),
            name="auto-connect-resume",
            daemon=True,
        )
        with self._lock:
            self._auto_connect_resume_thread = thread
            thread.start()
        return {"state": "started", "fudan": fudan_enabled, "github": github_enabled}

    def _run_auto_connect_resume(self, preference: dict) -> None:
        stop = self._auto_connect_resume_stop
        outcomes: dict[str, dict] = {}
        if not stop.is_set() and preference["fudan"]["enabled"]:
            outcomes["fudan"] = self._auto_connect_resume_fudan(
                preference["fudan"]["account_id"]
            )
        if not stop.is_set() and preference["github"]["enabled"]:
            outcomes["github"] = self._auto_connect_resume_github()
        if not outcomes or stop.is_set():
            return
        self._record_auto_connect_resume(outcomes)

    def _start_worker_auto_sync(self) -> None:
        """启动后台 best-effort Worker 对账（P59-WORKER-AUTOREPAIR-1）。

        仅当专属 Worker 已绑定（bootstrapped 四件齐 + worker 仓已存）时才
        发起远端核查；未配置则零远程调用。成功修复与失败都只写一行服务器
        日志，绝不弹窗、绝不阻塞启动时序；「修复 Worker」手动入口保持兜底。
        每进程至多一次：唯一调用点在 start_auto_connect_resume 的 once 门
        之后。
        """
        try:
            bootstrapped = bool(self.github_app.snapshot().get("bootstrapped"))
            bound = self.github_app.credentials.has_secret("github_worker_repo")
        except Exception:
            return
        if not bootstrapped or not bound:
            return
        thread = threading.Thread(
            target=self._run_worker_auto_sync,
            name="worker-auto-sync",
            daemon=True,
        )
        with self._lock:
            self._worker_auto_sync_thread = thread
        thread.start()

    def _run_worker_auto_sync(self) -> None:
        if self._worker_auto_sync_stop.is_set():
            return  # close 已收口：尚未起跑的对账直接放弃
        try:
            self.github_app.ensure_worker_trusted()
        except Exception as exc:
            # 检查侧异常（未配置/模板缺失/令牌/网络）闭集留痕即可：自动对账
            # 是 best-effort，任何失败都不打扰用户、不影响手动修复入口。
            code = str(getattr(exc, "code", "") or "github_error")
            print(
                f"[FudanCourseLens] Worker 自动同步已跳过（{code}）；"
                "「修复 Worker」手动入口不受影响",
                flush=True,
            )

    def _auto_connect_resume_fudan(self, account_id: str) -> dict:
        """One opt-in Fudan resume via the existing login/catalog flow.

        Missing or rotation-required credentials stop fail closed; the async
        login result itself surfaces through the normal session snapshot.  No
        other saved account is ever substituted and nothing retries.
        """
        account_id = str(account_id or "").strip()
        accounts = {
            str(item.get("student_id") or ""): item
            for item in self.credentials.list_accounts()
            if isinstance(item, dict)
        }
        account = accounts.get(account_id)
        if account is None:
            return {"state": "stopped", "code": "fudan_resume_account_missing"}
        if bool(account.get("requires_rotation")):
            return {"state": "stopped", "code": "fudan_resume_rotation_required"}
        try:
            with self._lock:
                ready = bool(
                    self._credentials.get("student_id") and self._credentials.get("password")
                )
            if not ready:
                self.use_saved_credentials(account_id)
            self.refresh_authorized_catalog_async()
        except (KeyError, RuntimeError, ValueError):
            return {"state": "stopped", "code": "fudan_resume_failed"}
        return {"state": "started", "code": "fudan_resume_started"}

    def _auto_connect_resume_github(self) -> dict:
        """One read-only GitHub grant verification; never device flow or setup."""
        if not self._github_user_grant_stored():
            return {"state": "stopped", "code": "github_resume_grant_missing"}
        try:
            verified = self.github_app.verify_user_authorization()
        except Exception as exc:
            from src.remote.github_app import GitHubAppError

            if isinstance(exc, GitHubAppError):
                return {"state": "stopped", "code": "github_resume_failed"}
            return {"state": "stopped", "code": "github_resume_unavailable"}
        if verified.get("authorized"):
            return {"state": "started", "code": "github_resume_verified"}
        return {"state": "stopped", "code": "github_resume_failed"}

    def _record_auto_connect_resume(self, outcomes: dict) -> None:
        preference = self._auto_connect_preference()
        preference["last_resume"] = {
            "observed_at": time.time(),
            "fudan": outcomes.get("fudan") or {},
            "github": outcomes.get("github") or {},
        }
        self.task_store.set_app_state(AUTO_CONNECT_STATE_KEY, preference)

    def tutorials_snapshot(self) -> dict:
        now = time.time()
        auth = self.authentication_snapshot()
        catalog = self.authorized_catalog_snapshot(page=1, page_size=1)
        github = self.github_app.snapshot()
        automation = self.automation.snapshot(refresh=False)
        analytics = analytics_summary(self.learning_store.path, term="")
        timetable = self.timetable_snapshot()
        evidence = {
            "runtime": True,
            "github_app": bool(github.get("installed") or github.get("installation_id")),
            "github_device": bool(github.get("authorized")),
            "worker_mailbox": bool(github.get("bootstrapped")),
            "fudan_login": auth.get("state") == "ready",
            "course_catalog": catalog.get("state") == "ready" and bool(catalog.get("course_count")),
            "timetable": timetable.get("code") in {
                "timetable_verified", "timetable_partial", "timetable_stale",
                "semester_start_required",
            },
            "deepseek": bool(self._deepseek_key()),
            "local_schedule": bool(self.task_store.get_app_state("daily_schedule", {}).get("enabled")),
            "cloud_schedule": automation.get("state") in {"ready", "running"},
            "analytics": bool(analytics.get("settings", {}).get("enabled")),
        }
        return {
            "state": "ready",
            "source": "local",
            "observed_at": now,
            "expires_at": now + 20,
            "code": "tutorial_evidence_ready",
            "actions": ["open-help"],
            "evidence": evidence,
            "next_step": next((key for key in ("github_app", "github_device", "worker_mailbox", "fudan_login", "course_catalog") if not evidence[key]), "learning-workflow"),
            "guide": self.onboarding_guide_snapshot(),
        }

    def start_remote_echo(self) -> dict:
        if not self._remote_configured():
            raise RuntimeError("GitHub Actions 尚未配置完整")
        if getattr(self, "remote_connection", None) is not None:
            self.remote_connection.preflight(
                maximum_age_seconds=30.0, require_channel_test=False
            )
        with self._lock:
            if self._remote_echo_thread and self._remote_echo_thread.is_alive():
                return self.remote_compute_snapshot()
            task_id = uuid.uuid4().hex
            self._remote_echo_status = {
                "state": "queued",
                "task_id": task_id,
                "run_id": 0,
                "stage": "remote_queue",
                "percent": None,
                "message": "正在启动 GitHub runner",
                "updated_at": time.time(),
            }

            def worker() -> None:
                from src.remote.coordinator import RemoteCoordinator, RemoteSettings
                from src.remote.protocol import JOB_SCHEMA, PROTOCOL_VERSION

                try:
                    with self.github_app.job_token_lease(
                        task_id=task_id, task_store=self.task_store
                    ):
                        base = RemoteSettings.load(self.credentials)
                        settings = replace(base, enabled=True, workflow="echo.yml", ref="main")
                        coordinator = RemoteCoordinator(settings, self.task_store, self.credentials)

                        def build_job(result_public_key: str) -> dict:
                            now = time.time()
                            return {
                                "schema": JOB_SCHEMA,
                                "protocol_version": PROTOCOL_VERSION,
                                "task_id": task_id,
                                "job_kind": "echo",
                                "created_at": now,
                                "expires_at": now + 600,
                                "result_public_key": result_public_key,
                                "pipeline": {"version": "actions-echo-v1"},
                                "payload": {"nonce": uuid.uuid4().hex},
                                "secrets": {},
                            }

                        def import_result(result: dict) -> None:
                            echo = dict(dict(result.get("outputs") or {}).get("echo") or {})
                            if echo.get("ok") is not True:
                                raise RuntimeError("GitHub echo result is incomplete")

                        def progress(stage: str, percent: float | None, label: str) -> None:
                            with self._lock:
                                self._remote_echo_status.update({
                                    "state": "running",
                                    "stage": stage,
                                    "percent": round(float(percent), 1) if percent is not None else None,
                                    "message": label,
                                    "updated_at": time.time(),
                                })

                        coordinator.execute(
                            task_id=task_id,
                            build_job=build_job,
                            import_result=import_result,
                            cancel_requested=lambda: False,
                            progress=progress,
                        )
                    run = self.task_store.get_remote_run(task_id) or {}
                    from src.remote.connection import verification_fingerprint
                    verified_tree = (
                        self.credentials.load_secret("github_worker_verified_tree")
                        if self.credentials.has_secret("github_worker_verified_tree") else ""
                    )
                    fingerprint = verification_fingerprint(
                        tree=verified_tree,
                        box_public_key=self.credentials.load_secret("worker_box_public_key"),
                        signing_public_key=self.credentials.load_secret("worker_signing_public_key"),
                    )
                    self.task_store.set_app_state("remote_channel_verification", {
                        "verified_at": time.time(),
                        "fingerprint": fingerprint,
                        "task_id": task_id,
                        "run_id": int(run.get("run_id") or 0),
                    })
                    with self._lock:
                        self._remote_echo_status.update({
                            "state": "success",
                            "run_id": int(run.get("run_id") or 0),
                            "stage": "complete",
                            "percent": 100.0,
                            "message": "加密连接测试通过，云端临时数据已清理",
                            "updated_at": time.time(),
                        })
                    if getattr(self, "remote_connection", None) is not None:
                        self.remote_connection.request_probe()
                except Exception as exc:
                    with self._lock:
                        self._remote_echo_status.update({
                            "state": "error",
                            "message": _safe_error_message(exc),
                            "updated_at": time.time(),
                        })
                    if getattr(self, "remote_connection", None) is not None:
                        self.remote_connection.request_probe()

            self._remote_echo_thread = threading.Thread(
                target=worker,
                name="remote-echo",
                daemon=True,
            )
            self._remote_echo_thread.start()
        return self.remote_compute_snapshot()

    def remote_connection_snapshot(self, *, fresh: bool = False) -> dict:
        # 等待窗新鲜语义（INIT-PATH-POLISH-1 单元A）：fresh=1 时强制一次新
        # 探针并等它收口（只读加速，窗口内重复轮询被合并）；常规请求不受影响。
        if fresh:
            snapshot = self.remote_connection.fresh_snapshot()
        else:
            snapshot = self.remote_connection.snapshot()
        # 分步进度（单元D）：动作执行期间附带闭集阶段字段，GET 轮询可读
        with self._lock:
            stage = dict(self._remote_action_stage) if self._remote_action_stage else {}
        if stage:
            snapshot = {**snapshot, "action_progress": stage}
        # 云端连接面（CLOUD-CONSENT-AUTO-1 U1）：读模型只剩连接真值两字段
        # （configured/verified）——开关退役后不再有 enabled/switch_locked；
        # 读取失败时如实置空，不发明状态。
        try:
            compute = self.remote_compute_snapshot()
            snapshot = {**snapshot, "remote_compute": {
                "configured": bool(compute.get("configured")),
                "verified": bool(compute.get("verified")),
            }}
        except Exception:
            snapshot = {**snapshot, "remote_compute": None}
        return snapshot

    def _remote_action_stage_set(self, action: str, stage: str) -> None:
        label = REMOTE_ACTION_STAGE_LABELS.get(stage)
        if not label:
            return
        with self._lock:
            self._remote_action_stage = {
                "action": action, "stage": stage, "label": label, "updated_at": time.time(),
            }

    def _remote_action_stage_clear(self) -> None:
        with self._lock:
            self._remote_action_stage = {}

    def remote_runs_snapshot(self) -> list[dict]:
        return self.remote_connection.remote_runs_snapshot()

    def remote_connection_action(
        self, action: str, *, operation_id: str, target_id: str = "", force: bool = False
    ) -> dict:
        from src.remote.connection import OPERATION_ID_RE, RemoteConnectionError
        from src.remote.github_client import GitHubClient

        action = str(action or "").strip().lower()
        operation_id = str(operation_id or "").strip()
        target_id = str(target_id or "").strip()
        if not OPERATION_ID_RE.fullmatch(operation_id):
            raise ValueError("operation_id is invalid")
        allowed = {
            "diagnose", "start-authorization", "poll-authorization", "bootstrap", "verify-worker",
            "repair-worker", "test-channel", "retry-import", "reattach-run",
            "cancel-run", "retry-cleanup", "rotate-worker-keys", "disconnect",
            "reconcile-mailbox-history",
        }
        if action not in allowed:
            raise ValueError("remote connection action is unsupported")
        operation, created = self.task_store.begin_remote_operation(
            operation_id, action, target_id
        )
        if not created:
            return operation
        try:
            result: dict = {}
            if action == "diagnose":
                result = self.remote_connection.probe()
            elif action == "start-authorization":
                result = self.start_github_device_authorization(force=force)
            elif action == "poll-authorization":
                result = self.poll_github_device_authorization()
            elif action == "bootstrap":
                result = self.bootstrap_github()
            elif action == "verify-worker":
                result = self.verify_github_worker()
            elif action == "repair-worker":
                result = self.repair_github_worker()
            elif action == "test-channel":
                self._remote_action_stage_set(action, "testing_channel")
                result = self.start_remote_echo()
            elif action == "reconcile-mailbox-history":
                result = self._reconcile_mailbox_history()
            elif action == "retry-cleanup":
                active_remote = any(
                    str(item.get("remote_state") or "") in {
                        "created", "queued", "awaiting_payload", "running", "canceling",
                    }
                    for item in self.task_store.list_remote_runs(limit=100)
                )
                if self.task_store.list_remote_token_leases() or active_remote:
                    raise RemoteConnectionError(
                        "active_token_lease", "Active remote tasks still require the job token"
                    )
                self.github_app.delete_job_token()
                result = {"cleanup_confirmed": True}
            elif action == "rotate-worker-keys":
                active_remote = any(
                    str(item.get("remote_state") or "") in {
                        "created", "queued", "awaiting_payload", "running", "canceling",
                        "artifact_ready", "downloading_result",
                    }
                    for item in self.task_store.list_remote_runs(limit=100)
                )
                if self.task_store.list_remote_token_leases() or active_remote:
                    raise RemoteConnectionError(
                        "active_remote_run", "Worker keys cannot be rotated during an active run"
                    )
                self._remote_action_stage_set(action, "rotating_keys")
                result = self.github_app.rotate_worker_keys()
                self._remote_action_stage_clear()
                self.task_store.set_app_state("remote_channel_verification", {})
                self._reload_remote_coordinator()
            elif action == "disconnect":
                result = self.disconnect_github()
            elif action == "cancel-run":
                run = self.task_store.get_remote_run(target_id)
                if not run or not int(run.get("run_id") or 0):
                    raise KeyError("remote run not found")
                settings = self.remote_settings
                github = GitHubClient(
                    self.github_app.access_token(minimum_lifetime_seconds=300),
                    proxy_url=settings.proxy_url,
                )
                github.cancel_run(str(run.get("repository") or settings.public_repo), int(run["run_id"]))
                self.task_store.upsert_remote_run(
                    target_id,
                    repository=str(run.get("repository") or settings.public_repo),
                    workflow=str(run.get("workflow") or settings.workflow),
                    run_id=int(run["run_id"]), attempt=int(run.get("attempt") or 1),
                    remote_state="canceling",
                )
                result = {"cancel_requested": True, "task_id": target_id}
            elif action in {"retry-import", "reattach-run"}:
                task = self.task_store.get_task(target_id)
                if not task:
                    raise KeyError("task not found")
                if task.get("state") == "failed":
                    task = self.task_store.retry_failed_task(target_id)
                if task and task.get("state") in {"paused", "pausing"}:
                    result = self.control_task(target_id, "resume")
                elif task and task.get("state") == "queued":
                    with self._lock:
                        self._queue_persisted_task(task)
                        if task.get("kind") == "subtitle":
                            self._ensure_subtitle_worker()
                        elif task.get("kind") == "summary":
                            self._ensure_summary_worker()
                        elif task.get("kind") == "question":
                            self._ensure_question_worker()
                    result = {"task": task, "accepted_action": action}
                else:
                    result = {"task": task, "accepted_action": action}
            self.remote_connection.request_probe()
            operation = self.task_store.finish_remote_operation(
                operation_id, state="accepted", result=result
            )
            self.task_store.append_remote_event(
                "remote-connection", operation_id,
                {"operation_id": operation_id, "action": action, "state": "accepted"},
            )
            return operation
        except Exception as exc:
            code = str(getattr(exc, "code", "") or "operation_failed")
            operation = self.task_store.finish_remote_operation(
                operation_id, state="failed", error_code=code
            )
            self.task_store.append_remote_event(
                "remote-connection", operation_id,
                {"operation_id": operation_id, "action": action, "state": "failed", "error_code": code},
            )
            raise
        finally:
            # 动作收口（单元D）：动作线程结束即清阶段字段——快照轮询只见
            # 在途动作的进度，绝不残留陈旧阶段
            self._remote_action_stage_clear()

    def _reconcile_mailbox_history(self) -> dict:
        """Seal closed unconsumed Mailbox history, preserving all comments.

        Every guard re-verifies fresh evidence at action time: zero local and
        GitHub-side activity, exact installation, trusted Worker pin, and a
        complete classified inventory.  Writes are bounded GET→PATCH→GET per
        issue and stop at the first drift.  The result carries counts only —
        never issue numbers, bodies, or comments.
        """
        from src.remote.connection import RemoteConnectionError
        from src.remote.github_client import GitHubClient, GitHubRemoteError
        from src.remote.worker_migration import (
            _active_task_run_count,
            _known_mailbox_issues,
            _managed_mailbox_inventory,
            _task_artifact_count,
        )

        if not self.credentials.has_secret("github_worker_repo") or not self.credentials.has_secret("github_mailbox_repo"):
            raise RemoteConnectionError(
                "mailbox_reconcile_not_configured",
                "GitHub Worker 或 Mailbox 尚未初始化",
            )
        # CLOUD-CONSENT-AUTO-1 U1：「必须先关闭远程计算」前置随开关退役——开关
        # 没了该门会永久拒绝；真正的零活动前置保留（下方本地/远程任务活动 +
        # GitHub 侧零 run/artifact/token），修复历史记录不会撞上在飞任务。
        # 2) No local subtitle/summary/question work or remote task activity.
        if self.has_active_work():
            raise RemoteConnectionError(
                "active_local_work_present", "仍有本地或远程任务活动，暂不能修复历史记录"
            )
        worker_repo = str(self.credentials.load_secret("github_worker_repo"))
        mailbox_repo = str(self.credentials.load_secret("github_mailbox_repo"))
        token = self.github_app.access_token(minimum_lifetime_seconds=900)
        # 3)-5) GitHub-side zero activity across runs, artifacts, and tokens.
        if _active_task_run_count(self.github_app, worker_repo, token) > 0:
            raise RemoteConnectionError(
                "active_worker_run_present", "Worker 仍有活动 run，暂不能修复历史记录"
            )
        if _task_artifact_count(self.github_app, worker_repo, token) > 0:
            raise RemoteConnectionError(
                "worker_artifacts_present", "Worker 仍有关联 Artifact，暂不能修复历史记录"
            )
        if list(self.task_store.list_remote_token_leases()):
            raise RemoteConnectionError(
                "remote_cleanup_pending", "仍有远程 token 租约，请先完成清理"
            )
        if list(self.credentials.list_secret_names(prefix="remote_result_private:")):
            raise RemoteConnectionError(
                "remote_cleanup_pending", "仍有未导入的远程结果密钥，请先完成清理"
            )
        cleanup_pending = int(self.task_store.migration_cleanup_pending_count()) + (
            1 if self.credentials.has_secret("github_job_token_cleanup_pending") else 0
        )
        # 6) Authorization, exact two-repo installation, trusted Worker pin.
        integrity = dict(self.github_app.check_worker_integrity(read_only=True))
        if (
            not integrity.get("trusted")
            or integrity.get("dispatch_mode") != "personal-worker"
            or str(integrity.get("repository") or "").casefold() != worker_repo.casefold()
        ):
            raise RemoteConnectionError(
                "worker_trust_unavailable", "Worker 签名模板校验未通过，暂不能修复历史记录"
            )
        live = dict(self.github_app.inspect_managed_resources(read_only=True))
        installation = dict(live.get("installation") or {})
        mailbox = dict(live.get("mailbox") or {})
        identity = dict(live.get("identity") or {})
        mailbox_valid = bool(
            mailbox.get("exists") and mailbox.get("private") and mailbox.get("has_issues")
            and not mailbox.get("archived") and not mailbox.get("disabled")
            and mailbox.get("managed")
            and str(mailbox.get("owner") or "").casefold()
            == str(identity.get("login") or "").casefold()
        )
        if not installation.get("repository_selection_exact") or not mailbox_valid:
            raise RemoteConnectionError(
                "installation_scope_not_exact", "GitHub App 安装范围不精确，请先在 GitHub 完成",
            )
        if "COURSELENS_JOB_TOKEN" in set(live.get("secret_names") or []) or cleanup_pending > 0:
            raise RemoteConnectionError(
                "remote_cleanup_pending", "仍有临时 job token 或清理待办，请先完成清理"
            )
        # 7) Fresh complete inventory; only closed unconsumed history qualifies.
        try:
            inventory = _managed_mailbox_inventory(
                self.github_app, mailbox_repo, token,
                known_issues=_known_mailbox_issues(self.task_store),
            )
        except RuntimeError as exc:
            # RuntimeError messages here are already closed codes (inventory
            # incomplete / too large); surface them without leaking content.
            raise RemoteConnectionError("mailbox_inventory_unavailable", str(exc)) from exc
        if int(inventory["open_unconsumed_count"]) > 0:
            raise RemoteConnectionError(
                "mailbox_open_unconsumed", "存在未收口的 open 任务载荷，请走任务恢复路径"
            )
        if int(inventory["metadata_drift_count"]) > 0 or int(inventory["active_issue_missing_count"]) > 0:
            raise RemoteConnectionError(
                "mailbox_metadata_drift", "Mailbox 元数据漂移，需要人工审计"
            )
        targets = [int(number) for number in inventory["reconcilable_issue_numbers"]]
        # 8) Bounded batch with fresh re-verification before every write.
        github = GitHubClient(token, proxy_url=self.remote_settings.proxy_url)
        processed = 0
        changed = 0
        first_error = ""
        for number in targets[:20]:
            if self.has_active_work() or _active_task_run_count(self.github_app, worker_repo, token) > 0:
                first_error = "mailbox_reconcile_stopped_activity"
                break
            try:
                outcome = github.mark_job_consumed_preserving_history(mailbox_repo, number)
            except GitHubRemoteError:
                first_error = "mailbox_reconcile_failed"
                break
            processed += 1
            changed += 1 if outcome.get("changed") else 0
        result = {
            "reconcile_complete": not first_error and processed >= len(targets),
            "processed_count": processed,
            "changed_count": changed,
            "remaining_count": max(0, len(targets) - processed),
            "error_code": first_error,
        }
        if first_error and processed == 0:
            raise RemoteConnectionError(first_error, "Mailbox 历史记录修复未执行任何写入")
        return result

    def _request_search_refresh(self, sub_ids: list[str]) -> None:
        if self._search_index_started:
            self.search_index.request_refresh(sub_ids)

    def _recorded_operation(
        self, kind: str, course_id: str, sub_id: str, config_key: str,
        payload: dict, operation, *, result_version: str = "local-v1",
    ):
        """Run a short local operation while leaving one truthful task record."""
        # Narrow unit-test/service adapters may intentionally construct only
        # the learning capability. The production application always owns a
        # TaskStore; keep those adapters source-compatible without inventing
        # an in-memory task state.
        if not hasattr(self, "task_store"):
            return operation()
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        input_hash = hashlib.sha256(encoded).hexdigest()
        task, created = self.task_store.add_task(
            str(kind), str(course_id), str(sub_id), dict(payload), config_key=str(config_key)
        )
        if not created:
            raise RuntimeError("operation_already_running")
        task_id = str(task["task_id"])
        started_at = time.time()
        self.task_store.update_task(task_id, state="running", started_at=time.time(), progress={
            "schema_version": 3, "stage": "running", "percent": None,
            "label": "后端正在处理", "indeterminate": True,
            "input_hash": input_hash, "result_version": str(result_version),
            "elapsed_seconds": 0.0, "observed_at": started_at,
        })
        try:
            result = operation()
            finished_at = time.time()
            self.task_store.update_task(task_id, progress={
                "schema_version": 3, "stage": "completed", "percent": 100.0,
                "label": "后端处理完成", "indeterminate": False,
                "input_hash": input_hash, "result_version": str(result_version),
                "elapsed_seconds": round(max(0.0, finished_at - started_at), 3),
                "observed_at": finished_at,
            })
            self.task_store.mark_terminal(task_id, "completed")
            return result
        except Exception as exc:
            code = re.sub(r"[^a-z0-9_]+", "_", str(exc).casefold()).strip("_")[:80] or "operation_failed"
            self.task_store.update_task(task_id, progress={
                "schema_version": 3, "stage": "failed", "percent": None,
                "label": "后端处理失败", "indeterminate": True,
                "input_hash": input_hash, "result_version": str(result_version),
                "elapsed_seconds": round(max(0.0, time.time() - started_at), 3),
                "observed_at": time.time(),
            })
            self.task_store.mark_terminal(task_id, "failed", error=code)
            raise

    def _retry_recorded_operation(self, task: dict) -> dict:
        """Retry a persisted local operation without inventing a new task identity."""
        task_id = str(task["task_id"])
        kind = str(task.get("kind") or "")
        payload = dict(task.get("payload") or {})
        operations = {
            "search_answer": lambda: self._answer_from_evidence_impl(
                str(payload.get("query") or ""), course_ids=list(payload.get("course_ids") or []),
                sub_id=str(payload.get("sub_id") or ""),
            ),
            "quiz": lambda: self._generate_quiz_impl(
                str(payload.get("course_id") or task.get("course_id") or ""),
                str(payload.get("sub_id") or task.get("sub_id") or ""),
            ),
            "review_plan": lambda: self._create_review_plan_impl(
                title=str(payload.get("title") or "复习计划"),
                exam_at=float(payload.get("exam_at") or 0),
                available_minutes=int(payload.get("available_minutes") or 60),
                course_id=str(payload.get("course_id") or ""),
                sub_id=str(payload.get("sub_id") or ""),
                daily_minutes=payload.get("daily_minutes"),
                strategy=str(payload.get("strategy") or "coverage"),
                course_scope=list(payload.get("course_scope") or []) or None,
            ),
            "document_alignment": lambda: self._align_learning_document_impl(
                str(payload.get("document_id") or "")
            ),
            "timeline_classification": lambda: self._classify_smart_timeline_impl(
                str(payload.get("course_id") or task.get("course_id") or ""),
                str(payload.get("sub_id") or task.get("sub_id") or ""),
            ),
            "concept_analysis": lambda: self._analyze_cross_course_concepts_impl(
                list(payload.get("course_ids") or [])
            ),
        }
        operation = operations.get(kind)
        if operation is None:
            raise ValueError("task_retry_not_supported")
        previous = dict(task.get("progress") or {})
        started_at = time.time()
        self.task_store.update_task(
            task_id, state="running", error="", finished_at=None, started_at=started_at,
            progress={
                **previous, "stage": "running", "percent": None,
                "label": "后端正在重试", "indeterminate": True,
                "elapsed_seconds": 0.0, "observed_at": started_at,
            },
        )
        try:
            operation()
            finished_at = time.time()
            self.task_store.update_task(task_id, progress={
                **previous, "stage": "completed", "percent": 100.0,
                "label": "后端重试完成", "indeterminate": False,
                "elapsed_seconds": round(max(0.0, finished_at - started_at), 3),
                "observed_at": finished_at,
            })
            return self.task_store.mark_terminal(task_id, "completed") or {}
        except Exception as exc:
            code = re.sub(r"[^a-z0-9_]+", "_", str(exc).casefold()).strip("_")[:80] or "operation_failed"
            self.task_store.update_task(task_id, progress={
                **previous, "stage": "failed", "percent": None,
                "label": "后端重试失败", "indeterminate": True,
                "elapsed_seconds": round(max(0.0, time.time() - started_at), 3),
                "observed_at": time.time(),
            })
            return self.task_store.mark_terminal(task_id, "failed", error=code) or {}


    def _normalize_recorded_operation_recovery(self) -> None:
        """Keep recoverable local work paused; fail imports whose bytes were never persisted."""
        # CLIENT-STATE-R1：paused 恢复扫描按最新优先——最近退出的导入最该
        # 先被收敛（states=("paused",) 非终态，不受保留窗影响）。
        for task in self.task_store.list_tasks(
            kinds=RECORDED_LOCAL_TASK_KINDS, states=("paused",), limit=200, newest_first=True
        ):
            if task.get("kind") != "document_import":
                continue
            progress = dict(task.get("progress") or {})
            now = time.time()
            self.task_store.update_task(str(task["task_id"]), progress={
                **progress, "stage": "failed", "percent": None,
                "label": "导入在客户端退出前未完成，原始文件需重新选择",
                "indeterminate": True, "observed_at": now,
            })
            self.task_store.mark_terminal(
                str(task["task_id"]), "failed", error="document_payload_not_recoverable"
            )

    def _normalize_task_progress_models(self) -> None:
        for task in self.task_store.list_tasks(states=ACTIVE_STATES):
            progress = task.get("progress") or {}
            payload = task.get("payload") or {}
            kind = str(task.get("kind") or "")
            if kind not in {"subtitle", "summary"}:
                continue
            if int(progress.get("schema_version") or 0) < 2:
                proofread = bool(payload.get("subtitle_proofread"))
                progress = upgrade_progress(
                    kind,
                    progress,
                    proofread=proofread,
                    include_ppt=bool(payload.get("include_ppt", True)),
                )
                task = self.task_store.update_task(str(task["task_id"]), progress=progress) or task
            if kind == "subtitle":
                self._refresh_subtitle_task_estimate(task)

    @staticmethod
    def _subtitle_duration_profile(source_kind: str) -> str:
        return f"subtitle-duration:automatic:{source_kind}"

    @staticmethod
    def _subtitle_duration_band_profile(source_kind: str, duration: float) -> str:
        """Finer prior bucket: kind+behavior history inside one duration band."""
        band = subtitle_duration_band(duration)
        return f"subtitle-duration:automatic:{source_kind}:{band}" if band else ""

    def _subtitle_media_duration(self, task: dict, progress: dict | None = None) -> float | None:
        value = dict(progress or task.get("progress") or {})
        payload = task.get("payload") or {}
        for candidate in (
            value.get("media_duration_seconds"),
            payload.get("media_duration_seconds"),
        ):
            try:
                duration = float(candidate or 0.0)
            except (TypeError, ValueError):
                duration = 0.0
            if duration > 0:
                return duration
        return None

    def _subtitle_duration_rtfs(self, source_kind: str) -> list[float]:
        exact = self.task_store.estimate_samples(self._subtitle_duration_profile(source_kind))
        if exact:
            return exact
        return []

    def _subtitle_phase_tail_seconds(self, proofread: bool, source_kind: str) -> dict[str, float]:
        """Per-phase history medians (total seconds) for the serial tail.

        Reads the existing per-phase sample table used by
        ``_persist_progress_samples`` (profile key
        ``{profile}:{source_kind}:{category}:{phase_id}``) so measured
        proofread/write durations replace the documented bootstrap constants.
        """
        tail: dict[str, float] = {}
        categories = {
            phase.id: phase.category
            for phase in phase_plan("subtitle", proofread=proofread)
        }
        for phase_id in SUBTITLE_SERIAL_TAIL_PHASES["proofread" if proofread else "fallback"]:
            category = categories.get(phase_id)
            if not category:
                continue
            samples = self.task_store.estimate_samples(
                f"subtitle:automatic:{source_kind}:{category}:{phase_id}"
            )
            if samples:
                # Samples store seconds per phase-percent; 100% is the phase total.
                tail[phase_id] = statistics.median(samples) * 100.0
        return tail

    def _refresh_subtitle_task_estimate(self, task: dict) -> dict:
        progress = dict(task.get("progress") or {})
        duration = self._subtitle_media_duration(task, progress)
        if duration is None:
            # Percent-only samples are not comparable across lectures. Suppress
            # an estimate until the remote media duration is known.
            return self.task_store.update_task(str(task["task_id"]), estimate={}) or task
        payload = task.get("payload") or {}
        proofread = bool(payload.get("subtitle_proofread"))
        source_kind = str(payload.get("source_kind") or "remote")
        chunk_seconds = 30
        processed_media = progress.get("processed_media_seconds")
        if processed_media is None and progress.get("chunk_index") is not None:
            processed_media = min(duration, float(progress.get("chunk_index") or 0) * chunk_seconds)
        tracker = ProgressTracker(
            "subtitle",
            proofread=proofread,
            initial_progress=progress,
            initial_estimate=task.get("estimate"),
        )
        tracker.configure_duration_estimate(
            duration,
            historical_rtfs=self._subtitle_duration_rtfs(source_kind),
            processed_media_seconds=processed_media,
            average_rtf=progress.get("average_rtf"),
            stable_media_events=progress.get("media_events"),
            phase_tail_seconds=self._subtitle_phase_tail_seconds(proofread, source_kind),
        )
        normalized_progress, estimate = tracker.snapshot()
        return self.task_store.update_task(
            str(task["task_id"]),
            progress=normalized_progress,
            estimate=estimate,
        ) or task


    def _seed_task_estimate(self, task: dict, profile: str) -> dict:
        if task.get("estimate"):
            return task
        costs = self.task_store.estimate_samples(profile)
        if not costs:
            return task
        estimate = estimate_from_progress({}, prior_seconds=statistics.median(costs) * 100.0)
        return self.task_store.update_task(str(task["task_id"]), estimate=estimate) or task

    def _persist_progress_samples(
        self,
        tracker: ProgressTracker,
        profile: str,
        source_kind: str,
    ) -> None:
        fingerprint = device_fingerprint()
        for phase_id, category, unit_cost, units, elapsed in tracker.drain_cost_samples():
            self.task_store.add_estimate_sample(
                f"{profile}:{source_kind}:{category}:{phase_id}",
                unit_cost,
                units,
                elapsed,
                device_fingerprint=fingerprint,
            )

    def _save_progress_tracker_snapshot(self, task_id: str) -> None:
        tracker = self._progress_trackers.get(str(task_id))
        if tracker is None:
            return
        progress, estimate = tracker.snapshot()
        existing = (self.task_store.get_task(str(task_id)) or {}).get("progress") or {}
        for key in (
            "stage", "stage_percent", "chunk_index", "chunk_count",
            "phase_id", "completed", "total", "media_events",
        ):
            if key in existing:
                progress[key] = existing[key]
        self.task_store.update_task(str(task_id), progress=progress, estimate=estimate)

    def _set_login_status(
        self,
        state: str,
        step: str,
        message: str,
        *,
        attempt: int = 0,
        max_attempts: int = 3,
        course_index: int = 0,
        course_total: int = 0,
        connected: bool = False,
        error_code: str = "",
    ) -> None:
        """Publish safe login progress without exposing credentials or raw errors."""
        with self._lock:
            self._login_status = {
                "state": state,
                "step": step,
                "attempt": max(0, int(attempt)),
                "max_attempts": max(1, int(max_attempts)),
                "course_index": max(0, int(course_index)),
                "course_total": max(0, int(course_total)),
                "connected": bool(connected),
                "message": str(message or "")[:160],
                "updated_at": int(time.time() * 1000),
            }
            if error_code:
                self._login_status["error_code"] = str(error_code)[:80]

    def _set_catalog_status(
        self,
        state: str,
        code: str,
        *,
        actions: list[str] | None = None,
        route_class: str = "",
        partial_failures: list[str] | None = None,
        diagnostics: dict[str, object] | None = None,
    ) -> None:
        """Publish a closed catalog state without upstream request details."""
        with self._lock:
            self._catalog_status = {
                "state": str(state or "degraded")[:24],
                "code": str(code or "catalog_payload_invalid")[:80],
                "actions": list(actions or []),
                "route_class": str(route_class or "")[:24],
                "partial_failures": [
                    str(value)[:80] for value in (partial_failures or [])[:8]
                ],
                "updated_at": int(time.time() * 1000),
            }
            if diagnostics:
                self._catalog_status["diagnostics"] = _closed_catalog_diagnostics(
                    diagnostics
                )

    def _stash_catalog_route_diagnostics(self, value: dict[str, object]) -> None:
        """Keep one closed-set route diagnostics record for the next status publish."""
        with self._lock:
            self._pending_catalog_diagnostics = _closed_catalog_diagnostics(value)

    def _consume_catalog_route_diagnostics(self) -> dict[str, object]:
        with self._lock:
            value = self._pending_catalog_diagnostics
            self._pending_catalog_diagnostics = {}
        return value if isinstance(value, dict) else {}

    def _reset_catalog_status(self) -> None:
        with self._lock:
            self._catalog_generation += 1
            self._pending_catalog_diagnostics = {}
            self._set_catalog_status(
                "idle",
                "fudan_login_required",
                actions=["login"],
            )

    def _mark_client_ready(self, message: str = "iCourse 已连接") -> None:
        with self._lock:
            current = dict(self._login_status)
            self._session_ever_connected = True
        if current.get("state") == "loading" and current.get("step") == "courses":
            return
        self._set_login_status("ready", "icourse", message, connected=True)





    def _hydrate_ticket_memory(self) -> None:
        """把持久化的登录 last-success 候选灌回进程内记忆（尽力而为）。

        仅当进程内记忆为空时生效；持久值闭集校验失败或代际不匹配即惰性
        弃用（不写回、不清理），任何读取失败静默返回。
        """
        if _LAST_TICKET_SUCCESS.get("route") and _LAST_TICKET_SUCCESS.get("transport"):
            return
        try:
            memory = self.network.load_ticket_memory()
        except Exception:
            return
        if memory:
            _LAST_TICKET_SUCCESS.update(
                route=memory["route"], transport=memory["transport"]
            )

    def _persist_ticket_memory(self) -> None:
        """尽力而为地把进程内 last-success 记忆落盘；绝不影响登录结果。"""
        route = _LAST_TICKET_SUCCESS.get("route")
        transport = _LAST_TICKET_SUCCESS.get("transport")
        if not (route and transport):
            return
        try:
            self.network.save_ticket_memory(route, transport)
        except Exception:
            pass  # 记忆只是加速路径；落盘失败不影响已建立的会话

    def _login_with_retry(
        self,
        max_attempts: int = 3,
        *,
        total_deadline_seconds: float = 90.0,
    ):
        from src.api.icourse import ICourseClient
        from src.api.webvpn import WebVPNSession
        from requests import RequestException, Timeout

        student_id = self._credentials.get("student_id", "").strip()
        password = self._credentials.get("password", "")
        if not student_id or not password:
            # LV1-4（LIVE-VALIDATE-1）：无凭据不是载荷异常——带显式闭集码，
            # 目录面按 login_required 归位，认证面落回等待登录终态。
            error = ValueError("Please set student ID and UIS password first.")
            error.code = "fudan_login_required"
            raise error

        # P2-E：航班级去标识计数（总数 + 本进程已有会话后的再认证航班）。
        with self._lock:
            reauth_flight = bool(self._session_ever_connected)
        self._bump_campus_metrics(
            auth_flights_total=1, reauth_flights=1 if reauth_flight else 0
        )
        deadline = time.monotonic() + max(1.0, float(total_deadline_seconds))
        last_error: Exception | None = None
        telemetry = LoginStageTelemetry()
        # P0.2：auto 模式候选来自 service_proxies（直连 + 显式本机代理，顺序由
        # 无凭据探测决策）；direct/manual 模式仍只有用户明确允许的一条路径。
        routes = self.network.service_proxies("webvpn")
        from src.api.webvpn import TICKET_TRANSPORTS

        # 候选排序（确定性，闭集）：优先进程内 last-success 组合，其后按原顺序。
        # route 与 transport 是两个独立维度，一次失败只推进一个维度。
        preferred_route = _LAST_TICKET_SUCCESS.get("route")
        preferred_transport = _LAST_TICKET_SUCCESS.get("transport")
        if preferred_route == "proxy" and routes and routes[0] == "":
            routes = [routes[1], routes[0]] if len(routes) > 1 else routes
        elif preferred_route == "direct" and routes and routes[0] != "":
            routes = [routes[-1]] + routes[:-1] if len(routes) > 1 else routes
        main_transport = (
            preferred_transport
            if preferred_transport in TICKET_TRANSPORTS
            else TICKET_TRANSPORTS[0]
        )
        route_index = 0
        transport = main_transport
        route_switches = 0
        for attempt in range(1, max_attempts + 1):
            step = "webvpn"
            proxy_url = routes[route_index % len(routes)]
            telemetry.set_attempt(attempt)
            try:
                self._set_login_status(
                    "connecting",
                    "webvpn",
                    "正在连接 WebVPN",
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
                vpn = WebVPNSession(
                    step_callback=telemetry,
                    proxy_url=proxy_url,
                    transport=transport,
                )
                begin_authentication = getattr(vpn, "begin_authentication", None)
                if callable(begin_authentication):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise Timeout("authentication deadline exceeded")
                    begin_authentication(min(float(vpn.AUTH_DEADLINE_SECONDS), remaining))
                print(f"[FudanCourseLens] WebVPN login attempt {attempt}/{max_attempts}...")
                vpn.login(student_id=student_id, password=password)
                step = "icourse"
                self._set_login_status(
                    "connecting",
                    "icourse",
                    "WebVPN 已连接，正在认证 iCourse",
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
                vpn.authenticate_icourse(student_id=student_id, password=password)
                end_authentication = getattr(vpn, "end_authentication", None)
                if callable(end_authentication):
                    end_authentication()
                _LAST_TICKET_SUCCESS.update(
                    route="proxy" if proxy_url else "direct",
                    # 记录实际使用的 transport：预热失败时 session 内部可能已
                    # 换到备用类别（prepare_ticket_transport 的 in-attempt 切换）
                    transport=getattr(vpn, "_transport_class", transport),
                )
                self._persist_ticket_memory()
                with self._lock:
                    self._webvpn_route = "local_proxy" if proxy_url else "direct"
                # P3.1：登录成功验证了该路由——记录为最后验证路由并清冷却。
                self.network.note_route_success("webvpn", proxy_url)
                # V5 会话检查点：登录成功后尽力保存一份最小 DPAPI 加密快照。
                # 纯尽力而为：捕获/加密/落盘的任何失败都绝不影响本次登录结果。
                try:
                    cookies = vpn.capture_session_checkpoint_cookies()
                    if cookies:
                        self.credentials.save_session_checkpoint(student_id, cookies)
                except Exception:
                    pass  # 检查点只是加速路径；保存失败不影响已建立的会话
                return ICourseClient(vpn)
            except Exception as exc:
                if "vpn" in locals():
                    try:
                        vpn.close()
                    except (AttributeError, OSError):
                        pass
                last_error = exc
                explicit_code = str(getattr(exc, "code", "") or "")
                error_code = (
                    "timeout" if explicit_code == "timeout" or _exception_chain_contains(exc, Timeout)
                    else explicit_code if explicit_code
                    else "network_unavailable" if isinstance(exc, RequestException)
                    else "fudan_login_failed"
                )
                # 终止性失败闭集（P0.2）：凭据拒绝、交互式挑战、不安全跳转、
                # 票链已消费——立即终止，绝不换路由、绝不重试、绝不重放 ticket。
                if explicit_code in _LOGIN_TERMINAL_CODES:
                    try:
                        # 服务端拒绝/挑战/维护：丢弃已保存的会话检查点。
                        # 恢复航班的探针本来也会拒绝它，这里只是提前清场。
                        self.credentials.clear_session_checkpoint(student_id)
                    except Exception:
                        pass
                    # P3.1：凭据拒绝/挑战/锁定/维护/不安全跳转/ticket 已消费
                    # 都与路径无关——路由健康记忆立即失效（含活跃冷却）。
                    try:
                        self.network.clear_route_health()
                    except Exception:
                        pass
                    self._set_login_status(
                        "error",
                        step,
                        _LOGIN_TERMINAL_MESSAGES.get(
                            explicit_code, "登录失败，请检查账号、密码或网络状态"
                        ),
                        attempt=attempt,
                        max_attempts=max_attempts,
                        error_code=explicit_code,
                    )
                    break
                # ticket 消费前的传输失败：把失败路径从决策缓存中失效，并向
                # 其他允许路径切换至多一次（既有 3 次有界重试保持不变）。
                # P3.1：冷却窗口内的重复失败被抑制（迟滞）——只计数，不再
                # 翻转跨请求的候选顺序；航班内的一次换路预算不受影响。
                if explicit_code == "timeout" or isinstance(exc, RequestException):
                    flip_armed = self.network.note_route_failure("webvpn", proxy_url)
                    if not flip_armed:
                        self._bump_campus_metrics(route_flap_suppressed=1)
                    if route_switches < 1 and len(routes) > 1 and route_index < len(routes) - 1:
                        route_switches += 1
                        route_index += 1
                        # P2-E：一次换路尝试计数（换路失败/成功由后续状态承载）。
                        self._bump_campus_metrics(route_fallback_attempts=1)
                delay = min(2.0, float(attempt))
                can_retry = attempt < max_attempts and time.monotonic() + delay < deadline
                if can_retry:
                    self._set_login_status(
                        "retrying",
                        step,
                        "连接失败，准备重试",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        error_code=error_code,
                    )
                    print(f"[FudanCourseLens] Login failed: {_safe_error_message(exc)}; retrying...")
                    time.sleep(delay)
                else:
                    self._set_login_status(
                        "error",
                        step,
                        "登录失败，请检查账号、密码或网络状态",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        error_code=error_code,
                    )
        raise last_error or RuntimeError("Login failed")

    def _restore_client_from_checkpoint(self):
        """Best-effort restore of one DPAPI session checkpoint; never raises.

        Must run while holding _client_refresh_lock (it is called only from
        client()'s refresh path), so concurrent startup callers share this
        single restore flight. Any absence, expiry, binding mismatch,
        corruption or server probe rejection discards the checkpoint and
        returns None so the ordinary login flow runs unchanged.
        """
        with self._lock:
            student_id = str(self._credentials.get("student_id") or "").strip()
        if not student_id:
            return None
        try:
            cookies = self.credentials.load_session_checkpoint(student_id)
        except Exception:
            return None
        if not cookies:
            return None
        self._set_login_status(
            "connecting", "webvpn", "正在尝试恢复本机加密校园会话",
        )
        from src.api.webvpn import TICKET_TRANSPORTS, WebVPNSession

        # 进程内记忆为空时先尝试持久候选（复用 __init__ 的同一 hydrate 函数）。
        self._hydrate_ticket_memory()

        vpn = None
        restored = False
        try:
            proxy_url = (self.network.service_proxies("webvpn") or [""])[0]
            transport = _LAST_TICKET_SUCCESS.get("transport")
            if transport not in TICKET_TRANSPORTS:
                transport = TICKET_TRANSPORTS[0]
            vpn = WebVPNSession(proxy_url=proxy_url, transport=transport)
            begin_authentication = getattr(vpn, "begin_authentication", None)
            if callable(begin_authentication):
                begin_authentication(_CHECKPOINT_RESTORE_DEADLINE_SECONDS)
            restored = bool(vpn.restore_session_checkpoint(cookies))
            if restored:
                vpn.logged_in = True
                _LAST_TICKET_SUCCESS.update(
                    route="proxy" if proxy_url else "direct",
                    transport=getattr(vpn, "_transport_class", transport),
                )
                self._persist_ticket_memory()
                with self._lock:
                    self._webvpn_route = "local_proxy" if proxy_url else "direct"
                # P3.1：恢复成功同样验证了该路由（最后验证路由记忆）。
                self.network.note_route_success("webvpn", proxy_url)
        except Exception:
            restored = False
        if not restored:
            if vpn is not None:
                try:
                    vpn.close()
                except (AttributeError, OSError):
                    pass
            try:
                self.credentials.clear_session_checkpoint(student_id)
            except Exception:
                pass
            # P2-E：恢复失败落回普通登录（去标识计数）。
            self._bump_campus_metrics(checkpoint_restores_fail=1)
            return None
        # P2-E：检查点恢复成功——免密码重启路径（去标识计数）。
        self._bump_campus_metrics(checkpoint_restores_ok=1)
        from src.api.icourse import ICourseClient

        return ICourseClient(vpn)

    def _maybe_note_host_resume(self) -> bool:
        """Lazy host sleep/resume detection at existing call points (P3.1).

        只在既有入口（client() / connection_snapshot() / campus_diagnostics()）
        比较上次观察：墙钟超前单调钟超过阈值，或调用点之间长时间静默，判定
        一次宿主休眠恢复并把它作为路由代际事件（复用 NetworkSettings 的
        P0.3 钩子——递增持久化代际、清空探测决策与健康记忆）。旧客户端只被
        标记为过期，绝不关闭：在途请求继续使用自己的引用，下一个受保护动作
        才按新代际重建。判定失败静默：恢复检测绝不允许打断调用方。
        """
        now_mono = time.monotonic()
        now_wall = time.time()
        with self._lock:
            last = self._host_activity_observed
            self._host_activity_observed = (now_mono, now_wall)
        if last is None or now_mono < last[0]:
            return False
        drift = (now_wall - last[1]) - (now_mono - last[0])
        if (
            drift < _HOST_RESUME_DRIFT_SECONDS
            and (now_mono - last[0]) < _HOST_RESUME_IDLE_SECONDS
        ):
            return False
        try:
            generation = self.network.note_resume()
        except Exception:
            return False
        with self._lock:
            self._route_generation = max(
                int(self._route_generation or 0), int(generation)
            )
            # REALRUN-1 旅程6：宿主恢复=恢复事件——自愈看门狗退避复位为一拍，
            # 校园会话重验最迟 ≤30s 内发起，不再沿用断网期放大的旧退避。
            self._session_self_heal_backoff = SESSION_SELF_HEAL_TICK_SECONDS
        return True

    def _bind_safe_get_recovery(self, client) -> None:
        """Attach the read-only safe-GET recovery hook to the live vpn (P3.1).

        绑定失败绝不影响会话本身（测试替身或不可变替身直接跳过）。
        """
        try:
            vpn = getattr(client, "vpn", None)
            if vpn is not None and hasattr(vpn, "__dict__"):
                vpn.safe_get_recovery = self._vpn_for_safe_get_recovery
        except Exception:
            pass

    def _vpn_for_safe_get_recovery(self):
        """One verified session-epoch recovery for read-only safe GETs (P3.1).

        复用既有账号/路由/会话纪元：client(verify_before_reuse=True) 走既有
        单航班（存活校验 → 检查点恢复 → 有界登录，航班内至多一次换路）。
        凭据/挑战/身份/维护/不安全跳转/已消费 ticket 等终止性失败在这里以
        异常结束并折损为 None——绝不额外重试。返回已验证的 vpn（可能已
        重建）或 None。
        """
        try:
            client = self.client(verify_before_reuse=True)
        except Exception:
            return None
        vpn = getattr(client, "vpn", None)
        if vpn is None or not bool(getattr(vpn, "logged_in", False)):
            return None
        return vpn

    def client(self, *, verify_before_reuse: bool = False) -> ICourseClient:
        # P3.1：宿主恢复在既有调用点惰性判定——恢复后旧客户端/旧路由决策
        # 不再被信任，但绝不打断在途请求（只标记代际过期）。
        self._maybe_note_host_resume()
        with self._lock:
            current = self._client
            verified_at = self._client_last_verified_at
            stale_generation = (
                current is not None and self._client_generation != self._route_generation
            )
        if current is not None and not stale_generation:
            if not verify_before_reuse and time.monotonic() - verified_at < CLIENT_REUSE_SECONDS:
                self._mark_client_ready()
                return current
            verdict = _client_alive_verdict(current)
            if verdict is None:
                # F1-R：探活不可判（传输失败/网关 5xx）——保留会话，仅推进
                # 探活时钟以限定探测节奏；绝不因此重建（重建=全量重登录）。
                with self._lock:
                    if self._client is current:
                        self._client_last_verified_at = time.monotonic()
                return current
            if verdict:
                with self._lock:
                    if self._client is current:
                        self._client_last_verified_at = time.monotonic()
                self._mark_client_ready()
                return current

        # P0.4 单一认证航班：并发调用方在 _client_refresh_lock 上排队等待同一次
        # 结果；失败按排队等待者数量精确共享，绝不级联出重复登录或重复弹窗。
        # P0.3：代际不一致的客户端不重用也不复验——必须按新路由代际重建。
        with self._lock:
            self._refresh_waiters += 1
            # P2-E：注册时已有其他航班在途 → 本调用者将共享同一次航班结果。
            queued_behind_flight = self._refresh_waiters > 1
        consumed_waiter = False
        try:
            with self._client_refresh_lock:
                with self._lock:
                    if not consumed_waiter:
                        self._refresh_waiters -= 1
                        consumed_waiter = True
                    current = self._client
                    verified_at = self._client_last_verified_at
                    stale_generation = (
                        current is not None
                        and self._client_generation != self._route_generation
                    )
                if current is not None and not stale_generation:
                    if not verify_before_reuse and time.monotonic() - verified_at < CLIENT_REUSE_SECONDS:
                        if queued_behind_flight:
                            self._bump_campus_metrics(duplicate_flight_waiters=1)
                        self._mark_client_ready()
                        return current
                    verdict = _client_alive_verdict(current)
                    if verdict is None:
                        # F1-R：探活不可判（传输失败/网关 5xx）——保留会话，
                        # 仅推进探活时钟；绝不因此重建（重建=全量重登录）。
                        with self._lock:
                            if self._client is current:
                                self._client_last_verified_at = time.monotonic()
                        if queued_behind_flight:
                            self._bump_campus_metrics(duplicate_flight_waiters=1)
                        return current
                    if verdict:
                        with self._lock:
                            if self._client is current:
                                self._client_last_verified_at = time.monotonic()
                        if queued_behind_flight:
                            self._bump_campus_metrics(duplicate_flight_waiters=1)
                        self._mark_client_ready()
                        return current
                with self._lock:
                    failure = self._refresh_failure
                    share_failure = failure is not None and failure[1] > 0
                    if share_failure:
                        remaining = failure[1] - 1
                        self._refresh_failure = (failure[0], remaining) if remaining else None
                if share_failure:
                    if queued_behind_flight:
                        self._bump_campus_metrics(duplicate_flight_waiters=1)
                    raise failure[0]
                with self._lock:
                    current = self._client
                    if current is not None and self._client is current:
                        self._client = None
                        self._vpn = None
                        self._client_last_verified_at = 0.0
                if current is not None:
                    try:
                        current.close()
                    except (AttributeError, OSError):
                        pass
                # P2-A：首次受保护动作/复验前的有界无凭据预检（代际 + 短 TTL 去重，
                # 绝不阻塞 UI 启动）；探测失败静默，登录按既有候选顺序继续。
                self._preflight_campus_route()
                # V5 会话检查点：先尝试一次单航班的加密会话恢复；任何失败
                # （无检查点/过期/绑定不符/损坏/服务端拒绝）都在这里丢弃并
                # 落回下方普通登录，两条路径不会叠加。
                restored = self._restore_client_from_checkpoint()
                if restored is not None:
                    with self._lock:
                        self._client = restored
                        self._vpn = restored.vpn
                        self._client_last_verified_at = time.monotonic()
                        self._client_generation = self._route_generation
                        self._refresh_failure = None
                    self._bind_safe_get_recovery(restored)
                    self._mark_client_ready("已通过本机加密校园会话恢复连接")
                    return restored
                try:
                    client = self._login_with_retry()
                except Exception as exc:
                    with self._lock:
                        self._refresh_failure = (exc, max(0, self._refresh_waiters))
                    # LV1-4（LIVE-VALIDATE-1）：无凭据失败必须落回等待登录终态。
                    # 否则「connecting」永不前进——认证面永挂 checking（无任何
                    # 动作按钮）、目录镜像 checking、self-heal 只认 error 不认
                    # connecting，学生只剩重启一条路。
                    if not (self._credentials.get("student_id", "").strip() and self._credentials.get("password", "")):
                        self._set_login_status("idle", "credentials", "等待登录")
                    raise
                with self._lock:
                    self._client = client
                    self._vpn = client.vpn
                    self._client_last_verified_at = time.monotonic()
                    self._client_generation = self._route_generation
                    self._refresh_failure = None
                self._bind_safe_get_recovery(client)
                self._mark_client_ready("登录成功，iCourse 已连接")
                return client
        finally:
            if not consumed_waiter:
                # 拿到刷新锁之前异常退出的兜底：不得让等待者计数泄漏。
                with self._lock:
                    self._refresh_waiters = max(0, self._refresh_waiters - 1)

    def _nudge_lapsed_verification(self) -> None:
        """快照发现复用窗口过期时借既有 nudge 机制补一次后台校验。

        状态轮询因此成为保活触发点之一；nudge 自身有在途旗标去重，
        这里的兜底只是保证快照永不因保活路径而失败。
        """
        try:
            self.nudge_client_verification()
        except Exception:
            pass

    def nudge_client_verification(self) -> None:
        """Refresh the reuse window from a heartbeat before it lapses.

        纯墙钟的 reuse 窗口在 300 秒无调用后会把仍存活的会话误报为需登录；
        心跳路径在剩余寿命不足 60 秒时后台触发一次真实校验（alive → 续窗），
        失败静默：任何异常都不允许打断心跳响应。
        """
        with self._lock:
            if self._client_nudge_in_flight:
                return
            if not self._client or not self._login_status.get("connected"):
                return
            remaining = CLIENT_REUSE_SECONDS - max(
                0.0, time.monotonic() - float(self._client_last_verified_at or 0.0)
            )
            if remaining >= 60.0:
                return
            self._client_nudge_in_flight = True
            self._client_revalidate_started_at = time.monotonic()

        def _verify() -> None:
            try:
                self.client(verify_before_reuse=True)
            except Exception:
                pass  # 后台保活：失败路径（退避重登）已在 client() 内落状态，这里保持静默
            finally:
                with self._lock:
                    self._client_nudge_in_flight = False
                    self._client_revalidate_started_at = 0.0

        thread = threading.Thread(target=_verify, name="client-keepalive-nudge", daemon=True)
        try:
            thread.start()
        except Exception:
            # 启动失败必须释放旗标，否则在途旗标卡死会让后续保活永久失效。
            with self._lock:
                self._client_nudge_in_flight = False
                self._client_revalidate_started_at = 0.0

    def _start_session_self_heal(self) -> None:
        """Start at most one degraded self-heal watchdog per process (SRC-SYNDROME-1 U2).

        传输类登录失败（timeout/network_unavailable/ticket_transport_failed 等）
        落 error 后原本零自愈——夜间一次 ReadTimeout 让 degraded 挂到用户手动
        介入（第廿六案实锤）。看门狗每 tick 检查一次：仅当登录态为 error、凭据
        在场且 error_code 不属身份源终态闭集时，发起一次既有单航班重登；失败
        指数退避封顶。身份源终态（凭据拒绝/挑战/锁定/不安全跳转/ticket 拒绝）
        唯一动作是用户介入，永不重试。
        """
        with self._lock:
            if self._session_self_heal_started:
                return
            self._session_self_heal_started = True
        thread = threading.Thread(
            target=self._session_self_heal_loop, name="session-self-heal", daemon=True
        )
        with self._lock:
            self._session_self_heal_thread = thread
        try:
            thread.start()
        except Exception:
            with self._lock:
                self._session_self_heal_started = False

    def _session_self_heal_loop(self) -> None:
        # N9-H：tick 用 Event.wait 等价替换 time.sleep——节奏/退避语义不变，
        # 但 close() 置停后最迟一个 tick 内退场（P63 收口模式对齐）。
        # REALRUN-1 旅程6：退避读实例态——恢复事件可在 tick 之间把它复位。
        stop = self._session_self_heal_stop
        while not stop.is_set():
            with self._lock:
                backoff = float(self._session_self_heal_backoff)
            if stop.wait(backoff):
                break
            self._session_self_heal_once()

    def _session_self_heal_once(self, backoff: float | None = None) -> float:
        """One bounded watchdog tick; returns the next backoff in seconds.

        REALRUN-1 旅程6（2026-10-08 真测）：断网恢复后 degraded 长挂不自愈——
        旧实现退避只在调用栈内单调放大（封顶 600s）且无任何恢复事件复位，
        恢复后一次重验最长要等 10 分钟。现退避落实例态：①宿主恢复事件复位
        为一拍（``_maybe_note_host_resume``）；②已放大的退避在到点 tick 先做
        一次有界无凭据探测，任一校园路径恢复即复位并立即发起既有单航班重验；
        探测确认仍不可达则本次不发起注定失败的登录航班（更轻，也少打校园）。
        ``backoff`` 形参保留为既有单测播种入口（None=纯实例态推进）。
        """
        with self._lock:
            if backoff is not None:
                self._session_self_heal_backoff = float(backoff)
            login = dict(self._login_status)
            has_credentials = bool(
                self._credentials.get("student_id") and self._credentials.get("password")
            )
            current_backoff = float(self._session_self_heal_backoff)
        if login.get("state") != "error" or not has_credentials:
            with self._lock:
                self._session_self_heal_backoff = SESSION_SELF_HEAL_TICK_SECONDS
            return SESSION_SELF_HEAL_TICK_SECONDS
        if str(login.get("error_code") or "") in _SESSION_SELF_HEAL_SKIP_CODES:
            return current_backoff  # 身份源终态：状态如实呈现，等用户动作
        if current_backoff > SESSION_SELF_HEAL_TICK_SECONDS:
            recovered = self._campus_path_recovered()
            if recovered is True:
                current_backoff = SESSION_SELF_HEAL_TICK_SECONDS
                with self._lock:
                    self._session_self_heal_backoff = SESSION_SELF_HEAL_TICK_SECONDS
            elif recovered is False:
                with self._lock:
                    self._session_self_heal_backoff = min(
                        current_backoff * 2.0, SESSION_SELF_HEAL_MAX_BACKOFF_SECONDS
                    )
                # 校园路径仍未恢复：本次不发起注定失败的登录航班，退避继续。
                return float(self._session_self_heal_backoff)
        try:
            self.client(verify_before_reuse=True)
        except Exception:
            with self._lock:
                self._session_self_heal_backoff = min(
                    self._session_self_heal_backoff * 2.0, SESSION_SELF_HEAL_MAX_BACKOFF_SECONDS
                )
            return float(self._session_self_heal_backoff)
        # 成功路径由 _mark_client_ready 落 ready/connected；闭集行给 tee 日志留证据
        with self._lock:
            self._session_self_heal_backoff = SESSION_SELF_HEAL_TICK_SECONDS
        print("[FudanCourseLens] session self-heal: reconnected", flush=True)
        return SESSION_SELF_HEAL_TICK_SECONDS

    def _campus_path_recovered(self) -> bool | None:
        """Bounded uncredentialled reachability re-probe（REALRUN-1 旅程6 修复）.

        auto 模式：``refresh_route_decision`` 对两条允许路径做无凭据成对探测
        （有界超时，兼喂决策缓存）；任一路径健康=恢复证据 True，双双失败=
        False。非 auto 模式按既有设计不探测（空证据）→ None：不发明证据，
        保持既有重验节拍。探测自身异常同样 None——绝不打断看门狗节拍。
        """
        try:
            evidence = self.network.refresh_route_decision("webvpn")
        except Exception:
            return None
        if not isinstance(evidence, dict):
            return None
        decision = str(evidence.get("decision") or "unknown")
        if decision == "unknown":
            return None
        return bool(evidence.get("direct_ok") or evidence.get("proxy_ok"))

    def set_credentials(self, student_id: str, password: str, remember: bool = False) -> None:
        """Store credentials in memory for this local server process."""
        student_id = (student_id or "").strip()
        # F13（N5FE-P3）：字面 "undefined"/"null" 是前端脏值，绝不作为账号落盘
        if not student_id or student_id.lower() in {"undefined", "null"} or not password:
            raise ValueError("student_id and password are required")
        self._discard_platform_client()
        with self._lock:
            self._credentials = {
                "student_id": student_id,
                "password": password,
            }
            if remember:
                self.credentials.save(student_id, password)
        self._reset_catalog_status()
        self._set_login_status("configured", "credentials", "登录信息已就绪")

    def use_saved_credentials(self, student_id: str) -> None:
        saved_student_id, password = self.credentials.load(student_id)
        self.set_credentials(saved_student_id, password, remember=False)

    def delete_saved_credentials(self, student_id: str) -> bool:
        student_id = (student_id or "").strip()
        deleted = self.credentials.delete_account(student_id)
        try:
            self.credentials.clear_session_checkpoint(student_id)
        except Exception:
            pass
        clear_current = False
        with self._lock:
            if self._credentials.get("student_id") == student_id:
                self._credentials = {"student_id": "", "password": ""}
                clear_current = True
        if clear_current:
            self._discard_platform_client()
            # P2-C：删除当前账号即身份边界事件——陈旧目录视图随账号一起清除。
            self._discard_authorized_catalog_cache()
            self._reset_catalog_status()
            self._set_login_status("idle", "credentials", "等待登录")
        return deleted

    def set_deepseek_key(self, api_key: str, remember: bool = False) -> None:
        api_key = (api_key or "").strip()
        if not api_key:
            raise ValueError("DeepSeek API key is required")
        with self._lock:
            self._deepseek_api_key = api_key
            if remember:
                self.credentials.save_deepseek_key(api_key)
            # remember=False 仅设置会话 key；已持久化的本机 key 只能通过
            # delete_deepseek_key() 显式移除，不得在未勾选保存时被静默删除。

    def delete_deepseek_key(self) -> bool:
        deleted = self.credentials.delete_deepseek_key()
        with self._lock:
            self._deepseek_api_key = ""
        return deleted

    def _deepseek_key(self) -> str:
        with self._lock:
            key = (self._deepseek_api_key or "").strip()
        if key:
            return key
        try:
            key = self.credentials.load_deepseek_key()
        except (KeyError, RuntimeError):
            return ""
        with self._lock:
            self._deepseek_api_key = key
        return key

    def has_deepseek_key(self) -> bool:
        """Report key availability without exposing the saved secret."""
        return bool(self._deepseek_key())

    def config_snapshot(self) -> dict:
        with self._lock:
            return {
                "has_student_id": bool(self._credentials.get("student_id")),
                "student_id": self._credentials.get("student_id", ""),
                "has_password": bool(self._credentials.get("password")),
                "has_deepseek_key": bool(self._deepseek_api_key) or self.credentials.has_deepseek_key(),
                "has_saved_deepseek_key": self.credentials.has_deepseek_key(),
                "deepseek_key_requires_rotation": self.credentials.deepseek_key_requires_rotation(),
                "player": {"kind": "browser", "label": "Online playback", "available": True},
                "output_dir": str(self.output_dir),
                "saved_accounts": self.credentials.list_accounts(),
                "onboarding": self.onboarding_snapshot(),
            }

    def discover_course(self, course_id: str) -> dict:
        client = self.client()
        detail = client.get_course_detail(str(course_id))
        self.catalog_repository.upsert_course(str(course_id), detail["title"], detail["teacher"])
        for lecture in detail["lectures"]:
            if lecture.get("has_playback"):
                self.catalog_repository.upsert_lecture(str(course_id), lecture)
        result = {
            "course_id": str(course_id),
            "title": detail["title"],
            "teacher": detail["teacher"],
            "lectures": [
                self._public_lecture(str(course_id), lecture)
                for lecture in detail["lectures"]
                if lecture.get("has_playback")
            ],
        }
        self._request_search_refresh(
            [str(lecture.get("sub_id")) for lecture in result["lectures"]]
        )
        metadata = next((
            item for item in self._authorized_catalog_courses_raw()
            if str(item.get("course_id") or "") == str(course_id)
        ), {})
        result.update({
            "term": str(metadata.get("term") or metadata.get("semester") or ""),
            "department": str(metadata.get("department") or metadata.get("faculty") or ""),
        })
        return self.timetable.enrich_catalog([result])[0]


    def _public_lecture(
        self, course_id: str, lecture: dict, *, resolve_stored: bool = True,
    ) -> dict:
        row = (
            self.catalog_repository.get_lecture(str(lecture.get("sub_id"))) or {}
            if resolve_stored
            else lecture
        )
        merged = {**lecture, **row}
        public_fields = {
            "sub_id", "sub_title", "date", "has_playback", "duration_seconds",
            "subtitle_status", "subtitle_mode", "summary_status",
            "summary_artifact_status", "learning_status", "watched_percent",
        }
        public = {
            key: merged.get(key)
            for key in public_fields
            if key in merged
        }
        public["course_id"] = str(course_id)
        with self._lock:
            authenticated = bool(
                self._credentials.get("student_id") and self._credentials.get("password")
            )
        has_playback = bool(public.get("has_playback", True))
        public.update({
            "media_source": "remote" if has_playback and authenticated else "auth_required" if has_playback else "unavailable",
            "can_stream": bool(has_playback and authenticated),
        })
        return public

    @staticmethod
    def _reject_reused_parked_task(task: dict) -> None:
        """Refuse to silently reuse a parked task that cannot make progress.

        ``add_task`` dedupes on kind+sub_id+config_key, so a new dispatch for
        the same excerpt would otherwise return a paused task whose recovery
        material is gone, swallowing the user's request without any signal.
        """
        if (
            str(task.get("state") or "") == "paused"
            and str(task.get("error") or "") == "remote_recovery_material_unavailable"
        ):
            raise RuntimeError("task_already_active")

    def enqueue_subtitle(
        self,
        course_id: str,
        sub_id: str,
        *,
        start_seconds: float = 0.0,
        duration_seconds: float | None = None,
    ) -> dict:
        self._prepare_remote_coordinator()
        row = self.catalog_repository.get_lecture(sub_id)
        if not row or str(row.get("course_id")) != str(course_id):
            # tasks/enqueue 出口闭集只收 KeyError/RuntimeError/ValueError；
            # FileNotFoundError 在集外，会逃逸成 500 runtime_failed。
            error = RuntimeError("Lecture is not in the authorized catalog")
            error.code = "lecture_not_found"
            raise error
        source_kind = "remote-actions"
        with self._lock:
            has_credentials = bool(
                self._credentials.get("student_id") and self._credentials.get("password")
            )
        if not has_credentials:
            # NIGHT2-W7：闭集显式码随 HTTP 出口直达前端，不再被启发式粗映射吞成 task_failed
            error = RuntimeError("Sign in before starting remote subtitle computation")
            error.code = "fudan_login_required"
            raise error
        # One automatic policy label.  The ASR/proofreading behavior is
        # selected from configuration: DeepSeek AI proofreading when a usable
        # key is configured, otherwise the non-AI fallback recognition.
        subtitle_mode = "automatic"
        subtitle_proofread = bool(self._deepseek_key())
        requested_start = max(0.0, float(start_seconds or 0.0))
        requested_duration = (
            float(duration_seconds) if duration_seconds is not None else None
        )
        try:
            catalog_duration = max(0.0, float(row.get("duration_seconds") or 0.0))
        except (TypeError, ValueError):
            catalog_duration = 0.0
        effective_duration = requested_duration
        if effective_duration is None and catalog_duration > requested_start:
            # The authorized catalog already carries the platform duration.
            # Sending it avoids an extra probe against a short-lived signed
            # media URL while keeping the Worker fallback for missing metadata.
            effective_duration = catalog_duration - requested_start
        payload = {
            "course_id": str(course_id),
            "sub_id": str(sub_id),
            "subtitle_mode": subtitle_mode,
            "subtitle_proofread": subtitle_proofread,
            "source_kind": source_kind,
            "start_seconds": requested_start,
            "duration_seconds": effective_duration,
            "media_duration_seconds": effective_duration,
        }
        range_key = (
            f":{payload['start_seconds']:.3f}:{payload['duration_seconds']:.3f}"
            if payload["duration_seconds"] is not None
            and (duration_seconds is not None or requested_start > 0)
            else ""
        )
        task, created = self.task_store.add_task(
            "subtitle", str(course_id), str(sub_id), payload, config_key=subtitle_mode + range_key,
        )
        if not created:
            self._reject_reused_parked_task(task)
        if int((task.get("progress") or {}).get("schema_version") or 0) < 2:
            task = self.task_store.update_task(
                str(task["task_id"]),
                progress=queued_progress(
                    "subtitle", proofread=subtitle_proofread, label="字幕任务排队中"
                ),
            ) or task
        task = self._refresh_subtitle_task_estimate(task)
        self.catalog_repository.update_lecture_fields(
            sub_id,
            subtitle_status="queued",
            subtitle_step="Queued",
            subtitle_step_index=0,
            subtitle_step_count=0,
            subtitle_step_percent=0.0,
            subtitle_file_percent=0.0,
            subtitle_progress_label="Waiting for subtitle generation",
            subtitle_error="",
            subtitle_mode=subtitle_mode,
        )
        with self._lock:
            if task.get("state") == "queued" and not any(str(item.get("sub_id")) == str(sub_id) for item in self._subtitle_queue):
                self._subtitle_queue.append({**payload, "task_id": task["task_id"]})
            if task.get("state") == "queued":
                self._ensure_subtitle_worker()
        return task

    def _ensure_subtitle_worker(self) -> None:
        """SRC-SYNDROME-1 U5 并行派发：按队列深度补消费者，至多 cloud_run_limit 路。

        每个消费者同一时刻至多携带一个在飞字幕任务；跨字幕/摘要/问答的硬闸仍是
        既有 _cloud_run_slot 信号量（cloud_run_limit，默认 5 可配）。排队文案与
        可取消语义沿用；同任务防重仍由任务存储 find_active + 队列 sub_id 去重承担。
        """
        with self._lock:
            pending = len(self._subtitle_queue)
            if not pending:
                return
            spawns = min(pending, max(0, self._cloud_run_limit() - self._subtitle_workers))
            self._subtitle_workers += spawns
            seq_start = self._subtitle_worker_seq
            self._subtitle_worker_seq += spawns
        for offset in range(spawns):
            try:
                threading.Thread(
                    target=self._run_subtitle_queue,
                    name=f"subtitle-generator-{seq_start + offset + 1}",
                    daemon=True,
                ).start()
            except Exception:
                with self._lock:
                    self._subtitle_workers -= 1

    def _run_subtitle_queue(self) -> None:
        try:
            while True:
                with self._lock:
                    if not self._subtitle_queue:
                        if not self._active_subtitle_task_ids:
                            self._subtitle_current = None
                        return
                    if self._paused:
                        item = None
                    else:
                        item = self._subtitle_queue.pop(0)
                if item is None:
                    time.sleep(0.25)
                    continue
                self._process_subtitle_item(item)
        finally:
            with self._lock:
                self._subtitle_workers -= 1

    def _process_subtitle_item(self, item: dict[str, object]) -> None:
        course_id = str(item["course_id"])
        sub_id = str(item["sub_id"])
        subtitle_mode = "automatic"
        subtitle_proofread = bool(item.get("subtitle_proofread"))
        source_kind = "remote-actions"
        row = self.catalog_repository.get_lecture(sub_id) or {}
        task = self.task_store.get_task(str(item.get("task_id") or "")) or self.task_store.find_active("subtitle", sub_id)
        if not task or task.get("state") != "queued":
            return
        task_id = str(task["task_id"])
        self.task_store.update_task(task_id, state="running", started_at=time.time())
        cancel_event = threading.Event()
        with self._lock:
            self._active_subtitle_task_ids.add(task_id)
            self._subtitle_cancel_events[task_id] = cancel_event
            self._subtitle_current = {
                "task_id": task_id,
                "course_id": course_id,
                "sub_id": sub_id,
                "sub_title": row.get("sub_title", ""),
                "subtitle_progress_label": "Starting subtitle generation",
                "subtitle_file_percent": 0.0,
                "subtitle_step_percent": 0.0,
                "subtitle_step_index": 0,
                "subtitle_step_count": 0,
                "subtitle_step": "Starting",
                "subtitle_mode": subtitle_mode,
                "started_monotonic": time.monotonic(),
            }
            tracker = ProgressTracker(
                "subtitle",
                proofread=subtitle_proofread,
                initial_progress=task.get("progress") or {},
                initial_estimate=task.get("estimate"),
            )
            duration = self._subtitle_media_duration(task)
            if duration is not None:
                tracker.configure_duration_estimate(
                    duration,
                    historical_rtfs=self._subtitle_duration_rtfs(source_kind),
                    processed_media_seconds=(task.get("progress") or {}).get("processed_media_seconds"),
                    average_rtf=(task.get("progress") or {}).get("average_rtf"),
                    stable_media_events=(task.get("progress") or {}).get("media_events"),
                    phase_tail_seconds=self._subtitle_phase_tail_seconds(subtitle_proofread, source_kind),
                )
            self._progress_trackers[task_id] = tracker
            progress_value, estimate_value = tracker.update(
                "prepare", 0.0, label="正在准备字幕环境"
            )
        self.task_store.update_task(
            task_id, progress=progress_value, estimate=estimate_value
        )
        try:
            print(
                f"[FudanCourseLens] Starting subtitle task {task_id} ({subtitle_mode})",
                flush=True,
            )
            self._generate_subtitle_remote(
                course_id,
                sub_id,
                task_id=task_id,
                proofread=subtitle_proofread,
                start_seconds=float(item.get("start_seconds") or 0.0),
                duration_seconds=(
                    float(item["duration_seconds"])
                    if item.get("duration_seconds") is not None else None
                ),
            )
            duration = tracker.media_duration_seconds
            if duration:
                elapsed = max(0.001, tracker.elapsed)
                self.task_store.add_estimate_sample(
                    self._subtitle_duration_profile(source_kind),
                    elapsed / duration,
                    duration,
                    elapsed,
                    device_fingerprint=device_fingerprint(),
                )
                band_profile = self._subtitle_duration_band_profile(
                    source_kind, duration
                )
                if band_profile:
                    # Successful runs feed the finer duration-band bucket;
                    # failed/canceled attempts never record pace here.
                    self.task_store.add_estimate_sample(
                        band_profile,
                        elapsed / duration,
                        duration,
                        elapsed,
                        device_fingerprint=device_fingerprint(),
                    )
            completed_progress, completed_estimate = tracker.complete("字幕已生成")
            self.task_store.update_task(
                task_id,
                progress=completed_progress,
                estimate=completed_estimate,
            )
            self.task_store.mark_terminal(task_id, "completed")
            self._request_search_refresh([sub_id])
            print(f"[FudanCourseLens] Subtitle task completed: {task_id}", flush=True)
        except TaskPaused:
            latest = self.task_store.get_task(task_id) or {}
            canceled = bool(dict(latest.get("payload") or {}).get("cancel_requested"))
            paused_task = (
                self.task_store.mark_terminal(task_id, "canceled")
                if canceled else self.task_store.acknowledge_pause(task_id)
            ) or {}
            resuming = paused_task.get("state") == "queued"
            self.catalog_repository.update_lecture_fields(
                sub_id,
                subtitle_status="canceled" if canceled else ("queued" if resuming else "paused"),
                subtitle_progress_label=(
                    "字幕任务已取消" if canceled else
                    ("正在从检查点恢复字幕" if resuming else "已暂停：云端机器已停止，已完成部分保留")
                ),
                subtitle_error="",
            )
            action = "canceled" if canceled else ("resume queued" if resuming else "paused")
            print(f"[FudanCourseLens] Subtitle task {task_id}: {action}", flush=True)
        except Exception as exc:
            if _is_supervisor_busy(exc):
                # AS10（第五十案）：busy 处置共用化——陈旧租约自愈恰一次 +
                # 有界退避 + 诚实排队态，替代旧 5s 永动重排；51 真值链与
                # 显式暂停语义零触碰。
                self._handle_remote_supervisor_busy(task_id, kind="subtitle")
            elif self._defer_remote_task_failure(task_id, kind="subtitle", sub_id=sub_id):
                # 第四十二案：客户端侧闸门失败≠远端失败——远端 run 仍可能
                # 产出结果；任务保持可恢复，交给真值核对者收敛（helper 已记日志）
                pass
            else:
                message = _task_failure_message(exc)
                print(
                    f"[FudanCourseLens] Subtitle task failed: {task_id} ({message})",
                    flush=True,
                )
                self.catalog_repository.update_lecture_fields(
                    sub_id,
                    subtitle_status="failed",
                    subtitle_step="Failed",
                    subtitle_step_percent=0.0,
                    subtitle_error=message,
                    subtitle_progress_label="字幕生成失败，可重新点击生成字幕",
                )
                self.task_store.mark_terminal(task_id, "failed", error=message)
                if "remote_runner_lost" in str(message) and self._auto_retry_runner_lost_once(task_id):
                    # ⑫：runner 中途失联自动重试恰一次；字幕行随重排队列
                    # 恢复，不打失败终态
                    self.catalog_repository.update_lecture_fields(
                        sub_id,
                        subtitle_status="queued",
                        subtitle_step="Queued",
                        subtitle_error="",
                        subtitle_progress_label="云端机器临时掉线，不是你的操作问题；已自动重试一次",
                    )
        finally:
            self._progress_trackers.pop(task_id, None)
            with self._lock:
                self._active_subtitle_task_ids.discard(task_id)
                self._subtitle_cancel_events.pop(task_id, None)
                if self._subtitle_current and self._subtitle_current.get("task_id") == task_id:
                    self._subtitle_current = None
            current_task = self.task_store.get_task(task_id) or {}
            if current_task.get("state") == "queued" and not self._paused:
                with self._lock:
                    latest = self.task_store.get_task(task_id) or {}
                    if latest.get("state") == "queued" and not self._paused:
                        self._queue_persisted_task(latest)

    def enqueue_summary(
        self,
        course_id: str,
        sub_id: str,
        *,
        include_ppt: bool = True,
        force: bool = False,
    ) -> dict:
        self._prepare_remote_coordinator()
        row = self.catalog_repository.get_lecture(sub_id)
        if not row or str(row.get("course_id")) != str(course_id):
            # 同 enqueue_subtitle：闭集形态显式码，不让 FileNotFoundError 逃逸。
            error = RuntimeError("Lecture is not in the authorized catalog")
            error.code = "lecture_not_found"
            raise error
        subtitle_data = self.subtitle_segments(sub_id)
        if not subtitle_data.get("segments"):
            # 显式码随异常走（R3-12）：分诊映射器优先读 code，不再依赖
            # 中英文关键词嗅探消息文本——措辞任一侧变化不再静默降级。
            error = RuntimeError("Generate timestamped subtitles before creating an AI timestamp summary")
            error.code = "transcript_missing"
            raise error
        if not self._deepseek_key():
            error = RuntimeError("DeepSeek API key is required for AI timestamp summaries")
            error.code = "ai_key_missing"
            raise error
        with self._lock:
            has_credentials = bool(
                self._credentials.get("student_id") and self._credentials.get("password")
            )
        if include_ppt and not has_credentials:
            error = RuntimeError("iCourse credentials are required for PPT OCR")
            error.code = "fudan_login_required"
            raise error
        course = (self.catalog_repository.snapshot().get("courses") or {}).get(str(course_id)) or {}
        title = " · ".join(
            value for value in (
                str(course.get("title") or "").strip(),
                str(row.get("sub_title") or "").strip(),
            ) if value
        ) or str(sub_id)
        payload = {
            "course_id": str(course_id),
            "sub_id": str(sub_id),
            "title": title,
            "include_ppt": bool(include_ppt),
            "force": bool(force),
            "source_kind": "remote-actions",
        }
        config_key = "lecture-summary:ppt" if include_ppt else "lecture-summary:transcript"
        task, created = self.task_store.add_task(
            "summary",
            str(course_id),
            str(sub_id),
            payload,
            config_key=config_key,
        )
        if not created:
            self._reject_reused_parked_task(task)
        if int((task.get("progress") or {}).get("schema_version") or 0) < 2:
            task = self.task_store.update_task(
                str(task["task_id"]),
                progress=queued_progress(
                    "summary",
                    include_ppt=bool(include_ppt),
                    label="AI 时间戳总结排队中",
                ),
            ) or task
        task = self._seed_task_estimate(
            task,
            "summary:ppt" if include_ppt else "summary:transcript",
        )
        if task.get("state") in ACTIVE_STATES:
            self.catalog_repository.update_lecture_fields(
                sub_id,
                summary_status=str(task.get("state") or "queued"),
                summary_percent=float((task.get("progress") or {}).get("percent") or 0.0),
                summary_progress_label="AI 时间戳总结已加入队列",
                summary_error="",
            )
        with self._lock:
            if (
                task.get("state") == "queued"
                and not any(str(item.get("task_id")) == str(task["task_id"]) for item in self._summary_queue)
            ):
                self._summary_queue.append({**payload, "task_id": task["task_id"]})
            if task.get("state") == "queued":
                self._ensure_summary_worker()
        return task

    def _ensure_summary_worker(self) -> None:
        if self._summary_worker and self._summary_worker.is_alive():
            return
        self._summary_worker = threading.Thread(
            target=self._run_summary_queue,
            name="learning-summary-generator",
            daemon=True,
        )
        self._summary_worker.start()

    def _run_summary_queue(self) -> None:
        while True:
            if self._generation_workers_stop.is_set():
                with self._lock:
                    self._summary_current = None
                return
            with self._lock:
                if not self._summary_queue:
                    self._summary_current = None
                    return
                item = None if self._paused else self._summary_queue.pop(0)
            if item is None:
                self._generation_workers_stop.wait(0.25)
                continue
            task = self.task_store.get_task(str(item.get("task_id") or ""))
            if not task or task.get("state") != "queued":
                continue
            task_id = str(task["task_id"])
            sub_id = str(item["sub_id"])
            include_ppt = bool(item.get("include_ppt", True))
            profile = "summary:ppt" if include_ppt else "summary:transcript"
            tracker = ProgressTracker(
                "summary",
                include_ppt=include_ppt,
                prior_costs=self.task_store.estimate_samples(profile),
                initial_progress=task.get("progress") or {},
            )
            self._progress_trackers[task_id] = tracker
            initial_progress, initial_estimate = tracker.update(
                "input", 0.0, label="准备 AI 时间戳总结"
            )
            task = self.task_store.update_task(
                task_id,
                state="running",
                started_at=time.time(),
                progress=initial_progress,
                estimate=initial_estimate,
            ) or task
            self.catalog_repository.update_lecture_fields(
                sub_id,
                summary_status="running",
                summary_percent=0.0,
                summary_progress_label="准备 AI 课程笔记",
                summary_error="",
            )
            self._active_summary_task_id = task_id
            self._summary_cancel.clear()
            with self._lock:
                self._summary_current = {
                    "task_id": task_id,
                    "course_id": str(item["course_id"]),
                    "sub_id": sub_id,
                    "sub_title": str(item.get("title") or sub_id),
                    "summary_progress_label": "准备 AI 课程笔记",
                    "summary_percent": 0.0,
                    "summary_stage": "starting",
                }

            def on_progress(value: dict) -> None:
                stage = str(value.get("phase_id") or value.get("stage") or "input")
                label = str(value.get("label") or stage)
                phase_percent = value.get("phase_percent")
                if phase_percent is None and value.get("indeterminate"):
                    progress_value = {
                        "schema_version": 2,
                        "stage": stage,
                        "phase_id": stage,
                        "percent": None,
                        "phase_percent": None,
                        "label": label,
                        "indeterminate": True,
                    }
                    self.task_store.update_task(task_id, progress=progress_value, estimate={})
                    self.catalog_repository.update_lecture_fields(
                        sub_id,
                        summary_status="running",
                        summary_percent="",
                        summary_progress_label=label,
                        summary_error="",
                    )
                    with self._lock:
                        if self._summary_current and self._summary_current.get("task_id") == task_id:
                            self._summary_current.update({
                                "summary_percent": None,
                                "summary_progress_label": label,
                                "summary_stage": stage,
                            })
                    return
                if phase_percent is None:
                    phase_percent = value.get("percent") or 0.0
                progress_value, estimate_value = tracker.update(
                    stage,
                    float(phase_percent),
                    label=label,
                    completed_units=value.get("completed_units"),
                    total_units=value.get("total_units"),
                )
                self._persist_progress_samples(
                    tracker,
                    profile,
                    "ppt" if include_ppt else "transcript",
                )
                progress_value["stage"] = stage
                if isinstance(value.get("ppt"), dict):
                    progress_value["ppt"] = value["ppt"]
                percent = float(progress_value.get("percent") or 0.0)
                self.task_store.update_task(
                    task_id,
                    progress=progress_value,
                    estimate=estimate_value,
                )
                self.catalog_repository.update_lecture_fields(
                    sub_id,
                    summary_status="running",
                    summary_percent=round(percent, 1),
                    summary_progress_label=label,
                    summary_error="",
                )
                with self._lock:
                    if self._summary_current and self._summary_current.get("task_id") == task_id:
                        self._summary_current.update({
                            "summary_percent": round(percent, 1),
                            "summary_stage": stage,
                            "summary_progress_label": label,
                        })

            try:
                print(f"[FudanCourseLens] Starting AI notes task: {task_id}", flush=True)
                result = self._run_summary_remote(dict(item), on_progress=on_progress)
                completed_progress, completed_estimate = tracker.complete(
                    "AI 时间戳总结已完成"
                )
                self.task_store.update_task(
                    task_id,
                    progress=completed_progress,
                    estimate=completed_estimate,
                )
                elapsed = max(0.001, tracker.elapsed)
                self.task_store.add_estimate_sample(
                    profile,
                    elapsed / 100.0,
                    100.0,
                    elapsed,
                    device_fingerprint=device_fingerprint(),
                )
                self.task_store.mark_terminal(task_id, "completed")
                self.catalog_repository.update_lecture_fields(
                    sub_id,
                    summary_status="done",
                    summary_percent=100.0,
                    summary_progress_label="AI 时间戳总结已完成",
                    summary_error="",
                )
                self._request_search_refresh([sub_id])
                cached = bool(result.get("cached"))
                print(
                    f"[FudanCourseLens] AI notes task {task_id}: "
                    f"{'reused' if cached else 'complete'}",
                    flush=True,
                )
            except TaskPaused:
                for artifact_kind in ("timestamp_summary", "review_views"):
                    self.learning_store.interrupt_ai_artifacts(
                        sub_id,
                        artifact_kind,
                        "AI notes generation was paused",
                    )
                latest = self.task_store.get_task(task_id) or {}
                canceled = bool(dict(latest.get("payload") or {}).get("cancel_requested"))
                paused_task = (
                    self.task_store.mark_terminal(task_id, "canceled")
                    if canceled else self.task_store.acknowledge_pause(task_id)
                ) or {}
                resuming = paused_task.get("state") == "queued"
                self.catalog_repository.update_lecture_fields(
                    sub_id,
                    summary_status="canceled" if canceled else ("queued" if resuming else "paused"),
                    summary_progress_label=(
                        "AI 课程笔记已取消" if canceled else
                        ("AI 课程笔记等待恢复" if resuming else "AI 课程笔记已暂停：云端机器已停止，已完成部分保留")
                    ),
                    summary_error="",
                )
                action = "canceled" if canceled else ("resume queued" if paused_task.get("state") == "queued" else "paused")
                print(f"[FudanCourseLens] AI notes task {task_id}: {action}", flush=True)
            except Exception as exc:
                if _is_supervisor_busy(exc):
                    # AS10（第五十案）：同字幕分支——busy 处置共用化
                    self._handle_remote_supervisor_busy(task_id, kind="summary")
                elif self._defer_remote_task_failure(task_id, kind="summary", sub_id=sub_id):
                    # 第四十二案：同上——总结的远端 run 也可能已经跑完，
                    # 本地闸门不构成终态判决（helper 已记日志）
                    pass
                else:
                    message = _task_failure_message(exc)
                    for artifact_kind in ("timestamp_summary", "review_views"):
                        self.learning_store.interrupt_ai_artifacts(
                            sub_id,
                            artifact_kind,
                            message,
                        )
                    self.task_store.mark_terminal(task_id, "failed", error=message)
                    if "remote_runner_lost" in str(message) and self._auto_retry_runner_lost_once(task_id):
                        # ⑫：runner 中途失联自动重试恰一次；字幕行随重排队列
                        # 恢复，不打失败终态
                        self.catalog_repository.update_lecture_fields(
                            sub_id,
                            subtitle_status="queued",
                            subtitle_step="Queued",
                            subtitle_error="",
                            subtitle_progress_label="云端机器临时掉线，不是你的操作问题；已自动重试一次",
                        )
                    self.catalog_repository.update_lecture_fields(
                        sub_id,
                        summary_status="failed",
                        summary_progress_label="AI 课程笔记生成失败",
                        summary_error=message,
                    )
                    print(
                        f"[FudanCourseLens] AI notes task failed: {task_id} ({message})",
                        flush=True,
                    )
            finally:
                self._active_summary_task_id = ""
                self._progress_trackers.pop(task_id, None)
                with self._lock:
                    if self._summary_current and self._summary_current.get("task_id") == task_id:
                        self._summary_current = None
                current_task = self.task_store.get_task(task_id) or {}
                if current_task.get("state") == "queued" and not self._paused:
                    with self._lock:
                        latest = self.task_store.get_task(task_id) or {}
                        if latest.get("state") == "queued" and not self._paused:
                            self._queue_persisted_task(latest)
                            self._summary_cancel.clear()

    def _worker_llm_module(self):
        """夜10-C 第七波②：守卫加载 worker 总结链（llm 模块为轻依赖面）。

        架构边界（application 禁改 import 搜索路径）：不碰路径注入，改以
        importlib 包别名指向仓库内 worker 包（装机器默认不携带 worker/，
        晨间可决策轻量打包）——缺失时抛 RuntimeError 闭集失败，绝不静默
        伪造结果。别名只增不覆盖：真实包已加载时原样复用。
        """
        import importlib
        import importlib.util

        module_name = "courselens_worker"
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, "__path__"):
            return importlib.import_module(f"{module_name}.llm")
        package_root = PROJECT_ROOT / "worker" / "courselens_worker"
        if not package_root.is_dir():
            raise RuntimeError(
                "local summary module is unavailable for pending completion"
            )
        spec = importlib.util.spec_from_file_location(
            module_name,
            package_root / "__init__.py",
            submodule_search_locations=[str(package_root)],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return importlib.import_module(f"{module_name}.llm")

    def _complete_pending_summary_locally(
        self, result: dict, pending: dict, *, task_id: str = "", course_id: str = ""
    ) -> dict:
        """夜10-C 第七波②：远端 LLM 段降级领回。

        runner 出口对 DeepSeek 不可达时，worker 以 llm_pending 回执交还非
        LLM 产物；学生机境内可达且已有 DeepSeek 链路与 key——本地执行同一
        create_summary（含部分窗口检查点续跑），消耗经同一 metrics 面落账，
        UI 全程保持同一任务的 running→completed 语义。
        """
        worker_llm = self._worker_llm_module()
        key = self._deepseek_key()
        if not key:
            raise RuntimeError("DeepSeek API key is required for pending summary completion")
        prior_checkpoint: dict = {}
        if task_id:
            raw = (self.task_store.get_remote_run(task_id) or {}).get("checkpoint")
            if isinstance(raw, dict) and (raw.get("summary_parts") or raw.get("summary_window_plan")):
                prior_checkpoint = raw
        # RR-P6MEM-1：领回执行与远端同课程记忆待遇（术语表进同一 glossary 形参）。
        memory_terms = self._course_memory_terms(course_id)
        worker_llm.reset_usage()
        started = time.monotonic()
        summary = worker_llm.create_summary(
            key,
            title=str(pending.get("title") or ""),
            transcript=list(pending.get("transcript") or []),
            ppt_pages=list(pending.get("ppt_pages") or []),
            prior_checkpoint=prior_checkpoint or None,
            evidence_packet=pending.get("evidence_packet"),
            course_context=pending.get("course_context"),
            **({"glossary": tuple(memory_terms)} if memory_terms else {}),
        )
        usage = worker_llm.usage_snapshot()
        return {
            **result,
            "status": "completed",
            "outputs": {
                **dict(result.get("outputs") or {}),
                "summary": summary,
                "lecture_ir": None,
            },
            "metrics": {
                **dict(result.get("metrics") or {}),
                "deepseek_tokens": int(usage.get("total_tokens") or 0),
                "local_completion_seconds": round(time.monotonic() - started, 3),
                **({"course_memory_terms": len(memory_terms)} if memory_terms else {}),
            },
            "warnings": list(result.get("warnings") or []) + ["summary_completed_locally"],
        }

    def _import_remote_summary_result(
        self, course_id: str, sub_id: str, result: dict, *, task_id: str = "",
        judge_request: dict | None = None,
    ) -> dict:
        """Write one verified remote summary result into the course.

        Shared by the live summary run and by the startup truth reconcile, so a
        recovered result lands exactly like an unimpeded one.  Returns the
        course-knowledge rebuild receipt (the import itself is unconditional).
        """
        # 夜10-C 第七波②：远端 LLM 段降级回执——客户端领回本地执行同一总结
        # 链，重建结果落回既有导入体（UI 与消耗计数保持一致；导入漏斗唯一）。
        outputs = dict(result.get("outputs") or {})
        pending = outputs.get("llm_pending")
        if str(result.get("status") or "") == "llm_pending" and isinstance(pending, dict):
            result = self._complete_pending_summary_locally(
                result, pending, task_id=task_id, course_id=str(course_id)
            )
            outputs = dict(result.get("outputs") or {})
        # AS6：结果验证通过即落本任务消耗（绝对值，幂等）。
        self._record_task_usage(task_id, dict(result.get("metrics") or {}))
        summary = dict(outputs.get("summary") or {})
        markdown = str(summary.get("markdown") or "").strip()
        chapters = list(summary.get("chapters") or [])
        if not markdown:
            # SUMMARY-FIX-1：闭集码取代裸英文句（诊断/任务账稳定可查）。
            raise SummaryIncompleteError()
        # RR-P6MEM-1：学生可见记忆标注——只信生成侧实报的注入数，没有实报
        # （旧结果/回放）= 不标注（宁缺毋滥）。
        memory_note = self._memory_note_for(result.get("metrics"), kind="summary")
        try:
            memory_applied = int((result.get("metrics") or {}).get("course_memory_terms") or 0)
        except (TypeError, ValueError):
            memory_applied = 0
        if memory_note:
            markdown = f"{markdown}{memory_note}"
        if memory_applied < 0:
            memory_applied = 0
        self.learning_store.import_remote_summary(
            course_id=course_id,
            sub_id=sub_id,
            input_hash=str(result.get("input_hash") or ""),
            model=str(summary.get("model") or "deepseek-flash"),
            markdown=markdown,
            chapters=chapters,
            ppt_pages=list(outputs.get("ppt_pages") or []),
            metrics=dict(result.get("metrics") or {}),
            key_takeaways=[str(item or "") for item in (summary.get("key_takeaways") or [])],
            # RR-ANCHORFE-1：takeaway 时间戳锚原样透传（归一化与对齐在 store 收口）
            takeaway_anchors=list(summary.get("takeaway_anchors") or []),
            memory_applied=memory_applied,
            # P2-CONTRACT-1 ④：多视图复习包随总结漏斗同权入库（shape 守门在
            # store 收口；老结果无键=None 零行）。llm_pending 领回路径共用本
            # 漏斗，远端/本地零特判。
            review_views=summary.get("review_views"),
        )
        self._export_summary_markdown(course_id, sub_id, markdown=markdown, chapters=chapters)
        self._run_assessment_radar(
            course_id, sub_id, markdown=markdown,
            llm_events=list(summary.get("assessment_events") or []),
        )
        # RR-P6MEM-1 反哺：总结落库后对照记忆做本地一致性检查（零 LLM），
        # 偏差回写为修正信号，绝不挡导入。
        self._memory_feedback_sink(course_id, markdown, source="summary", sub_id=sub_id)
        # N2-2a judge 共位导入：远端 summary run 顺路质检的报告以派发时冻结
        # 的同一身份落库（同 input_hash upsert 语义与独立派发同域），随后的
        # 自动抽检钩子据其幂等跳过，恰省一次独立派发；报告缺席（worker 零
        # 材料/共位失败）或落库失败时钩子照常独立派发兜底。绝不挡总结导入。
        if judge_request:
            self._import_co_run_quality_report(
                course_id, sub_id, result, judge_request=judge_request, summary=summary,
            )
        # THINK-LADDER-1 自动抽检：总结落库后同内容至多自动一次（幂等+
        # 预算门+try/except 收口，绝不挡导入）。
        self._auto_quality_judge_after_summary(sub_id)
        # Additive Lecture IR import alongside the accepted summary import.
        self.learning_store.import_lecture_ir(
            course_id=course_id,
            sub_id=sub_id,
            input_hash=str(result.get("input_hash") or ""),
            view=outputs.get("lecture_ir") if isinstance(outputs.get("lecture_ir"), dict) else None,
            metrics=dict(result.get("metrics") or {}),
        )
        # N7K：增强讲次知识落地后重建课程快照，并请求检索刷新。引用校验由
        # 冻结合同的 fail-closed 验证承担——构建失败即保留旧快照并如实报错，
        # 已成功的导入结果不受影响。
        rebuild = self._rebuild_course_knowledge(
            self._course_knowledge_course_id(sub_id) or str(course_id), reason="summary_import"
        )
        if rebuild.get("state") == "saved":
            try:
                self.search_index.request_refresh([str(sub_id)])
            except Exception:  # noqa: BLE001 - 检索刷新失败不影响已落地结果
                pass
        return rebuild

    def _judge_request_extra(self, sub_id: str, *, transcript_size: int) -> dict:
        """N2-2a：summary job 的 judge 共位请求（加性键；旧客户端不发此键=
        旧行为，旧 Worker 忽略未知键照常处理旧载荷）。

        segment_indices 与 transcript_digest 都取本地存储转写正典视图
        （`get_transcript_segments`）上的合同冻结采样算式（
        `_quality_sample_indices`）与 canonical 摘要——与 `request_quality_
        judge`/`_auto_quality_judge_after_summary` 共用同一身份空间，共位报
        告落库后钩子按同 hash 幂等跳过，恰省一次独立派发。worker 按索引切
        片自己的载荷转写副本并防御性丢弃越界索引（展示整形行数≠store 行数
        时样本仍为确定性 stride 采样，质检有效性不受影响）。零转写/任何异
        常=省键（增值面绝不挡总结派发）；``transcript_size`` 仅作零转写哨兵。
        """
        try:
            if int(transcript_size) <= 0:
                return {}
            stored = self.learning_store.get_transcript_segments(str(sub_id))
            indices = _quality_sample_indices(len(stored))
            if not indices:
                return {}
            return {"judge_request": {
                "segment_indices": [int(index) for index in indices],
                "transcript_digest": _quality_transcript_digest(stored),
            }}
        except Exception:  # noqa: BLE001 - 组装失败省键，绝不挡总结派发
            return {}

    def _import_co_run_quality_report(
        self, course_id: str, sub_id: str, result: dict, *,
        judge_request: dict, summary: dict,
    ) -> None:
        """N2-2a：judge 共位报告落库（远端 summary run 顺路质检）。

        身份=派发时冻结的 judge_request（segment_indices+transcript_digest）
        + 本结果 input_hash，与 `_auto_quality_judge_after_summary` 的独立派
        发身份同式同域——共位报告落库后钩子按同 hash 幂等跳过，恰省一次独立
        派发；转写在飞中被重同步时 hash 错位，钩子照常独立重检（诚实兜底）。
        normalize 的 sample_size=judge_request 冻结索引数（「sample_size 取
        客户端分派时冻结真值」的合同口径）。报告无效/落库失败=闭集日志，绝
        不挡总结导入主链；仅在携带 judge_request 的活体 run 导入路径被调
        （启动对账恢复件无派发时捕获，走独立派发兜底）。
        """
        try:
            raw_report = dict((result.get("outputs") or {}).get("quality_judge") or {})
            if not raw_report:
                return  # worker 零材料/共位失败：无键=导入漏斗照旧独立派发兜底
            indices = [int(index) for index in judge_request.get("segment_indices") or []]
            digest = str(judge_request.get("transcript_digest") or "")
            if not indices or not digest:
                return
            input_hash = _quality_input_hash(
                sub_id, indices, digest, str(result.get("input_hash") or "")
            )
            chapters = [
                chapter for chapter in (summary.get("chapters") or [])
                if isinstance(chapter, dict)
            ]
            cleaned, counts = _normalize_quality_judge_report(
                raw_report,
                sample_size=len(indices),
                summary_chapter_count=len(chapters),
                summary_takeaway_count=len(
                    [str(item or "") for item in (summary.get("key_takeaways") or [])]
                ),
            )
            merged_metrics = dict(result.get("metrics") or {})
            merged_metrics.update(counts)
            self.learning_store.import_quality_report(
                course_id=course_id, sub_id=sub_id, input_hash=input_hash,
                model="deepseek-flash", report=cleaned, metrics=merged_metrics,
            )
        except Exception as error:  # noqa: BLE001 - 共位导入是增值面，绝不挡总结
            code = str(getattr(error, "code", "") or error)
            print(
                f"[FudanCourseLens] Co-run quality judge import unavailable "
                f"for {sub_id}: {code}",
                flush=True,
            )

    def _run_summary_remote(self, config: dict, *, on_progress) -> dict:
        from src.remote.coordinator import RemoteTaskPaused
        from src.remote.protocol import JOB_SCHEMA, PROTOCOL_VERSION

        task_id = str(self._active_summary_task_id or config.get("task_id") or "")
        if not task_id:
            raise RuntimeError("Remote summary task is not active")
        course_id = str(config["course_id"])
        sub_id = str(config["sub_id"])
        include_ppt = bool(config.get("include_ppt", True))
        # N2-2a judge 共位：派发时冻结的 judge_request（segment_indices+
        # transcript_digest）随盒进导入闭包——共位报告按派发时真值落库，
        # 重试重发时 build_job 复跑自然刷新。
        judge_request_box: dict = {}

        def build_job(result_public_key: str) -> dict:
            transcript = list(self.subtitle_segments(sub_id).get("segments") or [])
            if not transcript:
                raise RuntimeError("Timestamped subtitles are required for remote summary generation")
            judge_extra = self._judge_request_extra(sub_id, transcript_size=len(transcript))
            if judge_extra:
                judge_request_box.clear()
                judge_request_box.update(judge_extra["judge_request"])
            now = time.time()
            secrets = {"deepseek_api_key": self._deepseek_key()}
            source_session = None
            if include_ppt:
                secrets["source_credentials"] = self._runner_source_credentials()
                source_session = {
                    "provider": "runner-session-v1",
                    "course_id": course_id,
                    "sub_id": sub_id,
                    "media": False,
                    "slides": True,
                }
            return {
                "schema": JOB_SCHEMA,
                "protocol_version": PROTOCOL_VERSION,
                "task_id": task_id,
                "job_kind": "summary",
                "requested_outputs": ["ocr", "summary", "chapters"] if include_ppt else ["summary", "chapters"],
                "created_at": now,
                "expires_at": now + 600,
                "result_public_key": result_public_key,
                "pipeline": {"version": "actions-summary-v2"},
                "payload": {
                    "title": str(config.get("title") or sub_id),
                    "transcript": transcript,
                    "slides": [],
                    **({"source_session": source_session} if source_session else {}),
                    "checkpoint": (self.task_store.get_remote_run(task_id) or {}).get("checkpoint") or {},
                    # N7K 加性证据包：老 Worker 忽略未知键即可照常处理旧载荷；
                    # 包本身有单项/总量硬上限，绝不整本教材上云。构建失败就退回
                    # 旧载荷，任何情况下不因增值字段挡住一次摘要。
                    **self._course_knowledge_job_extras(course_id=course_id, sub_id=sub_id),
                    # RR-P6MEM-1：课程记忆术语表随 payload.glossary 进总结窗口/
                    # 合并输入（worker 读侧 V4NONTHINK-1 件6 既有通道）；非空才
                    # 带键，缺席=旧行为。
                    **({"glossary": terms} if (terms := self._course_memory_terms(course_id)) else {}),
                    # N2-2a judge 共位请求（加性键，旧 Worker 忽略未知键照常
                    # 处理旧载荷）；零转写/组装异常=省键=旧行为。
                    **judge_extra,
                },
                "secrets": secrets,
            }

        def import_result(result: dict) -> None:
            self._import_remote_summary_result(
                course_id, sub_id, result, task_id=task_id,
                judge_request=dict(judge_request_box) or None,
            )

        stage_map = {
            "remote_queue": "input",
            "remote_compute": "ai_parts",
            "remote_result": "finalize",
            "remote_import": "finalize",
        }
        try:
            with self._cloud_run_slot(
                task_id,
                cancel_requested=self._summary_cancel.is_set,
                on_wait=lambda ahead: on_progress({
                    "phase_id": "input",
                    "phase_percent": None,
                    "indeterminate": True,
                    "label": self._cloud_queue_label(ahead),
                }),
            ):
                with self._leased_remote_coordinator(
                    task_id,
                    # N1-ROUTING：总结载荷媒体面缺席（PPT 腿=source_session.slides，
                    # OCR 仅需 rapidocr，f08b6c0 口径）→ llm.yml 快路径。
                    workflow=_remote_dispatch_workflow({
                        "transcript": [],
                        "slides": [],
                        **({"source_session": {"slides": True}} if include_ppt else {}),
                    }),
                ) as coordinator:
                    result = coordinator.execute(
                        task_id=task_id,
                        build_job=build_job,
                        import_result=import_result,
                        cancel_requested=self._summary_cancel.is_set,
                        progress=lambda stage, percent, label: on_progress({
                            "phase_id": stage_map.get(stage, "input"),
                            "phase_percent": percent,
                            "indeterminate": percent is None,
                            "label": label,
                        }),
                    )
            return {"artifact": {"input_hash": result.get("input_hash")}, "cached": False, "ppt": None}
        except RemoteTaskPaused as exc:
            raise TaskPaused(str(exc)) from exc




    @staticmethod
    def _remote_source_headers(raw_headers: str) -> dict[str, str]:
        allowed = {
            "accept", "accept-encoding", "accept-language", "cache-control",
            "cookie", "origin", "pragma", "referer", "user-agent",
        }
        return {
            name: value
            for name, value in _headers_to_dict(raw_headers).items()
            if name.strip().lower() in allowed
        }

    def _fresh_remote_media_source(self, course_id: str, sub_id: str) -> dict:
        client = self.client()
        signed_url = client.get_video_url(str(course_id), str(sub_id))
        if not signed_url:
            raise FileNotFoundError("No authorized online media source is available")
        media_url, raw_headers = client.get_stream_params(signed_url)
        progress = self.watch_progress(sub_id) or {}
        duration = float(progress.get("duration_seconds") or 0)
        source = {
            "url": media_url,
            "headers": self._remote_source_headers(raw_headers),
        }
        resolved_ip = _resolved_public_source_ip(media_url)
        if resolved_ip:
            source["resolved_public_ip"] = resolved_ip
        if duration > 0:
            source["duration_seconds"] = round(duration, 3)
        return source

    def _runner_source_credentials(self) -> dict[str, str]:
        """Return one-job credentials for the already encrypted task envelope."""
        with self._lock:
            account = str(self._credentials.get("student_id") or "")
            password = str(self._credentials.get("password") or "")
        if not account or not password:
            raise RuntimeError("iCourse credentials are required for runner-side authorization")
        return {"account": account, "password": password}

    def _remote_progress(
        self, task_id: str, sub_id: str, stage: str, percent: float | None, label: str
    ) -> None:
        task = self.task_store.get_task(task_id) or {}
        previous = dict(task.get("progress") or {})
        remote = self.task_store.get_remote_run(task_id) or {}
        attempt = self.task_store.get_remote_attempt(
            task_id, max(1, int(remote.get("attempt") or 1))
        ) or {}
        completed = attempt.get("completed") if str(attempt.get("worker_stage") or "") == str(stage) else None
        total = attempt.get("total") if str(attempt.get("worker_stage") or "") == str(stage) else None
        payload = dict(task.get("payload") or {})
        duration = payload.get("duration_seconds")
        try:
            media_duration = float(duration) if duration is not None else (
                float(total) * 30.0
                if total is not None and str(stage) == "asr" else None
            )
            processed_media = (
                min(media_duration, media_duration * float(completed) / float(total))
                if media_duration and completed is not None and total else None
            )
        except (TypeError, ValueError, ZeroDivisionError):
            media_duration = processed_media = None
        if processed_media is None:
            # Outside the ASR stage the worker reports no chunk numerator; keep
            # the last known processed media so the ETA keeps its position.
            processed_media = previous.get("processed_media_seconds")
        media_events = 0
        try:
            media_events = max(0, int(previous.get("media_events") or 0))
            previous_processed = float(previous.get("processed_media_seconds") or 0.0)
        except (TypeError, ValueError):
            previous_processed = 0.0
        if processed_media is not None and float(processed_media) > previous_processed + 0.5:
            media_events += 1
        elapsed_active = 0.0
        started_at = task.get("started_at")
        try:
            elapsed_active = max(0.0, float(previous.get("elapsed_active_seconds") or 0.0))
            if started_at:
                elapsed_active = max(
                    elapsed_active, max(0.0, time.time() - float(started_at))
                )
        except (TypeError, ValueError):
            elapsed_active = 0.0
        value = {
            "schema_version": 2,
            "percent": round(max(0.0, min(99.0, float(percent))), 1) if percent is not None else None,
            "label": str(label),
            "stage": str(stage),
            "phase_id": str(stage),
            "phase_percent": round(max(0.0, min(100.0, float(percent))), 1) if percent is not None else None,
            "indeterminate": percent is None,
            "completed": completed,
            "total": total,
            "media_duration_seconds": media_duration,
            "processed_media_seconds": processed_media,
            "media_events": media_events,
            "elapsed_active_seconds": round(elapsed_active, 1),
            "observed_at": float(attempt.get("observed_at") or time.time()),
        }
        task_fields = {"progress": value}
        if percent is None:
            task_fields["estimate"] = {}
        self.task_store.update_task(task_id, **task_fields)
        if media_duration:
            self._refresh_subtitle_task_estimate(self.task_store.get_task(task_id) or task)
        self.catalog_repository.update_lecture_fields(
            sub_id, subtitle_status="running", subtitle_step=str(stage),
            subtitle_file_percent=value["percent"] if value["percent"] is not None else "",
            subtitle_progress_label=str(label), subtitle_error="",
        )
        with self._lock:
            if self._subtitle_current and self._subtitle_current.get("task_id") == task_id:
                self._subtitle_current.update({
                    "subtitle_step": str(stage),
                    "subtitle_file_percent": value["percent"],
                    "subtitle_progress_label": str(label),
                })

    def _import_remote_subtitle(
        self, course_id: str, sub_id: str, result: dict, *, task_id: str = ""
    ) -> None:
        # AS6：结果验证通过即落本任务消耗（绝对值，幂等；无任务行的调用面
        # 不传 task_id，自然跳过）。
        self._record_task_usage(task_id, dict(result.get("metrics") or {}))
        subtitle = dict(dict(result.get("outputs") or {}).get("subtitle") or {})
        srt_text = str(subtitle.get("srt") or "")
        vtt_text = str(subtitle.get("vtt") or "")
        segments = list(subtitle.get("segments") or [])
        if not srt_text or not vtt_text or not segments:
            raise RuntimeError("Remote subtitle result is incomplete")
        artifact_key = hashlib.sha256(f"{course_id}:{sub_id}".encode("utf-8")).hexdigest()
        output_dir = self.output_dir / "artifacts" / "subtitles" / artifact_key
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = "subtitle"
        paths = {
            "srt": output_dir / f"{stem}.srt",
            "vtt": output_dir / f"{stem}.vtt",
            "optimized": output_dir / f"{stem}.remote.json",
        }
        payloads = {
            "srt": srt_text,
            "vtt": vtt_text,
            "optimized": json.dumps(
                {"segments": segments, "metrics": result.get("metrics") or {}},
                ensure_ascii=False,
                indent=2,
            ),
        }
        temporary_paths: list[Path] = []
        try:
            for key, destination in paths.items():
                temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
                temporary.write_text(payloads[key], encoding="utf-8")
                temporary_paths.append(temporary)
            for temporary, destination in zip(temporary_paths, paths.values()):
                os.replace(temporary, destination)
            temporary_paths.clear()
        finally:
            for temporary in temporary_paths:
                temporary.unlink(missing_ok=True)
        self.catalog_repository.update_lecture_fields(
            sub_id,
            subtitle_status="done",
            subtitle_step="Done",
            subtitle_step_percent=100.0,
            subtitle_file_percent=100.0,
            subtitle_progress_label="Subtitles complete",
            subtitle_error="",
            srt_path=paths["srt"].relative_to(self.output_dir).as_posix(),
            vtt_path=paths["vtt"].relative_to(self.output_dir).as_posix(),
            optimized_json_path=paths["optimized"].relative_to(self.output_dir).as_posix(),
            subtitle_mode="automatic",
            subtitle_pipeline_mode="actions-automatic",
        )
        # COURSEMEM-1 课程记忆循环：LLM 已应用的修正沉淀为课程专属 few-shot
        # 示例（幂等：按改前文本去重，重复回放零新增）。全程 fail-closed，
        # 沉淀失败绝不影响字幕导入结果。P10：带 sub_id 时同一次写内挂账
        # 跨讲映射（mapping_stats，候选流水主源）。
        try:
            sunk = sink_course_examples(
                self.output_dir, str(course_id), subtitle.get("deep_audit"),
                sub_id=str(sub_id),
            )
        except Exception:  # noqa: BLE001 - 记忆是增值面，绝不挡导入
            sunk = 0
        if sunk:
            print(
                f"[FudanCourseLens] Course memory: {sunk} new examples "
                f"for course {course_id}",
                flush=True,
            )
        self.subtitle_segments(sub_id)

    def _generate_subtitle_remote(
        self,
        course_id: str,
        sub_id: str,
        *,
        task_id: str,
        proofread: bool,
        start_seconds: float = 0.0,
        duration_seconds: float | None = None,
    ) -> None:
        from src.remote.coordinator import RemoteTaskPaused
        from src.remote.protocol import JOB_SCHEMA, PROTOCOL_VERSION

        task_id = str(task_id)
        if not task_id:
            raise RuntimeError("Remote subtitle task is not active")
        cancel_event = self._subtitle_cancel_events.get(task_id) or threading.Event()

        def build_job(result_public_key: str) -> dict:
            now = time.time()
            media = {"start_seconds": max(0.0, float(start_seconds or 0.0))}
            if duration_seconds is not None:
                media["duration_seconds"] = float(duration_seconds)
            return {
                "schema": JOB_SCHEMA,
                "protocol_version": PROTOCOL_VERSION,
                "task_id": task_id,
                "job_kind": "subtitle",
                "requested_outputs": ["subtitle"],
                "created_at": now,
                "expires_at": now + 600,
                "result_public_key": result_public_key,
                "pipeline": {
                    "version": "actions-v2",
                },
                "payload": {
                    "mode": "automatic",
                    "media": media,
                    "source_session": {
                        "provider": "runner-session-v1",
                        "course_id": str(course_id),
                        "sub_id": str(sub_id),
                        "media": True,
                        "slides": False,
                    },
                    "checkpoint": (self.task_store.get_remote_run(task_id) or {}).get("checkpoint") or {},
                    **self._course_memory_payload(course_id),
                },
                "secrets": {
                    "deepseek_api_key": self._deepseek_key() if proofread else "",
                    "source_credentials": self._runner_source_credentials(),
                },
            }

        try:
            with self._cloud_run_slot(
                task_id,
                cancel_requested=cancel_event.is_set,
                on_wait=lambda ahead: self._remote_progress(
                    task_id, sub_id, "remote_queue", None, self._cloud_queue_label(ahead)
                ),
            ):
                with self._leased_remote_coordinator(
                    task_id,
                    # N1-ROUTING：字幕载荷带 media 媒体腿 → 维持 process.yml 字节不变。
                    workflow=_remote_dispatch_workflow({"media": {"start_seconds": 0.0}}),
                ) as coordinator:
                    coordinator.execute(
                        task_id=task_id,
                        build_job=build_job,
                        import_result=lambda result: self._import_remote_subtitle(
                            course_id, sub_id, result, task_id=task_id
                        ),
                        cancel_requested=cancel_event.is_set,
                        progress=lambda stage, percent, label: self._remote_progress(
                            task_id, sub_id, stage, percent, label
                        ),
                    )
        except RemoteTaskPaused as exc:
            raise TaskPaused(str(exc)) from exc






    def open_remote_media(self, sub_id: str, range_header: str = "", *, head_only: bool = False):
        row = self.catalog_repository.get_lecture(sub_id)
        if not row:
            raise FileNotFoundError("Lecture is not in the authorized catalog")
        with self._lock:
            authenticated = bool(
                self._credentials.get("student_id") and self._credentials.get("password")
            )
        if not authenticated:
            raise PermissionError("Sign in before streaming this lecture")
        if not bool(row.get("has_playback", True)):
            raise FileNotFoundError("Remote lecture media is unavailable")
        course_id = str(row.get("course_id") or "")

        # MEDIAWEBVPN-3-20261002：三级取流层级（画清记档）——
        #   第 1 级 直连（现状网关原样，显式直连代理）
        #   第 2 级 WebVPN 中转（恰一次改写重取；仅在直连失败且失败类
        #            =upstream_unreachable、目标 host ∈ 改写闭集、偏好开时；
        #            RANGE-FIX-1：start=0 大窗在本腿内可走两段式取流缓解
        #            E1 瞬态 403，判别与拦截窗记忆见模块级注释）
        #   第 3 级 系统代理（原始签名 URL；仅在 WebVPN 腿同样 unreachable、
        #            系统代理偏好开且注册表检出代理时）
        # 直连明确被拒（upstream_rejected）与未知失败不进层级：上游已答话，
        # 换传输面不改变结局。失败账本只见最终结局（路由层记账不变）。
        last_url: list[str] = []

        def resolve():
            client = self.client()
            signed_url = client.get_video_url(course_id, str(sub_id))
            if not signed_url:
                raise FileNotFoundError("No playable media URL was returned")
            media_url, raw_headers = client.get_stream_params(signed_url)
            last_url.clear()
            last_url.append(media_url)
            return client.vpn.session, media_url, _headers_to_dict(raw_headers)

        def resolve_relay():
            session, media_url, _ = resolve()
            relay_url = _media_webvpn_relay_target(media_url)
            if not relay_url:
                # 闭集闸已在层级入口判过；此处 None 只可能是重签后 host 变化，
                # fail-closed 原样上抛（网关按 unknown 类收口）。
                raise RuntimeError("media relay target is not allowed")
            return session, relay_url, _media_webvpn_relay_headers()

        gateway = RemoteMediaGateway(resolve, self._refresh_client_session)
        try:
            return gateway.open(range_header, head_only=head_only)
        except Exception as direct_failure:
            if classify_open_failure(direct_failure) != MEDIA_STREAM_UPSTREAM_UNREACHABLE:
                raise
            relay_target = _media_webvpn_relay_target(last_url[0]) if last_url else None
            if relay_target is None or not self.media_webvpn_relay_enabled():
                raise
            print("[media] relay=webvpn route=open", flush=True)
        relay_gateway = RemoteMediaGateway(resolve_relay, self._refresh_client_session)

        def relay_open():
            # RANGE-FIX-1：中转腿内部的两段式策略——拦截窗记忆活跃时探针
            # 先行（不发送已知被拦形状）；单发恰逢形状特定 403 时切两段式
            # 重取。两段式自身失败原样上抛，由外层按既有分类走回退/错误卡。
            shape = _media_relay_twostage_shape(range_header, head_only=head_only)
            if shape and _media_relay_block_window_active():
                print("[media] relay=webvpn twostage=window route=open", flush=True)
                stream = _open_media_stream_twostage(relay_gateway, shape)
                _media_relay_note_block_window()
                return stream
            try:
                stream = relay_gateway.open(range_header, head_only=head_only)
            except _ConfirmedServiceResponse as failure:
                if not _media_relay_transient_block_failure(failure, shape):
                    raise
                print(
                    "[media] relay=webvpn transient403=detected route=twostage",
                    flush=True,
                )
                stream = _open_media_stream_twostage(relay_gateway, shape)
                _media_relay_note_block_window()
                return stream
            if shape:
                # start=0 大窗单发成功=拦截窗已自愈，清除记忆回零开销稳态。
                _media_relay_clear_block_window()
            return stream

        try:
            return relay_open()
        except Exception as relay_failure:
            if classify_open_failure(relay_failure) != MEDIA_STREAM_UPSTREAM_UNREACHABLE:
                raise
            if not (
                media_stream_proxy_enabled(self.task_store)
                and bool(detect_windows_system_proxy()[0])
            ):
                raise
            print("[media] relay=system-proxy route=open", flush=True)
        _media_system_proxy_leg_state.active = True
        try:
            return gateway.open(range_header, head_only=head_only)
        finally:
            _media_system_proxy_leg_state.active = False

    def is_authorized_course(self, course_id: str) -> bool:
        if self.authentication_snapshot().get("state") != "ready":
            return False
        cache = self._authorized_catalog_cache()
        return str(course_id or "") in {
            str(value) for value in cache.get("course_ids") or [] if str(value)
        }

    def observe_live_room(self, course_id: str) -> dict:
        if not self.is_authorized_course(course_id):
            return {"state": "denied", "code": "live_authorization_denied"}
        try:
            client = self.client()
        except Exception:
            # SRC-SYNDROME-1 U2（第廿六案）：会话 degraded 时取流航班的失败收编为
            # 闭集态——前端拿到可映射的 live_session_unavailable 而非裸 unknown；
            # 自愈看门狗恢复后 status 自然回真。闭集行给 tee 日志留证据。
            print("[live] failure_code=live_session_unavailable status=503 route=observe", flush=True)
            return {"state": "unknown", "code": "live_session_unavailable"}
        return client.get_live_course_observation(str(course_id))

    def open_live_room_upstream(self, course_id: str, *, view: str = "student"):
        from src.runtime.live_room import DEFAULT_LIVE_VIEW, LiveRoomError, UpstreamRequest

        # U17③：未授权/取流失败收编进闭集码族，不再让 PermissionError 逃逸成 500
        if not self.is_authorized_course(course_id):
            print("[live] failure_code=live_stream_unavailable status=502 route=upstream-open", flush=True)
            raise LiveRoomError("live_stream_unavailable", 502)
        try:
            session, url, headers = self.client().get_live_stream_params(
                str(course_id), view=view or DEFAULT_LIVE_VIEW
            )
        except PermissionError as exc:
            print("[live] failure_code=live_stream_unavailable status=502 route=upstream-open", flush=True)
            raise LiveRoomError("live_stream_unavailable", 502) from exc
        return UpstreamRequest(session, url, dict(headers))

    def _refresh_client_session(self) -> None:
        with self._lock:
            current = self._client
            self._client = None
            self._vpn = None
            self._client_last_verified_at = 0.0
        try:
            if current is not None:
                current.close()
        except (AttributeError, OSError):
            pass
        self.client()

    def transcript_segments(self, sub_id: str, since_ms: int | None = None) -> list[dict] | None:
        """平台原生文稿段（直播二期乙1/LIVESTUDY-1 F4）。

        授权门与字幕文件路由同界：讲次必须在已授权目录内，否则
        FileNotFoundError（HTTP 层映射 404 闭集码）。读取失败返回 None
        （HTTP 层映射 503 transcript_unavailable）；空列表=平台如实无文稿。
        复用 icourse.get_transcript_segments 的既有的会话与缓存，零新外联主机。
        """
        if not self.catalog_repository.get_lecture(sub_id):
            raise FileNotFoundError("Lecture is not in the authorized catalog")
        try:
            return self.client().get_transcript_segments(sub_id, since_ms=since_ms)
        except Exception:
            print("[transcript] failure_code=transcript_unavailable route=segments", flush=True)
            return None

    def subtitle_file_path(self, sub_id: str) -> Path | None:
        row = self.catalog_repository.get_lecture(sub_id)
        if not row:
            raise FileNotFoundError("Lecture is not in the authorized catalog")
        for field in ("vtt_path", "srt_path"):
            value = str(row.get(field) or "").strip()
            if not value:
                continue
            raw = Path(value)
            candidates = [raw] if raw.is_absolute() else [self.output_dir / raw, PROJECT_ROOT / raw]
            for candidate in candidates:
                resolved = candidate.resolve()
                try:
                    resolved.relative_to(self.output_dir.resolve())
                except ValueError:
                    continue
                if resolved.is_file() and file_size(resolved) > 0:
                    return resolved
        return None

    def _remote_subtitle_segments(self, sub_id: str) -> list[dict]:
        """Segments from the remote optimized subtitle artifact, when usable.

        The remote result carries the rich per-segment evidence metadata that
        SRT/VTT text cannot; when the artifact is missing or unreadable the
        caller falls back to parsing the subtitle file as before.
        """
        row = self.catalog_repository.get_lecture(sub_id) or {}
        value = str(row.get("optimized_json_path") or "").strip()
        if not value:
            return []
        raw = Path(value)
        candidates = [raw] if raw.is_absolute() else [self.output_dir / raw, PROJECT_ROOT / raw]
        for candidate in candidates:
            resolved = candidate.resolve()
            try:
                resolved.relative_to(self.output_dir.resolve())
            except ValueError:
                continue
            if not resolved.is_file() or file_size(resolved) <= 0:
                continue
            try:
                payload = json.loads(resolved.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return []
            segments = payload.get("segments") if isinstance(payload, dict) else None
            if not isinstance(segments, list):
                return []
            return [dict(segment) for segment in segments if isinstance(segment, dict)]
        return []

    def _backfill_subtitle_track(self, course_id: str, sub_id: str, rows: list[dict]) -> None:
        """夜10-C 第七波③：轨文件端点自愈（总控拍板的数据回写授权面）。

        vtt 实体缺失时从 transcript store 重建 vtt 并回写 catalog 的
        vtt_path 键（复用 persist 尾段的产物布局：artifacts/subtitles/
        <artifact-key>/subtitle.vtt）——旧讲 /subtitles/file 404 关案，读链
        回落保留兜底。尽力而为：任何失败静默跳过，绝不影响段读出；回写后
        下一次读取走正常文件链（源登记随 replace 幂等收敛）。
        """
        try:
            from src.runtime.course_data_inventory import subtitle_artifact_key
            from src.runtime.http_api import _vtt_bytes

            target = (
                self.output_dir / "artifacts" / "subtitles"
                / subtitle_artifact_key(str(course_id), str(sub_id)) / "subtitle.vtt"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_vtt_bytes(rows))
            self.catalog_repository.update_lecture_fields(
                sub_id,
                vtt_path=target.relative_to(self.output_dir).as_posix(),
            )
        except Exception:  # noqa: BLE001 - 回写是尽力而为，读链回落已兜底
            return

    def _segments_from_local_store(self, sub_id: str) -> dict | None:
        """夜10-C N10B-4 方案②：读链自愈回落。

        catalog 行缺 vtt_path/srt_path（登记与文件不成套的遗留态）但
        transcript_sources 有源登记、缓存段仍在时，直接从本地缓存段供段——
        学生视角「已有字幕无声消失」恢复为即时可见。附带第七波③轨文件
        自愈回写（尽力而为）。无登记或零段时返回 None，调用方维持既有
        诚实空响应。
        """
        source = self.learning_store.get_transcript_source(sub_id)
        if not source:
            return None
        rows = self.learning_store.get_transcript_segments(sub_id)
        if not rows:
            return None
        segments = split_long_cues(rows)
        row = self.catalog_repository.get_lecture(sub_id) or {}
        course_id = str(row.get("course_id") or "")
        if course_id:
            self._backfill_subtitle_track(course_id, sub_id, segments)
        fingerprint = (
            "store", str(sub_id), len(segments),
            int(segments[-1].get("end_ms") or 0) if segments else 0,
        )
        segments = shape_display_cues_cached(fingerprint, segments)
        return {
            "sub_id": str(sub_id),
            "segments": segments,
            "source": str(source.get("source_path") or ""),
            "count": len(segments),
            "source_state": "store_fallback",
        }

    def subtitle_segments(self, sub_id: str) -> dict:
        path = self.subtitle_file_path(sub_id)
        if path is None:
            fallback = self._segments_from_local_store(str(sub_id))
            if fallback is not None:
                return fallback
            return {"sub_id": str(sub_id), "segments": [], "source": ""}
        stat = path.stat()
        source_path = relative_to_project(path)
        # VTT-PERF 展示层落盘缓存：源指纹（path/mtime/size）未变且整形口径一致
        # 时直接供缓存行，跳过 store 读+重切+整形整链。store 登记核对本依旧
        # 执行（登记缺失时走下方原链路重存，自愈语义保持）；缓存只是纯优化层，
        # 任何 miss/损坏都回落原链路，证据层与响应形状零变化。
        cache_path = display_cache_path(
            self.output_dir / "artifacts" / "subtitles" / "display-cache",
            sub_id,
            source_path=source_path,
            source_mtime_ns=stat.st_mtime_ns,
            source_size=stat.st_size,
        )
        source_key = [str(sub_id), source_path, stat.st_mtime_ns, stat.st_size]
        store_registered = self.learning_store.transcript_source_matches(
            sub_id,
            source_path=source_path,
            source_mtime_ns=stat.st_mtime_ns,
            source_size=stat.st_size,
        )
        if store_registered:
            cached_segments = load_display_cache(cache_path, source_key)
            if cached_segments is not None:
                return {
                    "sub_id": str(sub_id),
                    "segments": cached_segments,
                    "source": source_path,
                    "count": len(cached_segments),
                }
        if not store_registered:
            # 展示层长句切分：阅读面板与缓存共用同一套拆分后 cue 单元；
            # 远端优化产物优先（保留随段证据元数据），否则按 SRT/VTT 文本解析
            segments = self._remote_subtitle_segments(sub_id) or parse_subtitle_file(path)
            segments = split_long_cues(segments)
            self.learning_store.replace_transcript_segments(
                sub_id,
                source_path=source_path,
                source_mtime_ns=stat.st_mtime_ns,
                source_size=stat.st_size,
                segments=segments,
            )
        # 读取路径同样做长句切分（幂等）：拆分上线前的旧缓存行可能是未拆分整段。
        # AS13（第五十三案）展示层整形挂在缓存读取之后：证据层缓存行一字不动，
        # 整形纯展示、进程内缓存，绝不写回 learning_store/runtime/data。
        segments = split_long_cues(self.learning_store.get_transcript_segments(sub_id))
        fingerprint = (
            str(sub_id), stat.st_mtime_ns, stat.st_size, len(segments),
            int(segments[-1].get("end_ms") or 0) if segments else 0,
        )
        segments = shape_display_cues_cached(fingerprint, segments)
        store_display_cache(cache_path, source_key, segments)
        return {
            "sub_id": str(sub_id),
            "segments": segments,
            "source": source_path,
            "count": len(segments),
        }

    def ai_artifact(self, sub_id: str, kind: str = "lecture_summary") -> dict:
        if not self.catalog_repository.get_lecture(sub_id):
            raise FileNotFoundError("Lecture is not in the authorized catalog")
        artifact = self.learning_store.find_ai_artifact(sub_id, kind)
        if not artifact:
            raise FileNotFoundError("AI artifact has not been generated")
        return artifact

    def _export_summary_markdown(self, course_id: str, sub_id: str, *, markdown: str, chapters: list) -> None:
        """Drop the readable markdown export when a summary completes.

        Both completion entries (cloud import + automation import) funnel
        through here.  The export is a convenience copy: its failure must
        never fail an already-accepted import.
        """
        try:
            write_summary_export(
                self.output_dir, str(course_id), str(sub_id),
                markdown=markdown, chapters=list(chapters or []),
                lecture=self.catalog_repository.get_lecture(sub_id),
            )
        except OSError:
            pass

    def _run_assessment_radar(
        self, course_id: str, sub_id: str, *, markdown: str, llm_events: list | None = None,
    ) -> None:
        """N5A-P1 钩子：总结工件落库后用本地规则扫描考核事件（零 token）。

        纯本地规则 + 本地表；任何失败只影响雷达台账，绝不影响已接受的总结。
        """
        try:
            ensure_assessment_schema(self.learning_store.path)
            segments = [
                {
                    "start_ms": int(row.get("start_ms") or 0),
                    "end_ms": int(row.get("end_ms") or 0),
                    "text": str(row.get("text") or ""),
                }
                for row in (self.learning_store.get_transcript_segments(sub_id) or [])
            ]
            lecture = self.catalog_repository.get_lecture(sub_id) or {}
            lecture_date = str(lecture.get("date") or "") or None
            rescan_lecture(
                self.learning_store,
                course_id=str(course_id),
                sub_id=str(sub_id),
                segments=segments,
                lecture_date=lecture_date,
                summary_text=markdown,
            )
            # N5A-P2 顺风车：合并调用输出的考核事件候选（source=llm，默认
            # unconfirmed，确认后才在学习桌展开）。fail-closed 逐项校验。
            converted = []
            for item in llm_events or []:
                if not isinstance(item, dict):
                    continue
                category = str(item.get("category") or "")
                title = str(item.get("title") or "").strip()
                quote = str(item.get("quote") or "").strip()[:80]
                if category not in CATEGORIES or not title or not quote:
                    continue
                converted.append({
                    "course_id": str(course_id),
                    "category": category,
                    "title": title,
                    "title_norm": title_norm(title),
                    "due_at": "",
                    "location": "",
                    "source": "llm",
                    "status": "unconfirmed",
                    "quote": quote,
                    "first_seen_sub_id": str(sub_id),
                })
            if converted:
                upsert_events(self.learning_store, converted, default_status="unconfirmed")
        except Exception:
            pass

    # --- local lecture courseware PDF (explicit user action only) ------------

    @staticmethod
    def _courseware_pdf_lecture_key(course_id: str, sub_id: str) -> str:
        """Opaque on-disk key for one lecture's courseware artifact directory."""
        digest = hashlib.sha256(
            f"courseware-pdf.v1\0{course_id}\0{sub_id}".encode("utf-8")
        ).hexdigest()
        return f"lec-{digest[:16]}"

    def _courseware_pdf_paths(self, course_id: str, sub_id: str) -> tuple[Path, Path, Path]:
        key = self._courseware_pdf_lecture_key(str(course_id), str(sub_id))
        artifact_dir = (self.output_dir / "courseware" / key).resolve()
        # path-checked: the derived directory must stay inside the data root
        artifact_dir.relative_to(self.output_dir.resolve())
        return artifact_dir, artifact_dir / "slides.pdf", artifact_dir / "manifest.json"

    # 闭集阶段文案：前端只转述，不自行发明阶段
    _COURSEWARE_PDF_PHASE_LABELS = {
        "records": "正在获取课堂画面",
        "pages": "正在读取课堂画面",
        "assemble": "正在合成 PDF",
        "done": "课件 PDF 已生成",
    }

    @staticmethod
    def _courseware_pdf_download_name(row: dict) -> str:
        """Sanitized lecture/date filename for the download boundary."""
        return courseware_pdf_download_name(row)

    def courseware_pdf_status(self, sub_id: str) -> dict:
        """Nested artifact/operation status: two independent facts.

        ``artifact`` is the last atomically published PDF/manifest pair;
        ``operation`` is the current generation attempt with trustworthy
        measured progress. A manifest summary never overwrites operation
        progress, and an older artifact never hides a live operation.
        """
        row = self.catalog_repository.get_lecture(sub_id)
        if not row:
            raise FileNotFoundError("Lecture is not in the authorized catalog")
        course_id = str(row.get("course_id") or "")
        artifact_dir, pdf_path, manifest_path = self._courseware_pdf_paths(course_id, sub_id)
        artifact = None
        if (
            pdf_path.is_file() and file_size(pdf_path) > 0 and manifest_path.is_file()
        ):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                manifest = {}
            if manifest.get("schema") == MANIFEST_SCHEMA:
                skipped = manifest.get("skipped") or {}
                generated_at = int(manifest.get("generated_at") or 0)
                if not generated_at:
                    try:
                        generated_at = int(pdf_path.stat().st_mtime)
                    except OSError:
                        generated_at = 0
                artifact = {
                    "ready": True,
                    "pages": int(manifest.get("kept") or 0),
                    "events_total": int(manifest.get("events_total") or 0),
                    "duplicates": int(manifest.get("duplicates") or 0),
                    "skipped_total": sum(
                        int(v) for v in skipped.values()
                        if isinstance(v, (int, float))
                    ),
                    "skipped": {str(k): int(v) for k, v in skipped.items()},
                    "generated_at": generated_at,
                    "download_name": self._courseware_pdf_download_name(row),
                }
        operation = None
        latest = next(
            (
                # CLIENT-STATE-R1 相邻缺陷同修：变量名 latest 但旧序会让
                # next() 取到最老一行——改最新优先使语义与命名一致。
                task for task in self.task_store.list_tasks(
                    kinds=("courseware_pdf",), limit=50, newest_first=True,
                )
                if str(task.get("sub_id") or "") == str(sub_id)
            ),
            None,
        )
        if latest is not None:
            state = str(latest.get("state") or "")
            if state in {"queued", "running", "pausing", "paused", "failed"}:
                progress = latest.get("progress") or {}
                percent = progress.get("percent")
                measured = (
                    progress.get("percent_measured") is True
                    and isinstance(percent, (int, float))
                )
                counts = progress.get("counts")
                operation = {
                    "task_id": str(latest.get("task_id") or ""),
                    "state": state,
                    "phase": str(progress.get("phase") or ""),
                    "label": str(progress.get("label") or ""),
                    "percent": round(float(percent), 1) if measured else None,
                    "percent_measured": measured,
                    "resumable": state == "paused",
                    "error_code": str(latest.get("error") or ""),
                    "counts": dict(counts) if isinstance(counts, dict) else None,
                }
        return {"sub_id": str(sub_id), "artifact": artifact, "operation": operation}

    def courseware_pdf_file_path(self, sub_id: str) -> Path:
        row = self.catalog_repository.get_lecture(sub_id)
        if not row:
            raise FileNotFoundError("Lecture is not in the authorized catalog")
        course_id = str(row.get("course_id") or "")
        _, pdf_path, _ = self._courseware_pdf_paths(course_id, sub_id)
        resolved = pdf_path.resolve()
        resolved.relative_to(self.output_dir.resolve())
        if not resolved.is_file() or file_size(resolved) <= 0:
            raise FileNotFoundError("Courseware PDF has not been generated")
        return resolved

    def enqueue_courseware_pdf(
        self, course_id: str, sub_id: str, *, action: str = "generate",
        plan: dict | None = None, plan_digest: str = "",
    ) -> dict:
        """Queue (or resume) the explicit local courseware-PDF operation.

        A verified cloud ``courseware_plan`` plus its digest ride in the task
        payload for the plan-driven executor; explicit manual generation
        without a plan keeps its existing behavior.
        """
        row = self.catalog_repository.get_lecture(sub_id)
        if not row or str(row.get("course_id")) != str(course_id):
            raise FileNotFoundError("Lecture is not in the authorized catalog")
        # CLIENT-STATE-R1 相邻缺陷同修：活动窗口取最新 20 条，防旧任务把
        # 当前 sub 的活动行挤出窗口（resume 亦取最新 paused 行）。
        existing = [
            task for task in self.task_store.list_tasks(
                kinds=("courseware_pdf",), states=ACTIVE_STATES, limit=20, newest_first=True,
            )
            if str(task.get("sub_id") or "") == str(sub_id)
        ]
        if action == "resume":
            paused = [task for task in existing if task.get("state") == "paused"]
            if not paused:
                raise ValueError("no paused courseware task to resume")
            task = paused[0]
            self.task_store.update_task(str(task["task_id"]), state="queued", error="")
            task = self.task_store.get_task(str(task["task_id"])) or task
        else:
            if existing:
                # one heavy operation per lecture: reuse the active task
                task = existing[0]
                if plan is not None and not dict(task.get("payload") or {}).get("courseware_plan"):
                    # Attach the verified plan to the already-queued task so
                    # the executor consumes the same plan the import verified.
                    updated = dict(task.get("payload") or {})
                    updated["courseware_plan"] = plan
                    updated["courseware_plan_digest"] = str(plan_digest or "")
                    task = self.task_store.update_task(
                        str(task["task_id"]), payload=updated,
                    ) or task
                return task
            payload = {
                "course_id": str(course_id),
                "sub_id": str(sub_id),
                "kind": "courseware_pdf",
            }
            if plan is not None:
                payload["courseware_plan"] = plan
                payload["courseware_plan_digest"] = str(plan_digest or "")
            task, _created = self.task_store.add_task(
                "courseware_pdf", str(course_id), str(sub_id), payload,
                config_key="courseware-pdf:v1",
            )
            task = self.task_store.update_task(
                str(task["task_id"]),
                progress={
                    "schema_version": 2,
                    "kind": "courseware_pdf",
                    "percent": 0.0,
                    "percent_measured": False,
                    "label": "课件 PDF 排队中",
                    "phase": "课件 PDF 排队中",
                    "stage": "queued",
                },
            ) or task
        with self._lock:
            task_id = str(task["task_id"])
            if task.get("state") == "queued" and task_id not in self._courseware_pdf_queue:
                self._courseware_pdf_queue.append(task_id)
            self._ensure_courseware_pdf_worker()
        return task

    def _ensure_courseware_pdf_worker(self) -> None:
        if self._courseware_pdf_worker and self._courseware_pdf_worker.is_alive():
            return
        self._courseware_pdf_worker = threading.Thread(
            target=self._run_courseware_pdf_queue,
            name="courseware-pdf-generator",
            daemon=True,
        )
        self._courseware_pdf_worker.start()

    def _courseware_pdf_fetch_page(self, record: dict) -> bytes:
        """Fetch one slide image through the existing authorized session."""
        from src.api.icourse import fetch_ppt_image
        client = self.client()
        raw = fetch_ppt_image(client, {"pptimgurl": str(record.get("pptimgurl") or "")})
        if raw is None:
            raise RuntimeError("slide fetch failed")
        return raw

    def _run_courseware_pdf_queue(self) -> None:
        while True:
            if self._generation_workers_stop.is_set():
                return
            with self._lock:
                if not self._courseware_pdf_queue:
                    return
                task_id = self._courseware_pdf_queue.pop(0)
            task = self.task_store.get_task(task_id)
            if not task or task.get("state") != "queued":
                continue
            payload = task.get("payload") or {}
            course_id = str(payload.get("course_id") or "")
            sub_id = str(payload.get("sub_id") or "")
            artifact_dir, pdf_path, manifest_path = self._courseware_pdf_paths(course_id, sub_id)
            work_dir = artifact_dir / "work"

            def report(
                stage: str,
                *,
                label: str,
                percent: float | None = None,
                measured: bool = False,
                counts: dict | None = None,
                **extra,
            ) -> None:
                # 百分比只在来自真实 processed/total 计数时才标记为 measured；
                # 前端对未测量进度只显示阶段文案，不显示进度条或百分比。
                progress = {
                    "schema_version": 2, "kind": "courseware_pdf",
                    "percent": round(float(percent), 1) if percent is not None else 0.0,
                    "percent_measured": bool(measured),
                    "label": label,
                    "phase": self._COURSEWARE_PDF_PHASE_LABELS.get(stage, stage),
                    "stage": stage,
                }
                if counts is not None:
                    progress["counts"] = counts
                progress.update(extra)
                self.task_store.update_task(task_id, progress=progress)

            self.task_store.update_task(
                task_id,
                state="running",
                started_at=time.time(),
            )
            # 第卅案可观测性：本地重生成全程留痕——此前启动/完成零日志，
            # 学生端无法判别管线是否真的开跑。
            print(
                f"[FudanCourseLens] Starting courseware PDF task {task_id}",
                flush=True,
            )
            report("records", label="正在获取课堂画面")
            try:
                records = self.client().get_ppt_list(course_id, sub_id)
            except Exception as exc:
                code = str(getattr(exc, "code", "") or "")
                print(
                    f"[FudanCourseLens] Courseware PDF task {task_id} failed: {code or 'ppt_list_failed'}",
                    flush=True,
                )
                self.task_store.mark_terminal(
                    task_id, "failed", error=code or "ppt_list_failed",
                )
                continue
            total = len(records)
            if not total:
                report("done", label="本讲没有可用的课堂画面")
                print(
                    f"[FudanCourseLens] Courseware PDF task {task_id} failed: no_records",
                    flush=True,
                )
                self.task_store.mark_terminal(task_id, "failed", error="no_records")
                continue

            last_report_key = {"value": None}

            def on_progress(stage: str, processed: int, kept: int, total_now: int) -> None:
                counts = {"processed": processed, "kept": kept, "total": total_now}
                percent = (
                    processed / total_now * 100.0
                    if stage == "pages" and total_now > 0
                    else None
                )
                key = (stage, round(float(percent), 1) if percent is not None else None)
                if key == last_report_key["value"]:
                    return
                last_report_key["value"] = key
                report(
                    stage,
                    label=self._COURSEWARE_PDF_PHASE_LABELS.get(stage, "正在处理课堂画面"),
                    percent=percent,
                    measured=percent is not None,
                    counts=counts,
                )

            run = CoursewarePdfRun(
                records=records,
                work_dir=work_dir,
                out_pdf=pdf_path,
                out_manifest=manifest_path,
                fetch_page=self._courseware_pdf_fetch_page,
                on_progress=on_progress,
                plan=payload.get("courseware_plan"),
                plan_digest=str(payload.get("courseware_plan_digest") or ""),
                course_id=course_id,
                sub_id=sub_id,
            )
            try:
                outcome = run.run()
            except Exception as exc:
                code = str(getattr(exc, "code", "") or "courseware_pdf_failed")
                print(
                    f"[FudanCourseLens] Courseware PDF task {task_id} failed: {code}",
                    flush=True,
                )
                self.task_store.mark_terminal(task_id, "failed", error=code)
                continue
            counts = {
                "events_total": int(outcome.get("events_total") or 0),
                "kept": int(outcome.get("kept") or 0),
                "duplicates": int(outcome.get("duplicates") or 0),
                "skipped": dict(outcome.get("skipped") or {}),
            }
            if outcome.get("state") == "paused":
                code = str(outcome.get("code") or "resource_budget_paused")
                reason = {
                    "resource_budget_paused": "已达到本地资源预算",
                    "event_storm_paused": "课堂画面密度异常",
                    "wall_clock_paused": "耗时达到上限",
                    "courseware_plan_changed": "课件清单已变化",
                    "plan_fetch_paused": "课堂画面获取失败",
                    "plan_page_unreadable": "课件页面无法读取",
                }.get(code, "已达到资源护栏")
                # 暂停只改写含义文案：可信的 measured 百分比与计数原样保留
                current = self.task_store.get_task(task_id) or {}
                progress = dict(current.get("progress") or {})
                progress.update({
                    "label": f"已暂停：{reason}；进度已保存",
                    "stage": "paused",
                    "pause_code": code,
                })
                self.task_store.update_task(task_id, progress=progress)
                self.task_store.update_task(task_id, state="paused")
                continue
            kept = int(outcome.get("kept") or 0)
            if outcome.get("code") == "no_pages":
                report("done", label="没有可保留的课堂画面", counts=counts)
                print(
                    f"[FudanCourseLens] Courseware PDF task {task_id} failed: no_pages"
                    f" events={counts['events_total']} skipped={counts['skipped']}",
                    flush=True,
                )
                self.task_store.mark_terminal(task_id, "failed", error="no_pages")
                continue
            report(
                "done",
                label="课件 PDF 已生成",
                percent=100.0,
                measured=True,
                counts=counts,
            )
            print(
                f"[FudanCourseLens] Courseware PDF task {task_id} completed:"
                f" pages={kept} events={counts['events_total']}"
                f" duplicates={counts['duplicates']} skipped={counts['skipped']}",
                flush=True,
            )
            self.task_store.mark_terminal(task_id, "completed")

    def search_learning(
        self,
        query: object,
        *,
        course_ids: list[str] | None = None,
        sources: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        sub_id: str = "",
    ) -> dict:
        self.start_search_index()
        return self.search_index.search(
            query,
            course_ids=course_ids,
            sources=sources,
            limit=limit,
            offset=offset,
            sub_id=sub_id,
        )

    def refresh_search_index(self, *, force: bool = False) -> dict:
        self.start_search_index()
        return self.search_index.request_refresh(force=force)

    def watch_progress(self, sub_id: str) -> dict:
        if not self.catalog_repository.get_lecture(sub_id):
            raise FileNotFoundError("Lecture is not in the authorized catalog")
        return self.learning_store.get_watch_progress(sub_id)

    def save_watch_progress(
        self,
        sub_id: str,
        *,
        position_seconds: object,
        duration_seconds: object,
        completed: bool = False,
        playback_rate: object = 1.0,
    ) -> dict:
        row = self.catalog_repository.get_lecture(sub_id)
        if not row:
            raise FileNotFoundError("Lecture is not in the authorized catalog")
        progress = self.learning_store.save_watch_progress(
            course_id=str(row.get("course_id") or ""),
            sub_id=str(sub_id),
            position_seconds=position_seconds,
            duration_seconds=duration_seconds,
            completed=completed,
            playback_rate=playback_rate,
        )
        course = dict((self.catalog_repository.snapshot().get("courses") or {}).get(str(row.get("course_id") or "")) or {})
        record_watch_event(
            self.learning_store.path,
            course_id=str(row.get("course_id") or ""),
            sub_id=str(sub_id),
            term=str(course.get("term") or course.get("semester") or course.get("term_name") or ""),
            position_seconds=float(progress.get("position_seconds") or 0),
            duration_seconds=float(progress.get("duration_seconds") or 0),
            playback_rate=float(progress.get("playback_rate") or 1),
            completed=bool(progress.get("completed")),
        )
        return progress










    def _bookmark_evidence(
        self, course_id: str, sub_id: str, start_ms: int, end_ms: int, bookmark_id: str = "pending"
    ) -> list[dict]:
        results: list[dict] = []
        try:
            segments = list(self.subtitle_segments(sub_id).get("segments") or [])
        except (FileNotFoundError, KeyError, ValueError):
            segments = []
        matches: list[tuple[int, int, int, dict]] = []
        for index, segment in enumerate(segments):
            segment_start = int(segment.get("start_ms") or 0)
            segment_end = max(segment_start, int(segment.get("end_ms") or segment_start))
            if segment_end < start_ms - 30_000 or segment_start > end_ms + 60_000:
                continue
            if not str(segment.get("text") or "").strip():
                continue
            matches.append((index, segment_start, segment_end, segment))

        def _anchor_rank(match: tuple[int, int, int, dict]) -> tuple[int, int, int]:
            index, segment_start, segment_end, _ = match
            intersects = segment_start <= end_ms and segment_end >= start_ms
            distance = 0 if intersects else min(abs(segment_start - end_ms), abs(start_ms - segment_end))
            return (0 if intersects else 1, distance, index)

        # 锚点段优先：30s 回看窗在密段讲次里足以填满 8 帽，若按时间序从旧端
        # 截断，被「没听懂」的那段话本身反而进不了证据（FINALWRAP-C4 实测：
        # 证据全在锚前 30s、锚段 225 落榜 → 解释恒 needs_context）。
        for index, segment_start, segment_end, segment in sorted(matches, key=_anchor_rank)[:8]:
            text = str(segment.get("text") or "").strip()
            results.append({
                "result_id": f"bookmark:{bookmark_id}:transcript:{index}",
                "course_id": str(course_id),
                "sub_id": str(sub_id),
                "snippet": text,
                "source": "transcript",
                "label": "同步字幕",
                "source_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "evidence_id": str(segment.get("evidence_id") or ""),
                "target": {
                    "start_seconds": segment_start / 1000.0,
                    "end_seconds": segment_end / 1000.0,
                },
            })

        chapter_artifact = self.learning_store.find_ai_artifact(sub_id, "lecture_chapters")
        chapters = list(dict((chapter_artifact or {}).get("content") or {}).get("chapters") or [])
        for index, chapter in enumerate(chapters):
            chapter_start = int(chapter.get("start_ms") or float(chapter.get("start_seconds") or 0) * 1000)
            if abs(chapter_start - start_ms) > 120_000:
                continue
            text = "：".join(
                value for value in (
                    str(chapter.get("title") or "").strip(),
                    str(chapter.get("summary") or "").strip(),
                ) if value
            )
            if not text:
                continue
            results.append({
                "result_id": f"bookmark:{bookmark_id}:chapter:{index}",
                "course_id": str(course_id), "sub_id": str(sub_id), "snippet": text,
                "source": "chapter", "label": str(chapter.get("title") or "课程章节"),
                "source_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "target": {"start_seconds": chapter_start / 1000.0, "end_seconds": chapter_start / 1000.0},
            })
            if len(results) >= 10:
                break

        for index, page in enumerate(document_search_pages(self.learning_store.path, sub_id)):
            page_start = page.get("start_ms")
            if page_start is None or abs(int(page_start) - start_ms) > 120_000:
                continue
            text = str(page.get("text") or "").strip()
            if not text:
                continue
            results.append({
                "result_id": f"bookmark:{bookmark_id}:document:{index}",
                "course_id": str(course_id), "sub_id": str(sub_id), "snippet": text,
                "source": "document",
                "label": f"{str(page.get('title') or '讲义')} · 第 {int(page.get('page_num') or 0)} 页",
                "source_hash": str(page.get("text_hash") or hashlib.sha256(text.encode("utf-8")).hexdigest()),
                "target": {"start_seconds": int(page_start) / 1000.0, "end_seconds": int(page_start) / 1000.0},
            })
            if len(results) >= 12:
                break
        return evidence_packet(results, limit=12)

    @staticmethod
    def _normalize_query_text(query: str) -> str:
        """N5A-P4 查询归一化：全角→半角、连续空白折叠、去首尾空白。"""
        width = {chr(code): chr(code - 0xFEE0) for code in range(0xFF01, 0xFF5F)}
        width[chr(0x3000)] = " "
        text = str(query or "").translate(str.maketrans(width))
        return " ".join(text.split()).strip()

    @staticmethod
    def _bookmark_input_hash(query: str, evidence: list[dict]) -> str:
        payload = {
            "query": CourseLensApplication._normalize_query_text(query),
            "evidence": evidence,
            "prompt_version": BOOKMARK_ANSWER_PIPELINE_VERSION,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def create_question_bookmark(self, course_id: str, sub_id: str, start_ms: int, end_ms: int, note: str = "") -> dict:
        course_id = str(course_id or "").strip()
        sub_id = str(sub_id or "").strip()
        if not course_id or not sub_id:
            # RR-BOOKMARK-1：缺课程/讲次身份的请求直接拒绝，不落库。
            raise ValueError("bookmark_request_invalid")
        query = str(note or "").strip() or "解释这个时间点附近的课程内容"
        evidence = self._bookmark_evidence(course_id, sub_id, start_ms, end_ms)
        if not evidence:
            # RR-BOOKMARK-1（WINIT-1 实测误建 1 枚空白书签）：讲次零可依据内容
            # （字幕/章节/讲义全空）时落库即废品——解释链对零证据书签恒
            # needs_context。创建时即拒绝；复用 explain 链同源闭集码，
            # 前端码表已有人话映射（请先生成字幕）。
            raise ValueError("bookmark_evidence_unavailable")
        input_hash = self._bookmark_input_hash(query, evidence)
        return create_bookmark(
            self.learning_store.path,
            course_id=course_id,
            sub_id=sub_id,
            start_ms=start_ms,
            end_ms=end_ms,
            note=note,
            evidence=evidence,
            input_hash=input_hash,
            prompt_version=BOOKMARK_ANSWER_PIPELINE_VERSION,
        )

    def create_review_plan(self, *, title: str, exam_at: float, available_minutes: int,
                           course_id: str = "", sub_id: str = "",
                           daily_minutes: int | None = None, strategy: str = "coverage",
                           course_scope: list[str] | None = None) -> dict:
        payload = {
            "title": str(title), "exam_at": float(exam_at),
            "available_minutes": int(available_minutes),
            "course_id": str(course_id), "sub_id": str(sub_id),
            "daily_minutes": daily_minutes,
            "strategy": str(strategy or "coverage"),
            "course_scope": [str(value) for value in (course_scope or []) if str(value)] or None,
        }
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:24]
        return self._recorded_operation(
            "review_plan", course_id, sub_id, key, payload,
            lambda: self._create_review_plan_impl(**payload), result_version="review-plan-v3",
        )

    def _resolve_course_identity(self, course_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Codes and titles for scope courses, from catalog + timetable links."""
        identities: dict[str, dict[str, Any]] = {
            course_id: {"codes": [], "name": ""} for course_id in course_ids
        }
        try:
            for course in self.catalog_repository.courses_for_ids(set(course_ids)):
                course_id = str(course.get("course_id") or "")
                if course_id in identities:
                    identities[course_id]["name"] = str(course.get("title") or "")
        except Exception:
            pass
        try:
            snapshot = self.timetable.snapshot()
            semester_start = str((snapshot.get("selected_semester") or {}).get("start_date") or "")
            semester_range = semester_range_from_start(semester_start)
            for course in snapshot.get("courses") or []:
                link = course.get("catalog_link") or {}
                linked_id = str(link.get("course_id") or "")
                if link.get("state") == "linked" and linked_id in identities:
                    code = str(course.get("course_code") or "")
                    if code and code not in identities[linked_id]["codes"]:
                        identities[linked_id]["codes"].append(code)
            for identity in identities.values():
                identity["semester_range"] = semester_range
        except Exception:
            for identity in identities.values():
                identity["semester_range"] = None
        return identities

    def _resolve_exam_context(self, *, scope_course_ids: list[str]) -> dict[str, Any]:
        """Read-only Fudan exam lookup; every failure degrades to unavailable."""
        now = time.time()
        if not scope_course_ids:
            return unavailable_context("no_match")
        auth = self.authentication_snapshot()
        student_id = str(self._credentials.get("student_id") or "").strip()
        if auth.get("state") != "ready" or not student_id:
            return unavailable_context("session_unavailable")
        try:
            rows = fetch_exam_rows(self._timetable_vpn(), student_id)
        except Exception:
            rows = None
        if rows is None:
            return unavailable_context("fetch_failed")
        if not rows:
            return unavailable_context("no_rows")
        identities = self._resolve_course_identity(scope_course_ids)
        matched: dict[str, dict[str, Any]] = {}
        ambiguous = False
        for course_id in scope_course_ids:
            identity = identities.get(course_id) or {"codes": [], "name": "", "semester_range": None}
            row, detail = match_exam_row(
                rows,
                course_codes=list(identity.get("codes") or []),
                course_name=str(identity.get("name") or ""),
                semester_range=identity.get("semester_range"),
            )
            if detail == "ambiguous":
                ambiguous = True
                continue
            if row is not None:
                matched[course_id] = row
        if not matched:
            return unavailable_context("ambiguous" if ambiguous else "no_match")
        # Multi-course plans bind to the nearest upcoming deadline, deterministically.
        def _sort_key(row: dict[str, Any]) -> tuple:
            try:
                start = datetime.fromisoformat(
                    f"{str(row.get('date') or '')}T{str(row.get('start_time') or '00:00')}:00"
                ).replace(tzinfo=SHANGHAI)
                return (start.timestamp(), str(row.get("course_code") or ""))
            except ValueError:
                return (float("inf"), str(row.get("course_code") or ""))
        chosen_row = min(matched.values(), key=_sort_key)
        context = context_from_exam(chosen_row, now=datetime.now(SHANGHAI))
        return context if context is not None else unavailable_context("malformed")

    def _create_review_plan_impl(self, *, title: str, exam_at: float, available_minutes: int,
                                 course_id: str = "", sub_id: str = "",
                                 daily_minutes: int | None = None,
                                 strategy: str = "coverage",
                                 course_scope: list[str] | None = None) -> dict:
        selected_strategy = str(strategy or "coverage")
        if selected_strategy not in PLAN_STRATEGIES:
            raise ReviewPlanValidationError("review_strategy_invalid")
        if daily_minutes is not None and (
            isinstance(daily_minutes, bool) or not isinstance(daily_minutes, int) or daily_minutes <= 0
        ):
            raise ReviewPlanValidationError("review_minutes_invalid")
        explicit_scope = [str(value) for value in (course_scope or []) if str(value)]
        if len(explicit_scope) > 50:
            raise ReviewPlanValidationError("review_scope_required")
        scope_course_ids = list(dict.fromkeys(explicit_scope or ([str(course_id)] if str(course_id) else [])))
        if float(exam_at) > 0:
            exam_context = user_confirmed_context(float(exam_at), now=time.time())
        else:
            exam_context = self._resolve_exam_context(scope_course_ids=scope_course_ids)
        if exam_context.get("exam_state") == "active" and not scope_course_ids:
            # 考试窗口内的计划必须有明确的课程范围，否则闭集拒绝。
            raise ReviewPlanValidationError("review_scope_required")
        planner_exam_at = float(exam_at)
        if planner_exam_at <= 0:
            planner_exam_at = exam_start_epoch(exam_context) or time.time() + 86400
        chapter_artifact = self.learning_store.find_ai_artifact(sub_id, "lecture_chapters") if sub_id else None
        chapter_content = dict((chapter_artifact or {}).get("content") or {})
        chapters = list(chapter_content.get("chapters") or [])
        quizzes: list[dict] = []
        for scoped_course_id in scope_course_ids or [""]:
            quizzes.extend(list_quiz_items(self.learning_store.path, course_id=scoped_course_id, sub_id=sub_id))
        ir_artifact = self.learning_store.find_ai_artifact(sub_id, "lecture_ir") if sub_id else None
        ir_content = dict((ir_artifact or {}).get("content") or {})
        key_moments = list(ir_content.get("key_moments") or []) if ir_content else []
        watch_progress = self.learning_store.get_watch_progress(sub_id) if sub_id else {}
        try:
            assessment_events = confirmed_schedule_events(
                self.learning_store, course_scope=scope_course_ids,
                until_epoch=planner_exam_at,
            )
        except Exception:
            # P13-B 联动 fail-open：聚合读任何失败→空列表照常建计划（考核步零增量）。
            assessment_events = []
        steps = build_review_steps(
            chapters=chapters,
            quiz_items=quizzes,
            exam_at=planner_exam_at,
            available_minutes=available_minutes,
            watched_seconds=float(watch_progress.get("position_seconds") or 0),
            strategy=selected_strategy,
            course_id=str(course_id),
            sub_id=str(sub_id),
            daily_minutes=daily_minutes if isinstance(daily_minutes, int) and not isinstance(daily_minutes, bool) and daily_minutes > 0 else None,
            key_moments=key_moments,
            attempt_stats=quiz_attempt_stats(self.learning_store.path),
            assessment_events=assessment_events,
        )
        return save_review_plan(
            self.learning_store.path,
            title=title,
            exam_at=exam_at,
            available_minutes=available_minutes,
            scope={"course_id": str(course_id), "sub_id": str(sub_id)},
            steps=steps,
            exam_context=exam_context,
            strategy=selected_strategy,
            daily_minutes=daily_minutes if isinstance(daily_minutes, int) and not isinstance(daily_minutes, bool) and daily_minutes > 0 else None,
            course_scope=scope_course_ids or None,
        )

    def answer_from_evidence(
        self, query: str, *, course_ids: list[str] | None = None, sub_id: str = "", mode: str = ""
    ) -> dict:
        # P3-CONTRACT-1：mode 闭集 {"", "deep"}——缺席/空串=既有本地拼装逐字
        # 不变；"deep"=深度回答（检索证据 → 云端问答链）；其余值一律拒绝。
        selected_mode = str(mode or "")
        if selected_mode not in ("", "deep"):
            raise ValueError("deep_answer_mode_invalid")
        if selected_mode == "deep":
            return self._deep_answer_dispatch(str(query), course_ids=course_ids, sub_id=sub_id)
        selected = sorted({str(value) for value in (course_ids or []) if str(value)})
        lecture = str(sub_id or "").strip()
        payload = {"query": str(query), "course_ids": selected}
        if lecture:
            payload["sub_id"] = lecture
        key = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:24]
        return self._recorded_operation(
            "search_answer", ",".join(selected), lecture, key, payload,
            lambda: self._answer_from_evidence_impl(str(query), course_ids=selected, sub_id=lecture),
            result_version="evidence-answer-v1",
        )

    def _answer_from_evidence_impl(
        self, query: str, *, course_ids: list[str] | None = None, sub_id: str = ""
    ) -> dict:
        result = self.search_learning(
            query, course_ids=course_ids or [], sources=[], limit=8, offset=0,
            sub_id=str(sub_id or "").strip(),
        )
        return evidence_answer(query, list(result.get("results") or []))

    def _deep_answer_dispatch(
        self, query: str, *, course_ids: list[str] | None = None, sub_id: str = ""
    ) -> dict:
        """P3 深度回答分派：检索证据 → 入队既有云端问答链（opt-in，每次点击一调用）。

        mode="deep" 不走 _recorded_operation——任务是记录本身，双重记录是污染；
        去重=add_task 既有活跃态判据（排队中双击零双扣，终态失败重发=新任务）；
        空 packet=诚实即时降级：零任务零计费零 LLM，判词与书签链/worker 三处统一。
        D-14-RELAX：检索走三阶松弛阶梯（exact→content_and→content_or），响应带
        match_tier 闭集字段（exact/content_and/content_or/none）可观测。
        """
        selected = sorted({str(value) for value in (course_ids or []) if str(value)})
        lecture = str(sub_id or "").strip()
        rows, match_tier = self._deep_search_with_relaxation(
            query, course_ids=selected or [], sub_id=lecture, limit=8,
        )
        packet = evidence_packet(rows, limit=8)
        if not packet:
            return {
                "answer": "资料不足，无法根据当前课程资料回答。",
                "citations": [], "grounded": False, "mode": "declined",
                "query": query, "error_code": "deep_answer_evidence_unavailable",
                "match_tier": "none",
            }
        if not self._deepseek_key():
            raise RuntimeError("DeepSeek API key is required for question explanations")
        input_hash = self._bookmark_input_hash(query, packet)
        # 入队前闭集预检（与书签链同式）：8 条×1000 字远低于帽值，正常恒过。
        outbound_query, oversized = _question_payload_preflight(query, packet)
        if oversized:
            raise ValueError(oversized)
        payload = {
            "deep_answer": True,
            "query": outbound_query,
            "evidence": packet,
            "input_hash": input_hash,
            "prompt_version": BOOKMARK_ANSWER_PIPELINE_VERSION,
            "cancel_requested": False,
        }
        task, created = self.task_store.add_task(
            "question", selected[0] if selected else "", lecture, payload,
            config_key=f"palette:{input_hash}",
        )
        # 合同偏差钉（提请总控仲裁）：v3 metadata 的 dedupe_key 带库级 UNIQUE
        # 索引，而本链终态失败重发=add_task 新任务（§① 步骤 8），同一
        # input_hash 的新旧行会撞键。取最小偏差：保留 question:palette:<hash>
        # 前缀家族身份，追加 task_id 保证每次尝试唯一；config_key（活跃态去重
        # 依据）仍逐字 = palette:<hash>。
        self.task_store.upsert_v3_metadata(
            str(task["task_id"]), dedupe_key=f"question:palette:{input_hash}:{task['task_id']}",
            stage="queued", privacy_state="sealed", requested_outputs=["answer"],
            input_hash=input_hash,
        )
        task = self.task_store.update_task(
            str(task["task_id"]), progress={
                "schema_version": 3, "stage": "queued", "percent": None,
                "label": "深度回答等待在线计算", "indeterminate": True,
                "observed_at": time.time(),
            },
        ) or task
        with self._lock:
            self._queue_persisted_task(task)
            self._ensure_question_worker()
        return {
            "mode": "deep",
            "task_id": str(task["task_id"]),
            "task_state": str(task.get("state") or "queued"),
            "created": bool(created),
            "query": outbound_query,
            "input_hash": input_hash,
            "match_tier": match_tier,
        }

    def _deep_search_with_relaxation(
        self, query: str, *, course_ids: list[str], sub_id: str, limit: int
    ) -> tuple[list[dict], str]:
        """D-14-RELAX 三阶检索阶梯：原句 exact → 内容词合取 content_and → 析取 content_or。

        缺陷 D-20261009-14：自然问句原句 FTS trigram 逐字 AND 恒零命中，学生
        换一种说法即被诚实降级「资料不足」。本阶梯只在 tier1 零命中时降阶重试：

        - tier1 ``exact``：原句逐字（与既有行为逐字相同，含异常语义，恒先试）；
        - tier2 ``content_and``：功能词剥离后的内容词合取（query_relaxation R1）；
        - tier3 ``content_or``：逐内容词独立检索，按每条结果命中的内容词数
          （再按各词内名次）合并排序截断——分派内部行列表，不伪造 search 响应。

        每阶都走既有 search_learning 的 exact AND——FTS/LIKE 层零改，单词精确
        查询与多词空格查询零回归。全阶零命中返回空表（诚实降级语义零改）；
        match_tier 闭集=exact/content_and/content_or/none，随分派响应可观测。
        """
        result = self.search_learning(
            query, course_ids=course_ids or [], sources=[], limit=limit, offset=0, sub_id=sub_id,
        )
        rows = list(result.get("results") or [])
        if rows:
            return rows, "exact"
        terms = extract_content_terms(query)
        if not terms:
            return [], "none"
        tier1_terms = normalize_search_text(query).split()
        if list(terms) != list(tier1_terms):
            # tier2 与 tier1 同串时跳过（避免同查重试与 match_tier 误标）。
            relaxed = self.search_learning(
                " ".join(terms), course_ids=course_ids or [], sources=[],
                limit=limit, offset=0, sub_id=sub_id,
            )
            rows = list(relaxed.get("results") or [])
            if rows:
                return rows, "content_and"
        merged: dict[str, list] = {}
        for term in or_tier_terms(terms):
            tier_rows = self.search_learning(
                term, course_ids=course_ids or [], sources=[],
                limit=limit, offset=0, sub_id=sub_id,
            ).get("results") or []
            for rank, row in enumerate(tier_rows):
                key = str(row.get("result_id") or "")
                if not key:
                    continue
                entry = merged.get(key)
                if entry is None:
                    entry = merged[key] = [0, rank, row]
                entry[0] += 1
                entry[1] = min(entry[1], rank)
        if not merged:
            return [], "none"
        ordered = sorted(merged.values(), key=lambda entry: (-entry[0], entry[1]))
        return [entry[2] for entry in ordered[:limit]], "content_or"

    def _persist_deep_search_answer(
        self, *, task_id: str, input_hash: str, query: str, course_ids: list[str],
        sub_id: str, state: str, answer: str, citations: list, grounded: bool,
        model: str, prompt_version: str, error_code: str = "",
    ) -> None:
        """P3 深度回答唯一本地落点：search_answers store（写入回调）。

        DEFECT-2 根修：save_search_answer 已在 student_features 落地（learning.db
        search_answers 表，task_id 主键），本处由 getattr 空转改为实调——学生的
        真实付费回答不再静默丢失。落地仍尽力而为（与 _record_task_usage 同纪律）：
        store 失败不推翻已交付的答案、不挡任务收口。ready/insufficient 由
        import_result 写，failed 由终态失败分支写；取消/暂停不落（state 闭集
        只有 ready|insufficient|failed）。
        """
        try:
            from src.runtime import student_features

            student_features.save_search_answer(
                self.learning_store.path,
                task_id=str(task_id), input_hash=str(input_hash), query=str(query),
                course_ids=[str(value) for value in (course_ids or [])],
                sub_id=str(sub_id), state=str(state), answer=str(answer),
                citations=list(citations or []), grounded=bool(grounded),
                model=str(model), prompt_version=str(prompt_version),
                error_code=str(error_code or ""),
            )
        except Exception:  # noqa: BLE001 - 落库尽力而为，绝不挡导入/收口
            return

    def generate_quiz(self, course_id: str, sub_id: str) -> list[dict]:
        payload = {"course_id": str(course_id), "sub_id": str(sub_id)}
        return self._recorded_operation(
            "quiz", course_id, sub_id, "quiz-v2", payload,
            lambda: self._generate_quiz_impl(str(course_id), str(sub_id)),
            result_version="quiz-evidence-v2",
        )

    def _generate_quiz_impl(self, course_id: str, sub_id: str) -> list[dict]:
        try:
            segments = list(self.subtitle_segments(sub_id).get("segments") or [])
        except (FileNotFoundError, KeyError, ValueError):
            segments = []
        chapter_artifact = self.learning_store.find_ai_artifact(sub_id, "lecture_chapters")
        chapters = list(dict((chapter_artifact or {}).get("content") or {}).get("chapters") or [])
        items = build_quiz_items(course_id=course_id, sub_id=sub_id, segments=segments, chapters=chapters)
        return save_quiz_items(self.learning_store.path, items)

    def list_quizzes(self, course_id: str = "", sub_id: str = "") -> list[dict]:
        return list_quiz_items(self.learning_store.path, course_id=course_id, sub_id=sub_id)

    def list_review_plans(self) -> list[dict]:
        return list_review_plans(self.learning_store.path)

    def import_learning_document(
        self,
        *,
        course_id: str,
        sub_id: str,
        title: str,
        original_name: str,
        media_type: str,
        content_base64: str,
        doc_type: str = "other",
        scope: str = "lecture",
    ) -> dict:
        content_hash = hashlib.sha256(str(content_base64).encode("ascii", errors="ignore")).hexdigest()
        payload = {
            "course_id": str(course_id), "sub_id": str(sub_id), "title": str(title),
            "original_name": str(original_name), "media_type": str(media_type),
            "content_hash": content_hash,
            "doc_type": str(doc_type), "scope": str(scope),
        }
        return self._recorded_operation(
            "document_import", course_id, sub_id, content_hash[:24], payload,
            lambda: self._import_learning_document_impl(
                course_id=course_id, sub_id=sub_id, title=title,
                original_name=original_name, media_type=media_type,
                content_base64=content_base64,
                doc_type=doc_type, scope=scope,
            ),
            result_version="document-import-v1",
        )

    def _import_learning_document_impl(
        self, *, course_id: str, sub_id: str, title: str, original_name: str,
        media_type: str, content_base64: str,
        doc_type: str = "other", scope: str = "lecture",
    ) -> dict:
        document = register_document(
            self.learning_store.path,
            self.learning_store.path.parent,
            course_id=course_id,
            sub_id=sub_id,
            title=title,
            original_name=original_name,
            media_type=media_type,
            content_base64=content_base64,
            doc_type=doc_type,
            scope=scope,
        )
        # N5A-P5：真题文档顺手拆题（纯结构正则，指纹缓存；失败不影响导入）
        if doc_type == "exam_paper":
            try:
                refresh_exam_questions(self.learning_store.path, str(document["document_id"]))
            except Exception:
                pass
        # N7R：考核文档（真题/作业）投影成 AssessmentItem——课程「练习与真题」视图
        # 直接读这张表，不投影就只剩空壳。闸门取回库后的规范化字段：类型必须是
        # 考核类（与拆题同集合 SPLITTABLE_DOC_TYPES），且只认讲次级——课程级资料、
        # 普通资料与个人笔记都不凭空生题。重复导入由内容指纹保证幂等；沿用拆题
        # 那一套「投影失败不阻断资料导入」语义。
        is_assessment = str(document.get("doc_type") or "") in {"exam_paper", "homework"}
        if is_assessment and str(document.get("scope") or "") != "course":
            try:
                refresh_assessment_items(self.learning_store.path, str(document["document_id"]))
            except Exception:
                pass
        # U8：课程级资料（scope=course，sub_id 强制为空）不参与时间轴对齐，
        # 只进搜索索引；讲次级保持既有对齐链路。
        try:
            segments = [] if scope == "course" or not sub_id else list(
                self.subtitle_segments(sub_id).get("segments") or []
            )
        except (FileNotFoundError, KeyError, ValueError):
            segments = []
        if segments and any(str(page.get("text") or "").strip() for page in document.get("pages") or []):
            align_document(self.learning_store.path, str(document["document_id"]), segments)
            document = get_document(self.learning_store.path, str(document["document_id"]), include_pages=True)
        self.search_index.request_refresh([sub_id or course_id], force=True)
        return document

    def list_learning_documents(self, course_id: str = "", sub_id: str = "") -> list[dict]:
        return list_documents(self.learning_store.path, course_id=course_id, sub_id=sub_id)

    def learning_document(self, document_id: str) -> dict:
        return get_document(self.learning_store.path, document_id, include_pages=True)

    def learning_document_preview_path(self, document_id: str) -> Path:
        path, extension = document_storage_path(self.learning_store.path, document_id)
        if extension not in {".png", ".jpg", ".jpeg", ".webp", ".bmp"} or not path.is_file():
            raise FileNotFoundError("document_preview_unavailable")
        root = (self.learning_store.path.parent / "documents").resolve()
        resolved = path.resolve()
        if root not in resolved.parents:
            raise PermissionError("document_preview_outside_storage")
        return resolved

    def align_learning_document(self, document_id: str) -> dict:
        document = get_document(self.learning_store.path, document_id, include_pages=False)
        payload = {
            "document_id": str(document_id), "course_id": str(document.get("course_id") or ""),
            "sub_id": str(document.get("sub_id") or ""),
        }
        return self._recorded_operation(
            "document_alignment", payload["course_id"], payload["sub_id"],
            str(document_id), payload,
            lambda: self._align_learning_document_impl(str(document_id), document=document),
            result_version="monotonic-alignment-v1",
        )

    def _align_learning_document_impl(self, document_id: str, *, document: dict | None = None) -> dict:
        document = document or get_document(self.learning_store.path, document_id, include_pages=False)
        try:
            segments = list(self.subtitle_segments(str(document["sub_id"])).get("segments") or [])
        except (FileNotFoundError, KeyError, ValueError):
            segments = []
        result = align_document(self.learning_store.path, document_id, segments)
        self.search_index.request_refresh([str(document["sub_id"])], force=True)
        return result

    def update_document_alignment(
        self, alignment_id: str, *, start_ms: int, end_ms: int, status: str = "confirmed"
    ) -> dict:
        value = update_alignment(
            self.learning_store.path,
            alignment_id=alignment_id,
            start_ms=start_ms,
            end_ms=end_ms,
            status=status,
        )
        self.search_index.request_refresh([str(value.get("sub_id") or "")], force=True)
        return value

    def delete_learning_document(self, document_id: str) -> bool:
        document = get_document(self.learning_store.path, document_id, include_pages=False)
        directory = delete_document(self.learning_store.path, document_id).resolve()
        root = (self.learning_store.path.parent / "documents").resolve()
        if directory != root and root in directory.parents and directory.is_dir():
            shutil.rmtree(directory)
        # N7R：题目与文档同生共死——文档行已删，残留的 AssessmentItem 仍挂在
        # course_id 上，课程「练习与真题」会继续显示点不开的幽灵题。按明确
        # document_id 精确级联（只删本文档的行，不碰同类其他文档）。文档此刻已经
        # 真的删掉了，这里失败也不向上抛：把「已删除」报成错误比留下残留更糟，
        # 历史残留由 sweep_orphan_assessment_items 标记兜底。
        try:
            delete_assessment_items(self.learning_store.path, document_id=str(document_id))
        except Exception:
            pass
        self.search_index.request_refresh([str(document.get("sub_id") or "")], force=True)
        return True

    def smart_timeline(self, sub_id: str) -> dict:
        try:
            transcript = list(self.subtitle_segments(str(sub_id)).get("segments") or [])
        except (FileNotFoundError, KeyError, ValueError):
            transcript = []
        current_hash = timeline_input_hash(transcript) if transcript else "__transcript_missing__"
        return list_timeline(
            self.learning_store.path,
            sub_id=str(sub_id),
            current_input_hash=current_hash,
        )

    def classify_smart_timeline(self, course_id: str, sub_id: str) -> dict:
        payload = {"course_id": str(course_id), "sub_id": str(sub_id)}
        return self._recorded_operation(
            "timeline_classification", course_id, sub_id, "evidence-rules-zh-v1", payload,
            lambda: self._classify_smart_timeline_impl(str(course_id), str(sub_id)),
            result_version="evidence-rules-zh-v1",
        )

    def _classify_smart_timeline_impl(self, course_id: str, sub_id: str) -> dict:
        try:
            transcript = list(self.subtitle_segments(str(sub_id)).get("segments") or [])
        except (FileNotFoundError, KeyError, ValueError):
            transcript = []
        if not transcript:
            raise ValueError("timeline_transcript_unavailable")
        classified = classify_timeline_segments(
            course_id=str(course_id),
            sub_id=str(sub_id),
            segments=transcript,
        )
        return save_timeline(
            self.learning_store.path,
            course_id=str(course_id),
            sub_id=str(sub_id),
            transcript_segments=transcript,
            classified=classified,
        )

    def analyze_cross_course_concepts(self, course_ids: list[str] | None = None) -> dict:
        selected_ids = sorted({str(value) for value in (course_ids or []) if str(value)})
        payload = {"course_ids": selected_ids}
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:24]
        return self._recorded_operation(
            "concept_analysis", ",".join(selected_ids), "", key, payload,
            lambda: self._analyze_cross_course_concepts_impl(selected_ids),
            result_version="evidence-concepts-zh-v1",
        )

    def _analyze_cross_course_concepts_impl(self, course_ids: list[str] | None = None) -> dict:
        snapshot = self.catalog_repository.snapshot()
        courses = dict(snapshot.get("courses") or {})
        lectures = dict(snapshot.get("lectures") or {})
        selected = {str(value) for value in (course_ids or []) if str(value)} or set(courses)
        if len(selected) < 2:
            raise ValueError("concept_analysis_requires_two_courses")
        sources: list[dict] = []
        for sub_id, lecture in lectures.items():
            course_id = str(lecture.get("course_id") or "")
            if course_id not in selected:
                continue
            try:
                segments = list(self.subtitle_segments(str(sub_id)).get("segments") or [])
            except (FileNotFoundError, KeyError, ValueError):
                segments = []
            normalized = [
                {
                    "start_ms": int(item.get("start_ms") or 0),
                    "end_ms": int(item.get("end_ms") or item.get("start_ms") or 0),
                    "text": str(item.get("text") or ""),
                    "source": "transcript",
                }
                for item in segments if str(item.get("text") or "").strip()
            ]
            for page in document_search_pages(self.learning_store.path, str(sub_id)):
                if page.get("start_ms") is None or not str(page.get("text") or "").strip():
                    continue
                normalized.append({
                    "start_ms": int(page["start_ms"]), "end_ms": int(page["start_ms"]),
                    "text": str(page["text"]), "source": "document",
                })
            if normalized:
                sources.append({"course_id": course_id, "sub_id": str(sub_id), "segments": normalized})
        result = analyze_concepts(self.learning_store.path, sources)
        result["requested_course_count"] = len(selected)
        result["source_lecture_count"] = len(sources)
        return result

    def _concept_source_hashes(self, sub_ids: set[str]) -> dict[str, set[str]]:
        values: dict[str, set[str]] = {}
        for sub_id in sub_ids:
            hashes: set[str] = set()
            try:
                segments = list(self.subtitle_segments(sub_id).get("segments") or [])
            except (FileNotFoundError, KeyError, ValueError):
                segments = []
            for item in segments:
                text = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(item.get("text") or ""))).strip()
                if text:
                    hashes.add(hashlib.sha256(text.encode("utf-8")).hexdigest())
            for page in document_search_pages(self.learning_store.path, sub_id):
                if page.get("text_hash"):
                    hashes.add(str(page["text_hash"]))
            values[sub_id] = hashes
        return values

    def cross_course_concepts(self, course_id: str = "") -> dict:
        graph = list_concept_graph(self.learning_store.path, course_id=str(course_id or ""))
        sub_ids = {
            str(item.get("sub_id") or "")
            for edge in graph.get("edges") or []
            for item in edge.get("evidence") or []
            if item.get("sub_id")
        }
        stale = mark_stale_edges(self.learning_store.path, self._concept_source_hashes(sub_ids)) if sub_ids else 0
        if stale:
            graph = list_concept_graph(self.learning_store.path, course_id=str(course_id or ""))
        courses = dict(self.catalog_repository.snapshot().get("courses") or {})
        graph["courses"] = [
            {
                "course_id": course_key,
                "title": str((courses.get(course_key) or {}).get("title") or course_key),
            }
            for course_key in graph.get("course_ids") or []
        ]
        graph["stale_edge_count"] = stale
        return graph

    def update_cross_course_concept(self, edge_id: str, *, action: str, relation: str = "") -> dict:
        return update_concept_edge(
            self.learning_store.path, edge_id=str(edge_id), action=str(action), relation=str(relation)
        )

    def learning_analytics(self, term: str = "") -> dict:
        result = analytics_summary(self.learning_store.path, term=str(term or ""))
        if not result.get("settings", {}).get("enabled"):
            result["fun_metrics"] = None
            return result
        snapshot = self.catalog_repository.snapshot()
        courses = dict(snapshot.get("courses") or {})
        lectures = []
        for sub_id, lecture in dict(snapshot.get("lectures") or {}).items():
            course_id = str(lecture.get("course_id") or "")
            course = dict(courses.get(course_id) or {})
            course_term = str(course.get("term") or course.get("semester") or course.get("term_name") or "")
            if term and course_term != str(term):
                continue
            try:
                segments = list(self.subtitle_segments(str(sub_id)).get("segments") or [])
            except (FileNotFoundError, KeyError, ValueError):
                segments = []
            if segments:
                lectures.append({"course_id": course_id, "sub_id": str(sub_id), "segments": segments})
        result["fun_metrics"] = estimate_fun_metrics(lectures)
        result["terms"] = sorted({
            str(value.get("term") or value.get("semester") or value.get("term_name") or "")
            for value in courses.values()
            if value.get("term") or value.get("semester") or value.get("term_name")
        })
        return result

    def configure_learning_analytics(self, enabled: bool, *, timezone_name: str = "Asia/Shanghai") -> dict:
        return set_analytics_enabled(
            self.learning_store.path, bool(enabled), timezone=str(timezone_name or "Asia/Shanghai")
        )

    def delete_learning_analytics(self, term: str) -> dict:
        deleted = delete_analytics_term(self.learning_store.path, str(term))
        return {"deleted_events": deleted, "term": str(term), "settings": analytics_settings(self.learning_store.path)}

    def record_study_event(self, *, kind: str, course_id: str, sub_id: str, dwell_ms: int = 0) -> dict:
        """AIRESEARCH H6 埋点入口（TELEMETRY-H64-1）：闭集校验在端点，这里只转发。

        analytics 未开启时 ``recorded=False`` 是诚实回执（不报错、不落行）。
        """
        recorded = record_study_event(
            self.learning_store.path,
            kind=str(kind or ""),
            course_id=str(course_id or ""),
            sub_id=str(sub_id or ""),
            dwell_ms=int(dwell_ms or 0),
        )
        return {"recorded": bool(recorded), "kind": str(kind or "")}

    def study_telemetry_summary(self) -> dict:
        return study_telemetry_summary(self.learning_store.path)

    def delete_study_events(self) -> dict:
        deleted = delete_study_events(self.learning_store.path)
        return {"deleted_events": deleted, "settings": analytics_settings(self.learning_store.path)}

    # ---- 学习统计 v2 默认层（STUDY-STATS-M1）：时长心跳 + 概览读面 ----
    def record_study_heartbeat(self, course_id: str, sub_id: str, seconds: float) -> dict:
        return record_study_heartbeat(
            self.learning_store.path,
            course_id=str(course_id or ""),
            sub_id=str(sub_id or ""),
            seconds=seconds,
        )

    def study_overview(self) -> dict:
        return study_overview(self.learning_store.path)

    def study_detail(self) -> dict:
        """学习统计展开层（STUDY-STATS-M2-b）：全课程掌握度+逐讲明细+FSRS 预测。

        学习库聚合（study_stats.study_detail）并课程目录讲次清单：目录里有但
        还没学过的讲补诚实零行；讲次标签（日期+副题）来自目录元数据，统计面
        零内容文本。目录缺失（超授权课程上限等）时按学习库信号行降级呈现。"""
        detail = study_detail(self.learning_store.path)
        course_ids = [str(entry.get("course_id") or "") for entry in detail.get("courses") or []]
        catalog_by_course: dict[str, list[dict]] = {}
        if course_ids and len(course_ids) <= 100:
            try:
                for course in self.catalog_repository.courses_for_ids(set(course_ids)):
                    catalog_by_course[str(course.get("course_id") or "")] = [
                        {
                            "sub_id": str(lecture.get("sub_id") or ""),
                            "label": " ".join(
                                part
                                for part in (str(lecture.get("date") or ""), str(lecture.get("sub_title") or ""))
                                if part
                            ),
                        }
                        for lecture in course.get("lectures") or []
                        if str(lecture.get("sub_id") or "")
                    ]
            except ValueError:
                catalog_by_course = {}  # 目录读取失败=降级为无标签，统计数字不受影响
        for entry in detail.get("courses") or []:
            course_id = str(entry.get("course_id") or "")
            catalog_lectures = catalog_by_course.get(course_id, [])
            labels = {item["sub_id"]: item["label"] for item in catalog_lectures}
            by_sub: dict[str, dict] = {}
            for row in entry.get("lectures") or []:
                sub_id = str(row.get("sub_id") or "")
                row["label"] = labels.get(sub_id, "")
                by_sub[sub_id] = row
            ordered = []
            for item in catalog_lectures:
                if item["sub_id"] in by_sub:
                    ordered.append(by_sub.pop(item["sub_id"]))
                else:
                    # 目录里有但还没学过的讲：诚实零行（这讲还没看过）。
                    ordered.append({
                        "course_id": course_id, "sub_id": item["sub_id"], "label": item["label"],
                        "percent": None, "completed": False, "seconds": 0, "replays": 0,
                        "open_bookmarks": 0, "quiz_graded": 0, "quiz_correct": 0,
                        "quiz_ungraded": 0, "last_correct": None,
                        "flashcards_total": 0, "flashcards_due": 0,
                    })
            ordered.extend(by_sub.values())  # 学习信号先于目录出现的历史讲次排在最后
            entry["lectures"] = ordered
        return detail

    def clear_study_daily_seconds(self, course_id: str = "") -> dict:
        deleted = clear_study_daily_seconds(self.learning_store.path, course_id=str(course_id or ""))
        return {"deleted_rows": deleted}

    def course_data_summary(self, *, page: int = 1, page_size: int = 50, include_orphans: bool = False) -> dict:
        """``course-data-summary.v1`` payload for the data management workspace."""
        return self._course_data_inventory().summary(
            page=page, page_size=page_size, include_orphans=include_orphans,
        )

    def course_data_lecture_page(self, course_id: str, *, limit: int = 50, offset: int = 0) -> dict:
        """``course-data-lecture-page.v1`` payload for one course."""
        return self._course_data_inventory().lecture_page(str(course_id), limit=limit, offset=offset)

    def course_data_action(
        self,
        action: str,
        *,
        operation_id: str,
        course_ids: list[str] | tuple[str, ...] = (),
        sub_ids: list[str] | tuple[str, ...] = (),
        confirm: bool = False,
        confirm_typed: str = "",
        include_orphans: bool = False,
    ) -> dict:
        """Execute one course-data action and return its action-result receipt."""
        return course_data_perform_action(
            action,
            learning_store=self.learning_store,
            catalog_repository=self.catalog_repository,
            task_store=self.task_store,
            data_root=self.output_dir,
            operation_id=str(operation_id),
            course_ids=tuple(course_ids),
            sub_ids=tuple(sub_ids),
            confirm=bool(confirm),
            confirm_typed=str(confirm_typed or ""),
            include_orphans=bool(include_orphans),
            refresh_search_index=lambda: self.refresh_search_index(force=True),
        )

    # -- 课程复习（N7K）：本地课程知识快照的读面与刷新动作 ----------------

    def _course_review_sub_ids(self, course_id: str) -> list[str]:
        """本课程在本机目录里的讲次（授权闭集：只认目录里真实存在的课）。"""
        pairs = self.catalog_repository.lecture_course_pairs()
        return sorted(
            str(sub_id) for sub_id, lecture in pairs.items()
            if str(lecture.get("course_id") or "") == str(course_id)
        )

    def _course_review_course_ids(self) -> set[str]:
        """授权课程闭集＝本机目录里的课程（含暂无讲次的课程）。

        只从讲次对里取会让「刚同步到目录、还没拉讲次」的课被误判成不存在，
        那样前端拿到的是 404 而不是诚实的空态。
        """
        try:
            rows = self.catalog_repository.courses()
        except Exception:  # noqa: BLE001 - 目录不可读时退回讲次对，绝不放大授权
            rows = []
        course_ids = {str(row.get("course_id") or "") for row in rows if isinstance(row, dict)}
        course_ids.discard("")
        if course_ids:
            return course_ids
        pairs = self.catalog_repository.lecture_course_pairs()
        return {str(lecture.get("course_id") or "") for lecture in pairs.values()} - {""}

    def _authorize_course_review(self, course_id: str, sub_id: str = "") -> None:
        """课程/讲次必须落在本机目录内，否则闭集拒绝（不猜、不越权）。"""
        course_id = str(course_id or "").strip()
        if not course_id or course_id not in self._course_review_course_ids():
            raise CourseReviewActionError("course_review_course_unknown")
        sub_id = str(sub_id or "").strip()
        if sub_id and sub_id not in self._course_review_sub_ids(course_id):
            raise CourseReviewActionError("course_review_lecture_unknown")

    @staticmethod
    def _empty_course_review(course_id: str) -> dict:
        """没有讲次时的诚实空态（形状与冻结 view 一致，状态明确为不可用）。"""
        return {
            "view": "course_overview",
            "contract": course_knowledge_contract.CONTRACT_ID,
            "course_id": str(course_id),
            "status": "error",
            "stale_reasons": ["never_built"],
            "coverage": {
                "lectures_total": 0, "lectures_ready": 0, "lectures_partial": 0,
                "lectures_stale": 0, "transcript_segments": 0, "slide_pages": 0,
                "document_pages": 0, "course_document_pages": 0,
                "assessment_items": 0, "bookmarks": 0,
            },
            "topics": [], "lectures": [],
            "assessment": {"total": 0, "lectures_with_items": 0},
            "sources": [], "updated_at": 0.0,
        }

    def course_review(
        self, course_id: str = "", *, sub_id: str = "", include_personal_notes: bool = False
    ) -> dict:
        """课程复习读面：course 概览、或指定讲次的明细（同一数据源）。"""
        course_id = str(course_id or "").strip()
        sub_id = str(sub_id or "").strip()
        self._authorize_course_review(course_id, sub_id)
        sub_ids = self._course_review_sub_ids(course_id)
        if not sub_ids:
            return {
                "view": self._empty_course_review(course_id),
                "assessment_workspace": None,
                "snapshot": None,
                "state": "unavailable",
                "reason_code": "course_has_no_lectures",
                "observed_at": time.time(),
            }
        review = course_review_document(
            self.learning_store,
            course_id=course_id,
            sub_ids=sub_ids,
            include_personal_notes=bool(include_personal_notes),
        )
        document = review["document"]
        try:
            if sub_id:
                view = course_knowledge_contract.lecture_detail_view(document, sub_id)
                assessment = None
            else:
                view = course_knowledge_contract.course_overview_view(document)
                assessment = self._assessment_workspace(course_id, document)
        except course_knowledge_contract.CourseKnowledgeContractError as error:
            if str(getattr(error, "code", "")) == "lecture_not_found":
                # 这一讲不在当前快照里（例如刚新增、而本次构建失败）：如实 404。
                raise CourseReviewActionError("course_review_lecture_unknown") from error
            # 快照本身读不出来：报「这次没成功」并保持 view 形状，绝不 500、
            # 也绝不把半个文档当好的展示。
            degraded = self._empty_course_review(course_id)
            degraded["status"] = "error"
            degraded["stale_reasons"] = ["snapshot_invalid"]
            return {
                "view": degraded,
                "assessment_workspace": None,
                "snapshot": review.get("snapshot"),
                "state": "error",
                "reason_code": "snapshot_invalid",
                "observed_at": time.time(),
            }
        return {
            "view": view,
            "assessment_workspace": assessment,
            "snapshot": review["snapshot"],
            "state": str(document.get("status") or ""),
            "diagnostics": review.get("diagnostics") or {},
            # COURSEMEM-1：课程记忆轻量入口的真实计数（前端 N>0 才渲染芯片）。
            "course_memory": self._course_memory_view(course_id),
            # P10：课程词汇修正候选（候选行+已确认数，前端 N>0 才渲染节）。
            "term_candidates": self._term_candidates_view(course_id),
            "observed_at": time.time(),
        }

    # 答案面只在本地题目工作台出现：合同把 AssessmentItem 定为无答案引用视图，
    # 云端证据包绝不携带答案（见 build_evidence_packet）。这里按 item_id 与 N7A
    # 的 Assessment IR 对齐，把「答案出处」诚实标注补进工作台投影，来源闭集原样
    # 透出（ai_generated 永不冒充 official）。
    _ANSWER_VIEW_KEYS = (
        "answer", "answer_source", "has_answer", "ai_explanation", "status", "kind",
        "ai_state", "ai_citations", "ai_error_code",
    )

    def _assessment_workspace(self, course_id: str, document: dict) -> dict:
        workspace = course_knowledge_contract.assessment_workspace_view(document)
        workspace = {**workspace, "local_practice": self._local_practice_workspace(course_id)}
        try:
            from src.runtime.assessment_ir import (
                assessment_view, list_ai_answers, list_assessment_items,
            )
            rows = list_assessment_items(
                self.learning_store.path, course_id=str(course_id), limit=2000
            )
            ai_rows = list_ai_answers(self.learning_store.path, course_id=str(course_id))
        except Exception:  # noqa: BLE001 - 读不到 IR 就退回无答案的合同视图，不崩
            return workspace
        by_id = {
            str(row.get("item_id") or ""): row
            for row in rows
            if str(row.get("item_id") or "") and str(row.get("status") or "") != "orphaned"
        }
        if not by_id:
            return workspace
        items = []
        annotated = 0
        ai_ready = 0
        for item in workspace["items"]:
            item_id = str(item.get("item_id") or "")
            row = by_id.get(item_id)
            if row is None:
                items.append(item)
                continue
            view = assessment_view(row, ai_rows.get(item_id))
            if view.get("has_answer"):
                annotated += 1
            if view.get("ai_state") == "ready":
                ai_ready += 1
            merged = dict(item)
            for key in self._ANSWER_VIEW_KEYS:
                if key in view and key != "kind":
                    merged[key] = view[key]
            items.append(merged)
        return {**workspace, "items": items, "answers": {"annotated": annotated, "ai": ai_ready}}

    def _local_practice_workspace(self, course_id: str) -> dict:
        """课程级工作台里的「本课程练习」分组：讲次级本地回忆题，答案不下发。

        取不到就返回一个诚实空视图（不抛），因为它是增值分组，不该拖垮整个工作台。
        """
        sub_ids = self._course_review_sub_ids(str(course_id))
        try:
            return local_practice_view(
                self.learning_store.path,
                course_id=str(course_id),
                lecture_labels={value: self._lecture_label(value) for value in sub_ids},
                empty_sub_id=(sub_ids[0] if sub_ids else ""),
            )
        except Exception:  # noqa: BLE001 - 练习分组读不出来不影响题目与知识面
            return {
                "view": "generated_quiz", "label": "本课程练习", "course_id": str(course_id),
                "source_label": "课程字幕依据（非官方）", "answer_visible_before_submit": False,
                "counts": {"total": 0, "answered": 0, "wrong": 0, "unanswered": 0, "lectures": 0},
                "lectures": [], "items": [],
            }

    # ---- 闪卡复习（RR-P4FSRS-1）：既有知识快照 → 本地 FSRS 间隔重复。--------
    # 零模型零外呼：卡从已存快照派生（input_hash 守卫幂等），调度是纯本地
    # 算法。快照缺席时返回诚实空态（empty_action 指向「更新课程知识」），
    # 绝不现场构建、绝不阻塞复习面其他视图。

    def course_flashcards(self, course_id: str) -> dict:
        """课程闪卡读面：到期队列 + 计数；快照有更新时顺带补新卡。"""
        course_id = str(course_id or "").strip()
        self._authorize_course_review(course_id)
        sub_ids = self._course_review_sub_ids(course_id)
        labels = {value: self._lecture_label(value) for value in sub_ids}
        try:
            stored = self.learning_store.get_course_knowledge_snapshot(course_id)
        except Exception:  # noqa: BLE001 - 快照读不出来=无产物，走诚实空态
            stored = None
        document = (stored or {}).get("document") if isinstance(stored, dict) else None
        input_hash = str((stored or {}).get("input_hash") or "")
        if isinstance(document, dict) and input_hash \
                and input_hash != last_derived_input_hash(self.learning_store, course_id=course_id):
            try:
                derive_flashcards(self.learning_store, course_id=course_id, document=document)
                mark_derived(self.learning_store, course_id=course_id, input_hash=input_hash)
            except Exception:  # noqa: BLE001 - 派生失败不挡读面，已有卡照常可复习
                pass
        return flashcard_deck(self.learning_store, course_id=course_id, lecture_labels=labels)

    def course_flashcard_review(self, course_id: str, card_id: str, rating: int) -> dict:
        """给一张卡记一次评分（评分闭集校验在 flashcards 层）；返回下一间隔。"""
        course_id = str(course_id or "").strip()
        self._authorize_course_review(course_id)
        card_id = str(card_id or "").strip()
        if not card_id or len(card_id) > 64 or rating not in FLASHCARD_RATINGS:
            raise CourseReviewActionError("course_review_action_invalid")
        try:
            return review_flashcard(
                self.learning_store, card_id=card_id, rating=int(rating), course_id=course_id
            )
        except KeyError as error:
            raise CourseReviewActionError("course_review_flashcard_unknown") from error
        except ValueError as error:
            raise CourseReviewActionError("course_review_action_invalid") from error

    def course_term_candidate_action(
        self, course_id: str, action: str, *, wrong: str, right: str
    ) -> dict:
        """P10：术语修正候选的人工确认/忽略（一次性终态，零 LLM 零外联）。

        与 review_flashcard 同级的轻量本地动作（无 recorded_operation 包
        装）：资格门与台账都在 course_memory_feedback，这里只做闭集码翻译
        ——词对不在候选集 → ``term_candidate_unknown``（404 族）。
        """
        course_id = str(course_id or "").strip()
        kind = str(action or "").strip().lower()
        wrong = str(wrong or "").strip()
        right = str(right or "").strip()
        if (
            not course_id
            or kind not in {"confirm_term_candidate", "dismiss_term_candidate"}
            or not wrong
            or not right
        ):
            raise CourseReviewActionError("course_review_action_invalid")
        self._authorize_course_review(course_id)
        try:
            if kind == "confirm_term_candidate":
                return confirm_term_mapping(self.output_dir, course_id, wrong=wrong, right=right)
            return dismiss_term_mapping(self.output_dir, course_id, wrong=wrong, right=right)
        except TermCandidateUnknown as error:
            raise CourseReviewActionError("term_candidate_unknown") from error

    def _course_memory_payload(self, course_id: str) -> dict:
        """COURSEMEM-1：字幕 job payload 的课程记忆注入面（元数据+few-shot 示例）。

        键名以 worker 读侧合同为准：``course_context`` 只收平铺标量
        （worker ``validate_course_context`` 保留短标量、丢嵌套），
        ``examples`` 收 ``resolve_course_examples`` 消费的 few-shot 形状
        （worker 检索里课程示例恒排通用示例前）。无处可取时省键（不发明
        空值）；任何读取失败都降级为缺省缺席——记忆是增值面，绝不挡派发。
        """
        payload: dict = {}
        try:
            context = self._course_job_metadata(course_id)
            if context:
                payload["course_context"] = context
        except Exception:  # noqa: BLE001 - 元数据缺席不挡派发
            pass
        try:
            examples = load_course_examples(
                self.output_dir, str(course_id), COURSE_MEMORY_MAX_EXAMPLES
            )
            if examples:
                payload["examples"] = examples
        except Exception:  # noqa: BLE001 - 示例缺席=旧行为
            pass
        try:
            terms_confirmed = confirmed_memory_terms(self.output_dir, str(course_id or ""))
            if terms_confirmed:
                # P10 字幕链通道：仅人工确认的术语进 ASR 热词与 v4 术语校对
                # （worker runner 留桩消费）；P6 自动注入面不经过此键。
                payload["glossary"] = [str(term) for term in terms_confirmed]
        except Exception:  # noqa: BLE001 - 确认术语缺席=旧行为
            pass
        return payload

    def _course_job_metadata(self, course_id: str) -> dict:
        """课程身份标量（course_title/teacher_names/term_label），缺省省键。"""
        row = next(
            (
                item for item in self.catalog_repository.courses()
                if str(item.get("course_id")) == str(course_id)
            ),
            None,
        )
        if not row:
            return {}
        context: dict[str, str] = {}
        for key, source in (
            ("course_title", "title"),
            ("teacher_names", "teacher"),
            ("term_label", "term"),
        ):
            value = str(row.get(source) or "").strip()[:190]
            if value:
                context[key] = value
        return context

    def _course_memory_view(self, course_id: str) -> dict:
        """COURSEMEM-1：学习页课程记忆轻量入口的真实计数（fail-closed）。"""
        try:
            return {"examples": course_memory_count(self.output_dir, course_id)}
        except Exception:  # noqa: BLE001 - 计数读不出=0，不挡复习页
            return {"examples": 0}

    def _term_candidates_view(self, course_id: str) -> dict:
        """P10：课程复习读面的术语修正候选（候选行+已确认数，fail-closed）。

        行形状与 HTTP 合同冻结一致（rows ≤50，lecture/total/signal 计数）；
        读不出=空表，绝不挡复习页。THINK-LADDER-1 加性键
        ``auto_confirmed_rows``：自动晋升件清单（wrong/right/signal_count/
        confirmed_at），供前端「自动确认 · 信号 ×N」标+一键撤销；旧消费者
        按名取键忽略未知键，schema 不变。THINK-LADDER-2 加性键
        ``judge_confirmed_rows``（judge 裁决自动确认件，同可一键撤销）与
        候选行加性覆盖 ``judge_ruling/judge_reason_code/judge_judged_at``
        （judge 判 invalid 的折叠标注；行仍是候选，人工动作不受限）。
        """
        try:
            rows = term_candidates(self.output_dir, course_id)
            confirmed_count = len(confirmed_term_list(self.output_dir, course_id))
            auto_rows = auto_confirmed_term_rows(self.output_dir, course_id)
            judge_rows = judge_confirmed_term_rows(self.output_dir, course_id)
            annotations = {
                f"{row['wrong']}\u0000{row['right']}": row
                for row in judge_annotation_rows(self.output_dir, course_id)
            }
        except Exception:  # noqa: BLE001 - 候选读不出=空表，不挡复习页
            return {
                "rows": [], "confirmed_count": 0,
                "auto_confirmed_rows": [], "judge_confirmed_rows": [],
            }
        for row in rows:
            note = annotations.get(f"{row['wrong']}\u0000{row['right']}")
            if note:
                row["judge_ruling"] = note["ruling"]
                row["judge_reason_code"] = note["reason_code"]
                row["judge_judged_at"] = note["judged_at"]
        return {
            "rows": rows,
            "confirmed_count": confirmed_count,
            "auto_confirmed_rows": auto_rows,
            "judge_confirmed_rows": judge_rows,
        }

    def _term_boundary_payload(self, course_id: str) -> list[dict]:
        """THINK-LADDER-2 设计 B：quality_judge 载荷的 ``term_boundary`` 组装。

        词对=signal<3 候选（帽 10；signal_count 属客户端导入语义数据，worker
        不消费不校验）。缺席/损坏=空列表=省键=旧行为（增值面绝不挡抽检派发）。
        """
        try:
            return judge_boundary_pairs(self.output_dir, str(course_id or ""))
        except Exception:  # noqa: BLE001
            return []

    def _import_boundary_rulings(
        self, course_id: str, *, pairs: list, rulings: dict
    ) -> dict:
        """THINK-LADDER-2 设计 B：边界裁决导入包装（user-adjudication-supreme
        三路在 course_memory_feedback；fail-closed 绝不抛给质检导入本体）。"""
        try:
            return import_boundary_rulings(
                self.output_dir, str(course_id or ""), pairs=pairs, rulings=rulings
            )
        except Exception:  # noqa: BLE001
            return {"confirmed": 0, "annotated": 0, "no_op": 0, "skipped": 0}

    def _course_memory_terms(self, course_id: str) -> list[str]:
        """RR-P6MEM-1：总结/问答派发的课程记忆术语表（反哺闭环的注入面）。

        术语由记忆示例推导、偏差信号排序（course_memory_terms），缺席/损坏
        一律空列表=省键=旧行为——记忆是增值面，绝不挡派发。
        """
        try:
            return list(course_memory_terms(self.output_dir, str(course_id or "")))
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _memory_note_for(metrics: Any, *, kind: str) -> str:
        """按生成侧实报的注入数（metrics.course_memory_terms）构造可见标注。

        没有实报=没有注入=不标注（宁缺毋滥：标注错比标注缺更伤信任）。
        """
        try:
            count = int((metrics or {}).get("course_memory_terms") or 0)
        except (TypeError, ValueError):
            return ""
        return memory_annotation(count, kind=kind)

    def _lecture_bucket_for(self, course_id: str, sub_id: str) -> str:
        """AIRESEARCH H4（TELEMETRY-H64 余量面）：讲次序号 → first/second/later 桶。

        序号来源=目录讲次正典序（``lectures_for_course`` 的 date,sub_title
        序）：首讲=first、第二讲=second、其余=later。讲次缺席/目录读不出/
        任何异常一律空串=不记桶账（feedback 侧 fail-closed 不抛）——分桶是
        增值观测，绝不挡反哺主链。
        """
        sub = str(sub_id or "").strip()
        if not sub:
            return ""
        try:
            subs = [
                str(row.get("sub_id") or "")
                for row in self.catalog_repository.lectures_for_course(str(course_id or ""))
            ]
            index = subs.index(sub)
        except Exception:  # noqa: BLE001 - 桶读不出=不记，反哺零行为差异
            return ""
        if index == 0:
            return "first"
        if index == 1:
            return "second"
        return "later"

    def _memory_feedback_sink(
        self, course_id: str, text: str, *, source: str, sub_id: str = ""
    ) -> None:
        """产物落库后的本地一致性检查：偏差回写为记忆修正信号（零 LLM）。

        H4 讲次桶透传：带 ``sub_id`` 时按目录正典序折算 first/second/later
        桶记账；不带（旧式调用）=零行为差异。
        """
        try:
            hits, recorded = record_product_deviations(
                self.output_dir, str(course_id or ""), str(text or ""), source=source,
                lecture_bucket=self._lecture_bucket_for(course_id, sub_id),
            )
        except Exception:  # noqa: BLE001 - 反哺绝不挡导入
            return
        if hits:
            # P2-12：命中数与实记账数分开报——回放/环内命中时 hits>0 而
            # recorded=0，旧口径把它们混成「recorded」是虚账。
            print(
                f"[FudanCourseLens] Course memory feedback: {hits} deviation hit(s), "
                f"{recorded} recorded for course {course_id} ({source})",
                flush=True,
            )

    def _auto_quality_judge_after_summary(self, sub_id: str) -> None:
        """THINK-LADDER-1 自动抽检：总结落库后同内容至多自动抽检一次。

        幂等=复用既有基建：同 input_hash 已有 quality_report（已抽检）或该
        讲存在在途 quality_judge 任务（在途）即静默跳过；预算门=日 token
        档（max_deepseek_tokens，哨兵 0=不限），超帽诚实跳过；请求面复用
        request_quality_judge 全量 preflight（材料/key/远端三连）。任何异常
        只记一行闭集日志，绝不挡总结导入主链；手动 POST 入口不变。
        """
        sub_id = str(sub_id or "").strip()
        if not sub_id:
            return
        try:
            segments = self.learning_store.get_transcript_segments(sub_id)
            artifact = self.learning_store.find_ai_artifact(sub_id, "timestamp_summary")
            summary_content = dict((artifact or {}).get("content") or {})
            if not segments and not summary_content:
                return
            segment_indices = _quality_sample_indices(len(segments))
            input_hash = _quality_input_hash(
                sub_id,
                segment_indices,
                _quality_transcript_digest(segments),
                str((summary_content.get("generation") or {}).get("input_hash") or ""),
            )
            if self.learning_store.find_ai_artifact(
                sub_id, "quality_report",
                input_hash=input_hash, prompt_version=QUALITY_JUDGE_PIPELINE_VERSION,
            ) is not None:
                return  # 同内容已抽检（import_quality_report 同 hash upsert 语义）
            if self.task_store.find_active("quality_judge", sub_id):
                return  # 该讲在途（任意 hash 在飞都不再叠同讲任务）
            limit = self.max_deepseek_tokens_limit()
            if limit > 0 and int(self.task_usage_month().get("deepseek_tokens") or 0) >= limit:
                print(
                    f"[FudanCourseLens] Auto quality judge skipped (budget cap {limit}) "
                    f"for {sub_id}",
                    flush=True,
                )
                return
            created = bool((self.request_quality_judge(sub_id) or {}).get("created"))
            print(
                f"[FudanCourseLens] Auto quality judge requested for {sub_id} (created={created})",
                flush=True,
            )
        except Exception as error:  # noqa: BLE001 - 自动抽检绝不挡总结导入
            code = str(getattr(error, "code", "") or error)
            print(
                f"[FudanCourseLens] Auto quality judge unavailable for {sub_id}: {code}",
                flush=True,
            )

    def _course_knowledge_job_extras(self, *, course_id: str, sub_id: str) -> dict:
        """加性 job 字段：有界证据包 + 课程上下文；失败返回空 dict（退旧载荷）。"""
        extras: dict = {}
        try:
            extras["evidence_packet"] = build_evidence_packet(
                self.learning_store,
                course_id=str(course_id),
                sub_id=str(sub_id),
                item_chars=PACKET_ITEM_CHARS,
                total_chars=PACKET_TOTAL_CHARS,
            )
        except Exception as error:  # noqa: BLE001 - 增值字段绝不挡住摘要
            extras["evidence_packet_error"] = _course_knowledge_error_code(error)
        try:
            sub_ids = self._course_review_sub_ids(course_id)
            if sub_ids:
                extras["course_context"] = course_context(
                    self.learning_store,
                    course_id=str(course_id),
                    sub_ids=sub_ids,
                    lecture_titles={
                        value: self._lecture_label(value) for value in sub_ids
                    },
                    current_sub_id=str(sub_id),
                )
        except Exception as error:  # noqa: BLE001
            extras["course_context_error"] = _course_knowledge_error_code(error)
        return extras

    def _active_summary_sub_ids(self, course_id: str) -> set[str]:
        active: set[str] = set()
        # CLIENT-STATE-R1：活动摘要判定找「还在跑的 summary」——窗口必须
        # 取最新（旧终态行再多也不许把新活动任务挤出 200 窗）。
        for task in self.task_store.list_tasks(limit=200, newest_first=True) or []:
            if str(task.get("kind") or "") != "summary":
                continue
            if str(task.get("course_id") or "") != str(course_id):
                continue
            if str(task.get("state") or "") in {"queued", "running", "pausing"}:
                active.add(str(task.get("sub_id") or ""))
        return active - {""}

    def course_review_refresh(self, course_id: str = "", *, include_personal_notes: bool = False) -> dict:
        """课程复习刷新：只把「缺摘要/已陈旧且有字幕」的讲次交给既有摘要队列。"""
        course_id = str(course_id or "").strip()
        self._authorize_course_review(course_id)
        sub_ids = self._course_review_sub_ids(course_id)
        if not sub_ids:
            raise CourseReviewActionError("course_review_course_unknown")
        plan = refresh_plan(
            self.learning_store,
            course_id=course_id,
            sub_ids=sub_ids,
            active_sub_ids=self._active_summary_sub_ids(course_id),
            include_personal_notes=bool(include_personal_notes),
        )
        queued: list[dict] = []
        blocked = list(plan["blocked"])
        for entry in plan["queued"]:
            sub_id = str(entry["sub_id"])
            try:
                task = self.enqueue_summary(course_id, sub_id, include_ppt=True, force=False)
            except (KeyError, RuntimeError, ValueError, FileNotFoundError) as error:
                blocked.append({"sub_id": sub_id, "reason": _summary_block_code(error)})
                continue
            queued.append({
                "sub_id": sub_id,
                "reason": entry["reason"],
                "task_id": str((task or {}).get("task_id") or ""),
            })
        if queued:
            status = "queued"
        elif blocked:
            status = "blocked"
        else:
            status = "noop"
        return {
            "course_id": course_id,
            "action": "refresh",
            "status": status,
            # 计数在上层（前端直接读），明细在下层（要追到哪一讲才看）。
            "queued": len(queued),
            "skipped": len(plan["skipped"]),
            "blocked": len(blocked),
            "reasons": self._course_review_reasons(queued, plan["skipped"], blocked),
            "queued_lectures": queued,
            "skipped_lectures": list(plan["skipped"]),
            "blocked_lectures": blocked,
            "counts": {
                "queued": len(queued),
                "skipped": len(plan["skipped"]),
                "blocked": len(blocked),
            },
            "observed_at": time.time(),
        }

    # ---- 题目 AI 解答/解析（N8A）------------------------------------------
    # 唯一入口是学生在题目工作台上点一道题（course-review/actions 的
    # explain_assessment）。导入、刷新快照、自动材料规则、quiz_after_import
    # 一律不碰这条链：上传的资料不会被自动烧到云上。

    def _assessment_item_for_answer(self, course_id: str, item_id: str) -> dict:
        """按课程闭集取一道可以求解的题；未知/孤儿/非作业真题一律 fail-closed。"""
        from src.runtime.assessment_ir import list_assessment_items

        item_id = str(item_id or "").strip()
        if not item_id or len(item_id) > 128:
            raise CourseReviewActionError("assessment_item_request_invalid")
        rows = list_assessment_items(self.learning_store.path, course_id=str(course_id), limit=2000)
        row = next((value for value in rows if str(value.get("item_id")) == item_id), None)
        if row is None:
            # 本地练习（quiz:...）也走同一个工作台，但它不是作业/真题：不烧云。
            raise CourseReviewActionError(
                "assessment_item_not_explainable" if item_id.startswith("quiz:")
                else "assessment_item_unknown"
            )
        if str(row.get("status") or "") == "orphaned":
            raise CourseReviewActionError("assessment_item_source_lost")
        document_id = str(row.get("document_id") or "")
        if document_id:
            try:
                get_document(self.learning_store.path, document_id, include_pages=False)
            except KeyError as exc:
                raise CourseReviewActionError("assessment_item_source_lost") from exc
        return row

    def _assessment_answer_receipt(self, item: dict, status: str, *, created: bool,
                                   error_code: str = "") -> dict:
        from src.runtime.assessment_ir import ai_answer_view, list_ai_answers

        row = list_ai_answers(
            self.learning_store.path, item_ids=[str(item.get("item_id") or "")]
        ).get(str(item.get("item_id") or ""))
        return {
            "course_id": str(item.get("course_id") or ""),
            "item_id": str(item.get("item_id") or ""),
            "action": "explain_assessment",
            "status": str(status),
            "created": bool(created),
            "error_code": str(error_code),
            "ai": ai_answer_view(row, item),
            "task_id": str((row or {}).get("task_id") or ""),
            "observed_at": time.time(),
        }

    def explain_assessment_item(self, course_id: str, item_id: str, *,
                                content_hash: str = "", retry: bool = False) -> dict:
        """学生点选一道题 → 用本机材料的有界证据请求 AI 解答/解析。

        证据不足时诚实说「资料不足」，不补外部知识；已经有在途任务或同身份结果时
        直接复用，不重复烧云；题目内容变了或来源失据时旧结果不再当当前答案。
        """
        from src.runtime.assessment_ir import (
            AI_ANSWER_IN_FLIGHT, AI_ANSWER_PROMPT_VERSION, ai_answer_input_hash,
            ai_answer_view, assessment_question_text, build_assessment_evidence,
            list_ai_answers, save_ai_answer,
        )

        course_id = str(course_id or "").strip()
        self._authorize_course_review(course_id)
        item = self._assessment_item_for_answer(course_id, item_id)
        current_hash = str(item.get("content_hash") or "")
        if content_hash and str(content_hash) != current_hash:
            # 前端拿的版本已经不是本机这一份：先刷新，别拿旧题去问。
            raise CourseReviewActionError("assessment_item_content_stale")

        evidence, reason = build_assessment_evidence(self.learning_store.path, item)
        if not evidence:
            save_ai_answer(
                self.learning_store.path, item=item, stage="insufficient",
                error_code=str(reason or "assessment_evidence_unavailable"),
            )
            return self._assessment_answer_receipt(
                item, "insufficient", created=False,
                error_code=str(reason or "assessment_evidence_unavailable"),
            )

        input_hash = ai_answer_input_hash(item, evidence)
        existing = list_ai_answers(
            self.learning_store.path, item_ids=[str(item.get("item_id") or "")]
        ).get(str(item.get("item_id") or ""))
        retry_task_id = ""
        if existing and str(existing.get("input_hash") or "") == input_hash:
            stage = str(existing.get("stage") or "")
            if stage in AI_ANSWER_IN_FLIGHT or stage == "ready":
                return self._assessment_answer_receipt(item, stage, created=False)
            retry_task_id = str(existing.get("task_id") or "")
            task = self.task_store.get_task(retry_task_id) if retry_task_id else None
            if task and str(task.get("state") or "") in ACTIVE_STATES:
                return self._assessment_answer_receipt(item, "queued", created=False)
            if not retry:
                # 失败/资料不足不自动重试：只认用户再点一次。
                return self._assessment_answer_receipt(
                    item, stage, created=False,
                    error_code=str(existing.get("error_code") or ""),
                )

        if not self._deepseek_key():
            raise CourseReviewActionError("ai_key_missing")

        query = assessment_question_text(item) or str(item.get("label") or "")
        # RR-QWIN-1 Q2：与书签解释同一条出站预检（考核证据组装自带 6 条/4000
        # 字帽，正常永不触发；触发即本地如实失败，不烧云端往返）。
        query, oversized = _question_payload_preflight(query, evidence)
        if oversized:
            save_ai_answer(
                self.learning_store.path, item=item, stage="insufficient",
                error_code=oversized,
            )
            return self._assessment_answer_receipt(
                item, "insufficient", created=False, error_code=oversized,
            )
        payload = {
            "assessment_item_id": str(item.get("item_id") or ""),
            "query": query,
            "evidence": evidence,
            "input_hash": input_hash,
            "content_hash": current_hash,
            "prompt_version": AI_ANSWER_PROMPT_VERSION,
            "cancel_requested": False,
        }
        dedupe_key = f"assessment:{payload['assessment_item_id']}:{input_hash}"
        if retry_task_id:
            # 用户显式重试：复用同一行任务（dedupe_key 是唯一键，不能另开一行），
            # 与疑问解释的重试同一条做法。
            task = self.task_store.update_task(
                retry_task_id, state="queued", resume_requested=False, error="",
                finished_at=None, payload=payload,
            )
            if task is None:
                raise CourseReviewActionError("assessment_item_unknown")
            created = False
        else:
            task, created = self.task_store.add_task(
                "question", course_id, str(item.get("sub_id") or ""), payload,
                config_key=dedupe_key,
            )
        self.task_store.upsert_v3_metadata(
            str(task["task_id"]),
            dedupe_key=dedupe_key,
            stage="queued", privacy_state="sealed", requested_outputs=["answer"],
            input_hash=input_hash,
        )
        task = self.task_store.update_task(
            str(task["task_id"]), progress={
                "schema_version": 3, "stage": "queued", "percent": None,
                "label": "题目解答等待在线计算", "indeterminate": True,
                "observed_at": time.time(),
            },
        ) or task
        save_ai_answer(
            self.learning_store.path, item=item, stage="queued",
            task_id=str(task["task_id"]), input_hash=input_hash,
        )
        with self._lock:
            self._queue_persisted_task(task)
            self._question_cancel.clear()
            self._ensure_question_worker()
        return self._assessment_answer_receipt(item, "queued", created=bool(created))

    def _lecture_label(self, sub_id: str) -> str:
        row = self.catalog_repository.get_lecture(str(sub_id)) or {}
        return str(row.get("sub_title") or "").strip() or "这一讲"

    def _course_review_reasons(self, queued: list[dict], skipped: list[dict], blocked: list[dict]) -> list[str]:
        """人话原因：像同学解释，不像系统日志；同类只说一次、最多几条。"""
        sentences: list[str] = []
        for entry in queued[:3]:
            sentences.append(f"{self._lecture_label(entry['sub_id'])}已排进 AI 整理队列，跑完会自动更新。")
        if len(queued) > 3:
            sentences.append(f"另外 {len(queued) - 3} 讲也一起排上了。")
        for entry in blocked:
            reason = str(entry.get("reason") or "")
            label = self._lecture_label(entry.get("sub_id") or "")
            if reason == "transcript_missing":
                sentences.append(f"{label}还没有同步字幕，等字幕好了再整理。")
            elif reason == "ai_key_missing":
                sentences.append("还没有配置 AI 密钥，先去设置里填一个再试。")
            elif reason == "lecture_not_in_catalog":
                sentences.append(f"{label}不在已授权的课程目录里，先刷新课程目录。")
            elif reason == "fudan_login_required":
                sentences.append("学校登录状态已过期，重新登录后再试。")
            else:
                sentences.append(f"{label}暂时排不进去，稍后再试一次就好。")
        for entry in skipped:
            if str(entry.get("reason")) == "already_running":
                sentences.append(f"{self._lecture_label(entry['sub_id'])}正在整理中，不用重复点。")
        if not sentences:
            sentences.append("这一课的知识已经是最新的，不用重新整理。")
        return sentences[:6]

    def _course_knowledge_course_id(self, sub_id: str) -> str:
        lecture = self.catalog_repository.get_lecture(str(sub_id)) or {}
        return str(lecture.get("course_id") or "")

    def _rebuild_course_knowledge(self, course_id: str, *, reason: str) -> dict:
        """重建课程快照：失败只记录，绝不影响已经落地的导入结果。"""
        course_id = str(course_id or "")
        if not course_id:
            return {"state": "skipped", "reason": "course_unknown"}
        sub_ids = self._course_review_sub_ids(course_id)
        if not sub_ids:
            return {"state": "skipped", "reason": "course_has_no_lectures"}
        try:
            result = save_course_knowledge(self.learning_store, course_id=course_id, sub_ids=sub_ids)
        except Exception as error:  # noqa: BLE001 - 快照失败不得反噬导入
            return {"state": "error", "code": _course_knowledge_error_code(error), "reason": reason}
        document = result["document"]
        return {
            "state": "saved",
            "status": document.get("status"),
            "input_hash": document.get("input_hash"),
            "lectures": len(document.get("lectures") or []),
            "reason": reason,
        }

    def _course_data_inventory(self):
        return CourseDataInventory(
            learning_store=self.learning_store,
            catalog_repository=self.catalog_repository,
            task_store=self.task_store,
            data_root=self.output_dir,
        )

    def client_reset_action(
        self,
        *,
        operation_id: str,
        confirm_typed: str = "",
        delete_derived: bool = False,
        delete_github_repos: bool = False,
    ) -> dict:
        """Execute the client reset and return its action-result receipt.

        The HTTP adapter fires ``request_shutdown("reset")`` itself after the
        reply is written, mirroring the client-update restart handoff.
        """
        return client_reset_perform_action(
            learning_store=self.learning_store,
            catalog_repository=self.catalog_repository,
            task_store=self.task_store,
            credentials=self.credentials,
            github_app=self.github_app,
            data_root=self.output_dir,
            search_index=self.search_index,
            delete_deepseek_key=self.delete_deepseek_key,
            operation_id=str(operation_id),
            confirm_typed=str(confirm_typed or ""),
            delete_derived=bool(delete_derived),
            delete_github_repos=bool(delete_github_repos),
        )

    # ---- 数据搬家包（D12 P0）：导出/导入/下载令牌/上传暂存 ----

    def data_migration_export_action(self, *, operation_id: str, password: str = "") -> dict:
        """一键导出搬家包：备份原语快照两库 + 四个文件命名空间 → 加密包。

        幂等台账与 course-data/client-reset 同法：同 operation_id 重放回上一
        份回执（含其下载令牌；令牌已被取走时 GET 会给 410 闭集码）。
        """
        ledger_key = f"data-migration-export:{str(operation_id or '')}"
        existing = self.task_store.get_app_state(ledger_key, None)
        if isinstance(existing, dict) and isinstance(existing.get("receipt"), dict):
            return existing["receipt"]
        export_dir = self.output_dir / _DATA_MIGRATION_EXPORT_DIRNAME
        export_dir.mkdir(parents=True, exist_ok=True)
        _data_migration_prune_exports(export_dir)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        output_path = export_dir / (
            f"courselens-data-{stamp}-{uuid.uuid4().hex[:6]}{PACKAGE_SUFFIX}"
        )
        receipt = build_migration_package(
            self.output_dir,
            output_path,
            password=str(password or ""),
            app_version=_data_migration_client_version(),
        )
        token = uuid.uuid4().hex
        with self._migration_download_lock:
            self._migration_downloads[token] = {
                "path": str(output_path),
                "filename": receipt["filename"],
                "expires_at": time.time() + _DATA_MIGRATION_DOWNLOAD_TTL,
            }
        receipt["download"] = {
            "url": f"/api/v3/data-migration/file?token={token}",
            "filename": receipt["filename"],
            "bytes": receipt["bytes"],
            "expires_in": int(_DATA_MIGRATION_DOWNLOAD_TTL),
        }
        self.task_store.set_app_state(
            ledger_key,
            {"action": "export", "receipt": _data_migration_receipt(
                "export", accepted=True, operation_id=operation_id, result=receipt,
            )},
        )
        return _data_migration_receipt(
            "export", accepted=True, operation_id=operation_id, result=receipt,
        )

    def data_migration_download_file(self, token: str) -> tuple[Path, str]:
        """取一次下载令牌对应的包文件（单次有效；无效/过期=FileNotFoundError）。"""
        entry = None
        with self._migration_download_lock:
            entry = self._migration_downloads.pop(str(token or ""), None)
        if entry is None or float(entry.get("expires_at") or 0) < time.time():
            raise FileNotFoundError("migration download is unavailable")
        path = Path(entry.get("path") or "")
        if not path.is_file():
            raise FileNotFoundError("migration download is unavailable")
        return path, str(entry.get("filename") or path.name)

    def data_migration_stage_upload(self, reader, *, expected_bytes: int) -> dict:
        """把上传的搬家包流式落进导入暂存区（不进内存整包、不进回执日志）。"""
        stage_dir = self.output_dir / _DATA_MIGRATION_IMPORT_STAGE_DIRNAME
        stage_dir.mkdir(parents=True, exist_ok=True)
        _data_migration_prune_exports(stage_dir)
        package_id = uuid.uuid4().hex
        target = stage_dir / f"{package_id}{PACKAGE_SUFFIX}"
        digest = hashlib.sha256()
        total = 0
        try:
            with target.open("wb") as out:
                remaining = int(expected_bytes)
                while remaining > 0:
                    chunk = reader.read(min(_DATA_MIGRATION_STREAM_CHUNK, remaining))
                    if not chunk:
                        raise DataMigrationError(
                            ERROR_PACKAGE_INVALID,
                            "上传中断了，包不完整；请重新选择文件再传一次。",
                        )
                    out.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
                    remaining -= len(chunk)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        if total > _DATA_MIGRATION_MAX_PACKAGE_BYTES:
            target.unlink(missing_ok=True)
            raise DataMigrationError(
                ERROR_PACKAGE_TOO_LARGE,
                "这个包超过了 8GB 的导入上限，换一个更小的包。",
            )
        return {"package_id": package_id, "bytes": total, "sha256": digest.hexdigest()}

    def data_migration_import_action(
        self, *, operation_id: str, package_id: str, password: str = "",
    ) -> dict:
        """安家向导落位：校验→解密→rebasing→落位→恢复两库→schema 面收尾。

        成功后 HTTP 适配层 ``request_shutdown("migration_import")``（与
        client-reset 的关停交接同法），学生重开应用即安家完成。
        """
        package_id = str(package_id or "").strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{32}", package_id):
            raise DataMigrationError(
                ERROR_PACKAGE_INVALID,
                "导入包标识不对；请重新选择文件再试一次。",
            )
        package = self.output_dir / _DATA_MIGRATION_IMPORT_STAGE_DIRNAME / f"{package_id}{PACKAGE_SUFFIX}"
        if not package.is_file():
            raise DataMigrationError(
                ERROR_PACKAGE_INVALID,
                "这个上传包已过期或不存在；请重新选择文件上传。",
            )
        # 与 client-reset 同面的活工作保护：先释放本地可清项；远端在途硬拒。
        released = _client_reset_release_active_work(self.task_store)
        inventory = CourseDataInventory(
            learning_store=self.learning_store,
            catalog_repository=self.catalog_repository,
            task_store=self.task_store,
            data_root=self.output_dir,
        )
        blockers = [
            probe for probe in _client_reset_blockers(self.task_store, inventory)
            if probe.get("code") == "active_remote_run"
        ]
        if blockers:
            return _data_migration_receipt(
                "import", accepted=False, operation_id=operation_id, blockers=blockers,
            )
        receipt = import_migration_package(
            self.output_dir,
            package,
            password=str(password or ""),
            app_version=_data_migration_client_version(),
        )
        # 恢复后的 schema 面收尾：ensure 链 + FTS 旗标 + 目录/任务仓重初始化
        # + 索引强制刷新（与 client-reset 重建后的路径同源，幂等）。
        with closing(connect_learning_db(self.learning_store.path)) as db:
            self.learning_store.search_fts_enabled = initialize_learning_schema(db)
        for ensure in (
            ensure_assessment_schema,
            ensure_student_feature_schema,
            ensure_document_schema,
            ensure_smart_playback_schema,
            ensure_concept_schema,
            ensure_analytics_schema,
            ensure_study_stats_schema,
        ):
            ensure(self.learning_store.path)
        self.catalog_repository._initialize()
        TaskStore(self.task_store.path)
        try:
            self.search_index.request_refresh(force=True)
        except Exception:
            pass  # 索引刷新永不失败导入（与 reset 同语义）
        package.unlink(missing_ok=True)
        receipt["released"] = released
        return _data_migration_receipt(
            "import", accepted=True, operation_id=operation_id, result=receipt,
        )

    # ---- 学习洞察（D7）：事件闭集存储/读取/一键抹除，全部只落本地 learning DB ----
    def record_watch_events(self, course_id: str, sub_id: str, events: list[dict]) -> int:
        return insert_watch_events(
            self.learning_store.path,
            course_id=str(course_id),
            sub_id=str(sub_id),
            events=list(events or []),
        )

    def list_watch_events(self, sub_id: str) -> list[dict]:
        return list_watch_events(self.learning_store.path, sub_id=str(sub_id))

    def clear_watch_events(self, sub_id: str = "") -> int:
        return clear_watch_events(self.learning_store.path, sub_id=str(sub_id or ""))

    def list_question_bookmarks(self, sub_id: str = "") -> list[dict]:
        values = list_bookmarks(self.learning_store.path, sub_id=sub_id)
        for bookmark in values:
            task_id = str(bookmark.get("task_id") or "")
            task = self.task_store.get_task(task_id) if task_id else None
            if not task:
                bookmark["task"] = None
                continue
            task_state = str(task.get("state") or "unknown")
            bookmark["task"] = {
                key: task.get(key) for key in (
                    "task_id", "kind", "course_id", "sub_id", "state", "resume_requested",
                    "progress", "estimate", "error", "updated_at",
                )
            }
            if task_state in {"queued", "running", "pausing", "paused", "failed", "canceled"}:
                bookmark["explanation_state"] = "canceling" if (
                    task_state == "pausing" and bool(dict(task.get("payload") or {}).get("cancel_requested"))
                ) else task_state
        return values

    def _question_bookmark(self, bookmark_id: str) -> dict:
        bookmark = next(
            (item for item in list_bookmarks(self.learning_store.path) if item.get("bookmark_id") == str(bookmark_id)),
            None,
        )
        if not bookmark:
            raise KeyError("bookmark not found")
        return bookmark

    def prime_session_restore(self) -> dict:
        """⑬a：启动即后台预热会话——V5 检查点恢复走既有受保护读路径。

        恢复期到来的真实请求在既有刷新航班等待面上排队（语义不变）；预热
        只是把十秒级首读从学生第一次点击挪到服务启动窗。绝不阻塞调用方、
        异常静默：预热失败 = 既有首读路径原样重试（0 等待者的共享失败会被
        既有逻辑丢弃，不污染后续读）。幂等：每进程至多起一次。"""
        with self._lock:
            if getattr(self, "_restore_prime_started", False):
                return {"state": "already_started"}
            self._restore_prime_started = True
        threading.Thread(
            target=self._run_restore_prime,
            name="session-restore-prime",
            daemon=True,
        ).start()
        return {"state": "started"}

    def _run_restore_prime(self) -> None:
        try:
            self.client()
        except Exception:
            pass  # 预热静默：无检查点/过期/无凭据都是既有首读的正常语义

    def max_deepseek_tokens_limit(self) -> int:
        """日 token 总量上限（缺省 10 万；10 万-100 万步进 10 万网格内取整；
        哨兵 0=不限，照实返回 0）。"""
        try:
            value = int(self.task_store.get_app_state(MAX_DEEPSEEK_TOKENS_STATE_KEY, MAX_DEEPSEEK_TOKENS_DEFAULT))
        except (TypeError, ValueError):
            value = MAX_DEEPSEEK_TOKENS_DEFAULT
        if value == 0:
            return 0
        if not MAX_DEEPSEEK_TOKENS_MIN <= value <= MAX_DEEPSEEK_TOKENS_MAX:
            return MAX_DEEPSEEK_TOKENS_DEFAULT
        return round(value / MAX_DEEPSEEK_TOKENS_STEP) * MAX_DEEPSEEK_TOKENS_STEP

    def set_max_deepseek_tokens(self, value: object) -> dict:
        """DeepSeek 配置面写入缝：闭集网格校验（10 万-100 万，步进 10 万；
        哨兵 0=不限）。"""
        try:
            number = int(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError("max_deepseek_tokens_invalid") from exc
        if number != 0:
            if not MAX_DEEPSEEK_TOKENS_MIN <= number <= MAX_DEEPSEEK_TOKENS_MAX:
                raise ValueError("max_deepseek_tokens_invalid")
            if number % MAX_DEEPSEEK_TOKENS_STEP:
                raise ValueError("max_deepseek_tokens_invalid")
        self.task_store.set_app_state(MAX_DEEPSEEK_TOKENS_STATE_KEY, number)
        return {"schema": "courselens.max-deepseek-tokens.v1", "max_deepseek_tokens": number}

    def ai_usage_month(self) -> dict:
        """本月 AI 用量（Z2：本地聚合任务计数，零外联，供用户定档滑条）。"""
        moment = time.time()
        local_month = time.strftime("%Y-%m", time.localtime(moment))
        month_start = time.mktime(time.strptime(f"{local_month}-01", "%Y-%m-%d"))
        return {
            "schema": "courselens.ai-usage-month.v1",
            "month": local_month,
            "questions": self.task_store.count_tasks_created_since("question", month_start),
            "summaries": self.task_store.count_tasks_created_since("summary", month_start),
            # P3-CONTRACT-1 H1 观测量（加性键，schema 不变，旧消费者无感）：
            # deep=palette 前缀的问题任务，quick=本地拼装的 search_answer 记录。
            "deep_answers": self.task_store.count_tasks_created_since(
                "question", month_start, config_key_prefix="palette:"
            ),
            "quick_answers": self.task_store.count_tasks_created_since("search_answer", month_start),
            # P11-CONTRACT-1：质量抽检任务计数（加性键，schema 不变，旧消费者
            # 按名取字段忽略未知键；v1 不做该数的 UI 展示）。
            "quality_checks": self.task_store.count_tasks_created_since("quality_judge", month_start),
        }

    def task_usage_month(self) -> dict:
        """AS6 消耗透镜：本月云端任务实际消耗（本机累计，零外联）。

        数据源=任务行绝对消耗列（record_task_usage 落库）；无记录返回 0，
        绝不推算、不伪造。
        """
        local_month = time.strftime("%Y-%m", time.localtime(time.time()))
        month_start = time.mktime(time.strptime(f"{local_month}-01", "%Y-%m-%d"))
        totals = self.task_store.usage_totals_since(month_start)
        return {
            "schema": "courselens.task-usage-month.v1",
            "month": local_month,
            "deepseek_tokens": int(totals["deepseek_tokens"]),
            "runner_minutes": round(float(totals["runner_seconds"]) / 60.0, 1),
        }

    def _attempt_stale_lease_cleanup(self, task_id: str) -> bool:
        """AS10 U1：按 cleanup_stale_job_token_lease 既有前置清一次陈旧租约。

        全部前置（进程内在途租约/外来租约/活跃远端任务）由该方法自带校验
        原样执行，fail-closed——这里不预判、不放宽；GitHub 瞬时不可达同样
        按「未清理」处理。幂等：无可清理对象时等价成功。
        """
        try:
            self.github_app.cleanup_stale_job_token_lease(
                task_id=str(task_id), task_store=self.task_store
            )
        except Exception:  # noqa: BLE001 - 前置不满足=闭集拒绝，按未清理处理
            return False
        return True

    def _clear_remote_busy_cycle(self, task_id: str) -> None:
        """AS10：清掉一个任务的 busy 退避计数（暂停/终态收尾用）。"""
        self.task_store.set_app_state(f"{REMOTE_BUSY_STATE_KEY_PREFIX}{task_id}", {})

    def _handle_remote_supervisor_busy(self, task_id: str, *, kind: str) -> None:
        """AS10：busy 分支共用处置——暂停优先→一次自愈→有界退避→诚实排队。

        替代旧「置回 queued+sleep 5s」永动重排：每个新运行世代（started_at
        前进）给恰一次陈旧 job-token 租约自愈机会（cleanup 的既有前置原样
        fail-closed）；再 busy 则 30/60/120s 有界退避并从第二个连续周期起把
        任务卡改写为诚实排队态（不再罐头 ETA）。51 真值链与显式暂停语义
        零触碰——暂停/取消意图永远优先于重排。
        """
        latest = self.task_store.get_task(task_id) or {}
        if dict(latest.get("payload") or {}).get(USER_PAUSE_INTENT_KEY) is True:
            self.task_store.acknowledge_pause(task_id)
            self._clear_remote_busy_cycle(task_id)
            print(
                f"[FudanCourseLens] {kind} task {task_id}: pause honored over busy requeue",
                flush=True,
            )
            return
        cycle_key = f"{REMOTE_BUSY_STATE_KEY_PREFIX}{task_id}"
        cycle = dict(self.task_store.get_app_state(cycle_key, {}) or {})
        started_at = float(latest.get("started_at") or 0.0)
        prior_generation = float(cycle.get("generation") or 0.0)
        # 新运行世代（重新开跑过）：计数归零，重新获得恰一次自愈机会
        count = int(cycle.get("count") or 0) + 1 if started_at <= prior_generation else 1
        first_busy_at = float(cycle.get("first_busy_at") or 0.0) or time.time()
        self.task_store.set_app_state(cycle_key, {
            "count": count,
            "generation": max(started_at, prior_generation),
            "first_busy_at": first_busy_at,
        })
        if count == 1:
            if self._attempt_stale_lease_cleanup(task_id):
                print(
                    f"[FudanCourseLens] {kind} task {task_id}: stale lease cleanup attempted, "
                    f"requeued to continue",
                    flush=True,
                )
            else:
                print(
                    f"[FudanCourseLens] {kind} task {task_id}: busy (another supervisor), requeued",
                    flush=True,
                )
            # 首周期不睡：外层调度按正常节拍立即续跑（等价「继续本次执行」）
            self.task_store.update_task(task_id, state="queued")
            return
        backoff = REMOTE_BUSY_BACKOFF_SECONDS[
            min(count - 2, len(REMOTE_BUSY_BACKOFF_SECONDS) - 1)
        ]
        if count >= REMOTE_BUSY_HONEST_STAGE:
            waited_minutes = max(0.0, (time.time() - first_busy_at) / 60.0)
            waited = (
                f"已等待约 {round(waited_minutes)} 分钟" if waited_minutes >= 1 else "刚进入排队"
            )
            progress = dict(latest.get("progress") or {})
            progress.update({
                "stage": "waiting_cloud_idle",
                "label": f"前面的云端任务还在收尾，本任务已自动排队（{waited}）",
                "percent": None,
                "indeterminate": True,
                "observed_at": time.time(),
            })
            self.task_store.update_task(task_id, state="queued", progress=progress)
            if count == REMOTE_BUSY_HONEST_STAGE:
                # 诚实排队态进入时日志一条留痕+计数；此后周期静默（退避仍在跑）
                print(
                    f"[FudanCourseLens] {kind} task {task_id}: waiting for cloud idle "
                    f"(cycle {count}, backoff {backoff:.0f}s)",
                    flush=True,
                )
        else:
            self.task_store.update_task(task_id, state="queued")
        time.sleep(backoff)

    def _record_task_usage(self, task_id: str, metrics: dict) -> None:
        """AS6：把一任务的 worker metrics 绝对落任务行（尽力而为，不挡导入）。"""
        values = dict(metrics or {})
        tokens = values.get("deepseek_tokens")
        elapsed = values.get("elapsed_seconds")
        if tokens is None and elapsed is None:
            return
        try:
            self.task_store.record_task_usage(
                str(task_id or ""),
                deepseek_tokens=max(0, int(tokens)) if tokens is not None else None,
                runner_seconds=max(0.0, float(elapsed)) if elapsed is not None else None,
            )
        except Exception:  # noqa: BLE001 - 消耗透镜是尽力而为证据，绝不挡导入
            return

    def deepseek_balance_snapshot(self) -> dict:
        """AS6：DeepSeek 余额读数（设置页显式触发，缓存两档 TTL）。

        无 key 零外联直接 no_key；请求失败/形状不合都归 unavailable 闭集态，
        单次尝试无重试。key 只进内存 Bearer 头，任何路径不落盘不进日志。
        """
        key = self._deepseek_key()
        if not key:
            return {"state": "no_key", "schema": DEEPSEEK_BALANCE_SCHEMA}
        now = time.time()
        cached = self._deepseek_balance_cache
        if cached is not None:
            cached_at, cached_snapshot = cached
            ttl = (
                DEEPSEEK_BALANCE_OK_TTL_SECONDS
                if cached_snapshot.get("state") == "ok"
                else DEEPSEEK_BALANCE_FAIL_TTL_SECONDS
            )
            if now - float(cached_at) < ttl:
                return dict(cached_snapshot)
        snapshot: dict = {"state": "unavailable", "schema": DEEPSEEK_BALANCE_SCHEMA}
        try:
            # 测试模式出站门（src/runtime/test_mode.py）：拒绝 = egress_blocked
            # 审计行，EgressBlockedError 落入下方闭集 except → unavailable。
            ensure_egress_allowed(DEEPSEEK_BALANCE_URL, purpose="deepseek_balance")
            request = urllib.request.Request(
                DEEPSEEK_BALANCE_URL,
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = json.loads((response.read() or b"{}").decode("utf-8") or "{}")
            infos = payload.get("balance_infos")
            info = infos[0] if isinstance(infos, list) and infos and isinstance(infos[0], dict) else {}
            snapshot = {
                "state": "ok",
                "is_available": bool(payload.get("is_available")),
                "currency": str(info.get("currency") or ""),
                "total_balance": str(info.get("total_balance") or ""),
            }
        except Exception:  # noqa: BLE001 - 闭集失败态；异常文本可能含 URL 细节，不外传
            snapshot = {"state": "unavailable", "schema": DEEPSEEK_BALANCE_SCHEMA}
        self._deepseek_balance_cache = (now, snapshot)
        return dict(snapshot)

    def _auto_retry_runner_lost_once(self, task_id: str) -> bool:
        """⑫（LOG1）：runner 中途失联自动重试恰一次（防 infra 杀 runner
        反复让学生手动点）。标记有界（最近 50 个 task_id），已用过则放行
        终态失败；重试复用既有 control_task("retry") 通道（state 机不变）。"""
        key = "remote_runner_lost_retry.v1"
        used = {str(item) for item in (self.task_store.get_app_state(key, []) or [])}
        if str(task_id) in used:
            return False
        try:
            used.add(str(task_id))
            self.task_store.set_app_state(key, sorted(used)[-50:])
            self.control_task(str(task_id), "retry")
        except Exception:
            return False
        return True

    def _auto_retry_question_transient_once(self, task_id: str) -> bool:
        """RR-QWIN-1 Q2：提问链 LLM 瞬态失败自动重试恰一次。⑫ runner_lost
        同款有界标记（独立计数键），重试复用既有 control_task("retry") 通道
        （state 机不变：failed→queued→自动再入队）；再败即终态，绝不连环
        重试烧学生的账。"""
        key = "question_transient_retry.v1"
        used = {str(item) for item in (self.task_store.get_app_state(key, []) or [])}
        if str(task_id) in used:
            return False
        try:
            used.add(str(task_id))
            self.task_store.set_app_state(key, sorted(used)[-50:])
            self.control_task(str(task_id), "retry")
        except Exception:
            return False
        return True

    def explain_question_bookmark(self, bookmark_id: str, *, retry: bool = False) -> dict:
        bookmark = self._question_bookmark(bookmark_id)
        evidence = list(bookmark.get("evidence") or [])
        query = str(bookmark.get("note") or "").strip() or "解释这个时间点附近的课程内容"
        if not evidence:
            explanation = {
                "answer": "资料不足，无法根据当前课程资料回答。",
                "citations": [], "grounded": False, "mode": "declined",
                "query": query, "input_hash": str(bookmark.get("input_hash") or ""),
                "prompt_version": BOOKMARK_ANSWER_PIPELINE_VERSION,
            }
            updated = update_bookmark_explanation(
                self.learning_store.path, bookmark_id=str(bookmark_id), explanation=explanation,
                status="needs_context", error_code="bookmark_evidence_unavailable",
            )
            return {"bookmark": updated, "task": None, "created": False}
        if not self._deepseek_key():
            raise RuntimeError("DeepSeek API key is required for question explanations")

        task_id = str(bookmark.get("task_id") or "")
        task = self.task_store.get_task(task_id) if task_id else None
        if task and str((task.get("payload") or {}).get("input_hash") or "") == str(bookmark.get("input_hash") or ""):
            if task.get("state") in ACTIVE_STATES or task.get("state") == "completed":
                return {"bookmark": bookmark, "task": task, "created": False}
            if retry and task.get("state") in {"failed", "canceled"}:
                task = self.task_store.update_task(
                    task_id, state="queued", resume_requested=False, error="", finished_at=None,
                    payload={**dict(task.get("payload") or {}), "cancel_requested": False},
                ) or task
                update_bookmark_task(
                    self.learning_store.path, bookmark_id=str(bookmark_id), task_id=task_id,
                    explanation_state="queued", input_hash=str(bookmark.get("input_hash") or ""),
                    prompt_version=BOOKMARK_ANSWER_PIPELINE_VERSION, increment_attempt=True,
                )
                with self._lock:
                    self._queue_persisted_task(task)
                    self._question_cancel.clear()
                    self._ensure_question_worker()
                return {"bookmark": self._question_bookmark(bookmark_id), "task": task, "created": False}
            return {"bookmark": bookmark, "task": task, "created": False}

        # RR-QWIN-1 Q2：入队前闭集预检——超长不再裸奔上云空跑一整轮。
        query, oversized = _question_payload_preflight(query, evidence)
        if oversized:
            explanation = {
                "answer": "这次要解释的内容太长了。把问题聚焦到一个具体的知识点，再点一次「解释」就能重试。",
                "citations": [], "grounded": False, "mode": "declined",
                "query": query, "input_hash": str(bookmark.get("input_hash") or ""),
                "prompt_version": BOOKMARK_ANSWER_PIPELINE_VERSION,
            }
            updated = update_bookmark_explanation(
                self.learning_store.path, bookmark_id=str(bookmark_id), explanation=explanation,
                status="needs_context", error_code=oversized,
            )
            return {"bookmark": updated, "task": None, "created": False}

        payload = {
            "bookmark_id": str(bookmark_id),
            "query": query,
            "evidence": evidence,
            "input_hash": str(bookmark.get("input_hash") or self._bookmark_input_hash(query, evidence)),
            "prompt_version": BOOKMARK_ANSWER_PIPELINE_VERSION,
            "cancel_requested": False,
        }
        task, created = self.task_store.add_task(
            "question", str(bookmark.get("course_id") or ""), str(bookmark.get("sub_id") or ""),
            payload, config_key=f"bookmark:{bookmark_id}:{payload['input_hash']}",
        )
        self.task_store.upsert_v3_metadata(
            str(task["task_id"]), dedupe_key=f"question:{bookmark_id}:{payload['input_hash']}",
            stage="queued", privacy_state="sealed", requested_outputs=["answer"],
            input_hash=payload["input_hash"],
        )
        task = self.task_store.update_task(
            str(task["task_id"]), progress={
                "schema_version": 3, "stage": "queued", "percent": None,
                "label": "疑问解释等待在线计算", "indeterminate": True,
                "observed_at": time.time(),
            },
        ) or task
        update_bookmark_task(
            self.learning_store.path, bookmark_id=str(bookmark_id), task_id=str(task["task_id"]),
            explanation_state=str(task.get("state") or "queued"), input_hash=payload["input_hash"],
            prompt_version=BOOKMARK_ANSWER_PIPELINE_VERSION, increment_attempt=bool(created),
        )
        with self._lock:
            self._queue_persisted_task(task)
            self._ensure_question_worker()
        return {"bookmark": self._question_bookmark(bookmark_id), "task": task, "created": bool(created)}

    def set_question_bookmark_resolution(self, bookmark_id: str, *, resolved: bool) -> dict:
        return set_bookmark_resolution(
            self.learning_store.path, bookmark_id=str(bookmark_id), resolved=bool(resolved)
        )

    def delete_question_bookmark(self, bookmark_id: str) -> dict:
        return delete_bookmark(self.learning_store.path, bookmark_id=str(bookmark_id))

    def cancel_question_explanation(self, bookmark_id: str) -> dict:
        bookmark = self._question_bookmark(bookmark_id)
        task_id = str(bookmark.get("task_id") or "")
        task = self.task_store.get_task(task_id) if task_id else None
        if not task or task.get("state") in {"completed", "failed", "canceled"}:
            return {"bookmark": bookmark, "task": task}
        payload = {**dict(task.get("payload") or {}), "cancel_requested": True}
        if task.get("state") in {"queued", "paused"}:
            task = self.task_store.mark_terminal(task_id, "canceled") or task
            state = "canceled"
        else:
            task = self.task_store.update_task(task_id, state="pausing", payload=payload) or task
            self._question_cancel.set()
            state = "canceling"
        update_bookmark_task(
            self.learning_store.path,
            bookmark_id=str(bookmark_id),
            task_id=task_id,
            explanation_state=state,
        )
        return {"bookmark": self._question_bookmark(bookmark_id), "task": task}

    def _ensure_question_worker(self) -> None:
        if self._question_worker and self._question_worker.is_alive():
            return
        self._question_worker = threading.Thread(
            target=self._run_question_queue,
            name="courselens-question-explainer",
            daemon=True,
        )
        self._question_worker.start()

    @staticmethod
    def _question_error_code(exc: Exception) -> str:
        # LIVE-VALIDATE-1 LV1-3：显式闭集码优先——派发前的授权/连接拒绝
        # （cloud_setup_required / 连接组件 authorization_missing）不再依赖
        # 中英文消息关键词启发式，学生看到的失败码如实反映「连接没就绪」。
        explicit = str(getattr(exc, "code", "") or "").strip()
        if explicit in {"cloud_setup_required", "authorization_missing"}:
            return "cloud_setup_required"
        message = str(exc).casefold()
        return CourseLensApplication._transient_answer_error_code(message)

    def _assessment_answer_target(self, task: dict, payload: dict) -> tuple[dict | None, str]:
        """worker 侧解出目标题目；解不到就返回闭集原因码（任务随后如实失败）。

        排队期间题目被重新拆过（content_hash 变了）就不算了：这份解答算出来也已经
        对不上现在这道题，宁可如实失败让学生重来。
        """
        from src.runtime.assessment_ir import list_assessment_items

        item_id = str(payload.get("assessment_item_id") or "")
        rows = list_assessment_items(
            self.learning_store.path, course_id=str(task.get("course_id") or ""), limit=2000
        )
        row = next((value for value in rows if str(value.get("item_id")) == item_id), None)
        if row is None:
            return None, "assessment_item_unknown"
        if str(row.get("content_hash") or "") != str(payload.get("content_hash") or ""):
            return None, "assessment_item_content_stale"
        if str(row.get("status") or "") == "orphaned":
            return None, "assessment_item_source_lost"
        return row, ""

    def _run_question_queue(self) -> None:
        from src.remote.coordinator import RemoteTaskPaused
        from src.remote.protocol import JOB_SCHEMA, PROTOCOL_VERSION
        from src.runtime.assessment_ir import save_ai_answer, validate_assessment_answer

        while True:
            if self._generation_workers_stop.is_set():
                with self._lock:
                    self._question_current = None
                return
            with self._lock:
                if not self._question_queue:
                    self._question_current = None
                    return
                item = None if self._paused else self._question_queue.pop(0)
            if item is None:
                self._generation_workers_stop.wait(0.25)
                continue
            task_id = str(item.get("task_id") or "")
            task = self.task_store.get_task(task_id)
            if not task or task.get("state") != "queued":
                continue
            payload = dict(task.get("payload") or {})
            bookmark_id = str(payload.get("bookmark_id") or "")
            assessment_id = str(payload.get("assessment_item_id") or "")
            # P3-CONTRACT-1：第三目标分支（palette 深度回答）只加不改——书签/
            # 考核两分支判据与代码路径逐字不动；三分支全缺才判任务无效。
            is_deep = payload.get("deep_answer") is True
            if not bookmark_id and not assessment_id and not is_deep:
                self.task_store.mark_terminal(task_id, "failed", error="bookmark_task_invalid")
                continue
            item_row: dict = {}
            if assessment_id:
                # 与疑问解释共用同一条云端链，只有「结果落到哪」不同。
                item_row, target_error = self._assessment_answer_target(task, payload)
                if item_row is None:
                    self.task_store.mark_terminal(task_id, "failed", error=target_error)
                    self.task_store.upsert_v3_metadata(task_id, stage="failed")
                    continue
            is_ai_answer = bool(assessment_id)
            subject = "题目解答" if is_ai_answer else ("深度回答" if is_deep else "疑问解释")

            def record_target_state(stage: str, code: str = "") -> None:
                if is_deep:
                    # P3：深度回答无本地书签/题目行，唯一落点=search_answers
                    # store；终态失败也落（学生可复看失败原因）。取消/暂停不落
                    # （state 闭集只有 ready|insufficient|failed）。
                    if stage == "failed":
                        self._persist_deep_search_answer(
                            task_id=task_id,
                            input_hash=str(payload.get("input_hash") or ""),
                            query=str(payload.get("query") or ""),
                            course_ids=[str(task.get("course_id") or "")] if str(task.get("course_id") or "") else [],
                            sub_id=str(task.get("sub_id") or ""),
                            state="failed", answer="", citations=[], grounded=False,
                            model="", prompt_version=BOOKMARK_ANSWER_PIPELINE_VERSION,
                            error_code=code,
                        )
                    return
                if is_ai_answer:
                    save_ai_answer(
                        self.learning_store.path, item=item_row, stage=stage, error_code=code,
                        task_id=task_id, input_hash=str(payload.get("input_hash") or ""),
                    )
                else:
                    update_bookmark_task(
                        self.learning_store.path, bookmark_id=bookmark_id, task_id=task_id,
                        explanation_state=stage, error_code=code,
                    )

            self._active_question_task_id = task_id
            self._question_cancel.clear()
            task = self.task_store.update_task(
                task_id, state="running", started_at=time.time(), progress={
                    "schema_version": 3, "stage": "preflight", "percent": None,
                    "label": "正在确认在线解释链路", "indeterminate": True,
                    "observed_at": time.time(),
                },
            ) or task
            record_target_state("running")
            with self._lock:
                self._question_current = {
                    "task_id": task_id, "sub_id": str(task.get("sub_id") or ""),
                    "sub_title": subject, "stage": "preflight",
                }

            def on_progress(stage: str, percent: float | None, label: str) -> None:
                progress = {
                    "schema_version": 3, "stage": str(stage), "percent": percent,
                    "label": str(label), "indeterminate": percent is None,
                    "observed_at": time.time(),
                }
                self.task_store.update_task(task_id, progress=progress)
                self.task_store.upsert_v3_metadata(task_id, stage=str(stage))
                with self._lock:
                    if self._question_current and self._question_current.get("task_id") == task_id:
                        self._question_current["stage"] = str(stage)

            def build_job(result_public_key: str) -> dict:
                now = time.time()
                return {
                    "schema": JOB_SCHEMA,
                    "protocol_version": PROTOCOL_VERSION,
                    "task_id": task_id,
                    "job_kind": "learning_pack",
                    "requested_outputs": ["answer"],
                    "created_at": now,
                    "expires_at": now + 600,
                    "result_public_key": result_public_key,
                    "pipeline": {"version": BOOKMARK_ANSWER_PIPELINE_VERSION, "llm": "deepseek-flash"},
                    "payload": {
                        "query": str(payload.get("query") or ""),
                        "evidence": list(payload.get("evidence") or []),
                        # RR-P6MEM-1：课程记忆术语表进问答链（worker 读侧加性
                        # course_terms 通道）；非空才带键，缺席=旧行为。
                        **({"glossary": terms} if (terms := self._course_memory_terms(str(task.get("course_id") or ""))) else {}),
                    },
                    "secrets": {"deepseek_api_key": self._deepseek_key()},
                }

            def import_result(result: dict) -> None:
                # AS6/RR-PARK-1 P2：解释/AI 解答同为真实消耗面——与
                # summary/subtitle 导入体同款，结果交付即落本任务消耗
                # （绝对值，幂等；尽力而为，绝不挡导入）。月账此前恒 0
                # 的断点正在于此：本链从不调 _record_task_usage。
                self._record_task_usage(task_id, dict(result.get("metrics") or {}))
                raw_answer = dict(dict(result.get("outputs") or {}).get("answer") or {})
                memory_note = self._memory_note_for(result.get("metrics"), kind="answer")
                if is_ai_answer:
                    # 同一条引用校验纪律，换成能容纳课程级（无讲次）文档页锚的判据。
                    validated = validate_assessment_answer(
                        raw_answer, list(payload.get("evidence") or [])
                    )
                    grounded = bool(validated.get("grounded"))
                    if grounded and memory_note:
                        # RR-P6MEM-1：学生可见记忆标注（真注入过才加）。
                        validated["answer"] = f"{str(validated.get('answer') or '')}{memory_note}"
                        validated["memory_applied"] = _memory_terms_count(result)
                    save_ai_answer(
                        self.learning_store.path, item=item_row,
                        stage="ready" if grounded else "insufficient",
                        answer=str(validated.get("answer") or ""),
                        citations=list(validated.get("citations") or []),
                        model="deepseek-flash", task_id=task_id,
                        input_hash=str(payload.get("input_hash") or ""),
                        error_code="" if grounded else "assessment_evidence_insufficient",
                    )
                    if grounded:
                        self._memory_feedback_sink(
                            str(task.get("course_id") or ""), str(validated.get("answer") or ""),
                            source="answer", sub_id=str(task.get("sub_id") or ""),
                        )
                    return
                if is_deep:
                    # P3：与书签解释同一份引用校验纪律（双层闭集=worker 引用闭
                    # 集 + 客户端 source_hash 复核）；结果落 search_answers
                    # store（书签分支不适用——palette 不调 update_bookmark_explanation）。
                    validated = validate_grounded_answer(raw_answer, list(payload.get("evidence") or []))
                    grounded = bool(validated.get("grounded"))
                    answer = dict(validated)
                    answer.update({
                        "mode": "remote",
                        "query": str(payload.get("query") or ""),
                        "input_hash": str(payload.get("input_hash") or ""),
                        "model": "deepseek-flash",
                        "prompt_version": BOOKMARK_ANSWER_PIPELINE_VERSION,
                    })
                    if grounded and memory_note:
                        # RR-P6MEM-1：学生可见记忆标注（真注入过才加），照书签分支同式。
                        answer["answer"] = f"{str(answer.get('answer') or '')}{memory_note}"
                        answer["memory_applied"] = _memory_terms_count(result)
                        self._memory_feedback_sink(
                            str(task.get("course_id") or ""), str(answer.get("answer") or ""),
                            source="answer", sub_id=str(task.get("sub_id") or ""),
                        )
                    self._persist_deep_search_answer(
                        task_id=task_id,
                        input_hash=str(payload.get("input_hash") or ""),
                        query=str(payload.get("query") or ""),
                        course_ids=[str(task.get("course_id") or "")] if str(task.get("course_id") or "") else [],
                        sub_id=str(task.get("sub_id") or ""),
                        state="ready" if grounded else "insufficient",
                        answer=str(answer.get("answer") or ""),
                        citations=list(answer.get("citations") or []),
                        grounded=grounded, model="deepseek-flash",
                        prompt_version=BOOKMARK_ANSWER_PIPELINE_VERSION, error_code="",
                    )
                    return
                answer = validate_grounded_answer(raw_answer, list(payload.get("evidence") or []))
                answer.update({
                    "mode": "remote",
                    "query": str(payload.get("query") or ""),
                    "input_hash": str(payload.get("input_hash") or ""),
                    "model": "deepseek-flash",
                    "prompt_version": BOOKMARK_ANSWER_PIPELINE_VERSION,
                })
                if answer.get("grounded") and memory_note:
                    # RR-P6MEM-1：学生可见记忆标注（真注入过才加）。
                    answer["answer"] = f"{str(answer.get('answer') or '')}{memory_note}"
                    answer["memory_applied"] = _memory_terms_count(result)
                    self._memory_feedback_sink(
                        str(task.get("course_id") or ""), str(answer.get("answer") or ""),
                        source="answer", sub_id=str(task.get("sub_id") or ""),
                    )
                update_bookmark_explanation(
                    self.learning_store.path,
                    bookmark_id=bookmark_id,
                    explanation=answer,
                    status="completed" if answer.get("grounded") else "needs_context",
                    model="deepseek-flash",
                    task_id=task_id,
                    error_code="" if answer.get("grounded") else "bookmark_evidence_insufficient",
                )

            try:
                with self._cloud_run_slot(
                    task_id,
                    cancel_requested=self._question_cancel.is_set,
                    on_wait=lambda ahead: on_progress(
                        "remote_queue", None, self._cloud_queue_label(ahead)
                    ),
                ):
                    with self._leased_remote_coordinator(
                        task_id,
                        # N1-ROUTING + D-20261009-13：learning_pack 在 worker N20
                        # profile 合同闭集内（必须 process-v1），白名单派 process.yml；
                        # 载荷（query/evidence/glossary）媒体面缺席不再决定归属。
                        workflow=_remote_dispatch_workflow(
                            {"query": "", "evidence": []}, job_kind="learning_pack",
                        ),
                    ) as coordinator:
                        coordinator.execute(
                            task_id=task_id,
                            build_job=build_job,
                            import_result=import_result,
                            cancel_requested=self._question_cancel.is_set,
                            progress=on_progress,
                        )
                self.task_store.update_task(task_id, progress={
                    "schema_version": 3, "stage": "completed", "percent": 100.0,
                    "label": f"{subject}已导入", "indeterminate": False,
                    "observed_at": time.time(),
                })
                self.task_store.upsert_v3_metadata(task_id, stage="completed")
                self.task_store.mark_terminal(task_id, "completed")
            except RemoteTaskPaused:
                latest = self.task_store.get_task(task_id) or {}
                canceled = bool(dict(latest.get("payload") or {}).get("cancel_requested"))
                if canceled:
                    self.task_store.mark_terminal(task_id, "canceled")
                    # AI 解答没有 canceled 这一态：如实记成失败并标清是被取消的，
                    # 学生再点一次就重来。
                    record_target_state("failed" if is_ai_answer else "canceled",
                                        "canceled" if is_ai_answer else "")
                else:
                    paused = self.task_store.acknowledge_pause(task_id) or {}
                    state = str(paused.get("state") or "paused")
                    record_target_state("failed" if is_ai_answer else state,
                                        "paused" if is_ai_answer else "")
            except Exception as exc:
                if _is_supervisor_busy(exc):
                    # AS10（第五十案）：同字幕分支——busy 处置共用化
                    self._handle_remote_supervisor_busy(task_id, kind="question")
                elif self._defer_remote_task_failure(task_id, kind="question", sub_id=""):
                    # 第四十二案：疑问解释的远端 run 同样跨进程；本地闸门失败
                    # 不构成终态判决（helper 已记日志）
                    pass
                else:
                    code = self._question_error_code(exc)
                    self.task_store.mark_terminal(task_id, "failed", error=code)
                    self.task_store.upsert_v3_metadata(task_id, stage="failed")
                    if code == "remote_runner_lost" and self._auto_retry_runner_lost_once(task_id):
                        # ⑫：云端机器临时掉线——自动重试已入队，不再把书签
                        # 打成失败终态（重试成功后照常收口）
                        self.task_store.update_task(task_id, progress={
                            "schema_version": 3, "stage": "queued", "percent": None,
                            "label": "云端机器临时掉线，不是你的操作问题；已自动重试一次",
                            "indeterminate": True, "observed_at": time.time(),
                        })
                    elif code in _QUESTION_TRANSIENT_RETRY_CODES and self._auto_retry_question_transient_once(task_id):
                        # RR-QWIN-1 Q2：LLM 坏形状/瞬态超时不再一次判死——自动
                        # 重试恰一次（有界标记），重试成功照常收口；再败走下面
                        # 原终态路径。书签/AI 解答态保持 in-flight（与 ⑫ 同款）。
                        self.task_store.update_task(task_id, progress={
                            "schema_version": 3, "stage": "queued", "percent": None,
                            "label": f"{subject}这次没答上来，已自动重试一次",
                            "indeterminate": True, "observed_at": time.time(),
                        })
                    else:
                        record_target_state("failed", code)
            finally:
                self._active_question_task_id = ""
                self._question_cancel.clear()
                with self._lock:
                    if self._question_current and self._question_current.get("task_id") == task_id:
                        self._question_current = None
                latest = self.task_store.get_task(task_id) or {}
                if latest.get("state") == "queued" and not self._paused:
                    with self._lock:
                        self._queue_persisted_task(latest)
                        self._ensure_question_worker()

    # ------------------------------------------------------------------
    # P11 质量抽检（quality_judge）：按需触发的 LLM-as-judge 离线质检。
    # 合同=P11-CONTRACT-1；线程族镜像 question worker，但不进 pause 全家
    # 桶——运行中质检不可取消（时长≈1-2 分钟），queued 态走 generic cancel。
    # ------------------------------------------------------------------

    def request_quality_judge(self, sub_id: str) -> dict:
        """POST /api/v3/quality-judge 入口：按需对一讲发起一次质量抽检。

        preflight 五连闭集拒绝（零 LLM 零外联）；同 input_hash 活跃任务幂等
        复用，不同 hash 在飞 409，failed 任务 re-POST 走 retry 语义。"""
        sub_id = str(sub_id or "").strip()
        if not sub_id:
            raise QualityJudgeRequestError("sub_id_required")
        lecture = self.catalog_repository.get_lecture(sub_id)
        if not lecture:
            raise QualityJudgeRequestError("lecture_not_in_catalog", status=404)
        segments = self.learning_store.get_transcript_segments(sub_id)
        artifact = self.learning_store.find_ai_artifact(sub_id, "timestamp_summary")
        summary_content = dict((artifact or {}).get("content") or {})
        if not segments and not summary_content:
            raise QualityJudgeRequestError("quality_no_material")
        if not self._deepseek_key():
            raise QualityJudgeRequestError("deepseek_key_required")
        try:
            compute = self.remote_compute_snapshot()
        except Exception:  # noqa: BLE001 - 快照失败=无法证明已验证，fail-closed
            compute = {}
        if not (compute.get("configured") and compute.get("verified")):
            raise QualityJudgeRequestError("remote_not_configured")

        course_id = str(lecture.get("course_id") or "")
        segment_indices = _quality_sample_indices(len(segments))
        input_hash = _quality_input_hash(
            sub_id,
            segment_indices,
            _quality_transcript_digest(segments),
            str((summary_content.get("generation") or {}).get("input_hash") or ""),
        )

        active = self.task_store.find_active("quality_judge", sub_id)
        if active:
            if str((active.get("payload") or {}).get("input_hash") or "") == input_hash:
                return {"task": active, "created": False}
            raise QualityJudgeRequestError("quality_task_in_flight", status=409)
        failed = next(
            (
                # CLIENT-STATE-R1：failed 复用查找取「最近一次同输入失败」——
                # 旧序会把最老的 failed 行当复用目标。
                task for task in self.task_store.list_tasks(
                    kinds=("quality_judge",), limit=200, newest_first=True,
                )
                if str(task.get("sub_id") or "") == sub_id
                and str(task.get("state") or "") == "failed"
                and str((task.get("payload") or {}).get("input_hash") or "") == input_hash
            ),
            None,
        )
        if failed is not None:
            # 合同 §①：failed 任务 re-POST=复用任务行走 retry 语义（re-POST 即重试）。
            retried = self.control_task(str(failed.get("task_id") or ""), "retry")
            return {"task": retried.get("task"), "created": False}

        payload: dict[str, object] = {
            "title": str(lecture.get("sub_title") or sub_id),
            "input_hash": input_hash,
            "cancel_requested": False,
        }
        terms = self._course_memory_terms(course_id)
        if terms:
            payload["glossary"] = [str(term) for term in terms]
        # THINK-LADDER-2 设计 B：记忆边界候选随载荷顺路 flash 裁决（加性键，
        # 旧客户端不发此键=worker 侧字节恒等）；零候选=省键=旧行为。
        boundary = self._term_boundary_payload(course_id)
        if boundary:
            payload["term_boundary"] = boundary
        if segments:
            payload["subtitle_sample"] = {
                "total_segments": len(segments),
                "segments": [_quality_canonical_segment(segments[index]) for index in segment_indices],
            }
        if summary_content:
            chapters = [
                chapter for chapter in summary_content.get("chapters") or []
                if isinstance(chapter, dict)
            ]
            payload["summary_pack"] = {
                "markdown": str(summary_content.get("overview") or "")[:5000],
                "chapters": [
                    {
                        "title": str(chapter.get("title") or ""),
                        "summary": str(chapter.get("summary") or ""),
                        "start_ms": int(chapter.get("start_ms") or 0),
                    }
                    for chapter in chapters
                ],
                "key_takeaways": [str(item) for item in summary_content.get("key_takeaways") or []],
                "transcript": [_quality_canonical_segment(segment) for segment in segments],
            }

        task, created = self.task_store.add_task(
            "quality_judge", course_id, sub_id, payload,
            config_key=f"quality-judge:{sub_id}:{input_hash}",
        )
        # dedupe_key 含 task_id：v3 元数据表 dedupe_key 有 UNIQUE 索引，同讲
        # 重检的新任务行不得与历史行碰撞（P3 已裁决的同款偏差形态）。
        self.task_store.upsert_v3_metadata(
            str(task["task_id"]),
            dedupe_key=f"quality_judge:{sub_id}:{input_hash}:{task['task_id']}",
            stage="queued", privacy_state="sealed", requested_outputs=["quality_judge"],
            input_hash=input_hash,
        )
        task = self.task_store.update_task(
            str(task["task_id"]), progress={
                "schema_version": 3, "stage": "queued", "percent": None,
                "label": "质量抽检等待在线检查", "indeterminate": True,
                "observed_at": time.time(),
            },
        ) or task
        with self._lock:
            self._queue_persisted_task(task)
            self._ensure_quality_judge_worker()
        return {"task": task, "created": bool(created)}

    def _ensure_quality_judge_worker(self) -> None:
        if self._quality_worker and self._quality_worker.is_alive():
            return
        self._quality_worker = threading.Thread(
            target=self._run_quality_queue,
            name="courselens-quality-judge",
            daemon=True,
        )
        self._quality_worker.start()

    @staticmethod
    def _transient_answer_error_code(message: str) -> str:
        """question/quality 两错误映射器共享的关键词尾部（R3-11 收敛，逐键等值）。

        已知疣点（有意保留现行为）：`"authorization" in message` 的宽松首支
        会把任何含 authorization 字样的消息（含 DeepSeek 侧措辞）归入
        remote_authorization_required；显式闭集码通道成熟后本启发式整段
        退役。括号仅为明示 `or/and` 优先级，与收敛前行为逐字等值。
        """
        if "429" in message or "rate" in message:
            return "deepseek_rate_limited"
        if "timeout" in message or "timed out" in message:
            return "remote_answer_timeout"
        if "authorization" in message or ("github" in message and "ready" in message):
            return "remote_authorization_required"
        if "deepseek" in message or "llm" in message:
            return "deepseek_answer_failed"
        return "remote_answer_failed"

    @staticmethod
    def _quality_error_code(exc: Exception) -> str:
        # LLM 瞬态=既有漏斗码原样（question 家族同款）；云侧旧镜像不识别
        # 新 job kind 时升为专属闭集码，绝不静默。
        explicit = str(getattr(exc, "code", "") or "").strip()
        if explicit in {"judge_output_invalid", "cloud_setup_required"}:
            return explicit
        if explicit == "authorization_missing":
            return "cloud_setup_required"
        message = str(exc).casefold()
        if "unsupported job kind" in message:
            return "worker_kind_unsupported"
        return CourseLensApplication._transient_answer_error_code(message)

    def _auto_retry_quality_transient_once(self, task_id: str) -> bool:
        """P11：质检链 LLM 瞬态失败自动重试恰一次（question 家族同款有界标记）。

        再败即终态，绝不连环重试烧学生的账；429/授权类不进瞬态集。"""
        key = "quality_transient_retry.v1"
        used = {str(item) for item in (self.task_store.get_app_state(key, []) or [])}
        if str(task_id) in used:
            return False
        try:
            used.add(str(task_id))
            self.task_store.set_app_state(key, sorted(used)[-50:])
            self.control_task(str(task_id), "retry")
        except Exception:
            return False
        return True

    def _run_quality_queue(self) -> None:
        from src.remote.coordinator import RemoteTaskPaused
        from src.remote.protocol import JOB_SCHEMA, PROTOCOL_VERSION

        while True:
            if self._generation_workers_stop.is_set():
                with self._lock:
                    self._quality_current = None
                return
            with self._lock:
                if not self._quality_queue:
                    self._quality_current = None
                    return
                item = None if self._paused else self._quality_queue.pop(0)
            if item is None:
                self._generation_workers_stop.wait(0.25)
                continue
            task_id = str(item.get("task_id") or "")
            task = self.task_store.get_task(task_id)
            if not task or task.get("state") != "queued":
                continue
            payload = dict(task.get("payload") or {})
            if not isinstance(payload.get("subtitle_sample"), dict) and not isinstance(
                payload.get("summary_pack"), dict
            ):
                self.task_store.mark_terminal(task_id, "failed", error="quality_no_material")
                self.task_store.upsert_v3_metadata(task_id, stage="failed")
                continue
            input_hash = str(payload.get("input_hash") or "")
            course_id = str(task.get("course_id") or "")
            sub_id = str(task.get("sub_id") or "")
            subject = "质量抽检"

            task = self.task_store.update_task(
                task_id, state="running", started_at=time.time(), progress={
                    "schema_version": 3, "stage": "preflight", "percent": None,
                    "label": "正在确认在线检查链路", "indeterminate": True,
                    "observed_at": time.time(),
                },
            ) or task
            with self._lock:
                self._quality_current = {
                    "task_id": task_id, "sub_id": sub_id,
                    "sub_title": subject, "stage": "preflight",
                }

            def on_progress(stage: str, percent: float | None, label: str) -> None:
                progress = {
                    "schema_version": 3, "stage": str(stage), "percent": percent,
                    "label": str(label), "indeterminate": percent is None,
                    "observed_at": time.time(),
                }
                self.task_store.update_task(task_id, progress=progress)
                self.task_store.upsert_v3_metadata(task_id, stage=str(stage))
                with self._lock:
                    if self._quality_current and self._quality_current.get("task_id") == task_id:
                        self._quality_current["stage"] = str(stage)

            def build_job(result_public_key: str) -> dict:
                now = time.time()
                return {
                    "schema": JOB_SCHEMA,
                    "protocol_version": PROTOCOL_VERSION,
                    "task_id": task_id,
                    "job_kind": "quality_judge",
                    "requested_outputs": ["quality_judge"],
                    "created_at": now,
                    "expires_at": now + 600,
                    "result_public_key": result_public_key,
                    "pipeline": {"version": QUALITY_JUDGE_PIPELINE_VERSION, "llm": "deepseek-flash"},
                    "payload": {
                        "title": str(payload.get("title") or sub_id),
                        **({"glossary": list(payload["glossary"])} if payload.get("glossary") else {}),
                        **(
                            {"subtitle_sample": dict(payload["subtitle_sample"])}
                            if isinstance(payload.get("subtitle_sample"), dict) else {}
                        ),
                        **(
                            {"summary_pack": dict(payload["summary_pack"])}
                            if isinstance(payload.get("summary_pack"), dict) else {}
                        ),
                        # THINK-LADDER-2 设计 B：边界候选词对随载荷进 worker 一次
                        # flash 裁决（加性键；任务载荷缺席=旧形状字节恒等）。
                        **(
                            {"term_boundary": [dict(item) for item in payload["term_boundary"]]}
                            if isinstance(payload.get("term_boundary"), list) else {}
                        ),
                    },
                    "secrets": {"deepseek_api_key": self._deepseek_key()},
                }

            def import_result(result: dict) -> None:
                # 真实消耗面：结果交付即落本任务消耗（绝对值，尽力而为）。
                self._record_task_usage(task_id, dict(result.get("metrics") or {}))
                raw_report = dict(dict(result.get("outputs") or {}).get("quality_judge") or {})
                summary_pack = payload.get("summary_pack") if isinstance(payload.get("summary_pack"), dict) else {}
                subtitle_sample = payload.get("subtitle_sample") if isinstance(payload.get("subtitle_sample"), dict) else {}
                cleaned, counts = _normalize_quality_judge_report(
                    raw_report,
                    sample_size=len(subtitle_sample.get("segments") or []),
                    summary_chapter_count=len(summary_pack.get("chapters") or []),
                    summary_takeaway_count=len(summary_pack.get("key_takeaways") or []),
                )
                merged_metrics = dict(result.get("metrics") or {})
                merged_metrics.update(counts)
                # 报告零内容落盘：唯一落点=闭集计数报告（ai_artifacts
                # kind=quality_report），judge 输入瞬态存在。
                self.learning_store.import_quality_report(
                    course_id=course_id, sub_id=sub_id, input_hash=input_hash,
                    model="deepseek-flash", report=cleaned, metrics=merged_metrics,
                )
                # THINK-LADDER-2 设计 B：边界裁决导入（user-adjudication-
                # supreme 三路：valid∧signal≥2 自动确认可撤销/invalid 注记折叠/
                # unsure·缺位 no-op）。fail-closed，绝不拖垮质检导入本体。
                self._import_boundary_rulings(
                    course_id,
                    pairs=(
                        payload["term_boundary"]
                        if isinstance(payload.get("term_boundary"), list) else []
                    ),
                    rulings=dict(result.get("outputs") or {}).get("term_boundary_rulings") or {},
                )

            try:
                with self._cloud_run_slot(
                    task_id,
                    cancel_requested=None,
                    on_wait=lambda ahead: on_progress(
                        "remote_queue", None, self._cloud_queue_label(ahead)
                    ),
                ):
                    with self._leased_remote_coordinator(
                        task_id,
                        # N1-ROUTING：质量抽检载荷（subtitle_sample/summary_pack）媒体面缺席 → llm.yml 快路径。
                        workflow=_remote_dispatch_workflow({"subtitle_sample": {}}),
                    ) as coordinator:
                        coordinator.execute(
                            task_id=task_id,
                            build_job=build_job,
                            import_result=import_result,
                            cancel_requested=lambda: False,
                            progress=on_progress,
                        )
                self.task_store.update_task(task_id, progress={
                    "schema_version": 3, "stage": "completed", "percent": 100.0,
                    "label": "质量抽检报告已生成（只有计数没有内容，全部在本机）",
                    "indeterminate": False,
                    "observed_at": time.time(),
                })
                self.task_store.upsert_v3_metadata(task_id, stage="completed")
                self.task_store.mark_terminal(task_id, "completed")
            except RemoteTaskPaused:
                # 质检不进 pause 全家桶（运行中不可取消）：该异常只剩云端 run
                # 被取消一种来源——如实终态失败，re-POST 即重试。
                self.task_store.mark_terminal(task_id, "failed", error="remote_answer_failed")
                self.task_store.upsert_v3_metadata(task_id, stage="failed")
            except Exception as exc:
                if getattr(exc, "code", "") == "judge_output_invalid" or (
                    isinstance(exc, ValueError) and str(exc) == "judge_output_invalid"
                ):
                    # 双 mode 无效：fail-closed 诚实失败（re-POST 即重试），
                    # 不进瞬态自动重试。
                    self.task_store.mark_terminal(task_id, "failed", error="judge_output_invalid")
                    self.task_store.upsert_v3_metadata(task_id, stage="failed")
                elif _is_supervisor_busy(exc):
                    self._handle_remote_supervisor_busy(task_id, kind="quality_judge")
                elif self._defer_remote_task_failure(task_id, kind="quality_judge", sub_id=sub_id):
                    # 本地闸门失败不构成终态判决（远端真值未定，helper 已记日志）。
                    pass
                else:
                    code = self._quality_error_code(exc)
                    self.task_store.mark_terminal(task_id, "failed", error=code)
                    self.task_store.upsert_v3_metadata(task_id, stage="failed")
                    if code == "remote_runner_lost" and self._auto_retry_runner_lost_once(task_id):
                        self.task_store.update_task(task_id, progress={
                            "schema_version": 3, "stage": "queued", "percent": None,
                            "label": "云端机器临时掉线，不是你的操作问题；已自动重试一次",
                            "indeterminate": True, "observed_at": time.time(),
                        })
                    elif code in _QUESTION_TRANSIENT_RETRY_CODES and self._auto_retry_quality_transient_once(task_id):
                        self.task_store.update_task(task_id, progress={
                            "schema_version": 3, "stage": "queued", "percent": None,
                            "label": "质量抽检这次没跑成，已自动重试一次",
                            "indeterminate": True, "observed_at": time.time(),
                        })
            finally:
                with self._lock:
                    if self._quality_current and self._quality_current.get("task_id") == task_id:
                        self._quality_current = None
                latest = self.task_store.get_task(task_id) or {}
                if latest.get("state") == "queued" and not self._paused:
                    with self._lock:
                        self._queue_persisted_task(latest)
                        self._ensure_quality_judge_worker()

    def configure_daily_schedule(self, value: dict) -> dict:
        previous = dict(self.task_store.get_app_state("daily_schedule", {}) or {})
        schedule = {
            **value,
            "account_id": str(self._credentials.get("student_id") or previous.get("account_id") or ""),
            "last_run_date": str(previous.get("last_run_date") or ""),
            "last_result": dict(previous.get("last_result") or {}),
            "pending": list(previous.get("pending") or []),
            "pipeline_version": DAILY_SCHEDULE_PIPELINE_VERSION,
        }
        registration = {"registered": False}
        if os.name == "nt":
            from src.runtime.scheduler_windows import register_daily_task

            # P67 ZERO-CONSOLE-1 U2: the 07:30 task action used to be
            # powershell.exe, so a console window flashed on the student's
            # desktop every morning. Inside a managed install the action is
            # the pythonw silent host instead: same trusted launcher chain as
            # the shortcuts (hidden console, -NoOpen forwarded, mutex-aware),
            # just windowless. The powershell form stays only for development
            # roots that have no managed launcher.
            install_root = os.environ.get("COURSELENS_INSTALL_ROOT")
            launcher_root = Path(install_root) / "launcher" if install_root else None
            pythonw_exe = launcher_root / "python312" / "pythonw.exe" if launcher_root else None
            silent_host = launcher_root / "start_managed_courselens.pyw" if launcher_root else None
            if pythonw_exe and silent_host and pythonw_exe.is_file() and silent_host.is_file():
                registration = register_daily_task(
                    command=str(pythonw_exe),
                    arguments=f'-B "{silent_host}" -InstallRoot "{install_root}" -NoOpen',
                    # The task XML is written under the working directory
                    # (register_daily_task); it must land in the managed root
                    # and NEVER inside the launcher tree — a stray file there
                    # would fail the launcher's trust walk on the task's next
                    # fire (live-sandbox finding, P67 U4).
                    working_directory=str(Path(install_root)),
                    at=str(schedule["time"]),
                    enabled=bool(schedule["enabled"]),
                )
            else:
                script = PROJECT_ROOT / "start_fudan_courselens.ps1"
                registration = register_daily_task(
                    command="powershell.exe",
                    arguments=f'-NoProfile -ExecutionPolicy Bypass -File "{script}" -NoOpen',
                    working_directory=str(PROJECT_ROOT),
                    at=str(schedule["time"]),
                    enabled=bool(schedule["enabled"]),
                )
        elif sys.platform == "darwin":
            # MAC-NIGHT-1 接线（平台抽象层 README §5）：macOS 每日同步走
            # launchd LaunchAgent 后端；Windows schtasks 形状逐位不变。
            # 打包形态（py2app .app）复用同一二进制无头重入；开发树走
            # `python -m src --no-open`。禁用=卸载 LaunchAgent（诚实缺席）。
            from src.platform.autostart import get_autostart_backend

            backend = get_autostart_backend()
            if bool(schedule["enabled"]):
                if getattr(sys, "frozen", False):
                    command, arguments = sys.executable, "--no-open"
                    working_directory = ""
                else:
                    command, arguments = sys.executable, "-m src --no-open"
                    working_directory = str(PROJECT_ROOT)
                registration = backend.register(
                    command=command,
                    arguments=arguments,
                    working_directory=working_directory,
                    at=str(schedule["time"]),
                )
            else:
                registration = backend.unregister()
        schedule["registration"] = registration
        self.task_store.set_app_state("daily_schedule", schedule)
        return schedule

    def _load_daily_schedule_account(self, schedule: dict) -> None:
        with self._lock:
            ready = bool(self._credentials.get("student_id") and self._credentials.get("password"))
        if ready:
            return
        account_id = str(schedule.get("account_id") or "").strip()
        if not account_id:
            accounts = [item for item in self.credentials.list_accounts() if not item.get("requires_rotation")]
            if len(accounts) == 1:
                account_id = str(accounts[0].get("student_id") or "")
        if not account_id:
            raise RuntimeError("每日任务需要先在界面中加密保存一个可用账号")
        self.use_saved_credentials(account_id)

    def run_daily_schedule(self, *, force: bool = False) -> dict:
        schedule = dict(self.task_store.get_app_state("daily_schedule", {}) or {})
        if not schedule.get("enabled") and not force:
            return {"state": "disabled", "queued": 0, "discovered": 0}
        now = _shanghai_now()
        today = now.date().isoformat()
        target = str(schedule.get("time") or "07:30")
        due = now.strftime("%H:%M") >= target
        if not force and str(schedule.get("last_run_date") or "") == today:
            return {"state": "already-ran", "queued": 0, "discovered": 0}
        if not force and not due:
            return {"state": "not-due", "queued": 0, "discovered": 0}

        if not force and schedule.get("catch_up") is False:
            previous_date = str(schedule.get("last_run_date") or "")
            if previous_date and previous_date < today and now.strftime("%H:%M") > target:
                result = {
                    "state": "missed",
                    "queued": 0,
                    "discovered": 0,
                    "deferred": 0,
                    "errors": 0,
                    "pending": len(schedule.get("pending") or []),
                    "timezone": "Asia/Shanghai",
                    "finished_at": time.time(),
                }
                schedule["last_run_date"] = today
                schedule["last_result"] = result
                self.task_store.set_app_state("daily_schedule", schedule)
                return result

        self._load_daily_schedule_account(schedule)
        outputs = set(schedule.get("outputs") or ["subtitle"])
        queued = 0
        discovered = 0
        deferred = 0
        errors = 0

        pending = []
        for item in list(schedule.get("pending") or []):
            course_id = str(item.get("course_id") or "").strip()
            sub_id = str(item.get("sub_id") or "").strip()
            requested = sorted({str(value).strip() for value in item.get("outputs") or [] if str(value).strip()})
            if course_id and sub_id and requested:
                pending.append({"course_id": course_id, "sub_id": sub_id, "outputs": requested})

        def add_pending(course_id: str, sub_id: str, requested: set[str]) -> None:
            if not requested:
                return
            for item in pending:
                if item["course_id"] == course_id and item["sub_id"] == sub_id:
                    item["outputs"] = sorted(set(item["outputs"]) | requested)
                    return
            pending.append({"course_id": course_id, "sub_id": sub_id, "outputs": sorted(requested)})

        def process_pending(course_id: str, sub_id: str, requested: set[str]) -> set[str]:
            nonlocal queued, deferred, errors
            remaining = set(requested)
            try:
                subtitle_ready = bool(self.subtitle_segments(sub_id).get("segments"))
                dependent = remaining.intersection({"ocr", "summary", "chapters", "quiz"})
                if dependent and not subtitle_ready:
                    self.enqueue_subtitle(course_id, sub_id)
                    queued += 1
                    deferred += 1
                    return remaining
                if "subtitle" in remaining:
                    if not subtitle_ready:
                        self.enqueue_subtitle(course_id, sub_id)
                        queued += 1
                    remaining.discard("subtitle")
                if remaining.intersection({"ocr", "summary", "chapters"}):
                    self.enqueue_summary(course_id, sub_id, include_ppt="ocr" in remaining)
                    queued += 1
                    remaining.difference_update({"ocr", "summary", "chapters"})
                if "quiz" in remaining:
                    self.generate_quiz(course_id, sub_id)
                    queued += 1
                    remaining.discard("quiz")
            except Exception:
                errors += 1
                deferred += 1
            return remaining

        retained_pending = []
        for item in pending:
            remaining = process_pending(item["course_id"], item["sub_id"], set(item["outputs"]))
            if remaining:
                retained_pending.append({**item, "outputs": sorted(remaining)})
        pending = retained_pending

        for course_id in [str(item) for item in schedule.get("course_ids") or []]:
            known = {str(item.get("sub_id")) for item in self.catalog_repository.lectures_for_course(course_id)}
            try:
                course = self.discover_course(course_id)
            except Exception:
                errors += 1
                continue
            new_lectures = [
                item for item in course.get("lectures") or []
                if str(item.get("sub_id")) not in known and item.get("has_playback", True)
            ]
            discovered += len(new_lectures)
            for lecture in new_lectures:
                sub_id = str(lecture.get("sub_id") or "")
                if not sub_id:
                    continue
                requested = set(outputs)
                if requested.intersection({"ocr", "summary", "chapters", "quiz"}):
                    requested.add("subtitle")
                remaining = process_pending(course_id, sub_id, requested)
                add_pending(course_id, sub_id, remaining)
        result = {
            "state": "completed" if not errors and not pending else ("partial" if not errors else "failed"),
            "queued": queued,
            "discovered": discovered,
            "deferred": deferred,
            "errors": errors,
            "pending": len(pending),
            "timezone": "Asia/Shanghai",
            "finished_at": time.time(),
        }
        schedule["last_run_date"] = today
        schedule["last_result"] = result
        schedule["pending"] = pending
        self.task_store.set_app_state("daily_schedule", schedule)
        return result

    def _load_authorized_catalog(self, client) -> tuple[list[dict], str, list[str]]:
        """Try bounded catalog routes while preserving the verified login session."""
        from src.api.icourse import SessionUnverifiable
        from src.api.icourse_direct import DirectICourseSession

        deadline = time.monotonic() + CATALOG_REFRESH_DEADLINE_SECONDS
        failures: list[str] = []
        telemetry = LoginStageTelemetry()
        route_candidates: list[tuple[str, str | None]] = [("webvpn", None)]
        route_candidates.extend(
            ("proxy" if proxy_url else "direct", proxy_url)
            for proxy_url in self.network.service_proxies("icourse")
        )
        for route_class, proxy_url in route_candidates:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failures.append("catalog_timeout")
                self._stash_catalog_route_diagnostics({"code": "catalog_timeout"})
                break
            direct = None
            catalog_client = client
            route_started = time.monotonic()
            try:
                if proxy_url is not None:
                    direct = DirectICourseSession(step_callback=telemetry, proxy_url=proxy_url)
                    catalog_client = client.with_catalog_session(direct)
                verified = catalog_client.list_authorized_courses(
                    deadline_seconds=min(CATALOG_ROUTE_ATTEMPT_SECONDS, remaining)
                )
                route_diagnostics = getattr(
                    catalog_client, "last_catalog_diagnostics", {}
                )
                self._stash_catalog_route_diagnostics({
                    "route_class": route_class,
                    "elapsed_ms": int((time.monotonic() - route_started) * 1000),
                    **(route_diagnostics if isinstance(route_diagnostics, dict) else {}),
                })
                # P3.1：目录读验证了该路径——记录为 icourse 的最后验证路由。
                # webvpn 会话候选与独立路径无关，不写健康记忆。
                if proxy_url is not None:
                    self.network.note_route_success("icourse", proxy_url)
                return verified, route_class, failures
            except Exception as exc:
                code = _catalog_error_code(exc)
                if code not in failures:
                    failures.append(code)
                # P0.2：路径维度（直连/代理候选）的传输失败让决策缓存失效；
                # webvpn 会话候选与路径无关。身份/跳转类终止性失败与路径无关，
                # 换路径重试既无意义也不安全。P3.1：冷却窗口内的重复失败被
                # 抑制（只计数），跨请求候选顺序不随交替失败逐请求翻转。
                if (
                    proxy_url is not None
                    and code in {"catalog_timeout", "catalog_route_unavailable"}
                ):
                    flip_armed = self.network.note_route_failure("icourse", proxy_url)
                    if not flip_armed:
                        self._bump_campus_metrics(route_flap_suppressed=1)
                if code in _CATALOG_TERMINAL_CODES:
                    if code == "catalog_identity_mismatch":
                        # P3.1：身份不匹配与路径无关——健康记忆立即失效。
                        try:
                            self.network.clear_route_health()
                        except Exception:
                            pass
                    self._stash_catalog_route_diagnostics({
                        "route_class": route_class,
                        "code": code,
                        "exception_class": type(exc).__name__,
                        "reason": str(getattr(exc, "catalog_reason", "") or ""),
                        "elapsed_ms": int((time.monotonic() - route_started) * 1000),
                        **(getattr(catalog_client, "last_catalog_diagnostics", {})
                           if isinstance(getattr(catalog_client, "last_catalog_diagnostics", {}), dict)
                           else {}),
                    })
                    break
                try:
                    alive = client.check_alive()
                except SessionUnverifiable:
                    alive = None  # F1-R：探活不可判——绝不判会话死亡
                route_diagnostics = getattr(
                    catalog_client, "last_catalog_diagnostics", {}
                )
                self._stash_catalog_route_diagnostics({
                    "route_class": route_class,
                    "code": code,
                    "exception_class": type(exc).__name__,
                    "reason": str(getattr(exc, "catalog_reason", "") or ""),
                    "elapsed_ms": int((time.monotonic() - route_started) * 1000),
                    **(route_diagnostics if isinstance(route_diagnostics, dict) else {}),
                })
                if alive is None:
                    # F1-R：探活不可判（传输失败/网关 5xx）——按路由不可用
                    # 收编并换下一候选；会话与已验证目录缓存原地保留。
                    if "catalog_route_unavailable" not in failures:
                        failures.append("catalog_route_unavailable")
                    continue
                if not alive:
                    raise CatalogRefreshError("catalog_session_expired") from exc
            finally:
                if direct is not None:
                    try:
                        direct.close()
                    except (AttributeError, OSError):
                        pass

        for code in (
            "catalog_additional_verification_required",
            "catalog_identity_mismatch",
            "catalog_bearer_missing",
            "catalog_timeout",
            "catalog_route_unavailable",
            "catalog_payload_invalid",
        ):
            if code in failures:
                raise CatalogRefreshError(code)
        raise CatalogRefreshError("catalog_payload_invalid")

    def discover_authorized_courses(self) -> list[dict]:
        """Atomically refresh the identity-scoped, platform-verified catalog."""
        client = self.client()
        with self._lock:
            expected_scope = self._identity_scope()
            expected_generation = self._catalog_generation
            if not expected_scope or self._client is not client:
                raise CatalogRefreshError("catalog_identity_mismatch")
        self._set_catalog_status(
            "checking",
            "authorized_catalog_refreshing",
        )
        verified, route_class, route_failures = self._load_authorized_catalog(client)
        results: list[dict] = []
        total = len(verified)
        for index, item in enumerate(verified, start=1):
            course_id = str(item.get("course_id") or "").strip()
            if not course_id:
                continue
            detail = {
                "course_id": course_id,
                "title": str(item.get("title") or course_id),
                "teacher": str(item.get("teacher") or ""),
                "lectures": [],
            }
            for lecture in item.get("lectures") or []:
                if lecture.get("has_playback"):
                    detail["lectures"].append(self._public_lecture(course_id, lecture))
            detail.update({
                "term": str(item.get("term") or ""),
                "department": str(item.get("department") or ""),
                "authorization_state": "verified",
            })
            results.append(detail)
        with self._lock:
            if (
                self._catalog_generation != expected_generation
                or self._identity_scope() != expected_scope
                or self._client is not client
            ):
                raise CatalogRefreshError("catalog_identity_mismatch")
            self.catalog_repository.replace_authorized_catalog(results)
            self._store_authorized_catalog(results)
            # P0.1 连接快照的服务级路由证据：目录成功走过的路径即 iCourse 的
            # 真实路由（webvpn 会话或直连/代理直连课目）。
            if route_class == "webvpn":
                self._icourse_route_class = "webvpn"
                self._icourse_route_path = str(self._webvpn_route or "")
            else:
                self._icourse_route_class = "icourse_direct"
                self._icourse_route_path = "local_proxy" if route_class == "proxy" else "direct"
            self._set_catalog_status(
                "ready",
                "authorized_catalog_verified",
                actions=["refresh-catalog"],
                route_class=route_class,
                partial_failures=route_failures,
                diagnostics=self._consume_catalog_route_diagnostics(),
            )
        self._request_search_refresh([
            str(lecture.get("sub_id") or "")
            for course in results
            for lecture in course.get("lectures") or []
        ])
        return results

    def refresh_authorized_catalog_async(self) -> dict:
        """Start one bounded catalog refresh and expose only confirmed state."""
        with self._lock:
            current = self._catalog_refresh_thread
            if current is not None and current.is_alive():
                return self.authentication_snapshot()
            with self._lock:
                has_client = self._client is not None
            if not has_client:
                self._set_login_status(
                    "connecting",
                    "webvpn",
                    "正在连接 WebVPN",
                )
            self._set_catalog_status(
                "checking",
                "authorized_catalog_refreshing",
            )
            refresh_generation = self._catalog_generation

            def refresh() -> None:
                try:
                    self.discover_authorized_courses()
                except Exception as exc:
                    code = _catalog_error_code(exc)
                    with self._lock:
                        if self._catalog_generation != refresh_generation:
                            return
                    self._set_catalog_status(
                        "degraded",
                        code,
                        actions=_catalog_actions(code),
                        diagnostics=self._consume_catalog_route_diagnostics(),
                    )
                    if code == "catalog_session_expired":
                        self._discard_platform_client()
                        self._set_login_status(
                            "error",
                            "icourse",
                            "iCourse 会话已过期，请重新登录",
                            error_code="fudan_session_expired",
                        )

            self._catalog_refresh_thread = threading.Thread(
                target=refresh,
                name="authorized-catalog-refresh",
                daemon=True,
            )
            self._catalog_refresh_thread.start()
        return self.authentication_snapshot()

    def start_daily_schedule_if_due(self) -> None:
        schedule = dict(self.task_store.get_app_state("daily_schedule", {}) or {})
        if not schedule.get("enabled"):
            return

        def run() -> None:
            try:
                self.run_daily_schedule()
            except Exception as exc:
                result = {"state": "failed", "queued": 0, "discovered": 0, "error": _safe_error_message(exc)}
                latest = dict(self.task_store.get_app_state("daily_schedule", {}) or {})
                latest["last_result"] = result
                self.task_store.set_app_state("daily_schedule", latest)

        threading.Thread(target=run, name="courselens-daily-schedule", daemon=True).start()

    def _queue_persisted_task(self, task: dict) -> None:
        payload = dict(task.get("payload") or {})
        if task.get("kind") == "subtitle":
            payload.setdefault("course_id", str(task.get("course_id") or ""))
            payload.setdefault("sub_id", str(task.get("sub_id") or ""))
            payload["task_id"] = task["task_id"]
            if not any(str(existing.get("task_id")) == str(task["task_id"]) for existing in self._subtitle_queue):
                self._subtitle_queue.append(payload)
        elif task.get("kind") == "summary":
            payload.setdefault("course_id", str(task.get("course_id") or ""))
            payload.setdefault("sub_id", str(task.get("sub_id") or ""))
            payload["task_id"] = task["task_id"]
            if not any(str(existing.get("task_id")) == str(task["task_id"]) for existing in self._summary_queue):
                self._summary_queue.append(payload)
        elif task.get("kind") == "question":
            payload.setdefault("course_id", str(task.get("course_id") or ""))
            payload.setdefault("sub_id", str(task.get("sub_id") or ""))
            payload["task_id"] = task["task_id"]
            if not any(str(existing.get("task_id")) == str(task["task_id"]) for existing in self._question_queue):
                self._question_queue.append(payload)
        elif task.get("kind") == "quality_judge":
            payload.setdefault("course_id", str(task.get("course_id") or ""))
            payload.setdefault("sub_id", str(task.get("sub_id") or ""))
            payload["task_id"] = task["task_id"]
            if not any(str(existing.get("task_id")) == str(task["task_id"]) for existing in self._quality_queue):
                self._quality_queue.append(payload)

    def pause(self) -> dict:
        for task_id in tuple(self._progress_trackers):
            self._save_progress_tracker_snapshot(task_id)
        affected = self.task_store.pause_all()
        with self._lock:
            self._paused = True
            # 第廿三案（用户拍板）：暂停=立即停止云端机器。既有取消通道会取
            # 消在飞 run 并保留已完成检查点；恢复时按检查点全新派发（U1），
            # 不再把在飞 run 留在云端占算力。U5：按任务事件全体叫停。
            for event in self._subtitle_cancel_events.values():
                event.set()
            self._summary_cancel.set()
            self._question_cancel.set()
        return {"affected": affected, "pausing": sum(1 for task_id in affected if (self.task_store.get_task(task_id) or {}).get("state") == "pausing")}

    def resume(self) -> dict:
        affected = self.task_store.resume_all()
        recorded_to_resume: list[dict] = []
        with self._lock:
            self._paused = False
            summary_resuming = self.task_store.list_tasks(kinds=("summary",), states=("pausing",), limit=1)
            question_resuming = self.task_store.list_tasks(kinds=("question",), states=("pausing",), limit=1)
            if not summary_resuming:
                self._summary_cancel.clear()
            if not question_resuming:
                self._question_cancel.clear()
            # U5：仍在暂停流程里的字幕任务保留取消旗标，其余按任务放行。
            still_pausing = {
                str(item.get("task_id") or "")
                for item in self.task_store.list_tasks(kinds=("subtitle",), states=("pausing",), limit=50)
            }
            for event_task_id, event in list(self._subtitle_cancel_events.items()):
                if event_task_id not in still_pausing:
                    event.clear()
            for task in self.task_store.list_tasks(states=("queued",)):
                if task.get("kind") in RECORDED_LOCAL_TASK_KINDS:
                    recorded_to_resume.append(task)
                else:
                    self._queue_persisted_task(task)
            self._ensure_subtitle_worker()
            self._ensure_summary_worker()
            self._ensure_question_worker()
            self._ensure_quality_judge_worker()
        for task in recorded_to_resume:
            self._retry_recorded_operation(task)
        resuming = sum(
            1
            for task in self.task_store.list_tasks(states=("pausing",))
            if task.get("resume_requested")
        )
        return {"affected": affected, "resuming": resuming}

    def control_task(self, task_id: str, action: str) -> dict:
        task = self.task_store.get_task(task_id)
        if not task:
            raise KeyError("task not found")
        if action == "pause":
            self._save_progress_tracker_snapshot(task_id)
            task = self.task_store.pause_task(task_id)
            # 第廿三案（用户拍板）：暂停=立即停止云端机器（U1：恢复走全新
            # 派发+检查点续跑，不再附着被暂停时留在云端的旧 run）。
            if task and task.get("kind") == "subtitle" and task_id in self._active_subtitle_task_ids:
                pause_event = self._subtitle_cancel_events.get(task_id)
                if pause_event is not None:
                    pause_event.set()
            if (
                task and task.get("kind") == "summary"
                and task_id == self._active_summary_task_id
            ):
                self._summary_cancel.set()
            if (
                task and task.get("kind") == "question"
                and task_id == self._active_question_task_id
            ):
                self._question_cancel.set()
        elif action == "resume":
            if task.get("kind") in RECORDED_LOCAL_TASK_KINDS and task.get("state") in {"paused", "pausing"}:
                if self.task_store.global_paused:
                    self.task_store.set_global_paused(False)
                with self._lock:
                    self._paused = False
                task = self._retry_recorded_operation(task)
                return {
                    "task": self.task_store.get_task(task_id),
                    "global_paused": bool(self.task_store.global_paused),
                    "accepted_action": action,
                }
            clear_global_pause = (
                task.get("state") in ACTIVE_STATES
                and (self._paused or self.task_store.global_paused)
            )
            task = self.task_store.resume_task(
                task_id,
                clear_global_pause=clear_global_pause,
            )
            if clear_global_pause:
                # Global pause makes every queued item durable-paused.  Lifting
                # the gate here resumes only the explicitly selected task;
                # the other task rows remain paused until the user resumes them.
                with self._lock:
                    self._paused = False
            if task and task.get("state") == "queued":
                with self._lock:
                    self._queue_persisted_task(task)
                    if task.get("kind") == "subtitle":
                        self._ensure_subtitle_worker()
                    elif task.get("kind") == "summary":
                        self._summary_cancel.clear()
                        self._ensure_summary_worker()
                    elif task.get("kind") == "question":
                        self._question_cancel.clear()
                        self._ensure_question_worker()
                    elif task.get("kind") == "quality_judge":
                        # P11：质检无取消事件可清（不进 pause 全家桶），只补 worker。
                        self._ensure_quality_judge_worker()
        elif action == "retry":
            if task.get("state") != "failed":
                raise ValueError("task_retry_requires_failed_state")
            if task.get("kind") in {
                "search_answer", "quiz", "review_plan", "document_alignment",
                "timeline_classification", "concept_analysis",
            }:
                task = self._retry_recorded_operation(task)
            else:
                task = self.task_store.retry_failed_task(task_id)
                if task and task.get("state") == "queued":
                    with self._lock:
                        self._queue_persisted_task(task)
                        if task.get("kind") == "subtitle":
                            self._ensure_subtitle_worker()
                        elif task.get("kind") == "summary":
                            self._summary_cancel.clear()
                            self._ensure_summary_worker()
                        elif task.get("kind") == "question":
                            self._question_cancel.clear()
                            self._ensure_question_worker()
                        elif task.get("kind") == "quality_judge":
                            self._ensure_quality_judge_worker()
        elif action == "cancel":
            if task.get("state") in {"completed", "failed", "canceled"}:
                raise ValueError("task_cancel_requires_active_state")
            payload = {**dict(task.get("payload") or {}), "cancel_requested": True}
            payload.pop(USER_PAUSE_INTENT_KEY, None)
            if task.get("state") in {"queued", "paused"}:
                if task.get("error") == "remote_recovery_material_unavailable":
                    task = self.task_store.cancel_unrecoverable_remote_recovery(task_id)
                    if task is None:
                        raise ValueError("remote_recovery_cancel_not_safe")
                else:
                    self.task_store.update_task(task_id, payload=payload)
                    task = self.task_store.mark_terminal(task_id, "canceled")
            elif task.get("kind") in {"subtitle", "summary", "question"}:
                task = self.task_store.update_task(task_id, state="pausing", payload=payload)
                if task.get("kind") == "subtitle":
                    # U5：按任务取消，只叫停目标字幕任务，不连坐其他在飞任务。
                    cancel_event = self._subtitle_cancel_events.get(task_id)
                    if cancel_event is not None:
                        cancel_event.set()
                elif task.get("kind") == "summary":
                    self._summary_cancel.set()
                elif task.get("kind") == "question":
                    self._question_cancel.set()
            else:
                raise ValueError("task_cancel_not_supported")
        elif action == "import_result":
            # AS2/U2：显式「导入远端结果」——学生点按钮才触发，走 51 的
            # 核真值+原生验签导入通道（判据零放宽）；拒绝给闭集诚实码。
            if task.get("state") not in {"failed", "paused"}:
                raise ValueError("task_action_invalid")
            self.import_remote_task_result(task_id)
        else:
            raise ValueError("action must be pause, resume, retry, or cancel")
        return {
            "task": self.task_store.get_task(task_id),
            "global_paused": bool(self.task_store.global_paused),
            "accepted_action": action,
        }

    def close(self, timeout: float = 6.0) -> dict[str, object]:
        deadline = time.monotonic() + max(0.0, float(timeout))
        phases: dict[str, str] = {}
        if getattr(self, "_automation_stop", None) is not None:
            self._automation_stop.set()
        if getattr(self, "_automation_thread", None) is not None:
            self._automation_thread.join(timeout=max(0.0, deadline - time.monotonic()))
            phases["automation"] = (
                "timeout" if self._automation_thread.is_alive() else "stopped"
            )
        if getattr(self, "remote_connection", None) is not None:
            phases["remote_connection"] = (
                "stopped"
                if self.remote_connection.stop(max(0.0, deadline - time.monotonic()))
                else "timeout"
            )
        if getattr(self, "search_index", None) is not None:
            phases["search_index"] = (
                "stopped"
                if self.search_index.close(max(0.0, deadline - time.monotonic()))
                else "timeout"
            )
        # T1 根因收口：connection-path-probe / auto-connect-resume 守护线程对
        # state.db 的瞬态连接必须先于 store 关停结束，否则与停机/临时目录清理
        # 竞态（WinError 32/145/267）。只收生命周期：置停 + 有界 join，超时放弃
        # 等待（daemon 不强杀）；探测决策、时序与冷却冻结语义零改动。
        # P63-T1：worker-auto-sync 同为 start_auto_connect_resume 派生线程，
        # 收口面对齐（置停 + 有界 join），P59 每进程恰一次/静默语义零改动。
        # N9-H：session-self-heal（SRC-SYNDROME-1 派生）纳入同一收口面——
        # 置停须先于 client 关停，杜绝停机途中的自愈重登外联。
        # WP-3（N9-H 全景卡，P63 家族第三例）：summary/question/pdf 三个本地
        # 生成 worker 同面收口——其完成任务路径写 learning_store，必须先于
        # store 关停有界 join；在途生成项超时放弃（daemon 不强杀），语义与
        # 同族一致。
        probe_stop = getattr(self, "_connection_probe_stop", None)
        resume_stop = getattr(self, "_auto_connect_resume_stop", None)
        sync_stop = getattr(self, "_worker_auto_sync_stop", None)
        heal_stop = getattr(self, "_session_self_heal_stop", None)
        gen_stop = getattr(self, "_generation_workers_stop", None)
        if probe_stop is not None:
            probe_stop.set()
        if resume_stop is not None:
            resume_stop.set()
        if sync_stop is not None:
            sync_stop.set()
        if heal_stop is not None:
            heal_stop.set()
        if gen_stop is not None:
            gen_stop.set()
        with self._lock:
            background = (
                ("connection_probe", getattr(self, "_connection_probe_thread", None)),
                ("auto_connect_resume", getattr(self, "_auto_connect_resume_thread", None)),
                ("worker_auto_sync", getattr(self, "_worker_auto_sync_thread", None)),
                ("session_self_heal", getattr(self, "_session_self_heal_thread", None)),
                ("summary_worker", getattr(self, "_summary_worker", None)),
                ("courseware_pdf_worker", getattr(self, "_courseware_pdf_worker", None)),
                ("question_worker", getattr(self, "_question_worker", None)),
                ("quality_worker", getattr(self, "_quality_worker", None)),
            )
        for label, thread in background:
            if thread is None:
                continue
            thread.join(timeout=min(1.0, max(0.0, deadline - time.monotonic())))
            timed_out = thread.is_alive()
            phases[label] = "timeout" if timed_out else "stopped"
            if timed_out:
                print(
                    f"[FudanCourseLens] {thread.name} did not stop within the close"
                    " budget; abandoning the daemon thread",
                    flush=True,
                )
        with self._lock:
            current_client = self._client
            self._client = None
            self._vpn = None
        if current_client is not None:
            try:
                current_client.close()
            except (AttributeError, OSError):
                pass
        self.pause()
        self.task_store.checkpoint_for_shutdown()
        self.catalog_repository.close()
        self.task_store.close()
        self.learning_store.close()
        phases["stores"] = "closed"
        return {
            "phases": phases,
            "timed_out": any(value == "timeout" for value in phases.values()),
        }


# ---------------------------------------------------------------------------
# Course-data management actions (courselens.course-data-action-result.v1).
#
# Execution semantics for the frozen action closed set; receipt shaping and
# every closed set live in src.runtime.course_data_inventory.  Deletions follow
# the frozen tier table in docs/course-data-management.md: purge-derived
# removes derived/regenerable text rows, remove-copies removes local file
# copies (immediate, no recycle bin, Q1), delete-records removes irreversible
# user records.  Mutating actions are rejected whole-batch, before any change,
# whenever a lifecycle blocker probe fires.


_COURSE_DATA_DERIVED_TABLES = (
    "transcript_sources",
    "transcript_segments",
    "ppt_pages",
    "ai_artifacts",
    "search_documents",
    "smart_timeline_segments",
)
_COURSE_DATA_RECORD_TABLES = (
    "watch_progress",
    "watch_events",
    "bookmarks",
    # STUDY-STATS-M1：播放时长聚合（一行/日/讲，零内容）与互动事件同族=
    # 用户学习记录，随 delete-records per sub_id 一并可擦（设计稿 §4.3 同族 delete）。
    "study_daily_seconds",
)
_COURSE_DATA_MUTATING_ACTIONS = ("purge-derived", "remove-copies", "delete-records")


def _course_data_table_exists(db, table: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return bool(row)


def _course_data_delete_by_subs(db, tables, sub_ids: set[str]) -> dict[str, int]:
    deleted: dict[str, int] = {}
    placeholders = ",".join("?" for _ in sub_ids)
    for table in tables:
        if not _course_data_table_exists(db, table):
            continue
        cursor = db.execute(
            f"DELETE FROM {table} WHERE sub_id IN ({placeholders})", tuple(sorted(sub_ids))
        )
        if cursor.rowcount > 0:
            deleted[table] = int(cursor.rowcount)
    return deleted


def _course_data_targeted_subs(catalog_repository, learning_store, task_store, course_ids, sub_ids) -> set[str]:
    """Expand the action target over the SAME pair universe the inventory shows.

    Course selection must reach every lecture the data page lists for that
    course (catalog ∪ learning ∪ tasks).  Catalog-only expansion stranded
    rolled-over courses: their rows stayed selectable in the UI while their
    documents/courseware became unreachable — DATA-DELETE-REPAIR-1 第十五案.
    """
    known = _course_data_known_pairs(catalog_repository, learning_store, task_store)
    targeted = {str(value) for value in sub_ids}
    for course_id in course_ids:
        targeted.update(
            str(sub_id)
            for known_course, sub_id in known
            if str(known_course) == str(course_id)
        )
    return targeted


def _course_data_purge_derived(learning_store, targeted_subs: set[str]) -> dict[str, int]:
    with closing(connect_learning_db(learning_store.path)) as db:
        deleted = _course_data_delete_by_subs(db, _COURSE_DATA_DERIVED_TABLES, targeted_subs)
        db.commit()
    return deleted


def _course_data_delete_records(learning_store, targeted_subs: set[str]) -> dict[str, int]:
    with closing(connect_learning_db(learning_store.path)) as db:
        deleted = _course_data_delete_by_subs(db, _COURSE_DATA_RECORD_TABLES, targeted_subs)
        if _course_data_table_exists(db, "quiz_items") and _course_data_table_exists(db, "quiz_attempts"):
            placeholders = ",".join("?" for _ in targeted_subs)
            cursor = db.execute(
                f"""DELETE FROM quiz_attempts WHERE quiz_id IN (
                        SELECT quiz_id FROM quiz_items WHERE sub_id IN ({placeholders}))""",
                tuple(sorted(targeted_subs)),
            )
            if cursor.rowcount > 0:
                deleted["quiz_attempts"] = int(cursor.rowcount)
        db.commit()
    return deleted


def _course_data_remove_document_copy(learning_store, document_id: str) -> None:
    """Delete one learning document's rows and file directory (immediate, Q1)."""
    path = learning_store.path
    directory = delete_document(path, document_id).resolve()
    root = (Path(path).parent / "documents").resolve()
    if directory != root and root in directory.parents and directory.is_dir():
        shutil.rmtree(directory)
    # N7R：与 delete_learning_document 同一边界的级联（清空课程数据时逐份删档）。
    # 单独 try 包住，不改本函数对调用方的异常契约。
    try:
        delete_assessment_items(path, document_id=str(document_id))
    except Exception:
        pass


def _course_data_remove_copies(
    inventory,
    learning_store,
    catalog_repository,
    data_root,
    course_ids: tuple[str, ...],
    sub_ids: tuple[str, ...],
    include_orphans: bool,
) -> dict:
    if data_root is None:
        data_root = Path(learning_store.path).parent
    data_root = Path(data_root)
    documents_root = data_root / "documents"
    subtitles_root = data_root / "artifacts" / "subtitles"
    courseware_root = data_root / "courseware"
    documents = list_documents(learning_store.path)
    wanted_course = {str(value) for value in course_ids}
    wanted_sub = {str(value) for value in sub_ids}

    # Pairs and targets must be resolved BEFORE the document rows are deleted:
    # learning aggregates are a source of known pairs, and dropping document
    # rows first collapsed them — the recomputed target set went empty and
    # silently skipped courseware/subtitles for learning-only lectures
    # (DATA-DELETE-REPAIR-1 第二缺陷).
    known = _course_data_known_pairs(catalog_repository, learning_store, inventory.task_store)
    subtitle_owners = {subtitle_artifact_key(course, sub) for course, sub in known}
    courseware_owners = {courseware_lecture_key(course, sub) for course, sub in known}
    targeted = _course_data_targeted_subs(catalog_repository, learning_store, inventory.task_store, course_ids, sub_ids)

    removed: dict[str, int] = {}
    bytes_freed = 0
    removed_documents = 0
    for document in documents:
        document_id = str(document.get("document_id") or "")
        if not document_id:
            continue
        if (
            str(document.get("course_id") or "") in wanted_course
            or str(document.get("sub_id") or "") in wanted_sub
        ):
            try:
                _course_data_remove_document_copy(learning_store, document_id)
            except (KeyError, FileNotFoundError, OSError):
                continue
            removed_documents += 1
    if removed_documents:
        removed["documents"] = removed_documents

    removed_subtitles = removed_courseware = 0
    for course_id, sub_id in sorted(known):
        if sub_id not in targeted:
            continue
        deleted, size = _course_data_rmtree_owned(subtitles_root, subtitle_artifact_key(course_id, sub_id))
        if deleted:
            removed_subtitles += 1
            bytes_freed += size
        deleted, size = _course_data_rmtree_owned(courseware_root, courseware_lecture_key(course_id, sub_id))
        if deleted:
            removed_courseware += 1
            bytes_freed += size
    if removed_subtitles:
        removed["subtitles"] = removed_subtitles
    if removed_courseware:
        removed["courseware"] = removed_courseware

    orphans_removed = 0
    if include_orphans:
        document_ids = {str(document.get("document_id") or "") for document in documents}
        for root, owners in (
            (documents_root, document_ids),
            (subtitles_root, subtitle_owners),
            (courseware_root, courseware_owners),
        ):
            try:
                entries = list(os.scandir(root))
            except OSError:
                continue
            for entry in entries:
                if entry.name in owners:
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                deleted, size = _course_data_rmtree_owned(root, entry.name)
                if deleted:
                    orphans_removed += 1
                    bytes_freed += size
    result: dict = {"removed": removed, "bytes_freed": bytes_freed}
    if orphans_removed:
        result["orphans_removed"] = orphans_removed
    return result


def course_data_perform_action(
    action: str,
    *,
    learning_store,
    catalog_repository,
    task_store,
    data_root,
    operation_id: str,
    course_ids: tuple[str, ...] = (),
    sub_ids: tuple[str, ...] = (),
    confirm: bool = False,
    confirm_typed: str = "",
    include_orphans: bool = False,
    refresh_search_index=None,
) -> dict:
    """Execute one frozen course-data action and return its action-result receipt.

    Lifecycle blockers reject mutating actions whole-batch before any change.
    Rejected receipts are not recorded in the idempotency ledger, so the same
    operation_id may be retried once the blocker clears.
    """
    action = str(action or "").strip().casefold()
    if action not in COURSE_DATA_ACTIONS:
        raise CourseDataActionError("course_data_action_invalid")
    course_ids = tuple(str(value) for value in course_ids if str(value))
    sub_ids = tuple(str(value) for value in sub_ids if str(value))
    inventory = CourseDataInventory(
        learning_store=learning_store,
        catalog_repository=catalog_repository,
        task_store=task_store,
        data_root=data_root,
    )

    if action == "rebuild-search":
        if refresh_search_index is None:
            raise CourseDataActionError("course_data_action_invalid", "search index is unavailable")
        return inventory.action_result(
            action, accepted=True, operation_id=operation_id,
            result={"search_index": dict(refresh_search_index() or {})},
        )

    if action == "export":
        manifest = inventory.summary(page=1, page_size=MAX_SUMMARY_PAGE_SIZE, include_orphans=True)
        total = int((manifest.get("page") or {}).get("total") or 0)
        return inventory.action_result(
            action, accepted=True, operation_id=operation_id,
            result={"manifest": manifest, "truncated": total > len(manifest.get("rows") or [])},
        )

    if action == "export-study-stats":
        # STUDY-STATS-M3 学习统计导出（设计稿 §3.4）：JSON 全量 + CSV×2 落
        # output/study-stats-exports/（24h 过期清剪，迁移包同法），回执带文件
        # 清单供前端 a[download] 直下（GET study-stats/file 闭集文件名路由）。
        # 纯读侧聚合，不需要课程目标，也绝不做任何生命周期变更。
        titles: dict[str, str] = {}
        labels: dict[str, str] = {}
        try:
            for course in catalog_repository.courses():
                titles[str(course.get("course_id") or "")] = str(course.get("title") or "")
            # 讲次标签（date+sub_title 目录元数据）：一次装入授权目录，逐课程展开。
            if titles and len(titles) <= 100:
                for course in catalog_repository.courses_for_ids(set(titles)):
                    for lecture in course.get("lectures") or []:
                        sub_id = str(lecture.get("sub_id") or "")
                        if sub_id:
                            labels[sub_id] = " ".join(
                                part for part in (str(lecture.get("date") or ""), str(lecture.get("sub_title") or ""))
                                if part
                            )
        except Exception:
            titles, labels = {}, {}  # 目录读取失败=名称列留空，统计数字照常导出
        export = build_study_stats_export(
            learning_store.path, course_titles=titles, lecture_labels=labels,
        )
        export_dir = Path(data_root) / STUDY_STATS_EXPORT_DIRNAME
        export_dir.mkdir(parents=True, exist_ok=True)
        _study_stats_prune_exports(export_dir)
        written = []
        for item in export["files"]:
            target = export_dir / item["filename"]
            # CSV 走 utf-8-sig（Excel 直开中文不乱码）；JSON 走裸 UTF-8（严格
            # 解析器零 BOM 容忍）。newline="" 保 CSV 的 CRLF 原样落盘。
            encoding = "utf-8-sig" if item["filename"].endswith(".csv") else "utf-8"
            target.write_text(item["content"], encoding=encoding, newline="")
            written.append({"filename": item["filename"], "bytes": target.stat().st_size})
        return inventory.action_result(
            action, accepted=True, operation_id=operation_id,
            result={"files": written, "directory": STUDY_STATS_EXPORT_DIRNAME},
        )

    if action == "release-stuck":
        # U5/M16 解锁路径：数据页此前对「卡住的生命周期行」只有整批拒绝、没有
        # 解锁动作，长期 paused / remote_state NULL / cleanup_pending 会让一切
        # 变更动作（含重置前的清理）永久卡死。此动作强制关停这些阻塞行；
        # 终态历史一行不动，所以它不删除任何学习记录。
        released = task_store.release_stuck_lifecycle_rows()
        try:
            if refresh_search_index is not None:
                refresh_search_index()
        except Exception:
            pass  # 解锁本身绝不因索引刷新失败而失败
        return inventory.action_result(
            action, accepted=True, operation_id=operation_id,
            result={"released": released},
        )

    targeted_subs = _course_data_targeted_subs(
        catalog_repository, learning_store, task_store, course_ids, sub_ids,
    )
    # Q2 orphan cleanup ("清除全部孤儿") is the one remove-copies variant that
    # needs no course target, but it always requires the typed acknowledgement.
    orphan_cleanup = action == "remove-copies" and include_orphans

    if action in _COURSE_DATA_MUTATING_ACTIONS:
        # Whole-batch rejection: the blocker probe sees every targeted lecture
        # (course-expanded), not just explicitly requested sub_ids.
        blockers = inventory.lifecycle_blockers(sub_ids=targeted_subs, course_ids=course_ids)
        if blockers:
            return inventory.action_result(
                action, accepted=False, operation_id=operation_id, blockers=blockers,
            )

    if not targeted_subs and not orphan_cleanup:
        raise CourseDataActionError(
            "course_data_target_invalid", "select at least one course or lecture",
        )

    if action == "purge-derived":
        if not confirm:
            raise CourseDataActionError("course_data_confirm_required")
        deleted = _course_data_purge_derived(learning_store, targeted_subs)
        return inventory.action_result(
            action, accepted=True, operation_id=operation_id,
            result={"deleted": deleted, "sub_ids": sorted(targeted_subs)},
        )

    if action == "remove-copies":
        if not confirm:
            raise CourseDataActionError("course_data_confirm_required")
        if orphan_cleanup and not confirm_typed.strip():
            raise CourseDataActionError(
                "course_data_confirm_required",
                "orphan cleanup requires the typed acknowledgement",
            )
        result = _course_data_remove_copies(
            inventory, learning_store, catalog_repository, data_root,
            course_ids, sub_ids, include_orphans,
        )
        return inventory.action_result(action, accepted=True, operation_id=operation_id, result=result)

    # delete-records — irreversible user records; Q1 typed course-name receipt,
    # one course per operation so the typed receipt is unambiguous.
    if len(course_ids) != 1:
        raise CourseDataActionError(
            "course_data_confirm_required",
            "delete-records requires exactly one confirmed course",
        )
    course_id = course_ids[0]
    title = ""
    for course in catalog_repository.courses():
        if str(course.get("course_id") or "") == course_id:
            title = str(course.get("title") or "")
            break
    if not title:
        raise CourseDataActionError("course_data_target_invalid", "course is not in the catalog")
    if confirm_typed.strip() != title.strip():
        raise CourseDataActionError("course_data_confirm_required", "typed course name does not match")
    deleted = _course_data_delete_records(learning_store, targeted_subs)
    return inventory.action_result(
        action, accepted=True, operation_id=operation_id,
        result={"deleted": deleted, "course_id": course_id},
    )


# ---------------------------------------------------------------------------
# Client reset (courselens.client-reset-action-result.v1).
#
# Frozen contract (2026-09-16): one destructive action that returns the app to
# fresh-install state.  Blockers reject whole-batch with zero execution; the
# background receipt manifest is written OUTSIDE the data root before any
# destructive step; GitHub repository deletion (the closed two-repo set from
# credentials) runs before any local deletion so a remote failure leaves the
# installation untouched.  Credentials are touched only through existing
# deletion APIs and secret values are never read.  The process shutdown is
# fired by the HTTP adapter after the reply is written.

_CLIENT_RESET_CONFIRM_TEXT = "重置"
# Blocker codes: the course-data closed set plus the remote token lease probe
# (existing "active_token_lease" code from the remote lifecycle checks).
_CLIENT_RESET_BLOCKER_CODES = (
    "active_task", "active_remote_run", "active_automation_import",
    "automation_rule", "cleanup_pending", "active_token_lease",
)
_CLIENT_RESET_RECEIPT_KEEP = 5
# Row groups that survive the base reset unless delete_derived opted in
# (contract §1): transcript caches, AI artifacts, courseware slide caches.
_CLIENT_RESET_DERIVED_TABLES = (
    "transcript_sources", "transcript_segments",
    "ai_artifacts", "ai_artifact_parts", "model_provenance",
    "ppt_pages",
)
_CLIENT_RESET_SINGLE_FLIGHT = threading.Lock()


def _client_reset_receipt(action: str, *, accepted: bool, operation_id: str,
                          blockers=None, result=None) -> dict:
    value: dict = {
        "schema": "courselens.client-reset-action-result.v1",
        "action": action,
        "operation_id": str(operation_id or ""),
        "status": "accepted" if accepted else "rejected",
    }
    normalized = []
    for blocker in blockers or []:
        code = str((blocker or {}).get("code") or "")
        if code not in _CLIENT_RESET_BLOCKER_CODES:
            raise ValueError("unknown client-reset blocker code")
        normalized.append({"code": code, "count": int((blocker or {}).get("count") or 0)})
    if normalized:
        value["blockers"] = normalized
    if result is not None:
        value["result"] = result
    return value


def _client_reset_managed_install_root() -> Path | None:
    """Managed-install root (same validation as the composition root), else None."""
    configured = os.environ.get("COURSELENS_INSTALL_ROOT", "").strip()
    if not configured:
        return None
    try:
        install_root = Path(configured).expanduser().resolve(strict=True)
        layout = json.loads(
            (install_root / "state" / "install-layout.json").read_text(encoding="utf-8-sig")
        )
        if (
            layout.get("schema") == "courselens.managed-install.v1"
            and PROJECT_ROOT.resolve().parent == (install_root / "versions").resolve()
        ):
            return install_root
    except (OSError, ValueError, TypeError):
        pass
    return None


def _client_reset_receipt_directory() -> Path:
    """Receipts live OUTSIDE the data root: managed install root, else reports."""
    install_root = _client_reset_managed_install_root()
    if install_root is not None:
        return install_root / "reset-receipts"
    return PROJECT_ROOT / "runtime" / "reports" / "reset-receipts"


def _client_reset_write_receipt(inventory) -> Path:
    """Pre-reset summary.v1 manifest; keep only the newest receipts on disk."""
    manifest = inventory.summary(page=1, page_size=MAX_SUMMARY_PAGE_SIZE, include_orphans=True)
    directory = _client_reset_receipt_directory()
    directory.mkdir(parents=True, exist_ok=True)
    now = time.time()
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now)) + f".{int(now * 1000) % 1000:03d}Z"
    path = directory / f"reset-{stamp}.json"
    temporary = directory / f".{path.name}.tmp"
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    receipts = sorted(
        directory.glob("reset-*.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for stale in receipts[_CLIENT_RESET_RECEIPT_KEEP:]:
        try:
            stale.unlink()
        except OSError:
            continue
    return path


def _client_reset_blockers(task_store, inventory) -> list[dict]:
    """Global blocker probes: the course-data closed set plus token leases."""
    probes: list[dict] = []
    active_tasks = int(task_store.count(states=("queued", "running", "pausing", "paused")) or 0)
    if active_tasks > 0:
        probes.append({"code": "active_task", "count": active_tasks})
    probes.extend(inventory.lifecycle_blockers())
    rules = task_store.automation_course_rule_counts()
    rule_total = sum(int((value or {}).get("count") or 0) for value in rules.values())
    if rule_total > 0:
        probes.append({"code": "automation_rule", "count": rule_total})
    leases = len(list(task_store.list_remote_token_leases() or []))
    if leases > 0:
        probes.append({"code": "active_token_lease", "count": leases})
    return probes


def _client_reset_release_active_work(task_store) -> dict:
    """Release every locally-cleared blocker source before the rebuild (U5).

    第十四案根治：重置曾被活动任务/自动化规则/租约/cleanup_pending 永久拒绝，
    学生的「重置」永远到不了 DROP。现在这些阻塞源在重置内被主动清除——任务
    逐个标记 canceled、规则清空、租约撤销。这些行本来也会随重建被 DROP；
    此步的真正目的是让内存中的持有者（监督线程/派发循环）在 DROP 之前停下，
    不再把旧行回写进重建后的空库。任何一步失败都不阻塞重置。
    """
    cleared: dict = {}
    canceled = 0
    try:
        for task in task_store.list_tasks(states=("queued", "running", "pausing", "paused")):
            try:
                if task_store.update_task(
                    str(task.get("task_id") or ""), state="canceled",
                    resume_requested=False, error="client_reset",
                ):
                    canceled += 1
            except Exception:
                continue
    except Exception:
        pass
    cleared["canceled_tasks"] = canceled
    try:
        profiles = list(task_store.automation_profile_ids())
        for profile_id in profiles:
            try:
                task_store.replace_automation_rules([], profile_id=str(profile_id))
            except Exception:
                continue
        cleared["automation_profiles_cleared"] = len(profiles)
    except Exception:
        pass
    try:
        leases = list(task_store.list_remote_token_leases() or [])
        revoked = 0
        for lease in leases:
            try:
                task_store.delete_remote_token_lease(str(lease.get("task_id") or ""))
                revoked += 1
            except Exception:
                continue
        cleared["token_leases_revoked"] = revoked
    except Exception:
        pass
    return cleared


_CLIENT_RESET_REPO_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")


def _client_reset_delete_github_repos(credentials, github_app) -> tuple[list[str], list[dict]]:
    """DELETE exactly the two credential-recorded repos; never enumerate.

    Returns ``(deleted, manual)``.  A permission-class refusal is the
    architectural case where the App token has no repository-deletion
    capability at any scope: that repository degrades to ``manual`` with a
    trusted settings URL and the local reset proceeds, instead of the old
    whole-reset abort that left the student stuck.  Any other GitHub failure
    (unreachable / rate limited / timeout) still raises so the caller aborts
    before any local change.
    """
    from src.remote.github_app import GitHubAppError

    deleted: list[str] = []
    manual: list[dict] = []
    token = github_app.access_token(minimum_lifetime_seconds=900)
    for secret_name in ("github_worker_repo", "github_mailbox_repo"):
        if not credentials.has_secret(secret_name):
            continue
        repo = str(credentials.load_secret(secret_name)).strip()
        if not repo:
            continue
        try:
            github_app._api("DELETE", f"/repos/{repo}", token=token, expected=(204, 404))
        except GitHubAppError as exc:
            if exc.code != "permission_denied":
                raise
            manual.append({
                "repo": repo,
                "settings_url": (
                    f"https://github.com/{repo}/settings"
                    if _CLIENT_RESET_REPO_FULL_NAME_RE.fullmatch(repo) else ""
                ),
                "reason": "app_token_cannot_delete",
            })
            continue
        deleted.append(repo)
    return deleted, manual


def _client_reset_clear_credentials(credentials, delete_deepseek_key=None) -> dict:
    """Credentials via existing deletion APIs only; values are never read."""
    deleted = {"accounts": 0}
    for account in credentials.list_accounts():
        student_id = str((account or {}).get("student_id") or "")
        if student_id and credentials.delete_account(student_id):
            deleted["accounts"] += 1
    deleted["session_checkpoints"] = int(credentials.clear_all_session_checkpoints() or 0)
    deepseek = delete_deepseek_key or credentials.delete_deepseek_key
    deleted["deepseek_key"] = bool(deepseek())
    github_secrets = 0
    for name in credentials.list_secret_names(prefix="github_"):
        if credentials.delete_secret(name):
            github_secrets += 1
    # Worker 公钥名属断开闭集（github_app.disconnect 同族）但无 github_
    # 前缀，必须显式补删：残留旧公钥会让下一轮初始化的密钥建立判断失真。
    for name in ("worker_box_public_key", "worker_signing_public_key"):
        if credentials.delete_secret(name):
            github_secrets += 1
    deleted["github_secrets"] = github_secrets
    return deleted


def _client_reset_export_rows(db, tables) -> dict[str, list[dict]]:
    exported: dict[str, list[dict]] = {}
    for table in tables:
        if not _course_data_table_exists(db, table):
            continue
        cursor = db.execute(f"SELECT * FROM {table}")
        columns = [item[0] for item in cursor.description or []]
        exported[table] = [dict(zip(columns, row)) for row in cursor.fetchall()]
    return exported


def _client_reset_reinsert_rows(db, exported: dict[str, list[dict]]) -> None:
    for table, rows in exported.items():
        if not rows:
            continue
        columns = list(rows[0])
        placeholders = ",".join("?" for _ in columns)
        quoted = ",".join(f'"{name}"' for name in columns)
        db.executemany(
            f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',
            [tuple(row.get(name) for name in columns) for row in rows],
        )


def _client_reset_drop_all_objects(db) -> None:
    """Drop every table/view/trigger so the ensure paths rebuild fresh."""
    objects = db.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for kind, name in objects:
        kind = str(kind or "").upper()
        if kind not in {"TABLE", "VIEW", "TRIGGER"}:
            continue
        db.execute(f'DROP {kind} IF EXISTS "{name}"')


def _client_reset_remove_tree(root: Path) -> tuple[int, int]:
    """Delete one namespace root; returns (entries, bytes) it had contained."""
    entries = bytes_freed = 0
    try:
        if not root.is_dir() or root.is_symlink():
            return 0, 0
        for child in root.rglob("*"):
            try:
                if child.is_file():
                    entries += 1
                    bytes_freed += int(child.stat().st_size)
            except OSError:
                continue
        shutil.rmtree(root, ignore_errors=True)
    except OSError:
        return entries, bytes_freed
    return entries, bytes_freed


def _client_reset_clear_update_scratch(data_root: Path) -> int:
    """Clear the updates scratch tree; the OS-held update.lock may survive."""
    entries, _ = _client_reset_remove_tree(data_root / "updates")
    return entries


def _client_reset_clear_managed_pending_json() -> bool:
    """Managed installs keep one pending.json under <install_root>/state/."""
    install_root = _client_reset_managed_install_root()
    if install_root is None:
        return False
    try:
        pending = install_root / "state" / "pending.json"
        if pending.is_file():
            pending.unlink()
            return True
    except OSError:
        pass
    return False


def _client_reset_rebuild_databases(learning_store, catalog_repository, task_store,
                                    search_index, *, delete_derived: bool) -> dict:
    """Drop both SQLite databases in place and re-run the ensure paths.

    In-place rebuild (instead of deleting the files) keeps every store object
    wired into the frozen service container valid: stores are
    connection-per-operation, so the recreated schema is picked up by the next
    query.  Preserved derived rows are exported before the drop and reinserted
    afterwards; artifact files stay on disk in that case.
    """
    result: dict = {"databases_rebuilt": ["state.db", "learning.db"]}
    preserved_rows: dict[str, int] = {}
    cleared_rows: dict[str, int] = {}
    with closing(connect_learning_db(learning_store.path)) as db:
        if not delete_derived:
            exported = _client_reset_export_rows(db, _CLIENT_RESET_DERIVED_TABLES)
            preserved_rows = {table: len(rows) for table, rows in exported.items()}
        else:
            for table in _CLIENT_RESET_DERIVED_TABLES:
                if _course_data_table_exists(db, table):
                    cleared_rows[table] = int(
                        db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    )
            exported = {}
        _client_reset_drop_all_objects(db)
        db.commit()
        learning_store.search_fts_enabled = initialize_learning_schema(db)
        db.commit()
        _client_reset_reinsert_rows(db, exported)
        db.commit()
    for path, ensure in (
        (learning_store.path, ensure_assessment_schema),
        (learning_store.path, ensure_student_feature_schema),
        (learning_store.path, ensure_document_schema),
        (learning_store.path, ensure_smart_playback_schema),
        (learning_store.path, ensure_concept_schema),
        (learning_store.path, ensure_analytics_schema),
        (learning_store.path, ensure_study_stats_schema),
    ):
        ensure(path)
    # state.db is shared by the catalog repository and the task store: drop
    # its objects once, then re-run both existing initialization paths.
    with closing(catalog_repository._connect()) as db:
        _client_reset_drop_all_objects(db)
        db.commit()
    catalog_repository._initialize()
    TaskStore(task_store.path)
    try:
        search_index.request_refresh(force=True)
    except Exception:
        pass  # Reindexing the fresh (empty) store must never fail the reset.
    if preserved_rows:
        result["preserved_rows"] = preserved_rows
    if cleared_rows:
        result["cleared_rows"] = cleared_rows
    return result


def client_reset_perform_action(
    *,
    learning_store,
    catalog_repository,
    task_store,
    credentials,
    github_app,
    data_root,
    search_index,
    delete_deepseek_key=None,
    operation_id: str,
    confirm_typed: str = "",
    delete_derived: bool = False,
    delete_github_repos: bool = False,
) -> dict:
    """Execute the client reset and return its action-result receipt.

    Rejected receipts (blockers) are not recorded in the idempotency ledger,
    so the same operation_id may be retried once the blockers clear.
    """
    operation_id = str(operation_id or "").strip()
    if str(confirm_typed or "") != _CLIENT_RESET_CONFIRM_TEXT:
        raise ClientResetActionError(
            "reset_confirm_required", "typed confirmation must be exactly 重置",
        )
    if not _CLIENT_RESET_SINGLE_FLIGHT.acquire(blocking=False):
        raise ClientResetActionError("reset_blocked", "another reset is already running")
    try:
        data_root = Path(data_root)
        inventory = CourseDataInventory(
            learning_store=learning_store,
            catalog_repository=catalog_repository,
            task_store=task_store,
            data_root=data_root,
        )
        # U5（第十四案根治）：重置先释放全部本地可清除的活动工作——活动任务、
        # 自动化规则、令牌租约（cleanup_pending 与导入阻塞随任务取消/DROP 消失）。
        # 只有「远端运行在途」这一外部事实仍拒绝：GitHub 侧运行稍后完成的回写
        # 不该落进重建后的空库。其余阻塞不再构成永久死锁，DROP 必达。
        released = _client_reset_release_active_work(task_store)
        blockers = [
            probe for probe in _client_reset_blockers(task_store, inventory)
            if probe.get("code") == "active_remote_run"
        ]
        if blockers:
            return _client_reset_receipt(
                "reset", accepted=False, operation_id=operation_id, blockers=blockers,
            )

        # Background receipt first: a pre-reset snapshot written outside the
        # data root (never surfaced as a user download).
        receipt_path = _client_reset_write_receipt(inventory)

        # Remote deletion before any local change: one transient failure aborts
        # whole; a permission-class refusal degrades to manual-deletion records
        # and the local reset proceeds (T3).
        repos_deleted: list[str] = []
        repos_manual: list[dict] = []
        if delete_github_repos:
            repos_deleted, repos_manual = _client_reset_delete_github_repos(credentials, github_app)

        # Credentials via existing deletion APIs only.
        credential_receipt = _client_reset_clear_credentials(
            credentials, delete_deepseek_key=delete_deepseek_key,
        )

        # Local namespaces: documents / migration-backups / updates scratch are
        # always cleared; the derived file namespaces only on opt-in.
        deleted: dict = dict(credential_receipt)
        bytes_freed = 0
        documents = _client_reset_remove_tree(data_root / "documents")
        deleted["documents"] = documents[0]
        bytes_freed += documents[1]
        backups = _client_reset_remove_tree(data_root / "migration-backups")
        bytes_freed += backups[1]
        updates_scratch = _client_reset_clear_update_scratch(data_root)
        deleted["updates_scratch"] = updates_scratch
        _client_reset_clear_managed_pending_json()
        if delete_derived:
            subtitles = _client_reset_remove_tree(data_root / "artifacts" / "subtitles")
            courseware = _client_reset_remove_tree(data_root / "courseware")
            deleted["subtitles"] = subtitles[0]
            deleted["courseware"] = courseware[0]
            bytes_freed += subtitles[1] + courseware[1]
        rebuild = _client_reset_rebuild_databases(
            learning_store, catalog_repository, task_store, search_index,
            delete_derived=delete_derived,
        )
        result: dict = {
            "deleted": deleted,
            "bytes_freed": bytes_freed,
            "released": released,
            **rebuild,
        }
        if repos_deleted:
            result["repos_deleted"] = repos_deleted
        if repos_manual:
            result["repos_manual_deletion"] = repos_manual
        if not delete_derived:
            result["preserved_derived"] = True
        result["receipt"] = str(receipt_path.name)
        return _client_reset_receipt(
            "reset", accepted=True, operation_id=operation_id, result=result,
        )
    finally:
        _CLIENT_RESET_SINGLE_FLIGHT.release()


# ---- 数据搬家包（D12 数据主权 P0，2026-10-07）：应用层引擎 ----
# 导出/导入的单飞与错误闭集都在 src.runtime.data_migration；这里只补
# 存放位置、保留策略、下载令牌与回执形态。密码永不进台账/回执/日志。

_DATA_MIGRATION_EXPORT_DIRNAME = "migration-exports"
_DATA_MIGRATION_IMPORT_STAGE_DIRNAME = "migration-import-staging"
_DATA_MIGRATION_DOWNLOAD_TTL = 3600.0
_DATA_MIGRATION_RETENTION_SECONDS = 24 * 3600.0
_DATA_MIGRATION_STREAM_CHUNK = 1024 * 1024
_DATA_MIGRATION_MAX_PACKAGE_BYTES = 8 * 1024 * 1024 * 1024
# 与 client-reset 同一闭集（import 的硬拒面只有「远端运行在途」）。
_DATA_MIGRATION_BLOCKER_CODES = _CLIENT_RESET_BLOCKER_CODES


def _data_migration_client_version() -> str:
    try:
        value = json.loads(
            (PROJECT_ROOT / "courselens-version.json").read_text(encoding="utf-8")
        )
    except (OSError, TypeError, ValueError):
        return ""
    if isinstance(value, dict) and value.get("schema") == "courselens.client-version.v1":
        return str(value.get("version") or "")
    return ""


def _data_migration_receipt(action: str, *, accepted: bool, operation_id: str,
                            blockers=None, result=None) -> dict:
    value: dict = {
        "schema": "courselens.data-migration-action-result.v1",
        "action": str(action or ""),
        "operation_id": str(operation_id or ""),
        "status": "accepted" if accepted else "rejected",
    }
    normalized = []
    for blocker in blockers or []:
        code = str((blocker or {}).get("code") or "")
        if code not in _DATA_MIGRATION_BLOCKER_CODES:
            raise ValueError("unknown data-migration blocker code")
        normalized.append({"code": code, "count": int((blocker or {}).get("count") or 0)})
    if normalized:
        value["blockers"] = normalized
    if result is not None:
        value["result"] = result
    return value


def _data_migration_prune_exports(directory: Path) -> None:
    """过期导出包/上传暂存包清剪（>24h）；清理失败静默——绝不影响主流程。"""
    try:
        now = time.time()
        for entry in directory.iterdir():
            try:
                if entry.is_file() and now - entry.stat().st_mtime > _DATA_MIGRATION_RETENTION_SECONDS:
                    entry.unlink()
            except OSError:
                continue
    except OSError:
        pass


def _study_stats_prune_exports(directory: Path) -> None:
    """学习统计导出清剪（STUDY-STATS-M3，>24h 同迁移包法）；失败静默不影响导出。"""
    try:
        now = time.time()
        for entry in directory.iterdir():
            try:
                if entry.is_file() and now - entry.stat().st_mtime > STUDY_STATS_EXPORT_RETENTION_SECONDS:
                    entry.unlink()
            except OSError:
                continue
    except OSError:
        pass
