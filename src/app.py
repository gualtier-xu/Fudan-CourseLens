"""Application composition root for the online CourseLens client."""

from __future__ import annotations

import faulthandler
import http.client
import json
import os
import shutil
import sys
import threading
import time
import webbrowser
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence

from path_utils import DEFAULT_DATA_DIR, PROJECT_ROOT
from src.runtime.http_api import FrontendSessionRegistry, PROJECT_INSTANCE_ID, make_handler
from src.runtime.lifecycle import (
    ERROR_INSTANCE_ACTIVE,
    ERROR_PORT_BUSY,
    ERROR_RECOVERY_REQUIRED,
    ERROR_SERVICE_STOP_TIMEOUT,
    ERROR_STARTUP_FAILED,
    InstanceLock,
    LifecycleController,
    LifecycleError,
    install_signal_handlers,
    restore_signal_handlers,
)
from src.runtime.tray_manager import TrayIcon
from src.runtime.window_shell import (
    MediaRunWatcher,
    NativeShellApi,
    TaskbarProgressPoller,
    WindowClosePolicy,
    confirm_tray_exit_with_active_tasks,
    focus_running_instance_window,
    query_active_tasks,
    read_window_geometry,
    tooltip_for,
    tray_status_line_for,
    virtual_screen_bounds,
)
from src.runtime.window_state import (
    clamp_geometry,
    consume_webview_cache_clear_request,
    load_window_state,
    save_window_state,
    webview_profile_dir,
)
from src.runtime.local_data_recovery import LocalDataRecoveryError
from src.runtime import test_mode as _test_mode
from src.runtime.startup_migration import coordinate_local_data
from src.services import (
    AuthCatalogService,
    AutomationService,
    CourseLensServices,
    LearningService,
    LifecycleService,
    LiveRoomDomainService,
    MediaSessionService,
    RemoteComputeService,
    SettingsService,
    TaskService,
    TimetableService,
    ClientUpdateService,
)


def create_application(data_dir: str | Path | None = None):
    from src.application import CourseLensApplication

    return CourseLensApplication(data_dir or DEFAULT_DATA_DIR)


