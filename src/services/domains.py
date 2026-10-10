"""Dependency-injected service boundaries for the local HTTP adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


Operation = Callable[..., Any]


class AutoConnectPreferenceError(ValueError):
    """Rejected auto-connect preference change, carrying a closed-set code.

    Lives at the service-domain boundary so the HTTP adapter can map
    precondition failures without depending on the application layer.
    """

    def __init__(self, code: str):
        super().__init__(code)
        self.code = str(code)


class CourseDataActionError(ValueError):
    """Rejected course-data action carrying a closed-set error code.

    Lives at the service-domain boundary so the HTTP adapter can map
    precondition failures without depending on the application layer.
    """

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = str(code)


class ClientResetActionError(ValueError):
    """Rejected client-reset action carrying a closed-set error code.

    Same boundary rationale as :class:`CourseDataActionError`: the HTTP
    adapter maps the precondition failure without importing the application
    layer.
    """

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = str(code)


class CourseReviewActionError(ValueError):
    """Rejected course-review read/action carrying a closed-set error code.

    Course review reads the local course-knowledge snapshot and can ask for a
    refresh of stale lectures; both need the same closed error vocabulary at
    the HTTP boundary.
    """

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = str(code)


@dataclass(frozen=True, slots=True)
class LifecycleService:
    prime_session_restore: Operation
    start_search_index: Operation
    start_remote_connection: Operation
    recover_remote_runs: Operation
    start_daily_schedule: Operation
    start_auto_connect: Operation
    has_active_work: Operation
    snapshot: Operation
    accepting_requests: Operation
    request_shutdown: Operation
    close: Operation


@dataclass(frozen=True, slots=True)
class AuthCatalogService:
    credentials: Any
    catalog: Any
    task_repository: Any
    app_shell_snapshot: Operation
    authentication_snapshot: Operation
    authorized_catalog_snapshot: Operation
    campus_diagnostics: Operation
    onboarding_snapshot: Operation
    onboarding_guide_snapshot: Operation
    onboarding_guide_action: Operation
    set_credentials: Operation
    use_saved_credentials: Operation
    delete_saved_credentials: Operation
    logout_fudan: Operation
    refresh_authorized_catalog_async: Operation
    identity_scope: Operation


@dataclass(frozen=True, slots=True)
class MediaSessionService:
    catalog: Any
    open_remote_media: Operation
    save_watch_progress: Operation
    watch_progress: Operation
    subtitle_file_path: Operation
    subtitle_segments: Operation
    transcript_segments: Operation


@dataclass(frozen=True, slots=True)
class LiveRoomDomainService:
    status: Operation
    issue_grant: Operation
    consume_grant: Operation
    fetch: Operation
    revoke_all: Operation


@dataclass(frozen=True, slots=True)
class LearningService:
    repository: Any
    search_index: Any
    ai_artifact: Operation
    align_learning_document: Operation
    analyze_cross_course_concepts: Operation
    answer_from_evidence: Operation
    cancel_question_explanation: Operation
    classify_smart_timeline: Operation
    clear_watch_events: Operation
    configure_learning_analytics: Operation
    course_data_action: Operation
    course_flashcard_review: Operation
    course_flashcards: Operation
    course_review: Operation
    course_review_refresh: Operation
    course_term_candidate_action: Operation
    create_question_bookmark: Operation
    create_review_plan: Operation
    cross_course_concepts: Operation
    delete_learning_analytics: Operation
    delete_learning_document: Operation
    delete_study_events: Operation
    explain_assessment_item: Operation
    explain_question_bookmark: Operation
    import_learning_document: Operation
    learning_analytics: Operation
    learning_document: Operation
    learning_document_preview_path: Operation
    list_learning_documents: Operation
    list_question_bookmarks: Operation
    list_quizzes: Operation
    list_review_plans: Operation
    list_watch_events: Operation
    record_study_event: Operation
    record_study_heartbeat: Operation
    record_watch_events: Operation
    refresh_search_index: Operation
    request_quality_judge: Operation
    search_learning: Operation
    set_question_bookmark_resolution: Operation
    delete_question_bookmark: Operation
    smart_timeline: Operation
    study_overview: Operation
    study_detail: Operation
    study_telemetry_summary: Operation
    update_cross_course_concept: Operation
    update_document_alignment: Operation


@dataclass(frozen=True, slots=True)
class TaskService:
    repository: Any
    control_task: Operation
    enqueue_subtitle: Operation
    enqueue_summary: Operation
    generate_quiz: Operation
    configure_daily_schedule: Operation
    run_daily_schedule: Operation
    courseware_pdf_status: Operation
    courseware_pdf_file_path: Operation
    enqueue_courseware_pdf: Operation


@dataclass(frozen=True, slots=True)
class RemoteComputeService:
    coordinator: Any
    lease_repository: Any
    connection_action: Operation
    connection_snapshot: Operation
    runs_snapshot: Operation


@dataclass(frozen=True, slots=True)
class AutomationService:
    repository: Any
    integration: Any
    action: Operation
    snapshot: Operation
    update_config: Operation
    upload_secrets: Operation


@dataclass(frozen=True, slots=True)
class TimetableService:
    runtime: Any
    action: Operation
    ics: Operation
    snapshot: Operation


@dataclass(frozen=True, slots=True)
class SettingsService:
    credentials: Any
    network: Any
    delete_deepseek_key: Operation
    diagnose_network: Operation
    has_deepseek_key: Operation
    privacy_snapshot: Operation
    set_auto_connect_preference: Operation
    set_deepseek_key: Operation
    update_network_settings: Operation
    update_processing_consent: Operation
    set_update_background_checks: Operation
    # Reset danger zone: the whole destructive engine behind one injected
    # operation so the HTTP adapter never imports the application layer.
    client_reset_action: Operation
    # 数据搬家包（D12 P0）：导出/导入引擎同样只经注入面到达，HTTP 适配层
    # 永不直接 import 应用层。导入成功后的 request_shutdown 由适配层开火。
    migration_export_action: Operation
    migration_import_action: Operation
    migration_stage_upload: Operation
    migration_download_file: Operation
    # U⑩：设置页读数面（GET /api/v3/settings 消费）。缺这两条读数时该路由
    # 直接 500——读数与写入同为一等注入面，不允许适配层绕道应用层。
    max_deepseek_tokens_limit: Operation
    ai_usage_month: Operation
    # MEDIA-VPN-1：媒体流系统代理偏好写入（闭集动作，默认关）。
    set_media_stream_proxy: Operation


@dataclass(frozen=True, slots=True)
class ClientUpdateService:
    snapshot: Operation
    action: Operation
    # Fires the deferred request_shutdown("update_restart") after the HTTP 202
    # reply has been flushed (the dying process never races its own response).
    complete_restart: Operation


__all__ = [
    "AuthCatalogService", "AutomationService", "AutoConnectPreferenceError",
    "ClientResetActionError", "CourseReviewActionError",
    "LearningService", "LifecycleService", "ClientUpdateService",
    "LiveRoomDomainService", "MediaSessionService", "RemoteComputeService",
    "SettingsService", "TaskService", "TimetableService",
]
