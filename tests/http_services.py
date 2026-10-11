"""Adapt narrow HTTP test doubles to the production service boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from src.services import CourseLensServices


def _op(source: Any, name: str, fallback: str = "") -> Any:
    value = getattr(source, name, None)
    if value is None and fallback:
        value = getattr(source, fallback, None)
    return value


def http_services(source: Any) -> Any:
    if isinstance(source, CourseLensServices):
        return source

    deepseek = _op(source, "has_deepseek_key")
    if deepseek is None:
        legacy_deepseek = _op(source, "_deepseek_key")
        deepseek = lambda: bool(legacy_deepseek()) if legacy_deepseek else False

    return SimpleNamespace(
        # AS3 后设置路由新增的应用级读/写（max tokens 档位、月度消耗、余额）：
        # 生产 CourseLensServices 容器以顶层字段承载（container.py），合成壳
        # 适配层此前缺这五个透传 → GET /api/v3/settings 恒 500（静默壳缺口，
        # N9-F SHELL-SEED-1 走查期发现并修复）。
        max_deepseek_tokens_limit=_op(source, "max_deepseek_tokens_limit"),
        set_max_deepseek_tokens=_op(source, "set_max_deepseek_tokens"),
        ai_usage_month=_op(source, "ai_usage_month"),
        task_usage_month=_op(source, "task_usage_month"),
        deepseek_balance_snapshot=_op(source, "deepseek_balance_snapshot"),
        auth_catalog=SimpleNamespace(
            credentials=getattr(source, "credentials", None),
            catalog=getattr(source, "catalog_repository", None),
            # TB-ADAPT-M1 死字段清理：task_repository 透传不被 http_api 任何
            # service.* 链引用（AST 反向钉；http_api 全文零出现，任务仓面经
            # tasks.repository 出），删除防白名单腐化。
            app_shell_snapshot=_op(source, "app_shell_snapshot"),
            authentication_snapshot=_op(source, "authentication_snapshot"),
            connection_snapshot=_op(source, "connection_snapshot"),
            campus_diagnostics=_op(source, "campus_diagnostics"),
            authorized_catalog_snapshot=_op(source, "authorized_catalog_snapshot"),
            onboarding_snapshot=_op(source, "onboarding_snapshot"),
            # TB-ADAPT-M1：引导操作两透传（GET /api/v3/onboarding 与
            # POST onboarding/actions 的唯一后端入口）。此前适配层缺这两键，
            # synthetic_shell_server.main() 以事后补挂顶替（车道自建补丁形态）
            # ——现收编进适配层显式字段表，AST 对账钉
            # tests/test_http_service_completeness.py 防复发。
            onboarding_guide_snapshot=_op(source, "onboarding_guide_snapshot"),
            onboarding_guide_action=_op(source, "onboarding_guide_action"),
            set_credentials=_op(source, "set_credentials"),
            use_saved_credentials=_op(source, "use_saved_credentials"),
            delete_saved_credentials=_op(source, "delete_saved_credentials"),
            logout_fudan=_op(source, "logout_fudan"),
            refresh_authorized_catalog_async=_op(source, "refresh_authorized_catalog_async"),
            identity_scope=_op(source, "identity_scope", "_identity_scope") or (lambda: "test"),
        ),
        media_session=SimpleNamespace(
            # TB-ADAPT-M1 死字段清理：catalog 透传不被 http_api 任何 service.* 链
            # 引用（AST 反向钉），删除防白名单腐化。
            open_remote_media=_op(source, "open_remote_media"),
            save_watch_progress=_op(source, "save_watch_progress"),
            watch_progress=_op(source, "watch_progress"),
            subtitle_file_path=_op(source, "subtitle_file_path"),
            subtitle_segments=_op(source, "subtitle_segments"),
            transcript_segments=_op(source, "transcript_segments"),
        ),
        live_room=SimpleNamespace(
            status=_op(source, "live_room_status") or (lambda _course_id: {"state": "unknown"}),
            issue_grant=_op(source, "issue_live_room_grant"),
            consume_grant=_op(source, "consume_live_room_grant"),
            fetch=_op(source, "fetch_live_room_resource"),
            revoke_all=_op(source, "revoke_live_rooms") or (lambda: None),
        ),
        learning=SimpleNamespace(
            repository=getattr(source, "learning_store", None),
            search_index=getattr(source, "search_index", None),
            **{
                name: _op(source, name)
                for name in (
                    "ai_artifact", "align_learning_document", "analyze_cross_course_concepts",
                    "answer_from_evidence", "cancel_question_explanation",
                    "classify_smart_timeline", "clear_watch_events", "configure_learning_analytics",
                    "course_flashcard_review", "course_flashcards",
                    # USEROPS-1：course_review（总体复习概览/讲次明细/题目工作台唯一
                    # 后端入口）此前漏出白名单 → 合成壳里 cr 三路由恒 500，
                    # 总体复习面无法走查（壳缺口，非产品缺陷）。
                    "course_review",
                    # USEROPS-1 白名单对账（http_api 全量 service.learning.* 求差）：
                    # 同族三漏 —— 「更新课程知识」/数据页单课程操作/题目 AI 解答
                    # 在合成壳同样恒 500。
                    "course_review_refresh",
                    "course_data_action",
                    "explain_assessment_item",
                    "course_term_candidate_action",
                    "create_question_bookmark", "create_review_plan", "cross_course_concepts",
                    "delete_learning_analytics", "delete_learning_document", "delete_study_events",
                    "explain_question_bookmark", "import_learning_document", "learning_analytics",
                    "learning_document", "learning_document_preview_path", "list_learning_documents",
                    "list_question_bookmarks", "list_quizzes", "list_review_plans",
                    "list_watch_events", "record_study_event", "record_watch_events", "refresh_search_index",
                    # STUDY-STATS-M1：学习统计 v2 默认层读写面（时长心跳+概览读面）。
                    # STUDY-STATS-M2-b：展开层读面（全课程掌握度+逐讲明细+FSRS 预测）。
                    "record_study_heartbeat", "study_overview", "study_detail",
                    "request_quality_judge",
                    "search_learning", "set_question_bookmark_resolution",
                    "study_telemetry_summary",
                    "delete_question_bookmark",
                    "smart_timeline", "update_cross_course_concept", "update_document_alignment",
                )
            },
        ),
        tasks=SimpleNamespace(
            repository=getattr(source, "task_store", None),
            **{
                name: _op(source, name)
                for name in (
                    "configure_daily_schedule", "control_task", "enqueue_subtitle",
                    "enqueue_summary", "generate_quiz", "run_daily_schedule",
                    "courseware_pdf_status", "courseware_pdf_file_path",
                    "enqueue_courseware_pdf",
                )
            },
        ),
        remote_compute=SimpleNamespace(
            connection_action=_op(source, "remote_connection_action"),
            connection_snapshot=_op(source, "remote_connection_snapshot"),
            runs_snapshot=_op(source, "remote_runs_snapshot"),
        ),
        automation=SimpleNamespace(
            # TB-ADAPT-M1 死字段清理：repository 与 integration.github_provider
            # 透传不被 http_api 任何 service.* 链引用（AST 反向钉），删除防
            # 白名单腐化（用量洞察实际经 automation.action/snapshot 面出）。
            action=_op(source, "automation_action"),
            snapshot=_op(source, "automation_snapshot"),
            update_config=_op(source, "update_automation_config"),
            upload_secrets=_op(source, "upload_automation_secrets"),
        ),
        timetable=SimpleNamespace(
            # TB-ADAPT-M1：runtime 透传（greeting-context 尾句经
            # service.timetable.runtime.store 直读真实 TimetableRuntime；
            # 与产品装配同源——app.py 将 application.timetable 注入门面 runtime
            # 位）。此前缺此键 → 合成壳 greeting-context 恒 AttributeError 500。
            runtime=getattr(source, "timetable", None),
            action=_op(source, "timetable_action"),
            ics=_op(source, "timetable_ics"),
            snapshot=_op(source, "timetable_snapshot"),
        ),
        settings=SimpleNamespace(
            # TB-ADAPT-M1 死字段清理：settings.credentials 透传不被 http_api
            # 任何 service.* 链引用（AST 反向钉），删除防白名单腐化。
            network=getattr(source, "network", None),
            delete_deepseek_key=_op(source, "delete_deepseek_key"),
            diagnose_network=_op(source, "diagnose_network"),
            has_deepseek_key=deepseek,
            privacy_snapshot=_op(source, "settings_privacy_snapshot"),
            set_auto_connect_preference=_op(source, "set_auto_connect_preference"),
            set_deepseek_key=_op(source, "set_deepseek_key"),
            update_network_settings=_op(source, "update_network_settings"),
            update_processing_consent=_op(source, "update_processing_consent"),
            # D-20261009-06：设置写入族透传补齐——合成壳此前 settings 命名空间
            # 缺 set_update_background_checks/set_media_stream_proxy（后台更新
            # 开关/媒体流系统代理写入）→ 对应路由恒 500 假阴性挡测量（真实容器
            # 层已证持久）。同族 client-reset/数据搬家五操作一并补齐（白名单
            # 对账钉 tests/test_service_facade_wiring.py 防复发）。
            set_update_background_checks=_op(source, "set_update_background_checks"),
            set_media_stream_proxy=_op(source, "set_media_stream_proxy"),
            client_reset_action=_op(source, "client_reset_action"),
            migration_export_action=_op(source, "migration_export_action"),
            migration_import_action=_op(source, "migration_import_action"),
            migration_stage_upload=_op(source, "migration_stage_upload"),
            migration_download_file=_op(source, "migration_download_file"),
        ),
        # TB-ADAPT-M1：lifecycle 命名空间补齐——http_api 引用
        # service.lifecycle.snapshot / request_shutdown（POST lifecycle/shutdown、
        # client-reset、migration-import 关停链），并经 getattr 守卫读
        # accepting_requests。此前适配层整命名空间缺失 → 上述路由在合成壳恒
        # AttributeError 500。合成壳语义=关停请求只记账不退出（测量仪器不自杀，
        # 与既有 client_update 合成 stub 同哲学）；兜底值与 http_api 在
        # lifecycle 缺失时的回退字典逐键一致（零行为漂移），_op 允许测试替身
        # 以 lifecycle_* 专名覆写观测调用。
        lifecycle=SimpleNamespace(
            snapshot=_op(source, "lifecycle_snapshot")
            or (lambda: {
                "state": "ready",
                "stage": "serving",
                "error_code": "",
                "accepting_requests": True,
            }),
            accepting_requests=_op(source, "lifecycle_accepting_requests")
            or (lambda: True),
            request_shutdown=_op(source, "lifecycle_request_shutdown")
            or (lambda _reason="": None),
        ),
        client_update=SimpleNamespace(
            snapshot=lambda: {
                "schema": "courselens.client-update-state.v1",
                "state": "available", "error_code": "", "current_version": "0.1.0",
                "channel": "stable", "available_version": "0.2.0",
                "package_size": 48 * 1024 * 1024,
                "release_notes": "已签名的合成更新说明。此内容用于离线视觉验收。",
                "last_checked_at": 0,
                "observed_at": 0,
                "rollback": {"available": True, "version": "0.1.0", "state": "available"},
                "actions": ["check", "download"],
            },
            action=lambda action, confirmed=False: {
                "schema": "courselens.client-update-state.v1",
                "state": "available", "error_code": "", "current_version": "0.1.0",
                "channel": "stable", "available_version": "0.2.0",
                "package_size": 48 * 1024 * 1024,
                "release_notes": "已签名的合成更新说明。此内容用于离线视觉验收。",
                "last_checked_at": 0, "observed_at": 0,
                "rollback": {"available": True, "version": "0.1.0", "state": "available"},
                "actions": ["check", "download"],
            },
            # TB-ADAPT-M1：complete_restart 补 stub——http_api 在 client-update
            # 动作回执写完后调用（编排重启的最后一跳），此前适配层缺此键 →
            # 合成壳该路由恒 AttributeError 500（TESTBENCH-DESIGN-1 实测缺口）。
            # 合成壳不重启（测量仪器不自杀），回执仍经上方 action stub 返回。
            complete_restart=lambda: None,
        ),
    )


__all__ = ["http_services"]