def _client_install_root(project_root: Path = PROJECT_ROOT) -> Path:
    """Resolve the stable managed-install root without weakening source mode."""
    configured = os.environ.get("COURSELENS_INSTALL_ROOT", "").strip()
    if not configured:
        return project_root.resolve()
    install_root = Path(configured).expanduser().resolve(strict=True)
    layout_path = install_root / "state" / "install-layout.json"
    try:
        layout = json.loads(layout_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("managed_install_layout_invalid") from exc
    active_root = project_root.resolve()
    if (
        layout.get("schema") != "courselens.managed-install.v1"
        or active_root.parent != (install_root / "versions").resolve()
    ):
        raise RuntimeError("managed_install_layout_invalid")
    return install_root


def _client_update_trust_path(
    project_root: Path = PROJECT_ROOT,
    install_root: Path | None = None,
) -> Path:
    """Keep managed trust outside release-key-controlled version slots."""
    active_root = project_root.resolve()
    resolved_install_root = (install_root or _client_install_root(project_root)).resolve()
    if resolved_install_root == active_root:
        return active_root / "config" / "client-update-trust.json"
    versions_root = (resolved_install_root / "versions").resolve(strict=True)
    raw_trust_root = resolved_install_root / "trust"
    if active_root.parent != versions_root or raw_trust_root.is_symlink():
        raise RuntimeError("managed_update_trust_invalid")
    trust_root = raw_trust_root.resolve(strict=True)
    raw_trust_path = trust_root / "client-update-trust.json"
    if trust_root.parent != resolved_install_root or raw_trust_path.is_symlink():
        raise RuntimeError("managed_update_trust_invalid")
    trust_path = raw_trust_path.resolve(strict=True)
    if trust_path.parent != trust_root:
        raise RuntimeError("managed_update_trust_invalid")
    return trust_path


def compose_services(
    data_dir: str | Path | None = None,
    *,
    lifecycle_controller: LifecycleController | None = None,
) -> CourseLensServices:
    """Construct all domain services without a compatibility fallback."""
    application = create_application(data_dir)
    from src.runtime.live_room import LiveRoomService
    live_room = LiveRoomService(
        authorize=application.is_authorized_course,
        identity_scope=application.identity_scope,
        observe=application.observe_live_room,
        open_upstream=application.open_live_room_upstream,
    )
    controller = lifecycle_controller or LifecycleController()
    from src.update import UpdateService
    version = json.loads((PROJECT_ROOT / "courselens-version.json").read_text(encoding="utf-8"))
    install_root = _client_install_root()
    client_update = UpdateService(
        current_version=str(version["version"]),
        trust_path=_client_update_trust_path(install_root=install_root),
        state_root=Path(data_dir or DEFAULT_DATA_DIR) / "updates",
        install_root=install_root,
        has_active_work=application.has_active_work,
        request_shutdown=controller.request_shutdown,
        background_checks_enabled=application.update_background_checks_enabled,
    )
    # Packaged installs only; source checkouts never background-check.
    client_update.start_background_checks()
    global _active_client_update
    _active_client_update = client_update
    return CourseLensServices(
        lifecycle=LifecycleService(
            application.prime_session_restore,
            application.start_search_index,
            application.remote_connection.start,
            application.recover_remote_runs_on_startup,
            application.start_daily_schedule_if_due,
            application.start_auto_connect_resume,
            application.has_active_work,
            controller.snapshot,
            controller.accepting_requests,
            controller.request_shutdown,
            application.close,
        ),
        auth_catalog=AuthCatalogService(
            application.credentials,
            application.catalog_repository,
            application.task_store,
            application.app_shell_snapshot,
            application.authentication_snapshot,
            application.authorized_catalog_snapshot,
            application.campus_diagnostics,
            application.onboarding_snapshot,
            application.onboarding_guide_snapshot,
            application.onboarding_guide_action,
            application.set_credentials,
            application.use_saved_credentials,
            application.delete_saved_credentials,
            application.logout_fudan,
            application.refresh_authorized_catalog_async,
            application.identity_scope,
        ),
        media_session=MediaSessionService(
            application.catalog_repository,
            application.open_remote_media,
            application.save_watch_progress,
            application.watch_progress,
            application.subtitle_file_path,
            application.subtitle_segments,
            application.transcript_segments,
        ),
        live_room=LiveRoomDomainService(
            live_room.status,
            live_room.issue_grant,
            live_room.consume_grant,
            live_room.fetch,
            live_room.revoke_all,
        ),
        learning=LearningService(
            application.learning_store,
            application.search_index,
            application.ai_artifact,
            application.align_learning_document,
            application.analyze_cross_course_concepts,
            application.answer_from_evidence,
            application.cancel_question_explanation,
            application.classify_smart_timeline,
            application.clear_watch_events,
            application.configure_learning_analytics,
            application.course_data_action,
            application.course_flashcard_review,
            application.course_flashcards,
            application.course_review,
            application.course_review_refresh,
            application.course_term_candidate_action,
            application.create_question_bookmark,
            application.create_review_plan,
            application.cross_course_concepts,
            application.delete_learning_analytics,
            application.delete_learning_document,
            application.delete_study_events,
            application.explain_assessment_item,
            application.explain_question_bookmark,
            application.import_learning_document,
            application.learning_analytics,
            application.learning_document,
            application.learning_document_preview_path,
            application.list_learning_documents,
            application.list_question_bookmarks,
            application.list_quizzes,
            application.list_review_plans,
            application.list_watch_events,
            application.record_study_event,
            application.record_study_heartbeat,
            application.record_watch_events,
            application.refresh_search_index,
            application.request_quality_judge,
            application.search_learning,
            application.set_question_bookmark_resolution,
            application.delete_question_bookmark,
            application.smart_timeline,
            application.study_overview,
            application.study_detail,
            application.study_telemetry_summary,
            application.update_cross_course_concept,
            application.update_document_alignment,
        ),
        tasks=TaskService(
            application.task_store,
            application.control_task,
            application.enqueue_subtitle,
            application.enqueue_summary,
            application.generate_quiz,
            application.configure_daily_schedule,
            application.run_daily_schedule,
            application.courseware_pdf_status,
            application.courseware_pdf_file_path,
            application.enqueue_courseware_pdf,
        ),
        remote_compute=RemoteComputeService(
            application.remote_coordinator,
            application.task_store,
            application.remote_connection_action,
            application.remote_connection_snapshot,
            application.remote_runs_snapshot,
        ),
        automation=AutomationService(
            application.task_store,
            application.automation,
            application.automation_action,
            application.automation_snapshot,
            application.update_automation_config,
            application.upload_automation_secrets,
        ),
        timetable=TimetableService(
            application.timetable,
            application.timetable_action,
            application.timetable_ics,
            application.timetable_snapshot,
        ),
        settings=SettingsService(
            application.credentials,
            application.network,
            application.delete_deepseek_key,
            application.diagnose_network,
            application.has_deepseek_key,
            application.settings_privacy_snapshot,
            application.set_auto_connect_preference,
            application.set_deepseek_key,
            application.update_network_settings,
            application.update_processing_consent,
            application.set_update_background_checks,
            application.client_reset_action,
            application.data_migration_export_action,
            application.data_migration_import_action,
            application.data_migration_stage_upload,
            application.data_migration_download_file,
            application.max_deepseek_tokens_limit,
            application.ai_usage_month,
            application.set_media_stream_proxy,
        ),
        max_deepseek_tokens_limit=application.max_deepseek_tokens_limit,
        ai_usage_month=application.ai_usage_month,
        set_max_deepseek_tokens=application.set_max_deepseek_tokens,
        task_usage_month=application.task_usage_month,
        deepseek_balance_snapshot=application.deepseek_balance_snapshot,
        client_update=ClientUpdateService(
            client_update.snapshot,
            client_update.action,
            client_update.complete_restart,
        ),
    )


# Raw update-service handle for the shutdown drain: ``compose_services`` runs
# once per process in production and registers the service it started; the
# ``serve`` teardown path stops its background check timer before exit.  Stop
# is idempotent, so a superseded or already-stopped service is harmless.
_active_client_update = None  # type: UpdateService | None


def _stop_client_update_background_checks() -> None:
    global _active_client_update
    service, _active_client_update = _active_client_update, None
    if service is not None:
        service.stop_background_checks()


# --- AS9（第四十八案）启动两阶段绑定 → P64（第六十四案）开窗时机迁移 ---------
# 端口仍先用启动态 handler 尽早立起（误入的请求被诚实告知「正在启动」，过渡页
# 退化为兜底面），但浏览器不再「绑定即开」：P64 起开窗延后到热替换完成、真
# handler 在位且 ready 之后——学生第一眼就是真界面，不再看「正在启动」过渡页。
# compose 的十几秒初始化体现为「点启动后通常几秒无窗口」。启动页零外部资源 +
# no-store，绝不把「正在启动」误当可用状态缓存。

_STARTUP_PAGE_GRACE_SECONDS = 60.0

_STARTUP_PAGE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="2">
<title>CourseLens 正在启动</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
font-family:"Segoe UI","Microsoft YaHei",sans-serif;background:#f5f7fb;color:#1f2d3d}
main{max-width:32rem;text-align:center;padding:2rem}
h1{font-size:1.25rem;margin:0 0 .75rem}
p{line-height:1.7;margin:.4rem 0;color:#4a5568}
a{color:#2b6cb0}
.dot{display:inline-block;width:.6em;height:.6em;border-radius:50%;background:#2b6cb0;
margin-right:.5em;animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:.25}50%{opacity:1}}
@media (prefers-reduced-motion:reduce){.dot{animation:none}}
</style>
</head>
<body>
<main>
<h1><span class="dot"></span>CourseLens 正在启动…</h1>
<p>正在准备本机数据与界面，通常几秒内完成。</p>
<p>页面会自动进入，你什么都不用做；也可以<a href="/">点这里立即重试</a>。</p>
</main>
</body>
</html>
"""

_STARTUP_ERROR_PAGE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CourseLens 启动没有成功</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
font-family:"Segoe UI","Microsoft YaHei",sans-serif;background:#f5f7fb;color:#1f2d3d}
main{max-width:32rem;text-align:center;padding:2rem}
h1{font-size:1.25rem;margin:0 0 .75rem}
p{line-height:1.7;margin:.4rem 0;color:#4a5568}
</style>
</head>
<body>
<main>
<h1>CourseLens 启动没有成功</h1>
<p>不是你的操作问题。启动窗口/终端里有详细原因，请把它留在原地。</p>
<p>关掉这个窗口后重新打开 CourseLens 再试一次；仍然失败的话，把终端内容反馈给维护者。</p>
</main>
</body>
</html>
"""


def _startup_state_handler(page_html: str, *, status: int) -> type[BaseHTTPRequestHandler]:
    """Build the minimal loopback startup-state handler (zero external refs)."""

    # P56-U3①（第五十六案）：启动期 /api/health 答 JSON 而非启动页 HTML——前端
    # 断连探测解析得出 ok=false 的诚实失败；热替换真 handler 后第一拍即 ok:true，
    # 横幅秒级自撤。此前 HTML 200 让探测的 json() 抛错，后端明明在跑也计失败。
    # 错误态（503 兜底）starting=false，同样诚实。零外部资源 + no-store。
    health_payload = json.dumps(
        {"ok": False, "starting": status == 200, "service": "fudan-courselens"},
    ).encode("utf-8")

    class StartupStateHandler(BaseHTTPRequestHandler):
        # 静默逐请求访问日志：启动期请求是页面自刷新，刷屏没有信息量
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _respond_html(self, include_body: bool) -> None:
            payload = page_html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if include_body:
                self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] == "/api/health":
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(health_payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(health_payload)
                return
            self._respond_html(include_body=True)

        def do_HEAD(self) -> None:  # noqa: N802
            self._respond(include_body=False)

        def do_POST(self) -> None:  # noqa: N802
            # 先排干请求体再应答：带 body 的 POST 若不读就关连接，Windows 会
            # 用 RST 掐断在途响应，客户端读到的就是 10053 而不是这个诚实的
            # 503（满载家族闪红真因，空载 1/15 复现）。启动态处理器只承诺
            # 诚实拒绝，读完即弃；上限 1MB 防异常大 body 拖住启动线程。
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > 0:
                try:
                    self.rfile.read(min(length, 1 << 20))
                except OSError:
                    pass
            self.send_response(503)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

    return StartupStateHandler


def _health_endpoint_ok(url: str) -> bool:
    """本机 /api/health 就绪探测：直连 loopback，绝不经系统代理。

    P64 开窗就绪确认用：只有真 handler 的 ok:true 才算就绪——启动态 handler 的
    200（ok=false, starting=true）与错误态 503 都不算，防止窗口开在过渡/错误页上。
    """
    parts = urlsplit(url)
    try:
        connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=1.0)
    except ValueError:
        return False
    try:
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            return False
        return json.loads(body).get("ok") is True
    except (OSError, ValueError):
        return False
    finally:
        connection.close()


def _open_browser_when_ready(url: str, *, timeout: float = 10.0) -> None:
    """P64：真界面可服务后再开窗，学生第一眼就是真界面。

    timeout>0 时先轮询 /api/health 就绪确认（成功即开，超时兜底也开，绝不因
    探测把窗口吞掉）；timeout=0 表示跳过轮询立即开窗（失败兜底页已就位时用）。
    开窗失败照旧打印手动地址，不静默吞掉。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not _health_endpoint_ok(url):
        time.sleep(0.2)
    if not webbrowser.open(url):
        print(f"[FudanCourseLens] Open the UI manually: {url}", flush=True)


# --- P68（第六十八案）原生应用窗口 -------------------------------------------
# 受管安装的产品形态是「真正应用程序」：点快捷方式弹出属于 CourseLens 自己的
# pywebview（WebView2）原生窗口——自己的标题栏、自己的任务栏图标、关窗即退出
# 应用，不再借用浏览器标签页。窗口是薄壳：只承载既有 frontend URL，零业务逻辑
# 入窗；本地服务架构（端口/health/单实例互斥/信任校验）零变化。浏览器路径保留
# 为 pywebview 不可用或初始化失败时的降级面（留痕，不静默）。

_NATIVE_WINDOW_TITLE = "CourseLens"
_NATIVE_WINDOW_WIDTH = 1280
_NATIVE_WINDOW_HEIGHT = 800
# 夜10-A T10：最小窗尺寸约束——窄于此的布局下限由前端断点保证（CUA 活体验证）。
_NATIVE_WINDOW_MIN_SIZE = (900, 600)


def _purge_webview_profile_if_requested(data_dir: Path) -> None:
    """夜10-A T8：消费「清理界面缓存」标记——下次启动真正清 WebView2 档案。

    运行期档案被窗口占用删不动，所以数据管理页的按钮只落标记（js 桥），
    这里在实例锁拿到之后、窗口创建之前消费它。只删 data_dir/webview 自身
    内容（字面路径），不新增信任面、不触碰任何学习数据。
    """
    if not consume_webview_cache_clear_request(data_dir):
        return
    profile = webview_profile_dir(data_dir)
    if not (profile.is_dir() and profile.parent == Path(data_dir)):
        return
    removed = 0
    try:
        for child in profile.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
            removed += 1
    except OSError:
        pass
    print(
        f"[FudanCourseLens] 已清理界面缓存（{removed} 项）。"
        "学习数据、课程与登录状态不受影响。",
        flush=True,
    )


def _native_webview_storage_path(data_dir: Path) -> Path:
    """WebView2 用户数据目录固定在客户端数据根内（P68）。

    WebView2 的默认用户数据目录跟宿主 exe 走，会落在 launcher 树里并在运行期
    持续写入缓存文件——下一次启动的信任遍历会（正确地）以「untrusted asset」
    拒绝整个应用。数据根是既有客户端数据域，浏览器档案归它管，不新增信任面。
    private_mode=False 保住 P61 主题等 localStorage 的持久性。
    """
    return Path(data_dir) / "webview"


def _native_window_icon_path() -> Path | None:
    """窗口/任务栏图标（P68）：受管运行时在版本槽根，开发头在 installer/。

    受管安装的 SourceRoot 整树拷贝会把 courselens-icon.ico 带进版本槽
    （PROJECT_ROOT）；源码检出里它只住在 installer/（Inno 脚本旁）。两处
    都没有就返回 None（pywebview 退回宿主 exe 图标，仅观感退化）。
    """
    for candidate in (
        PROJECT_ROOT / "courselens-icon.ico",
        PROJECT_ROOT / "installer" / "courselens-icon.ico",
    ):
        if candidate.is_file():
            return candidate
    return None


def _webview2_runtime_available() -> bool:
    """WebView2 运行时在位预检（P68）：按 WebView2 加载器的官方查找顺序。

    pywebview 在 WebView2 出问题时从不抛异常：运行时缺失 → 导入期静默选中
    IE(mshtml) 内核开窗（现代前端必然坏）；环境创建失败 → 异步 on_webview_ready
    只记日志留白窗。两者都比浏览器兜底差，所以建窗前先预检，不过=直接走浏览
    器降级，绝不开一个坏窗口。查找顺序=加载器契约：覆盖目录（须含
    msedewebview2.exe）→ 每用户 Evergreen → 每机器 Evergreen（64 位机查
    WOW6432Node 视图）；pv 为 0/0.0.0.0 视同缺失（官方口径的「已安排删除」态）。
    """
    override = os.environ.get("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", "").strip()
    if override:
        return (Path(override) / "msedewebview2.exe").is_file()
    try:
        import winreg
    except ImportError:
        return True
    candidates = [
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients", 0),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients", 0),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients", 0),
    ]
    for hive, path, view in candidates:
        try:
            with winreg.OpenKey(
                hive, rf"{path}\{{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}}",
                0, winreg.KEY_READ | view,
            ) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
                if version and str(version) not in {"0", "0.0.0.0"}:
                    return True
        except OSError:
            continue
    return False


_NATIVE_WINDOW_LOAD_WATCHDOG_SECONDS = 25.0
_NATIVE_WINDOW_AUMID = "Fudan.FudanCourseLens.Desktop"

# P2-7 长任务终态托盘通知文案（终态单枚，过程零播报；讲人话，不堆系统腔）。
_MEDIA_DONE_NOTIFY = (
    "任务跑完了",
    "字幕和总结都已经就绪，回 CourseLens 点开就能看。",
)
_MEDIA_FAILED_NOTIFY = (
    "有任务没成功",
    "刚才的字幕/总结任务没能完成，回 CourseLens 看一眼原因，重试一次通常就好。",
)

# TRAY-FIX-1 首次收进托盘的一次性气球文案：默认关窗语义变了，第一枚 × 之后
# 告诉学生应用去了哪、怎么回来、怎么真的退出——只发一次（状态落盘）。
_TRAY_FIRST_HIDE_NOTIFY = (
    "CourseLens 还在运行",
    "窗口只是收进了右下角托盘，任务照常在跑；点托盘图标随时回来，右键图标可以彻底退出。",
)


def _tray_tooltip_base() -> str:
    """托盘悬停提示的名字段（TRAY-FIX-1 顺带优化）：带上版本号，学生截图求助
    或对照更新说明时一眼可辨。版本读不到=退回纯名字，绝不因此影响托盘。"""
    try:
        version = json.loads(
            (PROJECT_ROOT / "courselens-version.json").read_text(encoding="utf-8")
        ).get("version")
    except Exception:
        version = None
    return f"CourseLens v{version}" if version else "CourseLens"


def _set_app_user_model_id() -> None:
    """显式进程 AppUserModelID（SYSTRAY-IMPL-1 P1-3）：任务栏分组/缩略图/
    通知归属 CourseLens 本尊而非按 pythonw 镜像推断。必须在首个窗口创建
    前调用；任何失败静默——纯身份增强，绝不阻塞开窗。"""
    try:
        import ctypes

        set_aumid = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        set_aumid.argtypes = [ctypes.c_wchar_p]
        set_aumid(_NATIVE_WINDOW_AUMID)
    except Exception:
        pass


def _window_handle(window) -> int:
    """pywebview 窗口的 Win32 句柄（WinForms Form.Handle）；拿不到返回 0。"""
    form = getattr(window, "native", None)
    if form is None:
        return 0
    try:
        handle = form.Handle
        return int(handle.ToInt64()) if hasattr(handle, "ToInt64") else int(handle)
    except Exception:
        return 0


def _open_native_window(
    url: str,
    *,
    data_dir: Path,
    frontend_sessions,
    fallback_open,
    exit_requested: threading.Event | None = None,
    started_at: float | None = None,
) -> None:
    """ready 之后在主线程创建原生窗口并阻塞至关窗（P68 + 夜10-A APP-SHAPE-2）。

    pywebview 自带 GUI 事件循环且要求主线程调用：主线程从 ``join`` 改为窗口
    循环，serve 循环线程照旧在后台服务——U0 已实测两线程共存。窗口加载即前端
    自身的 frontend-session open/heartbeat；WebView2 销毁页不一定发 pagehide 信
    标，closed 事件补上确定性离场信号。初始化失败降级为浏览器开窗并打印一行
    人话留痕。

    夜10-A 增强（全部失败即退化为 P68 裸窗，绝不阻塞开窗）：
    - T2 窗态还原：几何/最大化从 window-state.json 恢复，多显示器边界钳制；
      真实关窗时把当前几何落盘（更新重启后的自动重开也在同一位置）。
    - T10 最小窗尺寸 900×600。
    - T1 托盘：设置开启后关窗=最小化到托盘（双击恢复/右键菜单打开与退出）。
    - T5 关窗在飞任务一次性确认（工作保护语义不回退——确认后仍走既有收班链）。
    - T4 任务栏进度映射（ITaskbarList3，正常/暂停态色）。
    - T7 更新重启闭合：服务侧关停请求 → 窗口自动关闭 → launcher apply 后以
      --window 重开（几何还原自 T2 落盘）。
    - T11 焦点回归：恢复/拉起后让前端把焦点落回主内容区。
    """
    try:
        import webview
    except Exception as exc:
        print(
            "[FudanCourseLens] 原生窗口在这台电脑上不可用（"
            f"{exc}），已改用默认浏览器打开 CourseLens。",
            flush=True,
        )
        fallback_open()
        return
    if not _webview2_runtime_available():
        # 运行时缺失时 pywebview 会静默开一个 IE 内核窗口（前端必坏），
        # 宁可回浏览器——浏览器兜底永远可用。
        print(
            "[FudanCourseLens] 这台电脑缺少 WebView2 运行时，原生窗口开不出来，"
            "已改用默认浏览器打开 CourseLens。",
            flush=True,
        )
        fallback_open()
        return
    # 显式 AUMID 须在窗口创建前设定（SYSTRAY-IMPL-1 P1-3）；attach 模式经
    # 同一入口自然覆盖。
    _set_app_user_model_id()
    try:
        initial_state = load_window_state(
            data_dir, fallback_size=(_NATIVE_WINDOW_WIDTH, _NATIVE_WINDOW_HEIGHT)
        )
        geometry = dict(initial_state["geometry"])
        create_kwargs: dict = {
            "width": int(geometry.get("width") or _NATIVE_WINDOW_WIDTH),
            "height": int(geometry.get("height") or _NATIVE_WINDOW_HEIGHT),
            "resizable": True,
            "min_size": _NATIVE_WINDOW_MIN_SIZE,
            "maximized": bool(geometry.get("maximized")),
        }
        if "x" in geometry and "y" in geometry:
            bounds = virtual_screen_bounds()
            clamped = (
                clamp_geometry(geometry, bounds) if bounds is not None else geometry
            )
            create_kwargs["x"] = int(clamped["x"])
            create_kwargs["y"] = int(clamped["y"])
        shell_api = NativeShellApi(
            data_dir,
            app_origin=url,
            initial_exit_on_close=initial_state["exit_on_close"],
        )
        create_kwargs["js_api"] = shell_api
        icon_path = _native_window_icon_path()
        window = webview.create_window(_NATIVE_WINDOW_TITLE, url, **create_kwargs)
        loaded = threading.Event()
        window.events.loaded += lambda *args: loaded.set()
        if frontend_sessions is not None:
            window.events.closed += (
                lambda *args: frontend_sessions.close_all_for_native_window()
            )
        # 加载看门狗：WebView2 环境创建失败是异步的（白窗、不抛异常）。页面
        # 迟迟没加载成功就销毁窗口，webview.start 返回后走浏览器兜底；正常
        # 本地加载远快于此超时，误伤方向也是「回浏览器」，永远可用。
        def _load_watchdog() -> None:
            if not loaded.wait(_NATIVE_WINDOW_LOAD_WATCHDOG_SECONDS):
                try:
                    window.destroy()
                except Exception:
                    pass

        threading.Thread(
            target=_load_watchdog, name="courselens-window-watchdog", daemon=True
        ).start()

        # --- 夜10-A：托盘 / 任务栏进度 / 关窗决策 / 退出 watcher --------------
        tray = TrayIcon()

        def _focus_main_content() -> None:
            # T11：前端监听该事件把焦点落回主内容区（<main id=workspace-main>）。
            try:
                window.evaluate_js(
                    "window.dispatchEvent(new Event('courselens:native-window-focused'));"
                )
            except Exception:
                pass

        def _tray_open() -> None:
            try:
                window.show()
                window.restore()
            except Exception:
                pass
            _focus_main_content()

        def _tray_exit() -> None:
            # TRAY-FIX-1：托盘「退出」=唯一的显式退出入口，在飞任务在场时先
            # 问一次（与关窗确认同族）；学生拒绝就原样留在托盘里。
            snapshot = query_active_tasks(url)
            if not snapshot.unknown and snapshot.active > 0:
                if not confirm_tray_exit_with_active_tasks(snapshot.active):
                    return
            if exit_requested is not None:
                exit_requested.set()  # 让 closing 决策放行，不再弹托盘/确认分支
            try:
                window.destroy()
            except Exception:
                pass

        def _ensure_tray() -> bool:
            if not tray.running:
                tray.start(
                    str(icon_path) if icon_path is not None else None,
                    on_open=_tray_open,
                    on_exit=_tray_exit,
                    status_provider=_tray_status_line,
                )
            return tray.running

        def _tray_status_line() -> str:
            # P1-4：托盘右键菜单状态行。查询在菜单打开瞬间同步做（闭集内建
            # 1s 超时）；失败/未知=空串，状态行整行不显示。
            return tray_status_line_for(query_active_tasks(url))

        def _hide_to_tray() -> bool:
            # TRAY-FIX-1：关窗默认=收进托盘。托盘起不来就不隐藏：学生绝不能
            # 因为一个图标丢了窗口入口。
            if not _ensure_tray():
                return False
            try:
                window.hide()
            except Exception:
                return False
            _tray_first_hide_hint()
            return True

        # 首次收进托盘的一次性气球提示（TRAY-FIX-1）：默认关窗语义变了，第
        # 一次 × 之后让学生知道「应用去了哪、怎么回来」。只发一次（状态落
        # 盘），失败静默——提示缺席绝不影响隐藏本身。
        tray_hint_pending = not bool(initial_state.get("tray_hint_shown"))

        def _tray_first_hide_hint() -> None:
            nonlocal tray_hint_pending
            if not tray_hint_pending:
                return
            tray_hint_pending = False
            tray.notify(*_TRAY_FIRST_HIDE_NOTIFY)
            save_window_state(data_dir, tray_hint_shown=True)

        def _save_geometry() -> None:
            geometry_now = read_window_geometry(window)
            if geometry_now is not None:
                save_window_state(data_dir, geometry=geometry_now)

        # 句柄 getter 必须零参（NativeShellApi._hwnd 无参调用）：传函数本身
        # 会因缺参 TypeError 被吞掉、句柄恒 0，标题栏主题静默失效（夜10 真机
        # 活体抓到后改为闭包捕获 window）。
        # TRAY-FIX-1：偏好回调已随「关闭时退出」语义撤销——托盘 shown 即挂恒在
        # （P1-5），开关只落盘，不再触碰托盘生命周期。
        shell_api._bind(lambda: _window_handle(window))
        close_policy = WindowClosePolicy(
            exit_on_close=lambda: shell_api.exit_on_close,
            is_loaded=loaded.is_set,
            is_exit_requested=(exit_requested or threading.Event()).is_set,
            active_tasks=lambda: query_active_tasks(url),
            on_hide_to_tray=_hide_to_tray,
            on_save_geometry=_save_geometry,
        )
        window.events.closing += close_policy.on_closing
        # 夜10-A #9：任务栏轮询的每一拍同步喂托盘 tooltip（被动呈现，悬停才可见）。
        last_tooltip = {"text": ""}
        # P2-7：媒体长任务终态下降沿观测（观测器自己管基线与一次性）。
        media_watcher = MediaRunWatcher()

        def _on_task_snapshot(snapshot) -> None:
            text = tooltip_for(snapshot, base=_tray_tooltip_base())
            if text != last_tooltip["text"]:
                last_tooltip["text"] = text
                tray.set_tooltip(text)
            # 长任务终态单枚通知：媒体 running >0→0 下降沿触发；完成/失败两
            # 文案。托盘未挂（图标没挂上）=notify 返回 False 静默跳过。
            event = media_watcher.observe(snapshot)
            if event == "completed":
                tray.notify(*_MEDIA_DONE_NOTIFY)
            elif event == "failed":
                tray.notify(*_MEDIA_FAILED_NOTIFY, kind="error")

        taskbar = TaskbarProgressPoller(url, on_snapshot=_on_task_snapshot)

        def _on_user_geometry_settled(*_args) -> None:
            # 夜10-A #8：用户拖动/缩放结束（WinForms ResizeEnd 同时覆盖移动
            # 与缩放两种手势）即落盘——进程被杀（更新断电等）前几何不丢。
            _save_geometry()

        def _on_shown() -> None:
            if started_at is not None:
                print(
                    f"[FudanCourseLens] 窗口已就绪（启动后 "
                    f"{time.monotonic() - started_at:.1f} 秒）。",
                    flush=True,
                )
            _ensure_tray()  # P1-5：托盘常驻与偏好解耦——shown 即挂，不等偏好
            handle = _window_handle(window)
            if handle:
                taskbar.start(handle)
            # ResizeEnd 订阅放 shown 后（native Form 此时才存在）；失败=退化为
            # 仅关窗时落盘（既有语义），绝不影响开窗。
            try:
                form = getattr(window, "native", None)
                if form is not None:
                    form.ResizeEnd += _on_user_geometry_settled
            except Exception:
                pass

        window.events.shown += lambda *args: _on_shown()
        if exit_requested is not None:
            # T7：服务侧关停（更新重启/会话收班）→ 窗口自动退场，进程得以
            # 退出交还 launcher；launcher 对 verified pending switch 自动
            # apply 后以 --window 重开，几何由 T2 落盘还原。
            def _watch_shutdown() -> None:
                exit_requested.wait()
                try:
                    window.destroy()
                except Exception:
                    pass

            threading.Thread(
                target=_watch_shutdown, name="courselens-window-exit", daemon=True
            ).start()
        try:
            webview.start(
                icon=str(icon_path) if icon_path is not None else None,
                private_mode=False,
                storage_path=str(_native_webview_storage_path(data_dir)),
            )
        finally:
            # 夜10-A #12：任何退出路径（看门狗销毁/初始化异常/正常关窗）都要
            # 收口轮询线程与托盘——异常路径曾会漏停，托盘图标可能滞留到悬停。
            taskbar.stop()
            tray.stop()
    except Exception as exc:
        print(
            "[FudanCourseLens] 原生窗口启动失败（"
            f"{exc}），已改用默认浏览器打开 CourseLens。",
            flush=True,
        )
        fallback_open()
        return
    if not loaded.is_set():
        print(
            "[FudanCourseLens] 原生窗口的页面迟迟没有加载成功（WebView2 初始化失败），"
            "已改用默认浏览器打开 CourseLens。",
            flush=True,
        )
        fallback_open()


def _serve_startup_error_until_grace_ends(
    server: ThreadingHTTPServer,
    exc: Exception,
    *,
    url: str = "",
    open_browser: bool = True,
) -> None:
    """compose 失败的兜底：启动页换成人话错误页，补开窗展示，留满宽限期再走既有失败退出。

    终端照常打印真因（launcher tee 进日志）。P64 起浏览器在绑定后不再即开，
    失败路径必须在此补开一次（timeout=0 跳过就绪轮询——错误页本身已在服务），
    学生不致对着无响应的黑窗茫然。宽限窗内浏览器 2s 自刷新会拿到错误页，
    绝不留白页。Ctrl+C 可立即打断等待。
    """
    print(f"[FudanCourseLens] Startup failed: {exc}", flush=True)
    server.RequestHandlerClass = _startup_state_handler(
        _STARTUP_ERROR_PAGE_HTML, status=503
    )
    if open_browser and url:
        _open_browser_when_ready(url, timeout=0.0)
    time.sleep(_STARTUP_PAGE_GRACE_SECONDS)


class _TracedLifecycleController(LifecycleController):
    """关停留痕（BACKEND-DEATH-1②）：任意原因的首次关停请求打印一行到 stdout。

    launcher 把子进程 stdout/stderr tee 进 runtime/logs/server/，此后任何退出
    （前端会话关停/键盘中断/服务返回/API 请求/信号）都有留痕。重复请求不打印。
    """

    def request_shutdown(self, reason: str) -> bool:
        accepted = super().request_shutdown(reason)
        if accepted:
            print(f"CourseLens service shutting down: {self._shutdown_reason}", flush=True)
        return accepted


SERVER_LOG_DIR = PROJECT_ROOT / "runtime" / "logs" / "server"
SERVER_LOG_KEEP_DAYS = 14


def prune_server_logs(
    log_dir: Path = SERVER_LOG_DIR,
    *,
    keep_days: int = SERVER_LOG_KEEP_DAYS,
    now: float | None = None,
) -> int:
    """C4（PB-1）：启动期会话日志裁剪。

    launcher 每会话向 runtime/logs/server/ 落 server-*.log / server-python-*.log
    （transcript + tee），从不清理、无限累积；按文件 mtime 只保留最近
    ``keep_days`` 天（恰在边界的保留），更旧删除并返回删除计数供启动日志
    留痕。目录缺失/不可读或单件删除失败一律跳过：裁剪绝不阻塞启动，
    也绝不触碰目录外任何路径。
    """
    directory = Path(log_dir)
    if not directory.is_dir():
        return 0
    cutoff = (time.time() if now is None else float(now)) - keep_days * 86400
    removed = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if not entry.is_file() or not entry.name.endswith(".log"):
                continue
            if entry.stat().st_mtime >= cutoff:
                continue
            entry.unlink()
            removed += 1
        except OSError:
            continue
    return removed


# ---- BACKEND-DEATH-1④ 死亡可归因（直启 serve 的尸检日志） -------------------
# launcher 托管形态由 ② 的 tee 覆盖（stdout/stderr 进 runtime/logs/server/）；
# 「裸 python -m src serve」形态的输出去向是宿主任意物——2026-10-10 F2/F6 两度
# 「后端无声死亡」（product-onboardreal1-result-20261010 定谳=宿主清理者
# TerminateProcess）零 traceback、零 WER、零关停留痕，尸检无从下手。本钉把
# 三类死亡变成可归因：优雅退出→[exit] 行；原生崩溃→faulthandler 栈进同一
# 文件；硬终止→[heartbeat] 截断（最后心跳时刻=死亡时间下界，配合事件日志
# 缺席即可定性外部击杀）。日志落数据目录 logs/server/，随实例沙盒走、14 天
# 自清（复用 prune_server_logs）。任何安装失败只降级不拦启动。

_DIAGNOSTIC_HEARTBEAT_SECONDS = 300.0


class _RuntimeDiagnostics:
    """直启 serve 的死亡可归因面：stderr 落盘 + faulthandler 常开 + 心跳。"""

    def __init__(
        self,
        root: Path,
        *,
        heartbeat_seconds: float = _DIAGNOSTIC_HEARTBEAT_SECONDS,
        clock=time,
    ):
        self._dir = Path(root) / "logs" / "server"
        self._heartbeat_seconds = max(0.01, float(heartbeat_seconds))
        self._clock = clock
        self._file = None
        self._previous_stderr = None
        self._faulthandler_armed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.path: Path | None = None

    def install(self) -> Path | None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            prune_server_logs(self._dir)
            stamp = self._clock.strftime("%Y%m%d-%H%M%S")
            path = self._dir / f"backend-stderr-{stamp}-{os.getpid()}.log"
            self._file = path.open("a", encoding="utf-8", buffering=1)
        except OSError:
            return None
        self.path = path
        self._previous_stderr = sys.stderr
        sys.stderr = self._file
        faulthandler.enable(file=self._file)
        self._faulthandler_armed = True
        self._file.write(
            f"[autopsy] pid={os.getpid()} start={self._clock.strftime('%Y-%m-%d %H:%M:%S')} "
            f"python={sys.version.split()[0]} faulthandler=on\n"
        )
        self._thread = threading.Thread(
            target=self._heartbeat_loop, name="courselens-autopsy-heartbeat", daemon=True
        )
        self._thread.start()
        return path

    def _heartbeat_loop(self) -> None:
        started = self._clock.monotonic()
        while not self._stop.wait(self._heartbeat_seconds):
            log = self._file
            if log is None:
                return
            try:
                log.write(
                    f"[heartbeat] t={self._clock.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"uptime={self._clock.monotonic() - started:.0f}s\n"
                )
            except (OSError, ValueError):
                return

    def record_exit(self, reason: str) -> None:
        log = self._file
        if log is None:
            return
        try:
            log.write(
                f"[exit] reason={reason or 'unrecorded'} "
                f"t={self._clock.strftime('%Y-%m-%d %H:%M:%S')}\n"
            )
            log.flush()
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        if self._faulthandler_armed:
            # 释放 faulthandler 对日志文件的引用，句柄才能真正关掉
            # （Windows 上开着句柄的文件删不掉——临时目录清理会撞 WinError 32）。
            faulthandler.disable()
            self._faulthandler_armed = False
        log, self._file = self._file, None
        if log is not None:
            try:
                log.flush()
            except (OSError, ValueError):
                pass
            try:
                log.close()
            except (OSError, ValueError):
                pass
        if self._previous_stderr is not None:
            sys.stderr = self._previous_stderr
            self._previous_stderr = None


def _install_runtime_diagnostics(root: Path) -> _RuntimeDiagnostics | None:
    """装尸检面；失败不拦启动（返回 None=本次无尸检，服务照常）。"""
    diagnostics = _RuntimeDiagnostics(root)
    if diagnostics.install() is None:
        print(
            "[FudanCourseLens] 尸检日志不可写，本次运行不落 stderr 日志（服务照常）。",
            flush=True,
        )
        return None
    return diagnostics


def _describe_port_occupier(port: int) -> str:
    """Best-effort name of the process listening on ``port`` (P67 U3b).

    Purely diagnostic: any failure keeps the generic copy instead of guessing
    a process name, and nothing here ever kills or signals the occupier.
    """
    try:
        import psutil

        for conn in psutil.net_connections(kind="tcp"):
            if (
                conn.status == psutil.CONN_LISTEN
                and conn.laddr is not None
                and conn.laddr.port == port
                and conn.pid
            ):
                return psutil.Process(conn.pid).name()
    except Exception:
        pass
    return ""


def _print_port_busy_hint(port: int, exc: OSError) -> None:
    # P67 U3b (DEF-1 文案分岔): N9-A2 explained WSAEACCES(10013) as "system
    # reserved" (Hyper-V/WSL excluded ranges). But on Windows a socket bound
    # with SO_REUSEADDR failing against a plain listener also reports 10013,
    # so a real occupier was misdiagnosed as an empty reserved port (P67B:
    # AtlasCore actually held 6268). Name the listener when one exists; only
    # a listener-less 10013 keeps the reserved-range explanation, and
    # WSAEADDRINUSE(10048) is always a real occupier. The closed-set error
    # code stays ERROR_PORT_BUSY for every branch.
    winerror = getattr(exc, "winerror", None)
    if winerror not in (10013, 10048):
        return
    occupier = _describe_port_occupier(port)
    if occupier:
        print(
            f"[FudanCourseLens] 端口 {port} 正被「{occupier}」使用中。"
            "CourseLens 启动时会自动换一个空闲端口再试，通常不用你做任何事；"
            f"如果想固定这个端口，把「{occupier}」设置成使用其他端口即可。",
            flush=True,
        )
    elif winerror == 10013:
        # N9-A2：10013(WSAEACCES) 在 Windows 上多为动态排除端口段
        # （Hyper-V/WSL 保留，netsh excludedportrange 可见）——端口其实没被
        # 占用，是系统收走了。给一句人话指路，别让学生误以为要抢端口。
        print(
            "[FudanCourseLens] 这个端口可能被系统保留了（Hyper-V/WSL 的动态排除段），"
            "其实没有程序在用它：换个端口最省事；重启电脑也常能让保留段挪走。",
            flush=True,
        )
    else:
        print(
            f"[FudanCourseLens] 端口 {port} 被一个无法识别的程序占用了："
            "CourseLens 启动时会自动换一个空闲端口再试。",
            flush=True,
        )


def _resolve_native_window(
    native_window: bool | None, *, open_browser: bool
) -> bool:
    """P68 窗口决策面：显式旗标 > 受管默认 > 无头否决。

    受管安装（COURSELENS_INSTALL_ROOT 在位）默认原生窗口；开发头（源码检出）
    维持浏览器形态；open_browser=False（--no-open，每日任务/无头）是最强的
    无头意图，永不建窗。
    """
    if not open_browser:
        return False
    if native_window is not None:
        return bool(native_window)
    return bool(os.environ.get("COURSELENS_INSTALL_ROOT", "").strip())


# --- LAUNCH-FIX-1 A：挂接模式（attach window）--------------------------------
# 既有服务在跑但找不到它的原生窗口时（无头实例/窗口检测未命中），launcher 用
# 本模式新开一枚原生窗挂接到该服务的 URL：零服务、零生命周期、零业务逻辑——
# 不取实例锁、不组服务、不碰端口；窗口只是既有 frontend 的另一张薄壳（复用
# P68 窗口创建/状态/降级代码）。关窗只关本窗：attach 进程对服务零控制权，页
# 会话的离场由服务侧既有前端会话租约语义自理（其他窗口心跳在→服务继续；无
# 窗头实例按既有租约/头闲置语义收班）。服务身份（service 名/版本/pid 闭集指
# 纹）由 launcher 的探活面把关，这里只守住回环地址这一底线。


def _attach_native_window(url: str) -> int:
    parts = urlsplit(url)
    if parts.scheme != "http" or (parts.hostname or "") not in {"127.0.0.1", "localhost"}:
        print(
            "[FudanCourseLens] attach-window 只接受本机回环地址。",
            flush=True,
        )
        return 2

    def _fallback_open() -> None:
        if not webbrowser.open(url):
            print(f"[FudanCourseLens] Open the UI manually: {url}", flush=True)

    _open_native_window(
        url,
        data_dir=Path(DEFAULT_DATA_DIR),
        frontend_sessions=None,
        fallback_open=_fallback_open,
    )
    return 0


def _apply_test_mode_startup_contract(data_dir: str | Path | None, port: int) -> None:
    """测试模式启动契约（TESTBENCH-DESIGN-1 件②M1，src/runtime/test_mode.py）。

    fail-closed 前置校验：数据目录强制沙盒（单一语义 = COURSELENS_DATA_DIR，
    解析结果回写进程 env 让子进程同源同值——终结「--data-dir 须同时设 env」
    双旗坑）；端口禁用 6268 产品默认；egress 白名单档启动即校验。
    契约违例 = 人话提示 + 闭集 LifecycleError 码（main() 原样上打印）。
    """
    try:
        mode = _test_mode.resolve_test_mode()
        _test_mode.resolve_egress_allow_or_raise()
        _test_mode.ensure_test_port_allowed(port)
        resolved = _test_mode.test_data_dir_contract(data_dir)
        _test_mode.validate_instance_name(_test_mode.test_instance_name())
        credentials_file = _test_mode.resolve_test_credentials_linkage()
    except _test_mode.TestModeContractError as exc:
        print(f"[FudanCourseLens] {exc.human_message}", file=sys.stderr, flush=True)
        raise LifecycleError(exc.code) from exc
    _test_mode.publish_data_dir_env(resolved)
    allow_note = ""
    if os.environ.get(_test_mode.EGRESS_ALLOW_ENV, "").strip():
        allow_note = f"，egress 白名单={os.environ[_test_mode.EGRESS_ALLOW_ENV].strip()}"
    identity_note = "，测试身份=内存注入凭据文件" if credentials_file else ""
    print(
        f"[FudanCourseLens] 测试模式：{mode}"
        f"（沙盒数据目录={resolved}；出站默认全部拒绝{allow_note}{identity_note}）",
        flush=True,
    )


def _make_test_instance_registrar():
    """件③M2（src 侧）：测试模式下把 serve 进程登记进工作区实例注册表。

    未设实例名 env = None（发布版零成本，本函数只在测试模式分支被调用，
    import 也留在分支内）。实例名非法 = 启动契约拒绝（绑定端口前已在
    _apply_test_mode_startup_contract fail-closed，此处同口径兜底）。
    """
    from src.runtime import test_instance

    try:
        return test_instance.registrar_from_env()
    except _test_mode.TestModeContractError as exc:
        print(f"[FudanCourseLens] {exc.human_message}", file=sys.stderr, flush=True)
        raise LifecycleError(exc.code) from exc


def serve(
    *,
    data_dir: str | Path | None = None,
    port: int = 6268,
    host: str = "127.0.0.1",
    open_browser: bool = True,
    stop_when_frontend_closes: bool = True,
    native_window: bool | None = None,
) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("CourseLens only binds the loopback interface")
    # 测试模式契约（TESTBENCH-DESIGN-1 件②M1）：未设 env = 下面整块跳过，
    # 发布版启动路径与契约落地前逐位一致（惰性钉 tests/test_test_mode_contract.py）。
    test_mode_active = _test_mode.is_test_mode()
    test_instance_registrar = None
    if test_mode_active:
        _apply_test_mode_startup_contract(data_dir, port)
        test_instance_registrar = _make_test_instance_registrar()
    # C4：启动期裁剪历史会话日志（保留 14 天）；删除数>0 打一行留痕。
    pruned_logs = prune_server_logs()
    if pruned_logs:
        print(
            f"[FudanCourseLens] Pruned {pruned_logs} server log(s) older than {SERVER_LOG_KEEP_DAYS} days",
            flush=True,
        )
    open_window = _resolve_native_window(native_window, open_browser=open_browser)
    # 测试模式已由启动契约把沙盒根回写进 env（子进程同源）；从 env 取根，
    # 避免复用导入期常量 DEFAULT_DATA_DIR（那不是测试模式的沙盒根）。
    if test_mode_active:
        root = Path(os.environ[_test_mode.DATA_DIR_ENV])
    else:
        root = Path(data_dir or DEFAULT_DATA_DIR)
    controller = _TracedLifecycleController()
    instance = InstanceLock(root, PROJECT_INSTANCE_ID)
    services: CourseLensServices | None = None
    frontend_sessions: FrontendSessionRegistry | None = None
    server: ThreadingHTTPServer | None = None
    server_thread: threading.Thread | None = None
    previous_signals: dict[int, object] = {}
    diagnostics: _RuntimeDiagnostics | None = None
    started_at = time.monotonic()
    try:
        instance.acquire()
    except LifecycleError as exc:
        # 夜10-A T6：交互式二次启动撞上在跑实例 → 把已有窗口拉回前台并闪烁
        # （身份闭集=instance 证据文件里的属主 pid + 窗口标题）。无头实例
        # （每日任务）绝不抢学生焦点，保持原样安静退出。
        if getattr(exc, "code", "") == ERROR_INSTANCE_ACTIVE and open_browser:
            if focus_running_instance_window(root):
                print(
                    "[FudanCourseLens] CourseLens 已经在运行，已把它的窗口带到前台。",
                    flush=True,
                )
        raise
    try:
        # 夜10-A T8：清理标记在此消费（锁已到手=没有别的实例在用这个档案）。
        _purge_webview_profile_if_requested(root)
        # BACKEND-DEATH-1④：死亡可归因面（stderr 落数据目录+faulthandler+心跳）。
        diagnostics = _install_runtime_diagnostics(root)
        controller.transition("starting", "instance_lock")
        # 两阶段绑定①（AS9 保留）：端口先用启动态 handler 立起来并起循环，误入
        # 的请求被诚实告知「正在启动」。P64：浏览器不在绑定后即开——启动态页
        # 退化为兜底面，开窗统一迁到 ready 之后。启动/附加语义
        # （InstanceLock、publish）保持后置不动。
        try:
            server = _ExclusiveBindHTTPServer(
                (host, port), _startup_state_handler(_STARTUP_PAGE_HTML, status=200)
            )
        except OSError as exc:
            _print_port_busy_hint(port, exc)
            controller.transition("failed", "service_start", ERROR_PORT_BUSY)
            raise LifecycleError(ERROR_PORT_BUSY) from exc
        server_thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.1},
            name="courselens-http", daemon=True,
        )
        server_thread.start()
        actual_port = int(server.server_address[1])
        url = f"http://127.0.0.1:{actual_port}/"
        print(f"Fudan CourseLens UI: {url}", flush=True)
        # 件③M2：绑定落地即把 serve 进程登记进实例注册表（注册失败不拦服务，
        # 只留人话警告——注册表是可观测性设施，不是服务的可用性依赖）。
        if test_instance_registrar is not None:
            try:
                test_instance_registrar.register(port=actual_port, data_dir=root)
            except OSError as exc:
                print(
                    f"[FudanCourseLens] 测试实例注册表不可写（{exc}）；"
                    "本次运行不登记实例注册表，服务照常继续。",
                    file=sys.stderr,
                    flush=True,
                )
                test_instance_registrar = None
            else:
                print(
                    f"[FudanCourseLens] 测试实例已注册：{test_instance_registrar.name}"
                    f"（端口 {actual_port}，注册表 {test_instance_registrar.registry_dir}）",
                    flush=True,
                )
        controller.transition("migrating", "local_data")
        try:
            coordinate_local_data(root)
        except LocalDataRecoveryError as exc:
            # 恢复材料问题维持既有语义：立刻失败交回 launcher 引导（不加宽限页，
            # 恢复 UX 属启动脚本域）；循环线程由 finally 统一收班。
            controller.transition("recovery_required", "local_data", exc.code)
            raise LifecycleError(ERROR_RECOVERY_REQUIRED) from exc
        controller.transition("starting", "service_start")
        # 两阶段绑定②：全量应用初始化；失败时错误页留满宽限窗再走既有
        # 失败退出（U2），并补开窗展示人话错误页（P64）；成功则把真 handler
        # 热替换上去（在途启动态请求自然结束——handler 类按请求从实例属性
        # 读取），浏览器在 ready 后由开窗线程唤起（P64）。
        try:
            services = compose_services(root, lifecycle_controller=controller)
        except LifecycleError:
            raise
        except Exception as exc:
            controller.transition("failed", "service_start", ERROR_STARTUP_FAILED)
            _serve_startup_error_until_grace_ends(
                server, exc, url=url, open_browser=open_browser
            )
            raise LifecycleError(ERROR_STARTUP_FAILED) from exc
        frontend_sessions = FrontendSessionRegistry() if stop_when_frontend_closes else None
        server.RequestHandlerClass = make_handler(services, PROJECT_ROOT / "frontend",
            frontend_sessions=frontend_sessions,
        )
        # 夜10-A T7：关停回调组合化——任何关停请求（更新重启/会话收班/信号）
        # 除停 HTTP 循环外同时点亮 window_exit，让原生窗口自动退场、进程得以
        # 退出交还 launcher（verified pending switch → apply → --window 重开）。
        window_exit = threading.Event()

        def _on_shutdown_requested() -> None:
            if server is not None:
                server.shutdown()
            window_exit.set()

        controller.set_shutdown_callback(_on_shutdown_requested)
        if frontend_sessions is not None:
            frontend_sessions.set_shutdown_callback(
                lambda: controller.request_shutdown("last_frontend_session"),
                should_keep_alive=services.lifecycle.has_active_work,
            )
        # ⑬a：启动即后台预热会话（检查点恢复），学生首次点击不再付十秒首读
        services.lifecycle.prime_session_restore()
        services.lifecycle.start_search_index()
        services.lifecycle.start_remote_connection()
        services.lifecycle.recover_remote_runs()
        services.lifecycle.start_daily_schedule()
        instance.publish(actual_port)
        controller.transition("ready", "serving")
        print(
            f"[FudanCourseLens] 服务就绪，用时 {time.monotonic() - started_at:.1f} 秒。",
            flush=True,
        )
        # P64：真 handler 在位且 ready 之后才开窗——学生第一眼就是真界面，
        # 不再出现「正在启动」过渡页；后台线程执行，健康轮询仅作就绪确认，
        # 不阻塞就绪链。受管安装走原生窗口（P68，主线程窗口循环在下方信号
        # 安装后进入）；开发头/降级走浏览器。
        if open_window:
            pass  # P68：原生窗口在信号安装后由主线程打开（时序见下方注释）
        elif open_browser:
            threading.Thread(
                target=_open_browser_when_ready, args=(url,),
                name="courselens-open-browser", daemon=True,
            ).start()
        # 循环服务就绪后才允许启动自动连接：每进程至多一次、后台执行、不阻塞就绪。
        services.lifecycle.start_auto_connect()
        previous_signals = install_signal_handlers(controller)
        if open_window:
            # P68：主线程进入原生窗口循环（阻塞至关窗），serve 循环线程照旧
            # 后台服务。关窗=「前端离场」的确定性信号（registry 显式 close
            # 快路径），随后既有收班链接管；pywebview 初始化失败时降级开
            # 浏览器。启动页/失败页语义（P64）不变——窗口打开时 ready 已过。
            _open_native_window(
                url,
                data_dir=root,
                frontend_sessions=frontend_sessions,
                fallback_open=lambda: _open_browser_when_ready(url),
                exit_requested=window_exit,
                started_at=started_at,
            )
        # 主线程等循环线程收班：关停回调触发 serve_forever 返回后 join 即回，
        # Ctrl+C 在 join 上照常打断（异常路径经 finally 兜底收线程）。
        server_thread.join()
    except KeyboardInterrupt:
        controller.request_shutdown("keyboard_interrupt")
    except LifecycleError:
        raise
    except Exception as exc:
        controller.transition("failed", "service_start", ERROR_STARTUP_FAILED)
        raise LifecycleError(ERROR_STARTUP_FAILED) from exc
    finally:
        _stop_client_update_background_checks()
        if previous_signals:
            restore_signal_handlers(previous_signals)
        if controller.snapshot()["state"] not in {"failed", "recovery_required"}:
            controller.request_shutdown("service_returned")
        if frontend_sessions is not None:
            frontend_sessions.stop()
        controller.transition("stopping", "live_sessions")
        if services is not None:
            services.live_room.revoke_all()
        controller.transition("stopping", "task_checkpoint")
        stop_result = {"timed_out": False}
        if services is not None:
            stop_result = services.lifecycle.close(
                timeout=controller.budgets.service_stop
            )
        controller.transition(
            "stopping",
            "resource_release",
            ERROR_SERVICE_STOP_TIMEOUT if stop_result.get("timed_out") else "",
        )
        if server is not None:
            # AS9：循环线程收班后再关 socket。仅在线程确实在跑时调 shutdown()
            # ——serve_forever 尚未开跑时 shutdown() 会永远等（文档化死锁）。
            if server_thread is not None and server_thread.is_alive():
                server.shutdown()
                server_thread.join(timeout=5.0)
            server.server_close()
        instance.release()
        if test_instance_registrar is not None:
            test_instance_registrar.close()
        controller.transition(
            "stopped",
            "complete",
            ERROR_SERVICE_STOP_TIMEOUT if stop_result.get("timed_out") else "",
        )
        # BACKEND-DEATH-1④：优雅退出的尸检收笔——[exit] 行带关停原因落进
        # 同一文件后停心跳、还原 stderr。硬终止走不到这里，文件以最后一条
        # [heartbeat] 截断=死亡时间下界（可归因三态之一）。
        if diagnostics is not None:
            diagnostics.record_exit(
                str(controller.snapshot().get("shutdown_reason") or "")
            )
            diagnostics.close()


class _ExclusiveBindHTTPServer(ThreadingHTTPServer):
    """UI 服务器绑定语义按平台收口（FAULT-MATRIX-1 F-INST-1）。

    ``ThreadingHTTPServer`` 默认 ``allow_reuse_address=1``：POSIX 上只是
    TIME_WAIT 复用（安全），Windows 上 SO_REUSEADDR 却是「可劫持」语义——
    第二个实例能成功绑住在位实例的端口并同样报告「服务就绪」，两个数据
    目录不同的实例静默共享端口、请求随机归属（真双开互扰缺陷）。Windows
    改用 ``SO_EXCLUSIVEADDRUSE``：第二个绑定者确定性失败，走既有
    ``ERROR_PORT_BUSY`` 人话提示；MSDN 口径下 TIME_WAIT 不阻断重绑，
    崩溃后快速重启不受影响（由 FAULT-MATRIX-1 多实例 harness 用例 4 实证）。
    POSIX 保持既有语义不变。

    F-GATE-2（发布门 2026-10-10）：覆写 ``request_queue_size``（listen
    backlog）。socketserver 默认值 5 在首载并发突发下可被打穿——首屏约
    49 个静态模块 + 约 10 个 API + SSE 长连几乎同时到达，accept 线程被
    GIL/每请求线程启动短暂挤占时，Windows 的 backlog 溢出表现为新连接
    直接拒绝（ERR_CONNECTION_REFUSED）而非排队，浏览器侧即整页静态
    import 图死亡（白屏形，刷新才恢复）。取 128：浏览器对同域 HTTP/1.1
    并发上限约 6 连接，叠加多窗/attach/健康自探测仍有数量级余量；backlog
    只是内核排队深度，内存成本可忽略，128 为 listen 的常规工程值。
    """

    allow_reuse_address = os.name != "nt"
    request_queue_size = 128

    def server_bind(self) -> None:
        if os.name == "nt":
            import socket as _socket

            self.socket.setsockopt(
                _socket.SOL_SOCKET, _socket.SO_EXCLUSIVEADDRUSE, 1
            )
        super().server_bind()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the supported command surface (``serve`` and ``--attach-window``)."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--attach-window":
        if len(args) < 2:
            raise SystemExit("--attach-window requires a value")
        return _attach_native_window(args[1])
    if not args or args[0] != "serve":
        print(
            "usage: python -m src serve [--port PORT] [--host HOST] "
            "[--no-open] [--keep-server] [--e2e] [--window | --no-window]\n"
            "       python -m src --attach-window URL\n"
            "       --e2e = 测试模式 real 档别名（测试模式契约见 "
            "src/runtime/test_mode.py）"
        )
        return 2
    port = 6268
    host = "127.0.0.1"
    open_browser = True
    keep_server = False
    native_window: bool | None = None
    data_dir: str | Path | None = None
    e2e_flag = False
    index = 1
    while index < len(args):
        value = args[index]
        if value == "--no-open":
            open_browser = False
        elif value == "--window":
            native_window = True
        elif value == "--no-window":
            native_window = False
        elif value == "--keep-server":
            keep_server = True
        elif value == "--e2e":
            e2e_flag = True
        elif value in {"--port", "--host", "--data-dir"}:
            index += 1
            if index >= len(args):
                raise SystemExit(f"{value} requires a value")
            if value == "--port":
                port = int(args[index])
            elif value == "--host":
                host = args[index]
            else:
                data_dir = args[index]
        else:
            raise SystemExit(f"unknown argument: {value}")
        index += 1
    # 测试模式契约入口收敛（TESTBENCH-DESIGN-1 件②M1）：--e2e ≡ 测试模式
    # real 档（别名；env 已设 synthetic 时 = 契约冲突）。
    # 测试模式缺省 port=0（OS 分配）——产品默认端口 6268 结构性让位给用户实例。
    # env 字面量按惰性钉只允许出现在 src/runtime/test_mode.py（经符号引用）。
    env_mode = os.environ.get(_test_mode.TEST_MODE_ENV, "").strip()
    if e2e_flag:
        if env_mode and env_mode != _test_mode.TEST_MODE_REAL:
            print(
                f"[FudanCourseLens] {_test_mode.TestModeContractError(_test_mode.TEST_MODE_CONFLICT).human_message}",
                file=sys.stderr,
                flush=True,
            )
            return 2
        if not env_mode:
            os.environ[_test_mode.TEST_MODE_ENV] = _test_mode.TEST_MODE_REAL
        test_mode_active = True
    else:
        test_mode_active = bool(env_mode)
    if test_mode_active and port == 6268:
        port = 0
    try:
        serve(
            data_dir=data_dir,
            port=port,
            host=host,
            open_browser=open_browser,
            stop_when_frontend_closes=not keep_server,
            native_window=native_window,
        )
        return 0
    except LifecycleError as exc:
        print(f"[FudanCourseLens] {exc.code}", file=sys.stderr, flush=True)
        return 1


__all__ = ["compose_services", "create_application", "main", "serve"]
