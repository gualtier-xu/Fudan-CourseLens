from pathlib import Path
import contextlib
import io
import re
import json
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.runtime.http_api import _greeting_tail_key_from_ends, make_handler
from src.runtime.timetable import TimetableStore

from credentials import CredentialStore
from src.remote.connection import RemoteConnectionSupervisor
from src.runtime.task_store import TaskStore
from tests.frontend_family import family_entity, family_text


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


class _SnapshotFlakyGitHubApp:
    """FRONTEND-SMOOTH-1 单元C 行为钉用最小替身：本地 snapshot 可注入瞬态失败
    （模拟动作持有凭据库/状态库时快照构建被占用），其余语义=已配置、未授权。"""

    def __init__(self):
        self.fail_after_first = False
        self._calls = 0

    def snapshot(self):
        self._calls += 1
        if self.fail_after_first and self._calls > 1:
            raise RuntimeError("simulated transient failure during action")
        return {
            "app_configured": True,
            "authorized": False,
            "installation_url": "",
            "bootstrapped": False,
            "job_token_cleanup_pending": False,
        }


class _WaitWindowFreshGitHubApp:
    """INIT-PATH-POLISH-1 单元A 行为钉用替身：installation 可从未安装翻转为
    已安装（模拟学生在 GitHub 完成安装），并计数 inspect 调用；其余语义=
    已配置、已授权、最小在线证据。"""

    def __init__(self):
        self.installed = False
        self.inspect_calls = 0

    def snapshot(self):
        return {
            "app_configured": True,
            "authorized": True,
            "installation_url": "",
            "worker_repo": "",
            "mailbox_repo": "",
            "bootstrapped": True,
            "job_token_cleanup_pending": False,
        }

    def inspect_managed_resources(self):
        self.inspect_calls += 1
        return {
            "identity": {"login": "student", "account_id": 123},
            "installation": {
                "installed": self.installed, "installation_id": 1,
                "repository_selection_exact": True,
                "missing_installation_repositories": [],
                "unexpected_installation_repositories": [],
            },
            "worker": {
                "exists": False, "private": False, "archived": False,
                "disabled": False, "managed": True, "owner": "student",
                "default_branch": "main",
            },
            "mailbox": {
                "exists": False, "private": True, "archived": False,
                "disabled": False, "managed": True, "owner": "student",
                "has_issues": True,
            },
            "worker_commit": "", "worker_tree": "",
            "expected_commit": "a" * 40, "expected_tree": "b" * 40,
            "workflows": {
                "process.yml": {"exists": True, "state": "active"},
                "echo.yml": {"exists": True, "state": "active"},
            },
            "actions_enabled": True,
            "environment_exists": True,
            "secret_names": ["WORKER_INPUT_PRIVATE_KEY", "WORKER_SIGNING_PRIVATE_KEY"],
            "variables": {"COURSELENS_MAILBOX_REPO": "student/Fudan-CourseLens-Mailbox"},
            "rate_limit": {"remaining": 5000, "reset_at": 0},
        }


class FrontendWorkbenchTests(unittest.TestCase):
    def test_every_frontend_behavior_mjs_engages_the_real_artifacts(self):
        """装载形态哨兵（N15-W1，夜14-R7 审计纠错的固化）：每个 frontend 行为
        mjs 必须以任一已证形态真触达产物——await import( 动态真装载 / 静态
        ESM import 真模块 / new Function 真执行 / 复用 frontend_exec_harness
        装配 / readFile(Sync) 读真件做结构钉。全无 = 假执行回潮，直接红。
        词表本身带教训：审计工具曾只认 new Function 漏判 30/32；本哨兵首版
        也漏了静态 import 与 readFile 两形态（catalog_login_loop 误报）——
        任何新装载形态落地时必须同步扩此词表。"""
        import re

        behavior_files = sorted((ROOT / "tests").glob("frontend_*.mjs"))
        self.assertGreaterEqual(len(behavior_files), 10, "行为 mjs 集不应缩水")
        engagement_markers = (
            "await import(",
            "new Function",
            "frontend_exec_harness",
            "readFileSync",
            "readFile(",
        )
        static_import_re = re.compile(r'from\s+"\.\./frontend/')
        disengaged = []
        await_import_users = 0
        for path in behavior_files:
            text = path.read_text(encoding="utf-8")
            engaged = (
                any(marker in text for marker in engagement_markers)
                or bool(static_import_re.search(text))
            )
            if not engaged:
                disengaged.append(path.name)
            if "await import(" in text:
                await_import_users += 1
        self.assertEqual(
            disengaged, [], f"以下 mjs 未触达任何真实产物（假执行嫌疑）: {disengaged}"
        )
        # 装载形态普查锚：await-import 真装载形态必须始终是在场主力（夜14-R7
        # 误判的根因形态），数目骤降即提示有人批量改写装载方式。
        self.assertGreaterEqual(await_import_users, 20, "await-import 真装载面骤降")

    @classmethod
    def setUpClass(cls):
        cls.html = (FRONTEND / "index.html").read_text(encoding="utf-8")
        cls.app = (FRONTEND / "app.js").read_text(encoding="utf-8")
        # ARCH-DEBT-1：被拆模块的值=家族并集（门面+子目录模块），内容钉检索
        # 家族而非门面单文件；键集仍为顶层文件名（布局闭集钉不受影响）。
        cls.modules = {
            path.name: family_text(path.stem)
            for path in sorted((FRONTEND / "modules").glob("*.js"))
        }
        cls.javascript = "\n".join([cls.app, *cls.modules.values()])
        cls.css = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((FRONTEND / "styles").glob("*.css"))
        )
        cls.css_by_name = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted((FRONTEND / "styles").glob("*.css"))
        }

    # ---- structure: single study scene + settings page, overlays ----

    def test_study_is_the_only_persistent_scene(self):
        pages = re.findall(r'data-page="([a-z-]+)"', self.html)
        self.assertEqual(pages, ["study", "live", "settings", "data", "onboarding"])
        self.assertNotIn("data-page-target", self.html)
        self.assertNotIn("page-switch", self.html + self.css)
        self.assertNotIn("bottom-nav", self.html + self.css)
        for removed in (
            'data-page="search"', 'data-page="timetable"', "search-page", "timetable-page",
            "downloadsWorkspace", "localCompute", "filePipeline", "computePopover",
            "data-workspace", "workspace-nav",
        ):
            self.assertNotIn(removed, self.html + self.javascript)

    def test_ics_export_guard_source_pins(self):
        """N5FE-P8：未登录/零课表点 ICS 导出被拦截（mjs 桩的 dispatchEvent
        自定义实现暂无法承载该场景，先以源码钉守住护栏三要素）。"""
        timetable = self.modules["timetable.js"]
        self.assertIn("attachIcsGuard", timetable)
        self.assertIn('exportReady = "1"', timetable)
        self.assertIn("先登录复旦课程平台，再导出课表日历", timetable)
        self.assertIn("课表还没有可导出的内容", timetable)

    def test_custom_dropdowns_replace_native_select_popups(self):
        # 甲2/甲3（Prompt 42）：原生 select 的 OS 级下拉全站退役——触发钮+玻璃
        # 面板双皮（页面双主题/播放器恒暗向上展开）；原生 select 视觉隐藏但
        # 保留为 value/change 真相源，既有监听与程序化赋值零改动。
        dropdown = self.modules["dropdown.js"]
        self.assertIn("export function attachDropdown", dropdown)
        self.assertIn("export function initPageDropdowns", dropdown)
        self.assertIn('select.dispatchEvent(new Event("change", { bubbles: true }))', dropdown)
        self.assertIn('"dropdown-source"', dropdown)
        # 白名单：index.html 恰十一处 data-dropdown（倍速=media 皮，其余页面皮；
        # P56-U1 增主题三态 select；A11Y-IMPL-4 增界面字号档 select；
        # SIMPLIFY-AUDIT-1 S1 删设置页课表组镜像 select）
        self.assertEqual(self.html.count("data-dropdown"), 11)
        self.assertIn('id="player-ctrl-speed" class="player-ctrl-speed" data-dropdown="media"', self.html)
        for select_id in (
            "catalog-term-filter", "timetable-semester", "artifact-kind",
            "document-kind-filter", "document-type-select", "fudan-auto-connect-account",
            "network-mode", "data-category-filter",
            "settings-theme-mode", "settings-ui-font",
        ):
            self.assertRegex(self.html, rf'id="{select_id}"[^>]*data-dropdown')
        # 装配：app.js 页面级换装；player-core 自附 media 皮并同步禁用/文案
        self.assertIn('from "./modules/dropdown.js"', self.app)
        self.assertIn("initPageDropdowns(document)", self.app)
        player = self.modules["player-core.js"]
        self.assertIn('attachDropdown($("player-ctrl-speed"), { skin: "media" })', player)
        self.assertGreaterEqual(player.count("syncDropdown("), 4)
        # 玻璃面板：双主题 surface 半透明+blur；过渡走 --motion 档
        self.assertIn("backdrop-filter: blur(14px)", self.css)
        self.assertIn("color-mix(in srgb, var(--surface-strong) 92%, transparent)", self.css)
        self.assertIn("color-mix(in srgb, var(--stage-surface) 88%, transparent)", self.css)
        self.assertIn("transition: opacity var(--motion) ease", self.css)
        # 播放器皮向上展开贴控制台；reduced-motion 全局压平机制仍在位
        self.assertIn("bottom: calc(100% + 8px)", self.css)
        self.assertIn("accessibility", str(self.css_by_name))
        # 甲3：账户/连接菜单玻璃面板+箭头键导航
        self.assertIn('installMenuArrowNav($("account-menu"))', self.modules["shell.js"])
        self.assertIn('installMenuArrowNav($("conn-menu"))', self.modules["shell.js"])

    def test_module_layout_matches_the_migration_plan(self):
        expected = {
            "api.js", "store.js", "ui.js", "shell.js", "study.js", "player-core.js",
            "live-room.js", "live-state.js", "live-player.js", "live-page.js",
            "tasks-drawer.js", "search-palette.js", "timetable.js",
            "home-overview.js", "settings.js", "onboarding.js", "greeting.js",
            "greeting-boot.js", "update-widget.js", "course-data.js",
            "media-guard.js",  # NIGHT5-U2 页面级防泄露护栏
            "dropdown.js",  # Prompt 42 甲2/甲3：共享自绘下拉
            "ui-font.js",  # A11Y-IMPL-4：界面字号三态偏好（顶层=settings 家族零浏览器存储钉）
            "course-review.js",  # N7F：课程总体复习 surface（课程级，入口唯一）
            "quality-chip.js",  # THINK-LADDER-2：抽检质量 chip + 课程记忆候选复核面
            "review-multiview.js",  # RR-P2MULTI-1：讲次级多视图复习包（复习页签内挂载）
            "course-flashcards.js",  # RR-P4FSRS-1：课程级闪卡复习（总体复习第 4 页签）
            "stage-preload.js",  # PERF-C23：舞台级预载提示（纯观察者，预载期 spinner 事件族不触发）
            "update-mac.js",  # UPDATE-UX-1 a3ddf5e：mac 更新三态检查道（顶栏点亮信号源）
            "wait-expectations.js",  # WAIT-UX-1：等待预期（ETA）文案闭集单源（零呆等三律）
            "study-stats.js",  # STUDY-STATS-M1：本周学习面貌默认层卡（着陆页三块，取代死格）
        }
        self.assertEqual(set(self.modules), expected)

    # ---- N7F 课程总体复习：课程级唯一入口 + 三视图 surface ----

    def test_course_review_entry_is_unique_and_course_level(self):
        """课程级只有一个「总体复习」入口，且落在课程头而不是课次卡里。"""
        self.assertEqual(self.html.count('id="course-review-open"'), 1, "入口 DOM 全域唯一")
        head = self.html[
            self.html.index('<div class="lecture-head">'):self.html.index('id="study-lecture-list"')
        ]
        self.assertIn('id="course-review-open"', head, "入口在课程头 .lecture-head 内")
        self.assertIn("总体复习", head)
        study = self.modules["study.js"]
        # 入口只在选中课程后出现；不可在课次行/课次列表里再放一个
        self.assertIn('if (reviewEntry) reviewEntry.hidden = !course?.course_id;', study)
        self.assertNotIn("lecture-row-main", self.html)
        # 旧四 tab 导航不动：复习 tab 域不得污染既有 data-material-tab
        self.assertEqual(self.html.count('data-material-tab='), 4, "既有四 tab 导航原样保留")
        self.assertNotIn('data-material-tab="review"', self.modules["course-review.js"])

    def test_course_review_surface_markup_and_a11y_contract(self):
        """surface 四视图（RR-P4FSRS-1 增补闪卡）的 tab/tabpanel 接线与 roving tabindex 齐备。"""
        self.assertEqual(self.html.count('id="course-review-surface"'), 1)
        self.assertIn('aria-labelledby="course-review-title"', self.html)
        for name, label in (("topics", "知识脉络"), ("lectures", "按课次"), ("assessment", "练习与真题"), ("flashcards", "闪卡复习")):
            self.assertIn(f'data-review-tab="{name}"', self.html)
            self.assertIn(f'data-review-panel="{name}"', self.html)
            self.assertIn(f'id="cr-panel-{name}"', self.html)
            self.assertIn(label, self.html)
        self.assertEqual(self.html.count('data-review-tab='), 4, "恰好四个复习视图")
        self.assertEqual(self.html.count('data-review-panel='), 4)
        # 状态与刷新结果都是 live region（读屏能听到状态变化）
        self.assertIn('id="course-review-status" class="cr-status" role="status" aria-live="polite"', self.html)
        self.assertIn('id="course-review-refresh-result"', self.html)
        module = self.modules["course-review.js"]
        self.assertIn('setAttribute("aria-selected", String(active))', module)
        self.assertIn("node.tabIndex = active ? 0 : -1;", module)

    def test_course_review_consumes_frozen_contract_only(self):
        """消费层只认冻结合同：闭集外 status 诚实降级，答案默认不冒充官方。"""
        module = self.modules["course-review.js"]
        self.assertIn('export const CONTRACT_ID = "courselens.course-knowledge.v1";', module)
        self.assertIn('Object.freeze(["ready", "partial", "stale", "error"])', module)
        for state in ("complete", "partial", "stale", "processing", "failed", "empty", "conflict"):
            self.assertIn(f'"{state}"', module, f"七展示态含 {state}")
        self.assertIn('export const ANSWER_SOURCE_NONE = "暂无官方答案";', module)
        self.assertIn('ai: "AI 解答（非官方）"', module)
        # 合同把「题目伪装官方答案」列为拒绝样本：只有 hasAnswer 才允许展开参考答案，
        # 否则一律落到「暂无官方答案」文案（诚实默认，不是隐藏文案）
        self.assertIn("if (item.hasAnswer && item.answerText) {", module)
        self.assertIn('card.append(el("p", "cr-answer-none", ANSWER_SOURCE_NONE));', module)
        # 覆盖度只报真实计数：结构性禁止百分比换算（对渲染文案的行为断言在
        # frontend_course_review_behavior.mjs，这里只钉住「没有换算代码」，
        # 不以关键词禁注释——源码钉连注释都红是已记录的教训）
        for forbidden in ("* 100", "toFixed", "percent", "mastery"):
            self.assertNotIn(forbidden, module, f"复习面不得出现掌握度换算 {forbidden}")
        self.assertIn("String(row.have)", module)
        # 路由固定为三个 view + 唯一致新动作
        for route in ("course-review?course_id=", "course-review/lecture?", "course-review/assessment?", '"course-review/actions"'):
            self.assertIn(route, module)
        self.assertNotIn("course-review/second", module)

    def test_course_review_is_installed_by_study_not_app(self):
        """组合根归学习页：app.js 不在本包路径内，且不新增落地门解除点。"""
        self.assertNotIn("course-review", self.app, "app.js 不安装复习面")
        self.assertIn('from "./course-review.js"', self.modules["study.js"])
        self.assertIn("installCourseReview(store)", self.modules["study.js"])
        self.assertIn("provideReviewNavigation", self.modules["study.js"])
        study = self.modules["study.js"]
        self.assertEqual(
            len(re.findall(r"studyLandingNavigated = true;", study)), 5,
            "启动落地门解除点仍恰好五处（不因复习面新增）",
        )
        # 复习面与课程布局互斥由学习页联动，且既有 drilldown 语义不变
        self.assertIn("if (layout) layout.hidden = reviewOpen;", study)
        self.assertIn("function syncDeskReturnLabel()", study)

    def test_course_review_bounds_requests_and_list_dom(self):
        """有界：讲次详情失败即止不重试；长列表分批渲染。"""
        module = self.modules["course-review.js"]
        self.assertIn("const LECTURE_BATCH = 8;", module)
        self.assertIn("const ASSESSMENT_BATCH = 25;", module)
        self.assertIn("const DETAIL_FETCH_LIMIT = 8;", module)
        self.assertIn("detailFailed", module, "永久失败的讲次不再反复重试")
        self.assertIn("export function reviewSlice(", module)
        self.assertIn("显示更多（还有", module)
        # 不引入虚拟列表/图表/框架依赖
        for forbidden in ("virtual", "IntersectionObserver", "chart", "d3", "three"):
            self.assertNotIn(forbidden, module, f"复习面不得引入 {forbidden}")

    def test_course_review_styles_stay_in_the_design_closed_set(self):
        """复习面样式只取既有 token：仅对本面的规则断言，不误伤同文件其他板块。"""
        pages = self.css_by_name["pages.css"]
        components = self.css_by_name["components.css"]
        for fragment in (".course-review", ".cr-tabs", ".cr-panel", ".cr-lecture", ".cr-question"):
            self.assertIn(fragment, pages + components)

        # 只取选择器属于本面的规则块（.cr-* / .course-review），逐块断言声明闭集
        blocks = [
            block for block in re.findall(r"([^{}]+)\{([^}]*)\}", pages + components)
            if re.match(r"^\s*\.(cr-|course-review)", block[0])
        ]
        self.assertGreaterEqual(len(blocks), 12, "复习面样式规则块数量合理")
        joined = "\n".join(f"{sel}{{{body}}}" for sel, body in blocks)
        for forbidden in ("box-shadow", "gradient", "999px", "#"):
            self.assertNotIn(forbidden, joined, f"复习面样式不得出现 {forbidden}")
        for token in ("var(--radius)", "var(--space-", "var(--muted)", "var(--navy-ink)"):
            self.assertIn(token, joined, f"复习面样式应走既有 token {token}")
        self.assertIn("@media (max-width: 640px)", pages, "窄屏断点存在")
        self.assertIn("text-overflow: ellipsis", pages, "长标题截断而非撑破容器")

    def test_app_is_a_small_composition_entrypoint(self):
        self.assertLessEqual(len(self.app.splitlines()), 300)
        for module in (
            "store", "ui", "shell", "study", "player-core", "live-room", "live-page",
            "tasks-drawer", "search-palette", "timetable", "home-overview", "settings", "onboarding",
        ):
            self.assertIn(f'from "./modules/{module}.js"', self.app)
        # 台账 §4 冻结：app.js 不在 D 包路径内；顶栏更新小组件与数据管理页由
        # settings.js 作为组合根安装（settings 是 app.js 导入的既有模块）。
        self.assertNotIn("update-widget", self.app)
        self.assertNotIn("course-data", self.app)
        settings = self.modules["settings.js"]
        self.assertIn('from "./update-widget.js"', settings)
        self.assertIn('from "./course-data.js"', settings)
        self.assertNotIn("fetch(", self.app)

    def test_onboarding_guide_entries_and_page_exist(self):
        # 页面与手动入口：账户菜单、设置帮助（学习空态按钮已随视觉批 4 移出——
        # D1-design v2 D12，入口保留在账户菜单与设置，可达性不回退）；
        # 设置返回引导按钮默认隐藏
        self.assertIn('id="onboarding-page" class="page" data-page="onboarding" hidden', self.html)
        for element_id in (
            "onboarding-progress", "onboarding-skip", "onboarding-toc", "onboarding-prev",
            "onboarding-next", "onboarding-summary", "onboarding-live", "onboarding-pending",
            "onboarding-open-login", "onboarding-auth-retry", "onboarding-catalog-refresh",
            "onboarding-catalog-retry", "onboarding-dismiss-recovery", "onboarding-dismiss-retry",
            "onboarding-dismiss-leave", "onboarding-complete-recovery", "onboarding-complete-retry",
            "account-menu-onboarding", "help-open-guide", "settings-return-guide",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertNotIn('id="study-open-guide"', self.html)
        self.assertIn('id="settings-return-guide" class="btn-quiet" type="button" hidden', self.html)
        self.assertIn('setAttribute("aria-current", "step")', self.modules["onboarding.js"])
        self.assertNotIn("localStorage", self.modules["onboarding.js"])
        self.assertNotIn(
            'type="password"',
            self.html[self.html.index('id="onboarding-page"'):self.html.index("</main>")],
        )
        onboarding = self.modules["onboarding.js"]
        self.assertIn('postV3("onboarding/actions", { action: "mark-opened", version: GUIDE_VERSION })', onboarding)
        self.assertIn('courselens:open-login', onboarding)
        self.assertIn('$("refresh-catalog")?.click()', onboarding)
        # 移动端触达：引导页按钮在 ≤719px 下 ≥44px（coarse 指针由 layout.css 全局规则覆盖）
        self.assertIn("#onboarding-page button { min-height: 44px; }", self.css)
        self.assertIn(
            'if (!initiatedByGuide) closeGuide();',
            onboarding,
        )

    def test_onboarding_step4_proxy_card_contract(self):
        # PROXY-AUTODETECT-1：步骤 4 并入代理卡——闭集自动检测（settings/actions
        # detect-proxy）+ 检测失败手填引导 + 保存走既有 update-network 管道；
        # TOTAL_STEPS=5 冻结结构不变（不加新步骤）；SOCKS 由闭集文案本地拒绝。
        onboarding = self.modules["onboarding.js"]
        self.assertIn('postV3("settings/actions", { action: "detect-proxy" })', onboarding)
        self.assertIn('action: "update-network"', onboarding)
        self.assertIn("只支持 HTTP 或 HTTPS 代理地址。", onboarding)
        self.assertIn("Clash 默认 7890", onboarding)
        self.assertIn('input.id = "onboarding-proxy-input"', onboarding)
        self.assertIn('querySelector(".onboarding-optional-rows")', onboarding)
        self.assertIn("const TOTAL_STEPS = 5;", onboarding)

    def test_pb1_onboarding_visual_hierarchy_pins(self):
        """PB-1 A1/A2/A3 源码锚：向导主次恢复（下一步/完成引导恒 primary、
        跳过引导降 text-button 次级）、步骤 4 能力行三列网格文本左对齐、
        目录走过步带 ✓ 完成态（当前步 aria-current 高亮保持）。"""
        onboarding = self.modules["onboarding.js"]
        self.assertIn('nextButton.classList.add("btn-primary")', onboarding)
        self.assertNotIn('classList.toggle("btn-primary"', onboarding)
        self.assertIn('button.classList.toggle("is-done", step < currentStep)', onboarding)
        self.assertIn('id="onboarding-skip" class="text-button" type="button"', self.html)
        pages = self.css_by_name["pages.css"]
        row_block = pages.split(".onboarding-optional-row {", 1)[1].split("}", 1)[0]
        self.assertIn("display: grid;", row_block)
        self.assertIn("grid-template-columns: auto minmax(0, 1fr) auto;", row_block)
        self.assertNotIn("justify-content: space-between", row_block)
        self.assertIn(
            "#onboarding-proxy-row { grid-template-columns: auto minmax(0, 1fr) auto auto; }",
            pages,
        )
        self.assertIn(
            ".onboarding-optional-row span { min-width: 0; color: var(--ink);"
            " overflow-wrap: anywhere; text-align: left; }",
            pages,
        )
        self.assertIn(
            '.onboarding-toc button.is-done::after { content: "✓"; margin-left: 8px; font-weight: 600; color: var(--gold); }',
            pages,
        )

    def test_pb1_a4_topbar_icon_color_pins(self):
        """PB-1 A4 源码锚：顶栏图标钮（主题/更新/直播/任务）统一墨色，
        accent 色只留给状态点/徽标；悬停提亮统一 navy-soft。"""
        layout = self.css_by_name["layout.css"]
        pages = self.css_by_name["pages.css"]
        theme_block = layout.split(".theme-toggle {", 1)[1].split("}", 1)[0]
        self.assertIn("color: var(--ink);", theme_block)
        self.assertNotIn("navy-ink", theme_block)
        self.assertIn(".theme-toggle:hover:not(:disabled) { background: var(--navy-soft); }", layout)
        update_block = pages.split(".update-toggle {", 1)[1].split("}", 1)[0]
        self.assertIn("color: var(--ink);", update_block)
        self.assertIn(".update-toggle:hover:not(:disabled) { background: var(--navy-soft); }", pages)
        # 直播/任务原本即墨色，钉住不回退
        self.assertIn(
            "color: var(--ink);",
            self.css_by_name["live.css"].split(".live-entry {", 1)[1].split("}", 1)[0],
        )
        self.assertIn(
            ".status-capsule > .btn-tasks { background: transparent; color: var(--ink); }",
            layout,
        )
        # 更新状态色仍在徽标点（update-dot）而非图标本体；深色主题 gold 信号保留
        self.assertIn(
            '.update-toggle[data-update-state="available"] .update-dot',
            self.css_by_name["components.css"],
        )
        self.assertIn('html[data-theme="dark"] .theme-toggle svg', layout)

    def test_pb1_data_page_polish_pins(self):
        """PB-1 A6/A7/A8 源码锚：明细区空态一行人话（零课程沿用「暂无课程数据」，
        不误导学生选不存在的课）、批量条动作组独占一行、课程行相对时间+悬浮绝对时间。"""
        course_data = self.modules["course-data.js"]
        self.assertIn(
            'import { $, clear, closeOverlay, formatRelativeTime, formatTime,'
            ' operationId, textElement } from "./ui.js";',
            course_data,
        )
        self.assertIn('detailEmpty: "选择左侧课程查看数据明细"', course_data)
        self.assertIn("if (wideLayout() && !selectedCourseId) renderDetailEmptyState(rows.length > 0);", course_data)
        self.assertIn("hasRows ? DATA_TEXT.detailEmpty : DATA_TEXT.empty", course_data)
        self.assertIn("formatRelativeTime(updated)", course_data)
        self.assertIn("`更新于 ${formatTime(updated)}`", course_data)
        pages = self.css_by_name["pages.css"]
        actions_block = pages.split(".data-bulk-actions {", 1)[1].split("}", 1)[0]
        self.assertIn("flex-basis: 100%;", actions_block)

    def test_pb1_settings_and_catalog_affordance_pins(self):
        """PB-1 A9/A10/A11 源码锚：周课表弹窗课表设置的起始日 date input 旁注
        系统格式一行（SIMPLIFY-AUDIT-1 S1 后唯一一处，设置页镜像副本已删）、
        帮助组两入口统一整行卡片形态（链接去下划线）、AS11 圆圈 title 与 aria 同源同态。"""
        html = self.html
        self.assertEqual(html.count('class="hint tt-date-format-hint"'), 1)
        self.assertIn('id="timetable-start-date"', html)
        self.assertNotIn('id="settings-timetable-start-date"', html)
        pages = self.css_by_name["pages.css"]
        self.assertIn(".tt-date-format-hint { flex-basis: 100%; margin: 0; }", pages)
        entry_block = pages.split(".help-entry-group .help-entry-row {", 1)[1].split("}", 1)[0]
        self.assertIn("text-decoration: none;", entry_block)
        study = self.modules["study.js"]
        self.assertIn("button.title = courseAutomationToggleLabel(course, state);", study)

    def test_pb1_key_bound_and_artifact_envelope_pins(self):
        """PB-1 C1/C3 源码锚：DeepSeek key 输入 200 上限+保存前 trim；
        artifacts 空载荷合同（200+artifact:null）下 artifact_not_found 前端
        零残留——码表键同笔删除，空态文案由 study.js 空载荷路径直接呈现。"""
        self.assertIn(
            'id="deepseek-key" type="password" autocomplete="off" maxlength="200"',
            self.html,
        )
        settings = self.modules["settings.js"]
        self.assertIn('const apiKey = String(keyInput.value || "").trim();', settings)
        self.assertIn("api_key: apiKey,", settings)
        api = self.modules["api.js"]
        self.assertNotIn("artifact_not_found", api)
        study = self.modules["study.js"]
        self.assertNotIn("artifact_not_found", study)
        self.assertIn(
            'target.append(textElement("p", "本讲总结尚未生成，字幕就绪后会自动整理出来", "empty-state"));',
            study,
        )

    def test_pb1_player_focus_visible_pin(self):
        """PB-1 D2/S8-3 源码锚：滑杆键盘焦点必须有 thumb 阴影之外的贴内缘
        outline 环兜底——Chromium 实测不渲染 :focus-visible 限定的 thumb 伪元素
        阴影（无兜底=键盘用户零指示）；兜底规则须在音量 outline:none 之后
        （同特异性级联后者胜）。"""
        pages = self.css_by_name["pages.css"]
        fallback = (
            ".player-ctrl-timeline:focus-visible,\n"
            ".player-ctrl-volume:focus-visible {\n"
            "  outline: 2px solid var(--stage-ink);\n"
            "  outline-offset: -2px;\n"
            "}"
        )
        self.assertIn(fallback, pages)
        self.assertGreater(
            pages.index(fallback),
            pages.index(".player-ctrl-volume:focus-visible { outline: none; }"),
            "兜底环必须晚于两处 outline:none 加载，否则被同特异性规则吃掉",
        )

    def test_study_empty_greeting_verse_signature_surface(self):
        # 视觉 Stage 2 批 4：学习空态 = 问候/诗联/出处/唯一主按钮（D1-design v2 +
        # 合同 §9.3）；empty-title/empty-hint 类样式保留供数据页自建 DOM 使用。
        greeting = self.modules["greeting.js"]
        study = self.modules["study.js"]
        onboarding = self.modules["onboarding.js"]
        course_data = self.modules["course-data.js"]
        self.assertIn('id="empty-greeting"', self.html)
        self.assertIn('id="empty-verse"', self.html)
        self.assertIn('id="empty-verse-source"', self.html)
        self.assertIn('id="study-start-select"', self.html)
        # NAV-HANG-1③：可访问名恒为「选择课程」——可见文案随 auth 态变
        # （登录后选择课程/重新登录并选择课程），自动化定位与读屏不漂移。
        self.assertIn(
            'id="study-start-select" class="empty-action" type="button" aria-label="选择课程"',
            self.html,
        )
        # LANDING-AESTHETIC-1（用户裁决）：诗页复原——主钮原样原位；「继续学习」
        # 降级为主钮下极轻弱化链（.continue-lecture 单文字节点）；挂起提示行退出
        # 着陆页（宿主=选课面板概览）。
        self.assertNotIn('id="study-actions"', self.html)
        self.assertNotIn('class="study-link"', self.html)
        landing_html = self.html[self.html.index('id="study-empty"'):self.html.index('id="study-select"')]
        self.assertNotIn("home-guide-resume", landing_html)
        self.assertIn("你好。", self.html)  # JS 未起时的静态兜底问候
        self.assertIn("纸上得来终觉浅，绝知此事要躬行。", self.html)  # 静态兜底诗联
        self.assertIn("换一句", self.html)
        self.assertNotIn("empty-title", self.html)
        self.assertNotIn("empty-hint", self.html)
        self.assertIn(".empty-title {", self.css)  # 数据页在用
        self.assertIn('"empty-title"', course_data)
        self.assertNotIn('study-open-guide', self.html + self.javascript)
        self.assertNotIn('$("study-start-select").textContent', study)  # 文案已移交 greeting
        # CSP script-src 'self' 禁内联：装配入口外置为 greeting-boot.js，index.html 仅引用
        self.assertIn('<script type="module" src="/modules/greeting-boot.js"></script>', self.html)
        boot = self.modules["greeting-boot.js"]
        self.assertIn('import { installGreeting } from "/modules/greeting.js"', boot)
        self.assertIn('import { store } from "/modules/store.js"', boot)
        # 主按钮三态 + degraded 扩展（总控裁决②）
        self.assertIn('BUTTON_TEXT_READY = "选择课程"', greeting)
        self.assertIn('BUTTON_TEXT_LOGIN = "登录后选择课程"', greeting)
        self.assertIn('BUTTON_TEXT_RESUMING = "正在恢复会话…"', greeting)
        self.assertIn('BUTTON_TEXT_DEGRADED = "重新登录并选择课程"', greeting)
        # 节日小集三元（总控裁决①）与课量语义尾句闭集（轻文言零数字）
        for pinned in ('"01-01", "元旦快乐。"', '"05-04", "青年节快乐。"', '"09-10", "教师节快乐。"',
                       '"今日课毕，辛苦了。"', '"今日课满，已过半。"', '"今日课毕。"', '"今日课满。"'):
            self.assertIn(pinned, greeting)
        self.assertNotIn("aria-live", greeting)  # 换句不播报（D1 §4）
        self.assertIn('fetch("/data/verses.json"', greeting)
        self.assertIn('"courselens:timetable-snapshot"', greeting)
        self.assertIn('setInterval(handleClockTick, 60000)', greeting)
        self.assertIn("visibilitychange", greeting)
        self.assertIn('classList.add("is-swapping")', greeting)
        self.assertIn('classList.remove("is-swapping")', greeting)
        self.assertIn("fnv1a", greeting)
        self.assertIn("deterministicVerseIndex", greeting)
        # 合同 §9.3 视觉参数：--font-display 首消费、28/600 vs 24/500、ui 轨出处
        # （GREETING-EMPTY-STATE-FIX-1 起空态签名面走 navy-ink；条目1合并起
        #   登录身份卡的衬线学号行加入 display 轨；FIRST-LOGIN-UX-2 起
        #   「继续学习」卡标题行加入 display 轨；LANDING-AESTHETIC-1 用户裁决起
        #   该链降级为极轻弱化链回归 ui 轨（诗=唯一宋体主角），计 3 处）
        self.assertEqual(self.css.count("font-family: var(--font-display);"), 3)
        # A11Y-IMPL-4：全站 font-size rem 化（÷16 精确小数），28px→1.75rem、24px→1.5rem
        self.assertIn("font-size: 1.75rem;", self.css)
        self.assertIn("font-size: 1.5rem;", self.css)
        self.assertIn(".empty-verse.is-swapping { opacity: 0; transform: translateY(-8px); pointer-events: none; }", self.css)
        self.assertIn(".empty-verse:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }", self.css)
        self.assertIn("transition: opacity 700ms cubic-bezier(0.2, 0, 0, 1), transform 700ms cubic-bezier(0.2, 0, 0, 1);", self.css)
        self.assertIn("@keyframes empty-in {", self.css)
        self.assertIn("animation: empty-in var(--motion) cubic-bezier(0.2, 0, 0, 1);", self.css)
        self.assertIn(".empty-verse:hover .empty-verse-hint,", self.css)
        # 语料缺席诚实 UI：禁用态提示退场、指针归位（置于 hover/focus 规则之后）
        self.assertIn(".empty-verse:disabled { cursor: default; }", self.css)
        self.assertIn(".empty-verse:disabled .empty-verse-hint { opacity: 0; }", self.css)
        # 375 窄窗诗联换行（V10-P2/R38 预演验证）：≤480px 解除按钮 nowrap、
        # 换句提示独立成行；桌面 nowrap 形态由顶部按钮闭集钉继续看护
        self.assertIn("@media (max-width: 480px) {", self.css)
        self.assertIn(".empty-verse { white-space: normal; }", self.css)
        self.assertIn(".empty-verse .empty-verse-hint { display: block; margin-top: 4px; }", self.css)
        self.assertIn("export function stripTrailingPeriod", greeting)
        # CJK 字距 rider：全 CSS 仅剩 .wordmark 0.01em（Latin）与 .device-code-value
        # 0.12em（mono 验证码例外，注释在场）；顶栏对齐 §6 --surface。
        for banned in ("letter-spacing: 0.03em", "letter-spacing: 0.02em", "letter-spacing: 0.04em",
                       "letter-spacing: 0.05em", "letter-spacing: 0.06em", "letter-spacing: 0.08em",
                       "letter-spacing: 0.1em"):
            self.assertNotIn(banned, self.css)
        self.assertIn("letter-spacing: 0.12em;", self.css)
        self.assertIn("等宽轨验证码例外", self.css)
        layout = (FRONTEND / "styles" / "layout.css").read_text(encoding="utf-8")
        self.assertIn("letter-spacing: 0.01em;", layout)  # .wordmark Latin 字标合规保留
        topbar_block = layout.split(".topbar {", 1)[1].split("}", 1)[0]
        self.assertIn("background: var(--surface);", topbar_block)  # 合同 §6 顶栏
        self.assertNotIn("background: var(--canvas);", topbar_block)

    def test_verses_asset_contract(self):
        # 语料资产闭集：chinese-poetry（MIT）离线管线产物；schema {id,text,author,
        # work,dynasty,tags,len}；条数 1800–2200、长度 8–24、id/text 无重复、归属在场。
        import json as json_module
        verses_path = FRONTEND / "data" / "verses.json"
        raw = verses_path.read_text(encoding="utf-8")
        payload = json_module.loads(raw)
        self.assertIn("© JackeyGao", payload["attribution"])
        self.assertIn("MIT", payload["attribution"])
        self.assertEqual(payload["license"], "MIT")
        verses = payload["verses"]
        self.assertGreaterEqual(len(verses), 1800)
        self.assertLessEqual(len(verses), 2200)
        self.assertEqual(payload["count"], len(verses))
        seen_ids = set()
        seen_texts = set()
        cjk_pattern = re.compile(r"[\u4e00-\u9fff]")
        for verse in verses:
            self.assertEqual(set(verse), {"id", "text", "author", "work", "dynasty", "tags", "len"})
            cjk_count = len(cjk_pattern.findall(verse["text"]))
            self.assertEqual(verse["len"], cjk_count)
            self.assertGreaterEqual(cjk_count, 8)
            self.assertLessEqual(cjk_count, 24)
            self.assertTrue(verse["author"])
            self.assertTrue(verse["work"])
            self.assertNotIn(verse["id"], seen_ids)
            self.assertNotIn(verse["text"], seen_texts)
            seen_ids.add(verse["id"])
            seen_texts.add(verse["text"])

    def test_verse_pool_copy_states_no_exaggerated_size(self):
        # C10-B 口径钉：诗库实为约 2000 条（1800–2200 资产钉见上）。
        # 用户可见面禁止夸大诗库规模的宣称；禁用词为闭集，出现任一即失败。
        banned = ("万级", "万首", "数千首", "上万首", "海量诗")
        surfaces = [
            FRONTEND / "modules" / "greeting.js",
            FRONTEND / "index.html",
            ROOT / "README.md",
        ]
        surfaces.extend(sorted((ROOT / "docs").rglob("*.md")))
        for path in surfaces:
            text = path.read_text(encoding="utf-8")
            for word in banned:
                self.assertNotIn(word, text, f"{path.name} 出现失实诗库规模宣称: {word}")

    def test_element_ids_follow_the_zone_element_scheme(self):
        ids = re.findall(r'id="([^"]+)"', self.html)
        self.assertGreaterEqual(len(ids), 60)
        for element_id in ids:
            self.assertRegex(element_id, r"^[a-z0-9]+(-[a-z0-9]+)*$")

    # ---- topbar: wordmark, Ctrl K, conn cluster, tasks, account ----

    def test_topbar_has_wordmark_search_conn_tasks_account(self):
        self.assertIn('id="wordmark"', self.html)
        self.assertIn('data-goto-study', self.html)
        self.assertIn('id="search-trigger"', self.html)
        self.assertIn("Ctrl K", self.html)
        self.assertNotIn("⌘", self.html)
        self.assertIn('id="conn-status"', self.html)
        self.assertIn('id="task-chip"', self.html)
        self.assertIn('id="account-button"', self.html)
        self.assertIn('id="topbar-crumbs"', self.html)

    def test_skip_link_and_bulk_group_a11y_contract(self):
        """A11Y-IMPL-2（D14 审计 P2-4/P3-3）：①skip link 是 body 首个元素
        （Tab 序第一停点，免穿顶栏 9 控件），指向已 tabindex="-1" 可直接
        落焦的 main；accessibility.css 里基态视觉隐藏、聚焦显形，显形位置
        钉在顶栏正下方（--topbar-height 唯一源 tokens.css）且 z 高于全站
        最高浮层（.toast-region 60）。②数据页批量操作分组 span 全部带
        role="group"——span 上只有 aria-label 而无 role 时读屏会忽略该名。"""
        head, sep, _ = self.html.partition('<header class="topbar">')
        self.assertTrue(sep, "topbar 起始标记漂移")
        self.assertIn('<a class="skip-link" href="#workspace-main">跳到主内容</a>', head)
        self.assertIn('<main id="workspace-main" tabindex="-1">', self.html)
        a11y_css = self.css_by_name["accessibility.css"]
        self.assertIn(".skip-link {", a11y_css)
        self.assertIn("clip: rect(0 0 0 0)", a11y_css.split(".skip-link {", 1)[1].split("}", 1)[0])
        reveal = a11y_css.split(".skip-link:focus-visible {", 1)[1].split("}", 1)[0]
        self.assertIn("clip: auto", reveal)
        self.assertIn("top: calc(var(--topbar-height) + 8px)", a11y_css)
        self.assertIn("z-index: 70", a11y_css)
        groups = re.findall(
            r'<span class="data-action-group" data-bulk-group="([a-z]+)" role="group" aria-label="([^"]+)">',
            self.html,
        )
        # 审计时 3 处，「选择管理」manage 组为审计后新增，同类缺陷一并补齐=4 处
        self.assertEqual(groups, [
            ("safe", "安全操作"), ("space", "释放空间"),
            ("irreversible", "不可逆操作"), ("manage", "选择管理"),
        ])

    def test_connection_cluster_four_states_with_text_and_shape(self):
        shell = self.modules["shell.js"]
        # CLOUD-CONSENT-AUTO-1 U3b：云段退役——页眉/连接菜单只剩 fudan/github
        # 两段（2 header dots + 2 popover dots），云自动化状态不再入页眉。
        self.assertEqual(self.html.count('data-conn-dot'), 4)
        self.assertIn('data-conn-dot="fudan"', self.html)
        self.assertIn('data-conn-dot="github"', self.html)
        self.assertNotIn('data-conn-dot="cloud"', self.html)
        self.assertNotIn("data-cloud-conn-row", self.html)
        for state in ("ready", "checking", "action", "error"):
            self.assertIn(f'conn-dot[data-state="{state}"]', self.css)
        self.assertIn('conn-dot[data-state="error"]::after', self.css)
        self.assertIn('content: "!"', self.css)
        self.assertIn('border: 2px solid var(--gold)', self.css)
        self.assertIn('data-conn-text="fudan"', self.html)
        self.assertIn('data-conn-text="github"', self.html)
        self.assertIn("apiV3(\"remote-connection\")", shell)
        self.assertNotIn("setInterval", shell.split("function renderConn")[1].split("function updateFudanConn")[0])
        self.assertNotIn("EventSource", shell)
        self.assertIn('store.set("auth", auth)', shell)
        self.assertIn("apiV3(\"remote-connection\")", shell)

    def test_connection_popover_semantics_and_actions(self):
        self.assertIn('id="conn-menu"', self.html)
        self.assertIn('aria-controls="conn-menu"', self.html)
        self.assertIn('aria-expanded="false"', self.html)
        self.assertIn('id="conn-live"', self.html)
        self.assertNotIn("data-conn-goto", self.html)
        self.assertNotIn("去设置", self.html + self.javascript)
        shell = self.modules["shell.js"]
        self.assertIn("renderConn()", shell)
        self.assertIn('querySelectorAll("[data-open-conn]").forEach', shell)

    def test_connection_popover_merges_campus_into_fudan_card(self):
        """AS4-U2b（第四十四案）：校园连接卡降为复旦卡的网络副行——健康时 shell
        置 hidden 整块收起，弹层只讲「复旦课程平台」与「GitHub 远程连接」；
        独立校园卡头（H3+pill）退役；页眉/弹层四颗点语义不变。"""
        self.assertIn('id="fudan-connection"', self.html)
        self.assertIn('class="conn-card"', self.html)
        self.assertRegex(self.html, r'id="campus-connection"[^>]*hidden')
        self.assertNotIn("campus-connection-title", self.html)
        self.assertNotIn("campus-connection-pill", self.html)
        self.assertEqual(self.html.count('data-conn-dot'), 4)
        shell = self.modules["shell.js"]
        self.assertIn('root.hidden = view.state === "ready"', shell)
        layout = (FRONTEND / "styles" / "pages.css").read_text(encoding="utf-8")
        self.assertIn(".campus-connection[hidden] { display: none; }", layout)

    def test_fudan_conn_breathing_contract(self):
        """AS4-U2（第四十四案）：复旦会话登录中的呼吸提示——shell 在 checking
        （含 restoring）期挂 conn-breathing、其余态摘除；文案「复旦会话登录中…」；
        CSS 约 1.6s 脉动且 reduced-motion 退化为静态点。"""
        shell = self.modules["shell.js"]
        self.assertIn('"复旦会话登录中…"', shell)
        self.assertIn('"conn-breathing"', shell)
        self.assertIn('dot.dataset.connDot === "fudan" && connState.fudan === "checking"', shell)
        layout = (FRONTEND / "styles" / "layout.css").read_text(encoding="utf-8")
        self.assertRegex(layout, r"\.conn-dot\.conn-breathing\s*\{[^}]*animation:\s*conn-breath 1\.6s")
        self.assertRegex(
            layout,
            r"@media \(prefers-reduced-motion: reduce\)\s*\{\s*\.conn-dot\.conn-breathing\s*\{[^}]*animation:\s*none",
        )

    def test_study_catalog_loads_during_session_restore(self):
        """AS4-U1（第四十四案）：恢复期目录先行——auth 订阅 checking 分支与挂载
        都发出单发探测（restoreProbe），缓存课程随 checking 信封上屏；ready 后仍
        走既有完整 load 换新；探测不进重试/超时降级。"""
        study = self.modules["study.js"]
        self.assertIn("void load({ restoreProbe: true })", study)
        self.assertIn("restoreProbe = false", study)
        self.assertIn("正在登录复旦账号，先显示上次缓存的课程。", self.modules["ui.js"])
        self.assertIn("正在登录复旦账号，课程将在登录完成后显示。", study)

    def test_github_state_maps_overall_state_only(self):
        shell = self.modules["shell.js"]
        for state in ("ready", "checking", "action_required", "degraded"):
            self.assertIn(state, shell)
        self.assertIn('state === "action_required" || state === "degraded"', shell)

    def test_github_card_shows_authorized_login_from_closed_sources(self):
        """INSTALL-STEP-UX-1 交付 1：卡面头部显示当前授权账号——login 只取
        authorization 组件 evidence 既有字段与最近一次后端结果披露值；两个闭集
        来源都缺失时如实显示「账号未知」，绝不发明值，形状之外脏值按未知处理。"""
        settings = self.modules["settings.js"]
        self.assertIn("function renderGithubAccountLine(value)", settings)
        self.assertIn('line.textContent = login ? `当前 GitHub 账号：${login}` : "当前 GitHub 账号：账号未知";', settings)
        self.assertIn("const GITHUB_LOGIN_RE = /^[A-Za-z0-9-]{1,39}$/;", settings)
        self.assertIn("function remoteAuthorizedLogin(value)", settings)
        self.assertIn('const authorization = remoteComponentByName(value, "authorization");', settings)
        self.assertIn("if (GITHUB_LOGIN_RE.test(disclosedLogin)) remoteKnownGithubLogin = disclosedLogin;", settings)
        # 账号行挂在连接卡头部（状态徽标父节点），不在证据区占行
        self.assertIn('$("github-connection-state")?.parentElement', settings)
        self.assertIn('line.id = "github-account-line";', settings)

    def test_install_guidance_block_state_machine_and_single_auto_open(self):
        """INSTALL-STEP-UX-1 交付 3：安装引导块状态机 + 每个 setup URL 恰自动
        打开一次（模块级去重、弹出拦截退化为按钮不重试）；链接渲染仍只走
        trustedRemoteInstallLink 闭集正则，scope_not_exact 既有路径不变。"""
        settings = self.modules["settings.js"]
        self.assertIn("function remoteInstallGuidanceState(value)", settings)
        self.assertIn('if (String(installation.code || "") !== "installation_missing") return null;', settings)
        self.assertIn("if (!link && !remoteInstallGuidanceArmed) return null;", settings)
        self.assertIn("INSTALLATION_SETUP_URL_RE.test(evidence.installation_setup_url)", settings)
        self.assertIn("const remoteInstallAutoOpenedUrls = new Set();", settings)
        self.assertIn("if (result.setup_state === \"awaiting_installation\") remoteInstallGuidanceArmed = true;", settings)
        self.assertIn("if (installedNow && String(installedNow.state || \"\") === \"ready\") {", settings)
        self.assertIn("remoteInstallAutoOpenedUrls.add(autoOpenUrl);", settings)
        self.assertIn("window.open(autoOpenUrl, \"_blank\", \"noopener,noreferrer\");", settings)
        # 自动打开只发生在去重集合未收录该 URL 时（恰一次）；异常静默退化为按钮；
        # 账号级兜底链接（T2）与预选 setup URL 同一去重语义
        self.assertIn("if (autoOpenUrl && !remoteInstallAutoOpenedUrls.has(autoOpenUrl)) {", settings)
        # 引导块文案为闭集：说明这一步是什么、为什么安全、装完点哪个按钮
        self.assertIn("需要在 GitHub 确认安装 CourseLens App", settings)
        self.assertIn("不会触及你的其他仓库", settings)
        self.assertIn("回到这里点击「完成 App 安装后继续初始化」", settings)

    def test_install_missing_primary_button_merges_with_guidance_semantics(self):
        """INSTALL-STEP-UX-1 交付 4：安装缺失且有受信链接时主按钮 =「完成 App
        安装后继续初始化」（bootstrap 动作 + 受信链接），与 permission_denied
        非身份端的既有引导语义合并；旧标签退役。"""
        settings = self.modules["settings.js"]
        self.assertIn('? { action: "bootstrap", label: "完成 App 安装后继续初始化", withInstallLink: true }', settings)
        self.assertNotIn("我已完成安装，继续初始化", settings)

    def test_remote_actions_auto_reconcile_once_after_bootstrap_and_authorized_poll(self):
        """FIRST-RUN-SMOOTH-1 交付 4：poll 授权确认 / bootstrap 成功后恰一次自动
        对账（复用 diagnose 动作管道）再刷新卡面——消灭「动作成功、卡面 stale、
        让用户自点诊断」的悬空态；diagnose 自身不再触发（无递归），也不借显式
        诊断的上下文前缀与 toast；探测失败静默退化为既有 loadRemote 刷新。"""
        settings = self.modules["settings.js"]
        self.assertIn("async function remoteAutoReconcileOnce()", settings)
        helper = settings[
            settings.index("async function remoteAutoReconcileOnce()"):
            settings.index("async function remoteAction")
        ]
        self.assertIn('action: "diagnose"', helper, "自动对账复用 diagnose 动作管道")
        self.assertNotIn("toast(", helper, "自动对账不发人话反馈（与显式诊断区分）")
        self.assertNotIn("remoteEvidenceContextPending", helper)
        body = settings[settings.index("async function remoteAction"):]
        self.assertIn(
            '(action === "poll-authorization" && result.state === "authorized")',
            body,
        )
        self.assertIn(
            '(action === "bootstrap" && operationState !== "failed")',
            body,
        )
        # 单元D 自愈重试分支自带早退 loadRemote；成功主路径仍是先对账后刷新
        success_body = body[body.index("} else if (ASYNC_FEEDBACK_ACTIONS.has(action)) {"):]
        self.assertLess(
            success_body.index("await remoteAutoReconcileOnce();"),
            success_body.index("await loadRemote();"),
            "先同步探测一次，再刷新卡面",
        )

    def test_install_missing_guidance_copy_matches_trusted_link_presence(self):
        """FLOW-ORDER-FIX-1 单元①（文案一致）：installation_missing 指引按仓库
        存在证据三选一——仓库未建走装 App 前置指引（All repositories + 收紧预告），
        仓库已在走既有安装语义；旧序「先创建仓库再安装」文案全部退役。"""
        settings = self.modules["settings.js"]
        self.assertIn('if (code === "installation_missing") {', settings)
        self.assertIn("REMOTE_INSTALLATION_MISSING_INSTALL_FIRST_GUIDANCE", settings)
        self.assertIn("REMOTE_SCOPE_NOT_EXACT_PRE_REPOS_GUIDANCE", settings)
        self.assertIn(
            "仓库范围先选 All repositories（专属仓库尚未建立，无法精确指定）；创建完成后我们会引导你收紧到仅两个仓库",
            settings,
        )
        guidance_def = settings[
            settings.index("const REMOTE_INSTALLATION_MISSING_INSTALL_FIRST_GUIDANCE"):
            settings.index("const REMOTE_SCOPE_NOT_EXACT_PRE_REPOS_GUIDANCE")
        ]
        self.assertNotIn("预选", guidance_def, "装 App 前置指引绝不出现「已预选」")
        self.assertNotIn("先创建两个专属仓库", guidance_def)
        # 范围暂宽且仓库未建：指引=先建仓、后收紧（新序），与主按钮「创建专属仓库」同语义
        scope_pre_repos = settings[
            settings.index("const REMOTE_SCOPE_NOT_EXACT_PRE_REPOS_GUIDANCE"):
            settings.index("/* permission_denied 细分闭集")
        ]
        self.assertIn("先创建两个专属仓库", scope_pre_repos)
        self.assertIn("收紧到仅这两个仓库", scope_pre_repos)
        self.assertIn("REMOTE_ACTION_INSTALLATION_MISSING_INSTALL_FIRST_TEXT", settings)
        failure_def = settings[
            settings.index("const REMOTE_ACTION_INSTALLATION_MISSING_INSTALL_FIRST_TEXT"):
            settings.index("function remoteActionAuthorizationInvalid")
        ]
        self.assertNotIn("预选", failure_def, "缺装失败文案绝不出现「已预选」")
        self.assertNotIn("先创建两个专属仓库", failure_def)
        # 旧序文案整体退役：先创建仓库再安装的指引与失败变体不复存在
        self.assertNotIn("REMOTE_INSTALLATION_MISSING_PRE_REPOS_GUIDANCE", settings)
        self.assertNotIn("REMOTE_ACTION_INSTALLATION_MISSING_PRE_REPOS_TEXT", settings)
        self.assertLess(
            settings.index('if (normalized === "installation_missing" && !trustedRemoteInstallLink())'),
            settings.index("return (normalized && Object.hasOwn(REMOTE_ACTION_FAILURE_TEXT, normalized))"),
            "失败文案先按链接有无分派，再落既有闭集表",
        )

    def test_connection_flow_order_installs_app_before_bootstrap(self):
        """FLOW-ORDER-FIX-1 单元①（流程序重排）：正确学生路径=授权→装 App
        （All repositories）→建仓→收紧→初始化→通道测试。授权完成且安装缺失、
        仓库未建 → 主按钮=安装 App（受信账号级安装页）；已装（任意范围）且仓库
        未建 → 创建专属仓库；仓库已建且范围不精确 → 收紧引导（现状）。"""
        settings = self.modules["settings.js"]
        self.assertIn("function dedicatedRepositoriesExist(value)", settings)
        self.assertIn("function trustedRemoteAccountInstallUrl(value)", settings)
        self.assertIn(
            "if (accountUrl) return { installAccountUrl: accountUrl, label: \"安装 CourseLens App\" };",
            settings,
        )
        self.assertIn(
            "if (!dedicatedRepositoriesExist(value)) {\n      return { action: \"bootstrap\", label: \"创建专属仓库并完成初始化\" };",
            settings,
        )
        self.assertIn(
            '? { action: "bootstrap", label: "完成 App 安装后继续初始化", withInstallLink: true }',
            settings,
        )
        # 账号级安装页只认后端披露的闭集形态（快照顶层 installation_url 优先，
        # awaiting 证据收录值兜底），绝不本地发明
        self.assertIn("INSTALLATION_SETUP_ACCOUNT_URL_RE.test(disclosed)", settings)
        # 渲染：安装主按钮为主样式外链锚，不发远程动作请求
        self.assertIn('installAnchor.className = "button-link primary";', settings)
        self.assertIn("推荐安装锚不发远程动作请求", settings)
        # 阶段序重排：授权 → App 安装 → 专属仓库 → 加密通道
        labels_order = settings[
            settings.index("const GITHUB_PHASE_LABELS = Object.freeze({"):
            settings.index("const GITHUB_PHASE_STATE_TEXT")
        ]
        self.assertLess(
            labels_order.index('authorization: "账号授权"'), labels_order.index('installation: "App 安装"'),
        )
        self.assertLess(
            labels_order.index('installation: "App 安装"'), labels_order.index('repositories: "专属仓库"'),
        )
        # 新序下安装未就绪时仓库阶段保持「待进行」，不与安装主按钮抢焦点
        self.assertIn(
            '["installation_present", "installation_scope_not_exact"].includes(String(installation.code || ""))',
            settings,
        )

    def test_permission_denied_prescription_routes_by_installation_evidence(self):
        """FLOW-ORDER-FIX-1 单元①（处方分流）：permission_denied 失败且快照携带
        缺装证据（installation_missing / repos_denied）→ 处方改为「请先安装
        CourseLens App」，绝不引导重新授权；授权组件确已失效时保留重新授权处方。"""
        settings = self.modules["settings.js"]
        self.assertIn("function remoteActionAuthorizationInvalid()", settings)
        self.assertIn("function remoteActionInstallMissingEvidence()", settings)
        self.assertIn("REMOTE_ACTION_INSTALL_FIRST_PRESCRIPTION", settings)
        self.assertIn(
            "请先安装 CourseLens App：点击「安装 CourseLens App」打开官方安装页，仓库范围先选 All repositories；安装完成后再回到客户端继续初始化。",
            settings,
        )
        routing = settings[
            settings.index("function remoteActionFailureText"):
            settings.index("// ---- Mailbox 历史记录修复")
        ]
        self.assertIn("if (remoteActionInstallMissingEvidence()) {", routing)
        self.assertLess(
            routing.index("if (remoteActionInstallMissingEvidence()) {"),
            routing.index("const endpointClass = remoteActionEndpointClass();"),
            "缺装处方优先于端点类文案表",
        )
        # 缺装处方文案（字符串常量本体）绝不出现「重新授权」字样（含否定句式）
        prescription_line = settings[settings.index("const REMOTE_ACTION_INSTALL_FIRST_PRESCRIPTION ="):]
        prescription_line = prescription_line[:prescription_line.index('";') + 2]
        self.assertNotIn("重新授权", prescription_line)

    def test_async_action_feedback_and_rotate_keys_wiring(self):
        """FLOW-ORDER-FIX-1 单元②：bootstrap/rotate-worker-keys/test-channel/
        repair-worker 提交后进入进行中态（disabled+aria-busy+进行中文案）+ 有界
        轮询（2s 间隔、~120s 死线，复用 GET remote-connection 读模型缝）+ 终态
        恰一次通知；rotate-worker-keys 由 environment 组件证据渲染次级按钮 +
        一次确认弹层。INIT-PATH-POLISH-1 单元C：轮换按钮条件化上移——仅
        environment action_required 且缺钥清单非空时渲染，位置=卡面主按钮区
        下方次级动作，高级操作区不再常驻。"""
        settings = self.modules["settings.js"]
        self.assertIn('const ASYNC_FEEDBACK_ACTIONS = new Set(["bootstrap", "rotate-worker-keys", "test-channel", "repair-worker"]);', settings)
        self.assertIn("const REMOTE_ASYNC_POLL_INTERVAL_MS = 2000;", settings)
        self.assertIn("const REMOTE_ASYNC_POLL_DEADLINE_MS = 120000;", settings)
        self.assertIn("function beginRemoteActionFeedback(action, immediate = false)", settings)
        self.assertIn("function asyncActionSettled(value)", settings)
        self.assertIn("async function pollRemoteActionOutcome()", settings)
        # 进行中态随渲染重放：disabled + aria-busy +「进行中…」文案
        self.assertIn('primaryButton.setAttribute("aria-busy", "true");', settings)
        self.assertIn("REMOTE_ASYNC_PROGRESS_LABEL[remoteActionInProgress]", settings)
        # 点击即反馈（INIT-PATH-POLISH-1 单元B/发现⑩b）：动作入口立即进入
        # 进行中态 + 闭集首标签补位；终局失败两分支都解除进行中态
        self.assertIn("const REMOTE_ACTION_FIRST_STAGE_TEXT = \"正在确认连接状态\";", settings)
        self.assertIn("let asyncActionLocalStage = \"\";", settings)
        self.assertIn("function beginRemoteActionFeedback(action, immediate = false)", settings)
        self.assertIn("asyncActionLocalStage = immediate ? REMOTE_ACTION_FIRST_STAGE_TEXT : \"\";", settings)
        self.assertIn("beginRemoteActionFeedback(action, true);", settings)
        self.assertIn("if (remoteActionInProgress !== action) beginRemoteActionFeedback(action);", settings)
        self.assertIn("asyncActionLocalStage = \"\";", settings)
        self.assertEqual(settings.count("finishRemoteActionFeedback();"), 4, "终态/超时/两终局失败分支都收口进行中态")
        # 阶段行位置（单元B）：主按钮正下方、连接卡主区内（非折叠角标位）
        self.assertLess(
            self.html.index('id="remote-primary-action"'),
            self.html.index('id="remote-action-progress"'),
            "阶段行位于主按钮下方",
        )
        self.assertLess(
            self.html.index('id="remote-action-progress"'),
            self.html.index('id="github-auto-connect"'),
            "阶段行常驻连接卡主区",
        )
        # 乐观 toast 退役：异步动作改走反馈循环
        dispatch = settings[
            settings.index('if (operationState === "failed") {'):
            settings.index("/* 动作后自动对账恰一次")
        ]
        self.assertIn("} else if (ASYNC_FEEDBACK_ACTIONS.has(action)) {", dispatch)
        self.assertIn("beginRemoteActionFeedback(action);", dispatch)
        # 终态恰一次通知：失败闭集码 + 处方分流；超时诚实提示；刷新卡面
        poll = settings[
            settings.index("async function pollRemoteActionOutcome()"):
            settings.index("// ---- Mailbox 历史记录修复")
        ]
        self.assertIn('toast(REMOTE_ASYNC_TIMEOUT_TEXT, "checking")', poll)
        self.assertIn('toast(remoteActionFailureText(code), "error")', poll)
        self.assertIn("await loadRemote();", poll)
        # rotate 接线：environment 证据渲染 + 确认弹层复用既有动作
        self.assertIn("function remoteRotateAction(value)", settings)
        self.assertIn('return actions.includes("rotate-worker-keys") ? "rotate-worker-keys" : "";', settings)
        # 单元C 条件化：缺钥清单非空才可见（action_required 之外的第二道门）
        self.assertIn("if (!remoteComponentMissingSecrets(environment).length) return \"\";", settings)
        self.assertIn("rotateRow.hidden = !rotateAction || Boolean(remoteActionInProgress);", settings)
        self.assertIn(
            '$("remote-rotate-confirm").addEventListener("click", () => {\n    rotateDialog.close();\n    void remoteAction("rotate-worker-keys");\n  });',
            settings,
        )
        self.assertIn('id="remote-rotate-keys" type="button"', self.html)
        self.assertIn('id="remote-rotate-dialog"', self.html)
        self.assertIn("将重新生成 Worker 加密密钥", self.html)
        # 单元C 上移：轮换行位于卡面主按钮区下方（阶段行之后），高级操作区不残留
        self.assertLess(
            self.html.index('id="remote-action-progress"'),
            self.html.index('id="remote-rotate-row"'),
            "轮换行位于主按钮区下方",
        )
        self.assertLess(
            self.html.index('id="remote-rotate-row"'),
            self.html.index('<details class="connection-diagnostics">'),
            "轮换行不再常驻高级操作区",
        )

    def test_p58_settings_feedback_and_noise_reduction(self):
        """P58（第五十八/六十案）：①key 保存反馈显性化——toast 按「保存在本机」
        勾选分岔播报，状态行升级为 status-pill 确认条（闭集文案不漂移）；
        ②GitHub 卡诚实降噪——就绪态不再常驻加密测试主按钮（需要态才显示）、
        连接已验证时历史盘点未完成降级为中性一行、「启动时自动连接」补一行
        诚实用途说明；③轮换行重复 id 修复（实体并入主区，高级操作区零残留）。"""
        settings = self.modules["settings.js"]
        # U1：toast 分岔 + 确认条 tone（闭集文案逐字钉）
        self.assertIn(
            'toast(remember ? "DeepSeek Key 已更新，并已保存到本机" : "DeepSeek Key 已更新，仅本次启动有效", "ready");',
            settings,
        )
        self.assertIn('function setDeepseekSaveState(text, tone = "")', settings)
        self.assertIn('setDeepseekSaveState("已保存在本机", "ready")', settings)
        self.assertIn('setDeepseekSaveState("保存状态异常，本机保存不可用", "error")', settings)
        self.assertIn('setDeepseekSaveState("保存失败，请稍后重试", "error")', settings)
        self.assertIn('id="deepseek-save-state" class="status-pill deepseek-save-pill"', self.html)
        self.assertIn(".deepseek-save-pill { width: 100%; font-weight: 600; }", self.css)
        # U2①：mailbox 历史盘点未完成在连接已验证时降级为中性一行
        self.assertIn("MAILBOX_HISTORY_NEUTRAL_UNCHECKED_TEXT", settings)
        self.assertIn("历史记录本次未能检查（限流或超时），不影响正常使用。", settings)
        self.assertIn("function mailboxHistoryGuidanceText(value, history)", settings)
        self.assertIn(
            'if (code === "github_state_unknown" && String(value?.overall?.state || "") === "ready") {',
            settings,
        )
        self.assertIn("mailboxHistoryGuidanceText(value, history)", settings)
        # U2②：就绪态主按钮收起；需要态（required/legacy）仍经主映射显示加密测试
        self.assertIn('if (state === "ready") return null;', settings)
        self.assertIn("primary.hidden = true;", settings)
        self.assertIn('channel_test_required: { action: "test-channel", label: "加密测试" }', settings)
        self.assertIn('legacy_channel_test: { action: "test-channel", label: "加密测试" }', settings)
        # U2③：自动连接用途说明（与后端语义一致：只读授权确认，每启动一次）
        self.assertIn("github-auto-connect-purpose", self.html)
        self.assertIn("打开后：启动时自动在后台确认一次 GitHub 连接；连接正常时无区别。", self.html)
        # ③相邻缺陷修复：rotate 行重复 id 归一——实体唯一且在主区
        self.assertEqual(self.html.count('id="remote-rotate-row"'), 1, "轮换行 id 唯一（重复壳已删）")
        self.assertEqual(self.html.count('id="remote-rotate-keys"'), 1, "轮换按钮唯一")
        self.assertIn('<div id="remote-rotate-row" hidden>', self.html)

    def test_install_wait_detection_poll_focus_and_auto_advance(self):
        """FRONTEND-SMOOTH-1 单元A（终验发现①）：安装页锚点点出后卡面进入
        「等待安装…」态——3 秒有界轮询 + 5 分钟窗口 + 窗口聚焦立即重探；
        探针证据显示已安装（任意范围）即收口等待并自动推进主按钮到建仓
        （bootstrap 恰一次）；等待行随每次渲染重放；设置页锚点不在等待语义内。"""
        settings = self.modules["settings.js"]
        self.assertIn("const INSTALL_WAIT_POLL_MS = 3000;", settings)
        self.assertIn("const INSTALL_WAIT_WINDOW_MS = 300000;", settings)
        self.assertIn('const INSTALL_WAIT_TEXT = "等待你在 GitHub 完成安装…安装完成后会自动继续";', settings)
        # 自动推进自述（INIT-PATH-POLISH-1 单元B/发现⑩）：闭集文案双通道播报
        # COPY-SWEEP 20261007：破折号改括号（X3 指纹），「已检测到…已安装」双「已」修一
        # WAIT-UX-1：bootstrap 预期窗按 R4 实测分布收准（20.5s 实测锚，区间半分钟到两分钟）
        self.assertIn(
            'const INSTALL_ADVANCE_TEXT = "检测到 CourseLens App 已安装，自动继续初始化（约需半分钟到两分钟）";',
            settings,
        )
        self.assertIn("installWaitAdvanceAnnounced = true;", settings)
        self.assertIn("if (installWaitAdvanced && installWaitAdvanceAnnounced && remoteActionInProgress) {", settings)
        self.assertIn("line.textContent = INSTALL_ADVANCE_TEXT;", settings)
        # 「已安装（任意范围）」闭集判定：与阶段行同一份探针证据
        self.assertIn("function installationEstablished(value)", settings)
        self.assertIn('code === "installation_present"', settings)
        self.assertIn('code === "installation_scope_not_exact"', settings)
        self.assertIn("async function installWaitPollTick()", settings)
        self.assertIn("function beginInstallWait()", settings)
        self.assertIn("async function autoAdvanceAfterInstallDetected()", settings)
        self.assertIn('await remoteAction("bootstrap");', settings)
        self.assertIn("if (installWaitAdvanced || remoteActionInProgress) return;", settings)
        # 安装/范围锚点统一等待触发（单元A+单元D）：安装页锚点→安装等待，
        # 范围调整页锚点（settings_url 形态）→收紧等待
        self.assertIn("function attachRemoteWaitTrigger(anchor, href)", settings)
        self.assertIn("attachRemoteWaitTrigger(installAnchor, recommended.installAccountUrl);", settings)
        self.assertIn("attachRemoteWaitTrigger(guidanceAnchor, state.link.href);", settings)
        self.assertIn("attachRemoteWaitTrigger(linkAnchor, link.href);", settings, "主按钮与错误区范围锚点同挂收紧等待")
        self.assertIn("if (INSTALLATION_SETTINGS_URL_RE.test(target)) {", settings)
        self.assertIn("!INSTALLATION_SETUP_URL_RE.test(target) && !INSTALLATION_SETUP_ACCOUNT_URL_RE.test(target)", settings)
        # 窗口聚焦立即重探 + 卸载清理
        self.assertIn("handleWindowFocusForInstallWait", settings)
        self.assertIn('window.addEventListener("focus", handleWindowFocusForInstallWait);', settings)
        self.assertIn('window.removeEventListener("focus", handleWindowFocusForInstallWait);', settings)
        self.assertIn("clearTimeout(installWaitTimer);", settings)
        # 等待行真实节点在连接卡内（status 语义 + 默认隐藏）
        self.assertIn(
            '<p id="remote-install-wait" class="hint" role="status" aria-live="polite" hidden></p>',
            self.html,
        )
        # 渲染重放：renderRemote 每拍同步等待行
        self.assertIn("renderInstallWait(); /* 等待安装行随每次渲染重放（单元A） */", settings)

    def test_tighten_wait_completion_summary_and_catalog_rename(self):
        """INIT-PATH-POLISH-1 单元D：收紧等待对称——范围调整页锚点点出后进入
        「等待收紧…」轮询（复用安装等待节拍/窗口与单元A新鲜语义），探针证据
        显示安装范围已精确即自述+自动推进初始化（bootstrap 恰一次），焦点重探
        对称，消灭最后一次手动诊断点击；全绿完成总结态红→绿沿一次性登场、
        确认后归档为常规连接卡；catalog 诊断按钮对齐「复制连接诊断」。"""
        settings = self.modules["settings.js"]
        # 收紧等待闭集文案 + 状态机与安装等待同族（复用节拍/窗口/新鲜语义）
        self.assertIn('const TIGHTEN_WAIT_TEXT = "等待你在 GitHub 收紧安装范围…收紧后会自动继续";', settings)
        self.assertIn('const TIGHTEN_ADVANCE_TEXT = "检测到安装范围已收紧，自动继续初始化（约需半分钟到两分钟）";', settings)
        self.assertIn("function installationScopeExact(value)", settings)
        self.assertIn('String(installation.code || "") === "installation_present"', settings)
        self.assertIn("async function tightenWaitPollTick()", settings)
        self.assertIn("await loadRemoteFresh(); /* 与安装等待同享等待窗新鲜语义（单元A） */", settings)
        self.assertIn("function beginTightenWait()", settings)
        self.assertIn("async function autoAdvanceAfterTightenDetected()", settings)
        self.assertIn('await remoteAction("bootstrap");', settings)
        self.assertIn("if (tightenWaitAdvanced || remoteActionInProgress) return;", settings)
        # 状态行真实节点 + 渲染重放 + 焦点重探对称 + 卸载清理对称
        self.assertIn(
            '<p id="remote-tighten-wait" class="hint" role="status" aria-live="polite" hidden></p>',
            self.html,
        )
        self.assertIn("renderTightenWait(); /* 收紧等待行随每次渲染重放（单元D） */", settings)
        self.assertIn("if (tightenWaitActive() && !tightenWaitAdvanced) void tightenWaitPollTick();", settings)
        self.assertIn("clearTimeout(tightenWaitTimer); /* 收紧等待与安装等待同款卸载清理（单元D） */", settings)
        # 完成总结态：闭集文案 + 沿触发一次性登场 + 确认归档
        self.assertIn('title: "初始化完成",', settings)
        self.assertIn('body: "两个专属仓库已就绪，加密通道已验证。",', settings)
        self.assertIn('next: "现在回到课程页即可开始学习。",', settings)
        self.assertIn('confirm: "知道了",', settings)
        # CLOUD-CONSENT-AUTO-1 U2：完成总结态次级入口随开关退役——总结只留「知道了」，
        # 不再指向任何云算力控件（开关本体已从设置页下线）
        self.assertNotIn("cloudEntry", settings)
        self.assertNotIn("remote-compute-toggle", settings)
        # 云端处理控制面改为只读状态行：三态闭集 + 与入队门同源的连接真值
        self.assertIn("REMOTE_COMPUTE_STATUS_TEXT", settings)
        self.assertIn("function remoteComputeStatus(value)", settings)
        self.assertIn('const pill = $("remote-compute-state");', settings)
        self.assertIn("if (allGreen && !remoteCompletionPrevReady) remoteCompletionSummaryShown = true;", settings)
        self.assertIn("if (allGreen && remoteCompletionSummaryShown) {", settings)
        self.assertIn("remoteCompletionSummaryShown = false;", settings)
        self.assertIn('block.dataset.role = "completion-summary";', settings)
        # 微项：catalog 诊断按钮对齐「复制连接诊断」，旧措辞全站清零
        self.assertIn('id="copy-catalog-diagnostics" type="button">复制连接诊断</button>', self.html)
        self.assertNotIn("复制脱敏诊断", self.html)

    def test_stage_machine_single_sourcing_and_repo_established_gate(self):
        """FRONTEND-SMOOTH-1 单元B（终验发现②⑥）：卡面标题与阶段行同源——同一份
        探针证据派生的阶段状态驱动，加密通道为唯一剩余动作时标题=「加密通道测试」
        与主按钮同拍；「仓库已建」门：已安装+未建仓组合绝不引导收紧，范围类失败
        处方与主按钮同拍（先建仓），收紧引导仅在两仓存在时登场。"""
        settings = self.modules["settings.js"]
        self.assertIn("function githubConnectionTitleText(phases)", settings)
        self.assertIn('return othersDone && phases.channel === "action_required" ? "加密通道测试" : "GitHub 连接";', settings)
        # 单源化：renderRemote 计算阶段状态恰一次，标题与阶段行共用
        self.assertIn("const phases = githubPhaseStates(value); /* 阶段状态单源：标题与阶段行共用（单元B） */", settings)
        self.assertIn("renderGithubPhases(value, phases);", settings)
        self.assertIn('titleNode.textContent = githubConnectionTitleText(phases);', settings)
        self.assertIn('id="github-connection-title"', self.html)
        # 「仓库已建」门：两仓未建时范围类失败处方=先建仓（与主按钮同拍）
        self.assertIn("const REMOTE_ACTION_SCOPE_NOT_EXACT_PRE_REPOS_TEXT =", settings)
        self.assertIn('if (normalized === "installation_scope_not_exact" && !dedicatedRepositoriesExist()) {', settings)
        self.assertIn("CourseLens 遵循最小权限：专属仓库尚未创建。点击「创建专属仓库并完成初始化」先建仓；创建完成后再把安装范围收紧到仅这两个仓库。", settings)
        # 终验发现②机理修正：settings_url（范围调整页）不是建仓证据——
        # 它随「已安装但范围暂宽」出现，与两仓是否已建无关；只认预选形态链接
        self.assertIn("function trustedRemotePreselectedSetupLink(snapshot)", settings)
        self.assertIn("if (trustedRemotePreselectedSetupLink(value)) return true;", settings)
        self.assertNotIn("if (trustedRemoteInstallLink(value)) return true;", settings)

    def test_remote_connection_snapshot_stale_while_revalidate_during_actions(self):
        """FRONTEND-SMOOTH-1 单元C（终验发现⑤）：动作执行期间快照读不被拒/不清空
        ——supervisor 快照走 stale-while-revalidate：构建瞬态失败时返回最近一次
        成功快照（动作持锁时快照仍可读），从未成功过才如实上抛。"""
        test_cache = ROOT / "runtime" / "cache"
        test_cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_cache) as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            credentials = CredentialStore(Path(tmp) / "credentials.json")
            app = _SnapshotFlakyGitHubApp()
            supervisor = RemoteConnectionSupervisor(store, credentials, lambda: app)
            good = supervisor.snapshot()
            self.assertTrue(good["components"])
            app.fail_after_first = True
            resilient = supervisor.snapshot()
            self.assertEqual(resilient["overall"]["state"], good["overall"]["state"])
            self.assertEqual(
                [item["component"] for item in resilient["components"]],
                [item["component"] for item in good["components"]],
            )
            # 从未成功过：诚实上抛，绝不伪造空快照
            flaky = _SnapshotFlakyGitHubApp()
            flaky.fail_after_first = True
            fresh = RemoteConnectionSupervisor(store, credentials, lambda: flaky)
            with self.assertRaises(RuntimeError):
                fresh.snapshot()
            store.close()

    def test_remote_connection_wait_window_fresh_semantics(self):
        """INIT-PATH-POLISH-1 单元A（终验发现⑨）：等待窗新鲜语义——
        fresh_snapshot() 强制一次新探针并等它收口（学生装完 App 后下一次
        等待窗读即见新证据，不等 60s 空闲节拍/90s 记录时效；窗口内重复
        轮询被合并）；普通 snapshot() 只读存储证据、绝不触发探针（常规
        请求不受影响）。HTTP 面 fresh 查询语义只加速只读探测，动作形状
        不变；前端仅等待轮询/焦点重探改走 ?fresh=1。"""
        test_cache = ROOT / "runtime" / "cache"
        test_cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_cache) as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            credentials = CredentialStore(Path(tmp) / "credentials.json")
            app = _WaitWindowFreshGitHubApp()
            supervisor = RemoteConnectionSupervisor(store, credentials, lambda: app)
            # 普通读只消费存储证据：未启动监视线程时零探针（常规请求不受影响）
            ordinary = supervisor.snapshot()
            installation = next(
                item for item in ordinary["components"] if item["component"] == "installation"
            )
            self.assertEqual(installation["code"], "not_observed")
            app.installed = True
            installation = next(
                item for item in supervisor.snapshot()["components"] if item["component"] == "installation"
            )
            self.assertEqual(installation["code"], "not_observed")
            self.assertEqual(app.inspect_calls, 0, "普通读不触发探针")
            # 等待窗新鲜读：强制探针并等它收口，读到的就是最新证据
            first_fresh = supervisor.fresh_snapshot()
            self.assertEqual(app.inspect_calls, 1, "fresh 读强制恰好一次探针")
            installation = next(
                item for item in first_fresh["components"] if item["component"] == "installation"
            )
            self.assertEqual(installation["code"], "installation_present")
            self.assertEqual(installation["state"], "ready")
            # 合并窗口：max_age 内的第二次 fresh 读不再探针
            supervisor.fresh_snapshot()
            self.assertEqual(app.inspect_calls, 1, "窗口内重复 fresh 读被合并")
            store.close()
        # HTTP/前端缝钉：fresh 查询语义只新增只读参数，动作形状不变；
        # 零参读模型保持原形（fresh 仅在有参时透传）
        http_source = (ROOT / "src" / "runtime" / "http_api.py").read_text(encoding="utf-8")
        self.assertIn('fresh = str(query.get("fresh", [""])[0])', http_source)
        self.assertIn("value = service.remote_compute.connection_snapshot(fresh=True)", http_source)
        self.assertIn("value = service.remote_compute.connection_snapshot()", http_source)
        application_source = (ROOT / "src" / "application.py").read_text(encoding="utf-8")
        self.assertIn("def remote_connection_snapshot(self, *, fresh: bool = False) -> dict:", application_source)
        self.assertIn("self.remote_connection.fresh_snapshot()", application_source)
        settings = self.modules["settings.js"]
        self.assertIn('apiV3("remote-connection?fresh=1")', settings)
        self.assertIn("async function loadRemoteFresh()", settings)
        self.assertIn("await loadRemoteFresh();", settings, "等待轮询改走新鲜读")
        self.assertIn('const value = await apiV3("remote-connection");', settings, "普通读保持无参读模型")

    def test_action_stage_progress_and_selfheal_discipline(self):
        """FRONTEND-SMOOTH-1 单元D（终验发现③⑦）：bootstrap/rotate/test-channel
        分步进度——后端在连接快照暴露闭集阶段字段（正在创建仓库/正在同步文档/
        正在校验 Worker/正在配置密钥），动作收口即清字段；前端白名单判读 +
        预期管理句；可自愈中间失败静默重试恰一次，终局恰一次通知，中途不弹错。"""
        settings = self.modules["settings.js"]
        application_source = (ROOT / "src" / "application.py").read_text(encoding="utf-8")
        # 后端闭集阶段表 + 快照附带 + 动作收口清理
        self.assertIn("REMOTE_ACTION_STAGE_LABELS: dict[str, str] = {", application_source)
        for label in ("正在创建仓库", "正在同步文档", "正在校验 Worker", "正在配置密钥"):
            self.assertIn(label, application_source)
        self.assertIn('"action_progress": stage', application_source)
        self.assertIn("def _remote_action_stage_set", application_source)
        self.assertIn("def _remote_action_stage_clear", application_source)
        self.assertIn("self._remote_action_stage_set(\"bootstrap\", \"creating_repositories\")", application_source)
        self.assertIn('self._remote_action_stage_set(action, "rotating_keys")', application_source)
        self.assertIn('self._remote_action_stage_set(action, "testing_channel")', application_source)
        self.assertIn("self._remote_action_stage_clear()", application_source)
        # 前端白名单判读：匹配当前动作且文案在闭集内，否则回退角标
        self.assertIn("const REMOTE_ACTION_STAGE_TEXT = Object.freeze(new Set([", settings)
        self.assertIn("function remoteActionStageText(value)", settings)
        self.assertIn('if (!progress || String(progress.action || "") !== remoteActionInProgress) return "";', settings)
        # WAIT-UX-1：预期句按动作实测分布分档（闭集单源 wait-expectations.js：
        # bootstrap R4 20.5s / 加密测试 9s~60s / 修复约 113s；无样本动作回退保守区间）
        self.assertIn(
            'const REMOTE_ASYNC_EXPECTATION_TEXT = (action) => (',
            settings,
        )
        self.assertIn("REMOTE_ACTION_EXPECTATIONS[action] || REMOTE_ACTION_EXPECTATION_FALLBACK", settings)
        self.assertIn('REMOTE_ACTION_EXPECTATION_FALLBACK, REMOTE_ACTION_EXPECTATIONS } from "../wait-expectations.js"', settings)
        self.assertIn("${stageText} · ${REMOTE_ASYNC_EXPECTATION_TEXT(remoteActionInProgress)}", settings)
        # 自愈纪律：闭集码 + 恰一次（_selfhealRetry 门）+ 非错误形态提示
        self.assertIn('const REMOTE_ACTION_SELFHEAL_CODES = new Set(["timeout", "rate_limited", "github_unreachable"]);', settings)
        self.assertIn("const REMOTE_ACTION_SELFHEAL_RETRY_MS = 4000;", settings)
        self.assertIn("function shouldSelfHealRetry(action, code, options)", settings)
        self.assertIn("options?._selfhealRetry !== true;", settings)
        self.assertIn("function scheduleSelfHealRetry(action, options)", settings)
        self.assertIn('toast(REMOTE_ACTION_SELFHEAL_NOTICE, "checking");', settings)
        self.assertEqual(settings.count("if (shouldSelfHealRetry(action, "), 2, "两失败分支（operation failed / 提交被拒）都走自愈门")

    def test_minimal_permission_copy_framework(self):
        """FRONTEND-SMOOTH-1 单元E（终验发现④+卫生⑤）：连接面权限/阶段类文案
        统一「最小权限+具名清单」框架——禁「不精确/异常」类技术措辞，删除
        「保留 Actions 权限」句；锚点文案（范围收紧指引）仓库名只取后端
        configured_repositories 闭集插值，缺失时退回无名单变体；ui.js 60/66
        「复制脱敏诊断」对齐为「复制连接诊断」。"""
        settings = self.modules["settings.js"]
        ui = self.modules["ui.js"]
        # 禁用措辞全量清零（用户拍板：措辞体系问题，非单条文案）
        self.assertNotIn("不精确", settings)
        self.assertNotIn("保留 Actions", settings)
        # 锚点文案：最小权限 + 具名清单（受管记录闭集插值 + 无名单变体）
        self.assertIn("function managedRepositoryNames(value)", settings)
        self.assertIn("function remoteScopeExactNamedGuidance(value)", settings)
        self.assertIn("GITHUB_REPO_FULL_NAME_RE.test(item.trim())", settings)
        self.assertIn(
            "CourseLens 遵循最小权限：除了它自己创建的两个仓库，不会访问你账号里的其他任何仓库。",
            settings,
        )
        self.assertIn("${worker} 和 ${mailbox}。", settings)
        self.assertIn("const REMOTE_SCOPE_EXACT_ANONYMOUS_GUIDANCE =", settings)
        # 指引分流：仓库已建 → 具名锚点；未建 → 先建仓变体
        self.assertIn("return dedicatedRepositoriesExist(value)", settings)
        self.assertIn("? remoteScopeExactNamedGuidance(value)", settings)
        # 权限端点细分文案同框架（不再「拒绝」开头、不再保留 Actions 句）
        self.assertIn("CourseLens 只访问它自己创建的两个仓库。", settings)
        self.assertIn("CourseLens 只需要这两个仓库的 Actions 权限来运行学习任务。", settings)
        # ui.js：脱敏措辞退役，对齐「复制连接诊断」
        self.assertNotIn("复制脱敏诊断", ui)
        self.assertEqual(ui.count("复制连接诊断"), 2)

    def test_worker_template_constant_matches_bundled_release(self):
        """FRONTEND-SMOOTH-1 卫生③：镜像仓真实名=连字符（REPO-GOVERN-1 已正名
        gualtier-xu/Fudan-CourseLens-Worker，REPUBLISH-23 起 active pin 随
        45dd6230 环换代至正名；旧名 -Worker-Release 经改名转发仍可达；C 404
        真因=总控 URL 下划线笔误的教训保留）。
        github_app.py WORKER_TEMPLATE 兜底常量、runtime-assets.json
        worker_mirror.active.repository 与 docs/client-reset.md 分发仓名
        三处钉为同一连字符名，下划线陈旧引用不得回潮。"""
        github_app_source = (ROOT / "src" / "remote" / "github_app.py").read_text(encoding="utf-8")
        # N9-B2 起仓名唯一真值=config/distribution.json（checker 防漂移），
        # WORKER_TEMPLATE 改钉「来自注册表」，注册表值仍钉同一连字符名。
        self.assertIn("WORKER_TEMPLATE = DISTRIBUTION_REPOSITORY", github_app_source)
        self.assertNotIn("gualtier_xu", github_app_source)
        registry = json.loads((ROOT / "config" / "distribution.json").read_text(encoding="utf-8"))
        self.assertEqual(registry["distribution_repository"], "gualtier-xu/Fudan-CourseLens")
        release = json.loads((ROOT / "runtime-assets.json").read_text(encoding="utf-8"))
        self.assertEqual(release["worker_mirror"]["active"]["repository"], "gualtier-xu/Fudan-CourseLens-Worker")
        reset_doc = (ROOT / "docs" / "client-reset.md").read_text(encoding="utf-8")
        self.assertIn("分发仓库 `gualtier-xu/Fudan-CourseLens-Worker-Release` 永远不在闭集内", reset_doc)
        self.assertNotIn("Fudan_CourseLens`", reset_doc)

    def test_connection_diagnostic_copy_is_renamed_and_snapshot_sourced(self):
        """FLOW-ORDER-FIX-1 单元③：卡内复制按钮更名「复制连接诊断」（与设置页
        应用级「复制脱敏诊断」区分，活体两次取错）；复制载荷 remote 字段以卡面
        同一连接快照为准（同源，陈旧 settings 快照只作兜底）；组件层只复制闭集
        状态码与环境缺钥清单；卡内安全证据行随连接快照刷新。"""
        settings = self.modules["settings.js"]
        self.assertIn(
            'id="copy-diagnostics" type="button" aria-describedby="diagnostic-privacy-summary">复制连接诊断</button>',
            self.html,
        )
        self.assertIn("const diagnosticSource = remoteSnapshotValue || settingsValue?.remote || null;", settings)
        self.assertIn("remote: String(diagnosticOverall.code || \"unknown\"),", settings)
        self.assertIn("remote_state: String(diagnosticOverall.state || \"unknown\"),", settings)
        self.assertIn("if (missing.length) entry.missing_secrets = missing;", settings)
        self.assertIn('toast("连接诊断已复制", "ready");', settings)
        # 复制载荷不含 evidence 自由字段：组件条目仅 component/state/code/缺钥清单
        copy_body = settings[
            settings.index('$("copy-diagnostics").addEventListener'):
            settings.index('toast("连接诊断暂时无法复制，请稍后重试。", "error")')
        ]
        self.assertNotIn("evidence:", copy_body)
        self.assertNotIn("login", copy_body)
        # environment 缺钥清单只认闭集密钥名，其他 evidence 字段一律不上屏
        self.assertIn(
            'const ENVIRONMENT_SECRET_NAMES = new Set(["WORKER_INPUT_PRIVATE_KEY", "WORKER_SIGNING_PRIVATE_KEY"]);',
            settings,
        )
        self.assertIn("function remoteComponentMissingSecrets(component)", settings)
        self.assertIn("`缺少密钥：${missing.join(\"、\")}`", settings)
        # 卡内安全证据行（security-evidence）随连接快照同源刷新
        self.assertIn(
            'if (securityEvidence) securityEvidence.textContent = evidenceText(overall);',
            settings,
        )

    def test_app_level_diagnostics_remote_field_is_connection_snapshot_sourced(self):
        """FLOW-ORDER-FIX-1 单元③：应用级诊断 remote 字段与连接卡同源（py 钉）——
        settings 与 app-shell 两个应用级快照的 remote 一律来自
        remote_connection_snapshot()（连接快照为准），防未来出现第二计算路径
        （活体 authorization_missing vs authorization_valid 矛盾的防回归钉）。"""
        source = (ROOT / "src" / "application.py").read_text(encoding="utf-8")
        for method in ("def settings_privacy_snapshot(self) -> dict:", "def app_shell_snapshot(self) -> dict:"):
            start = source.index(method)
            body = source[start:source.index("\n    def ", start + 10)]
            self.assertIn("remote = self.remote_connection_snapshot()", body, method)
            self.assertIn('"remote": remote,', body, method)

    def test_remote_action_failures_surface_closed_codes_and_endpoint_class(self):
        """NIGHT-FRONT-1 T1 观测透码：动作失败闭集码与端点类进卡面行内区与 toast。
        permission_denied 复用按端点文案表（快照证据闭集校验，安装/范围类 403
        不再推「重新授权」）；operation.failed 携带的 error_code 走同一文案表并进
        内联区，不再一律兜底 operation_failed；透码 chip 只呈现闭集码与端点类。"""
        settings = self.modules["settings.js"]
        self.assertIn("function remoteActionEndpointClass()", settings)
        helper = settings[
            settings.index("function remoteActionEndpointClass()"):
            settings.index("function remoteActionFailureText")
        ]
        self.assertIn("Object.hasOwn(REMOTE_PERMISSION_GUIDANCE_BY_ENDPOINT, endpointClass)", helper)
        refinement = settings[
            settings.index("function remoteActionFailureText"):
            settings.index("return (normalized && Object.hasOwn(REMOTE_ACTION_FAILURE_TEXT, normalized))")
        ]
        self.assertIn('if (normalized === "permission_denied") {', refinement)
        self.assertIn("REMOTE_PERMISSION_GUIDANCE_BY_ENDPOINT[endpointClass]", refinement)
        self.assertIn('chip.className = "remote-error-code";', settings)
        self.assertIn(
            "chip.textContent = endpointClass ? `${normalized} · ${endpointClass}` : normalized;",
            settings,
        )
        body = settings[settings.index("async function remoteAction"):]
        self.assertIn(
            'const failureCode = String(value.operation?.error_code || "operation_failed");',
            body,
        )
        self.assertIn("showRemoteActionError(failureCode, action, options);", body)
        self.assertIn('toast(remoteActionFailureText(failureCode), "error");', body)
        self.assertIn(".remote-action-error .remote-error-code", self.css)

    def test_device_poll_honors_backend_interval_and_never_resets_to_floor(self):
        """C1-R20 micro（RFC 8628 §3.5）：pending 轮询间隔采用后端回传值
        （slow_down 时 +5s）；缺省保持既有间隔——绝不静默重置回 5s 重新触发慢放。"""
        settings = self.modules["settings.js"]
        self.assertIn(
            "authorizationPollInterval = Math.max(5, Number(result.interval || authorizationPollInterval || 5));",
            settings,
        )

    def test_leftover_reuse_denial_downgrades_to_account_level_install_url(self):
        """NIGHT-FRONT-1 T2 遗留状态降级：bootstrap 复用路径私有仓 403 零证据
        → awaiting_installation。账号级安装页为第二受信 URL 形态（闭集正则），
        仅后端 awaiting 证据收录、恰自动打开一次、仅无预选链接时兜底渲染；
        预选形态永远优先；安装就绪后兜底链接随之失效。"""
        settings = self.modules["settings.js"]
        self.assertIn(
            "const INSTALLATION_SETUP_ACCOUNT_URL_RE = "
            "/^https:\\/\\/github\\.com\\/apps\\/[A-Za-z0-9.-]+\\/installations\\/new$/;",
            settings,
        )
        self.assertIn("let remoteInstallAccountSetupUrl = \"\";", settings)
        self.assertIn("remoteInstallAccountSetupUrl = disclosedAccountSetupUrl;", settings)
        self.assertIn("const autoOpenUrl = installGuidance.setupUrl || installGuidance.accountUrl;", settings)
        self.assertIn(
            'const fallbackAnchor = trustedRemoteAnchor({ href: state.accountUrl, label: "打开 CourseLens App 安装页" });',
            settings,
        )
        self.assertIn("attachRemoteWaitTrigger(fallbackAnchor, state.accountUrl);", settings)
        self.assertIn("remoteInstallAccountSetupUrl = \"\";", settings)
        self.assertIn("暂无预选链接", settings)

    # ---- overlays: single-overlay guard, Esc, inert, focus return ----

    def test_overlay_base_enforces_single_overlay(self):
        ui = self.modules["ui.js"]
        self.assertIn("function openOverlay", ui)
        self.assertIn("if (overlayStack.length) return null;", ui)
        self.assertIn('event.key === "Escape"', ui)
        self.assertIn("main.inert = true", ui)
        self.assertIn("main.inert = false", ui)
        # e375ce6 页锚归还重构（LIVE-VALIDATE-1 全量归因同步）：关闭归还走
        # returnFocus 锚（函数=关时求值），锚离场回落 trigger，绝不归还孤儿节点。
        self.assertIn("focusTarget?.focus({ preventScroll: true })", ui)
        self.assertIn("? anchor : entry.trigger", ui)
        for overlay_id in ("tasks-root", "palette-root", "conn-menu", "account-menu"):
            self.assertIn(f'id="{overlay_id}"', self.html)

    def test_tasks_drawer_is_a_global_overlay(self):
        tasks = self.modules["tasks-drawer.js"]
        self.assertIn('id="tasks-root"', self.html)
        self.assertIn("openOverlay(", tasks)
        self.assertIn("closeTaskEventSource()", tasks)
        # CLIENT-STATE（2026-10-06）：订阅面补 automation 主题——自动材料运行记录
        # 落账事件即时刷新抽屉；remote-connection 主题同时广播窗口事件供页眉/
        # 设置页连接卡即时复核（状态及时性专项）。
        self.assertIn(
            'new EventSource("/api/v3/events?topics=remote-connection,remote-runs,tasks,automation"',
            tasks,
        )
        # C8-1：后端只发命名事件（tasks|remote-runs|remote-connection|automation），message 通道
        # 永不触发——必须按订阅 topics 逐名 addEventListener，禁回退默认通道。
        self.assertIn('addEventListener("tasks"', tasks)
        self.assertIn('addEventListener("remote-runs"', tasks)
        self.assertIn('addEventListener("remote-connection"', tasks)
        self.assertIn('addEventListener("automation"', tasks)
        self.assertNotIn("onmessage", tasks)
        self.assertIn("eventSource?.close()", tasks)
        self.assertNotIn("silhouette", self.html + tasks)
        self.assertIn('id="task-drawer"', self.html)
        # SSE 提升为应用生命周期（合同 §3.3）：安装期建立唯一订阅，抽屉开合不再创建/关闭；
        # 任务新鲜度由 SSE 消息驱动（保护告警链已随预算门整体退役）
        self.assertNotIn("eventSource.onerror", tasks)
        self.assertNotIn("markProtectionStaleIfExpired", tasks)
        self.assertNotIn("armMidnightRefresh(", tasks)
        self.assertIn("closeTaskEventSource();", tasks)

    def test_no_permanent_quota_or_billing_surface_remains(self):
        # 永久额度/账单面整体移除：无预算 DOM、无 billing 卡片、无余额文案
        for fragment in (
            "budget-pill", "budget-menu", "billing-insight", "billing-opt-in",
            "github-billing", "evidence-strip", "strip-local", "strip-billing",
            "strip-runner", "今日计算证据", "今日预计剩余", "已观察用量",
            "标准公共 Runner 不计费",
        ):
            self.assertNotIn(fragment, self.html)
        for fragment in ("BillingInsight", "BILLING_STATE_TEXT", "billingSnapshot", "budgetSnapshot"):
            self.assertNotIn(fragment, self.modules["tasks-drawer.js"])
        for fragment in ("BILLING_INSIGHT_STATE_TEXT", "github-billing", "billingValue"):
            self.assertNotIn(fragment, self.modules["settings.js"])
        # 保护告警链已随预算门整体退役：DOM/样式/store 键零残留
        self.assertNotIn("protection-alert", self.html)
        self.assertNotIn("protectionAlert", self.modules["store.js"])
        self.assertNotIn(".protection-alert", self.css)
        # 全站仍无装饰性渐变（唯一 gradient 是播放器功能性 scrim）
        self.assertNotIn("linear-gradient(to bottom", self.css)
        self.assertNotIn("radial-gradient", self.css)
        self.assertNotIn("conic-gradient", self.css)

    def test_password_inputs_stay_in_the_frozen_closed_set(self):
        # 秘密输入闭集：2 只凭据（登录密码 + DeepSeek Key）+ 3 只搬家包口令
        # （D12 P0：导出/确认/导入；用户自设、永不持久化，不属凭据面）。
        self.assertEqual(self.html.count('type="password"'), 5)

    def test_single_event_source_lives_only_in_tasks_drawer(self):
        # 额度组件复用唯一共享订阅，不得新建第二条 SSE（合同 §3.3）
        owners = [name for name, source in self.modules.items() if "new EventSource(" in source]
        self.assertEqual(owners, ["tasks-drawer.js"])
        self.assertNotIn("new EventSource(", self.app)
        # 唯一 interval=30s 任务轮询；保护告警 60s ticker 已随预算门退役，不得回流
        self.assertEqual(self.modules["tasks-drawer.js"].count("setInterval"), 1)
        self.assertIn("setInterval(() => void loadTasks(store), 30000)", self.modules["tasks-drawer.js"])

    def test_task_eta_uses_friendly_closed_format(self):
        # ETA 友好映射（合同 §5.2 + S09-C）：排队三段式 / 高置信点估计 / 区间 /
        # 资料不足 / stale→等待重新确认 / 重新估算
        tasks = self.modules["tasks-drawer.js"]
        self.assertIn("function taskEtaText(", tasks)
        self.assertIn("parts.push(`已等待 ${waited} 分钟`)", tasks)
        self.assertIn('parts.push("等待 GitHub Runner")', tasks)
        self.assertIn("通常还需 ${a}–${b} 分钟开始", tasks)
        self.assertIn("开始后预计 ${a}–${b} 分钟", tasks)
        self.assertIn("预计还需 ${from}–${to} 分钟", tasks)
        self.assertIn("预计约 ${friendlyMinutes(remaining)} 分钟", tasks)
        self.assertIn("正在估算 · 已运行 ${friendlyMinutes(elapsed)} 分钟", tasks)
        self.assertIn('return "等待重新确认";', tasks)
        self.assertIn('return "重新估算中";', tasks)
        self.assertIn("function friendlyMinutes(", tasks)
        # 上次渲染字符串守卫：同档位不抖动
        self.assertIn("lastEvidenceByTask", tasks)
        # basis/confidence 只进技术详情，非高置信不假精确点估计由区间覆盖
        self.assertIn("taskEstimateDetail(task)", tasks)

    def test_tasks_requirement_is_described_in_docs_not_placeholder(self):
        self.assertNotIn("TODO", self.javascript)
        self.assertNotIn("FIXME", self.javascript)

    # ---- security invariants (migrated verbatim from the v2 guardrails) ----

    def test_browser_routes_are_v3_only(self):
        routes = re.findall(r'["`](/api/[^"`?${ ]+)', self.javascript + self.html)
        self.assertTrue(routes)
        self.assertTrue(all(route == "/api/health" or route.startswith("/api/v3/") for route in routes), routes)
        for removed in ("/api/download", "/api/subtitle-merge", "/api/open-folder", "/api/remote-compute"):
            self.assertNotIn(removed, self.javascript + self.html)

    def test_secrets_are_password_inputs_and_never_persisted(self):
        self.assertIn('id="login-password" type="password" autocomplete="current-password"', self.html)
        self.assertRegex(self.html, r'id="deepseek-key"[^>]*type="password"|type="password"[^>]*id="deepseek-key"')
        storage_writes = [line for line in self.javascript.splitlines() if "localStorage.setItem" in line]
        self.assertEqual(storage_writes, [
            # 乙-3（#6）：首用淡出启动计数（纯 UI 偏好，零课程身份）
            '  localStorage.setItem(APP_STARTS_KEY, String(startCount));',
            # D3：字幕样式三档闭集（纯外观偏好，单键 JSON）
            '    localStorage.setItem(SUBTITLE_STYLE_KEY, JSON.stringify(next));',
            # U7①：每课倍速记忆（纯播放偏好，无任何用户输入内容）
            '    localStorage.setItem(RATE_MEMORY_KEY, JSON.stringify(map));',
            # AS5：洞察开关条目退役（默认开；显式 "off" 旧偏好只读不写），
            # 写入闭集不再含 courselens:insight 键
            # P56-U1：主题三态偏好（v2 键唯一真源，applyThemePreference 持久；
            # applyTheme 不再写存储，旧 v1 键永不再读）
            '  localStorage.setItem(THEME_PREF_KEY, preference);',
            # FIRST-LOGIN-UX-2（U1 继续学习）：store last-lecture 记忆最小键
            # （纯 ID+时刻，零课程名/个人数据；与 client-reset.js 重置闭集同笔注册）
            '      localStorage.setItem("courselens.last-lecture.v1", JSON.stringify(clean));',
            '    localStorage.setItem(COURSE_ORDER_KEY, JSON.stringify(order));',
            '    localStorage.setItem(COURSE_TERM_FILTER_KEY, courseTermFilter);',
        ], "storage 写入闭集顺序按模块拼接序（player-core 在 shell/study 之前）")
        for secret in ("login-password", "deepseek-key"):
            self.assertFalse(any(secret in line for line in storage_writes))
        self.assertNotIn('id="automationPassword"', self.html)
        self.assertNotIn('id="automationKey"', self.html)
        # BUGFIX-UI-CONSOLIDATION-1：设置页自动化管理板块与重复隐私披露对话框退场；
        # 按课程开关直接走既有保存链，运行记录并入任务抽屉。秘密输入仍只有两个。
        self.assertNotIn('id="settings-automation-group"', self.html)
        self.assertNotIn('id="course-automation-dialog"', self.html)
        self.assertNotIn('id="course-automation-ack"', self.html)

    def test_subtitle_mode_selector_is_replaced_by_automatic_policy_text(self):
        """One fixed ASR/OCR/AI bundle: no selectable mode or per-course output selector anywhere."""
        self.assertNotIn("automation-subtitle-mode", self.html)
        self.assertNotIn('id="automation-subtitle-policy"', self.html)
        self.assertNotIn('id="automation-outputs"', self.html)
        # 固定包事实随运行记录并入任务抽屉（tasks-drawer.js）呈现
        self.assertIn("字幕 ASR · 课件 OCR · AI 总结与章节", self.javascript)
        settings = self.modules["settings.js"]
        self.assertNotIn("automation-subtitle-mode", settings)
        self.assertNotIn("automation-output-", settings)
        self.assertNotIn("automationNeedsAi", settings)

    def test_lifecycle_and_background_updates_are_bounded(self):
        shell = self.modules["shell.js"]
        self.assertIn('sendSession("open")', shell)
        self.assertIn('sendSession("heartbeat")', shell)
        self.assertIn('sendSession("close", true)', shell)
        self.assertIn("navigator.sendBeacon", shell)
        self.assertIn("pagehide", self.app)
        self.assertGreaterEqual(self.javascript.count("new AbortController()"), 3)

    def test_device_flow_and_timetable_controls_are_complete(self):
        settings = self.modules["settings.js"]
        timetable = self.modules["timetable.js"]
        for element_id in (
            "remote-device-authorization",
            "timetable-semester", "timetable-start-date", "set-timetable-start", "export-timetable",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('remoteAction("poll-authorization")', settings)
        # BUGFIX-UI-CONSOLIDATION-1：设置页管理卡退场；快照轮询归课程目录、
        # 运行记录归任务抽屉。NIGHT2-W7：上传与保存配置同一 PUT 幂等通道，
        # 必带闭集形状 operation_id（后端仅 PUT 分支，POST 落 404）。
        self.assertIn('apiV3("automation")', self.javascript)
        self.assertIn('putV3("automation/cloud-secrets"', self.javascript)
        self.assertIn('operationId("cloud-secrets")', self.javascript)
        self.assertIn('action: "set-semester-start"', timetable)
        self.assertIn('operationId("timetable")', timetable)

    def test_settings_page_gates_requests_on_backend_auth_evidence(self):
        palette = self.modules["search-palette.js"]
        timetable = self.modules["timetable.js"]
        self.assertIn('store.auth?.state === "ready"', palette)
        self.assertIn('store.auth?.state === "ready"', timetable)
        self.assertIn('store.subscribe("auth"', timetable)

    def test_proxy_save_validates_before_post_and_error_code_parity(self):
        # D8：保存前闭集校验（manual 必填+可解析，零 POST），禁静默补 http://；
        # 后端拒绝码 proxy_url_invalid 与前端文案键同笔等集。
        settings = self.modules["settings.js"]
        api = self.modules["api.js"]
        self.assertIn("proxyUrlParseable(proxyValue)", settings)
        self.assertNotIn('f"http://${', settings)
        self.assertIn("proxy_url_invalid", api)

    def test_focus_return_family_d4_d5_d7(self):
        # 焦点归还家族（CUI-2 D4/D5/D7）：动作/过场收口处显式还焦点给语义锚。
        study = self.modules["study.js"]
        dropdown = self.modules["dropdown.js"]
        course_data = self.modules["course-data.js"]
        # D4：选择面↔播放桌过场，隐藏让位区域前捕获焦点，过场后归还（active 行）
        self.assertIn("focusLeftSelect", study)
        # WP1-D3：过场锚=播放钮（Space 即播放/暂停）；返回钮不再当锚——
        # 它吃空格会把键盘生直接弹回目录（二序风险）。
        self.assertIn('$("player-ctrl-play")', study)
        self.assertIn("!playButton.disabled", study)
        self.assertNotIn('$("study-back-select")?.focus', study)
        self.assertIn("#study-lecture-list .lecture-row", study)
        # D5：自绘下拉提交后焦点归还触发钮（键盘路径焦点本就在触发钮）
        self.assertIn("state.trigger.focus", dropdown)
        # D7：数据页动作收口焦点已丢时归还原钮/页头稳定钮
        self.assertIn("focusAnchor", course_data)
        self.assertIn('$("data-refresh")', course_data)

    def test_update_click_sync_lock_and_palette_recheck_close(self):
        # D10：update 点击即禁用（同步锁），双击只发一笔 POST；
        # D11：palette 跳转类条目执行收口兜底复核关面板（课程类 Enter 语义）。
        update_widget = self.modules["update-widget.js"]
        palette = self.modules["search-palette.js"]
        self.assertIn("if (button?.disabled) return;", update_widget)
        self.assertIn("if (button) button.disabled = true;", update_widget)
        self.assertIn('if (item.closes !== false && !$("palette-root").hidden) closePalette();', palette)

    def test_error_surface_copy_is_humanized(self):
        # D3/C3-3：remote_failed 补键；未知应用码兜底人话，原文连码折叠进
        # ApiError.detail；播放器行内证据行不再拼「码 · 英文 payload」。
        api = self.modules["api.js"]
        player = self.modules["player-core.js"]
        self.assertIn("remote_failed:", api)
        self.assertIn("GENERIC_ACTION_FAILURE", api)
        self.assertIn("this.detail = detail;", api)
        self.assertNotIn("${error.code} · ${error.message}", player)

    def test_catalog_recovery_copy_separates_login_from_directory_failures(self):
        study = self.modules["study.js"]
        ui = self.modules["ui.js"]
        api = self.modules["api.js"]
        self.assertIn("登录仍然有效，可以重试或诊断网络", ui)
        self.assertIn("无需关闭两步验证", ui)
        self.assertIn('else if (auth?.state === "checking") {', study)
        # NAV-HANG-1①：else 分支按 configured 分流——上游抖动（configured=true）
        # 保目录保选择；显式登出/切号（凭据消失）才清空。
        self.assertIn("const keepCatalog = auth?.configured === true;", study)
        self.assertIn("courses: keepCatalog ? store.courses : [],", study)
        for code in (
            "catalog_route_unavailable",
            "catalog_additional_verification_required",
            "catalog_identity_mismatch",
        ):
            self.assertIn(code, study + ui + api)

    def test_catalog_recovery_actions_are_closed_localized_and_diagnostic_only(self):
        study = self.modules["study.js"]
        ui = self.modules["ui.js"]
        for element_id in (
            "catalog-recovery", "catalog-recovery-title", "catalog-recovery-impact",
            "catalog-recovery-actions", "catalog-diagnostic-code", "copy-catalog-diagnostics",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn("<summary>诊断信息</summary>", self.html)
        self.assertIn('new Set(["login", "refresh-catalog", "diagnose-network"])', study)
        self.assertIn("JSON.stringify(latestCatalogDiagnostics, null, 2)", study)
        self.assertNotIn('return `${value.state || "unknown"}${code}`', ui)
        for label in ("登录", "重新认证", "重试刷新", "诊断网络"):
            self.assertIn(label, ui)

    def test_course_selection_and_generation_prerequisites_are_accessible(self):
        study = self.modules["study.js"]
        player = self.modules["player-core.js"]
        for element_id in ("generate-subtitle", "generate-notes"):
            self.assertRegex(
                self.html,
                rf'id="{element_id}"[^>]*aria-describedby="player-action-hint"[^>]*disabled',
            )
        self.assertIn('id="player-action-hint"', self.html)
        self.assertIn('button.setAttribute("aria-controls", "study-lecture-list")', study)
        self.assertIn('button.setAttribute("aria-controls", "player-stage")', study)
        self.assertIn('node.setAttribute("aria-current", "true")', study)
        self.assertNotIn('node.setAttribute("aria-pressed"', study + player)
        self.assertIn("updateLectureActions(store.activeLecture)", player)

    # ---- schedule disclosure (timetable as study context) ----

    def test_schedule_widget_and_week_dialog_replace_details(self):
        timetable = self.modules["timetable.js"]
        overview = self.modules["home-overview.js"]
        self.assertIn('id="schedule-box"', self.html)
        self.assertNotRegex(self.html, r'id="schedule-box"[^>]*\bopen\b')
        self.assertIn('id="schedule-widget"', self.html)
        self.assertIn('id="schedule-summary"', self.html)
        self.assertIn('id="schedule-now"', self.html)
        self.assertIn('id="schedule-state"', self.html)
        self.assertIn('id="schedule-week-tag"', self.html)
        # SIMPLIFY-AUDIT-1 S2：周导航三钮保留，常驻手动「刷新」钮已退役
        for element_id in ("schedule-week-prev", "schedule-week-next", "schedule-week-current"):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertNotIn('id="schedule-refresh"', self.html)
        self.assertIn('id="schedule-conflicts"', self.html)
        # 周课表浮层：原生 dialog，小组件 aria 关联，打开/关闭不离开页面
        self.assertIn('<dialog id="schedule-week-dialog"', self.html)
        self.assertIn('aria-controls="schedule-week-dialog"', self.html)
        self.assertIn('aria-haspopup="dialog"', self.html)
        self.assertIn('id="week-grid"', self.html)
        self.assertNotIn("timetable-grid", self.html)
        # Now/Next 选择是可测试纯函数；周网格按 start_unit/end_unit 放置
        self.assertIn("export function selectNowNext(", overview)
        self.assertIn("export function weekGridModel(", overview)
        self.assertIn("startUnit", overview)
        self.assertIn("endUnit", overview)
        self.assertIn("renderState(", timetable)
        self.assertIn('apiV3(`timetable?${query}`', timetable)
        for code in (
            "timetable_login_required", "timetable_stale", "semester_start_required",
            "timetable_not_loaded", "timetable_partial", "timetable_verified",
        ):
            self.assertIn(code, timetable)

    def test_status_capsule_groups_conn_tasks(self):
        # 顶栏状态胶囊：连接/任务合并为一个胶囊，各段仍是独立控件
        self.assertIn('id="status-capsule" class="status-capsule" role="group"', self.html)
        capsule_start = self.html.index('id="status-capsule"')
        capsule_end = self.html.index("</div>", self.html.index('id="task-chip"'))
        capsule_html = self.html[capsule_start:capsule_end]
        for element_id in ("conn-status", "task-chip"):
            self.assertIn(f'id="{element_id}"', capsule_html)
        self.assertIn("export function statusCapsuleLabel(", self.modules["home-overview.js"])
        self.assertIn("refreshStatusCapsuleLabel()", self.modules["shell.js"])
        # 方向1：离散同高控件 + quiet 间距，不再锁分段胶囊边框/分隔线
        self.assertIn(".status-capsule {", self.css)
        self.assertIn(".status-capsule > * { border: 0; border-radius: var(--radius); min-height: 34px; }", self.css)
        self.assertNotIn(".status-capsule > * + *", self.css)
        self.assertNotIn(".status-capsule { border-radius: 22px; }", self.css)

    def test_global_status_quiets_ready_and_marks_actionable_states(self):
        # 方向2：ready 成功句静默（保留占位，右侧控件不跳位）；
        # checking/actionable/error 保持可见，状态色条 + 文字双通道，不靠颜色单独表达
        self.assertIn(".global-status[data-state] { border-left: 3px solid var(--state-color", self.css)
        self.assertIn('.global-status[data-state="ready"] { visibility: hidden; }', self.css)
        # 任务计数徽标从白透明（旧 navy 实底残留）改为主题令牌，透明控件上仍可读
        self.assertIn("background: var(--navy-soft);\n  color: var(--navy-ink);", self.css)
        self.assertNotIn("rgb(255 255 255 / 16%)", self.css)

    def test_schedule_conflicts_are_not_color_only(self):
        self.assertIn(".week-block.conflict .wb-name::after", self.css)
        self.assertIn('content: " ！"', self.css)
        self.assertIn("conflict-note", self.css)
        self.assertIn("时间冲突", self.modules["timetable.js"])

    def test_schedule_meeting_maps_to_catalog_course_only_with_evidence(self):
        timetable = self.modules["timetable.js"]
        self.assertIn("catalog_course_id", timetable)
        self.assertIn('store.set("activeCourse", course)', timetable)
        self.assertIn("本周没有安排的课程", timetable)

    def test_schedule_empty_state_surfaces_refresh_failures_and_uses_explicit_refresh(self):
        timetable = self.modules["timetable.js"]
        # 空/过期快照保持诚实空态；partial_failures 存在时必须给出失败来源而不是静默空态
        self.assertIn("本周没有可显示的课表，可刷新读取。", timetable)
        self.assertIn("课表来源暂时不可用（", timetable)
        self.assertIn("partialFailureNames()", timetable.split("timetable_not_loaded:")[1].split("semester_start_required:")[0])
        # 空态的唯一动作是显式刷新（POST actions），而不是只读 GET 的空转
        self.assertIn('actionButton("刷新", () => void refresh(store))', timetable)
        self.assertNotIn('actionButton("刷新", () => void load(store))', timetable)
        # 刷新全失败（upstream_unavailable 仅在无任何本地数据时出现）不得伪称已显示本地课程
        self.assertNotIn("已显示本地已确认的课程", timetable)

    def test_schedule_partial_state_is_passive_and_stale_auto_refreshes_once(self):
        timetable = self.modules["timetable.js"]
        # partial 只出现在刷新响应里（GET 快照无 partial_failures）：数据是刚抓取的结果，
        # 只被动披露失败来源与已显示范围，不再催促刷新，也不在横幅内叠加第二个刷新入口
        partial = timetable.split("timetable_partial:")[1].split("timetable_verified:")[0]
        self.assertIn("部分课表来源暂不可用（", partial)
        self.assertIn("已显示已确认的课程", partial)
        self.assertIn("partialFailureNames()", partial)
        self.assertNotIn("load(store)", partial)
        self.assertNotIn("actionButton", partial)
        self.assertNotIn("可稍后刷新重试", timetable)
        # verified/empty 语义保持不变：verified 清状态、empty 只读空态
        verified = timetable.split("timetable_verified:")[1].split("empty:")[0]
        self.assertIn("clear(target)", verified)
        empty = timetable.split("empty:")[1].split("};")[0]
        self.assertIn("本周没有安排的课程。", empty)
        self.assertNotIn("actionButton", empty)
        # stale：本周加载自动触发一次后台刷新（episode/在途双防循环标志 + 被动在途文案）；
        # 自动刷新失败或结果仍 stale 时，手动回退横幅保留显式刷新动作
        stale = timetable.split("timetable_stale:")[1].split("timetable_partial:")[0]
        self.assertIn("正在显示上次同步的课表（缓存已过期），可刷新。", stale)
        self.assertIn('actionButton("刷新", retryRefresh)', stale)
        self.assertIn("课表缓存已过期，正在自动更新…", timetable)
        self.assertIn("staleEpisodeAutoRefreshed", timetable)
        self.assertIn("refreshInFlight", timetable)

    def test_catalog_recovery_stays_separate_from_timetable_diagnostics(self):
        timetable = self.modules["timetable.js"]
        # catalog 错误码不得进入课表状态机
        for code in ("catalog_payload_invalid", "catalog_timeout", "catalog_route_unavailable", "authorized_catalog_stale"):
            self.assertNotIn(code, timetable)
        # catalog 恢复面板与课表状态节点是相邻独立面板，互不嵌套
        start = self.html.index('id="catalog-recovery"')
        end = self.html.index('id="schedule-box"')
        self.assertLess(start, end)
        self.assertNotIn("schedule-state", self.html[start:end])

    def test_timetable_low_frequency_settings_live_in_disclosure(self):
        """SIMPLIFY-AUDIT-1 S1：低频课表设置只住在周课表弹窗折叠区——设置页
        「课表」组（双副本镜像）整组退役，弹窗内 details 是唯一入口。"""
        self.assertIn("tt-settings", self.html)
        self.assertNotIn('id="settings-timetable-group"', self.html)
        self.assertNotIn('id="settings-timetable-semester"', self.html)
        self.assertNotIn('data-settings-target="settings-timetable-group"', self.html)
        # NIGHT2-W3：「设置起始日」用 App 标准文字动作形态，不再是孤立的安静按钮
        self.assertIn(
            '<button class="btn-quiet" type="button" id="set-timetable-start">设置起始日</button>',
            self.html,
        )

    def test_help_entries_form_one_card_group(self):
        """NIGHT2-W4：帮助区「问题反馈/新手引导」收进整洁卡片入口组；
        既有 id（help-links / help-open-guide）必须保留（钉与引导流程依赖）。"""
        self.assertIn('<div id="help-links" class="help-entry-group">', self.html)
        self.assertIn('class="help-entry-row" href="https://github.com/gualtier-xu/Fudan-CourseLens-Worker/issues"', self.html)
        self.assertIn('id="help-open-guide" class="help-entry-row" type="button"', self.html)
        self.assertIn(".help-entry-group {", self.css)
        self.assertIn(".help-entry-group .help-entry-row {", self.css)
        self.assertIn("min-height: 44px;", self.css)

    def test_hls_vendor_script_is_deferred(self):
        """NIGHT2-W10：hls.js 厂商脚本 defer 化——不再阻塞首屏 HTML 解析；
        defer 保持文档序执行，app.js 运行前 window.Hls 仍已就绪。"""
        self.assertIn('<script src="/vendor/hlsjs/hls.min.js" defer></script>', self.html)
        self.assertNotIn('<script src="/vendor/hlsjs/hls.min.js"></script>', self.html)

    def test_cloud_control_card_renders_backend_actions_only(self):
        """NIGHT2-W9 / A26：学习页云卡只渲染快照 actions 闭集内的既有动作；
        危险动作两击确认；未知键与空行动集不渲染（卡隐藏）。"""
        study = self.modules["study.js"]
        self.assertIn('id="cloud-control-card"', self.html)
        self.assertIn("CLOUD_CONTROL_ACTION_TEXT = Object.freeze({", study)
        for action in (
            "run-now", "retry-import", "reset-circuit",
            "update-account", "revoke-cloud-credentials", "erase-cloud-data",
        ):
            self.assertIn(f'"{action}":', study)
        self.assertIn(
            'CLOUD_CONTROL_DANGEROUS = new Set(["revoke-cloud-credentials", "erase-cloud-data"])',
            study,
        )
        self.assertIn("armCloudControlConfirmation(button", study)
        self.assertIn("operationId(`cloud-${action}`)", study)
        self.assertIn("renderCloudControlCard();", study)
        self.assertIn(".cloud-control-card {", self.css)

    def test_student_reachable_error_codes_have_front_end_copy(self):
        """NIGHT2-W11 码表对账：后端可达且学生会看到的失败码必须有中文映射，
        不再落英文后端文本；证据面未知码保持诚实兜底（另有人性化表）。"""
        api = self.modules["api.js"]
        for code in (
            "bookmark_evidence_unavailable",
            "courseware_plan_mismatch",
            "cloud_secret_empty", "identity_unavailable",
            "personal_worker_migration_required", "cloud_verification_evidence_missing",
            "mailbox_repository_missing", "mailbox_repository_unavailable",
            "authorization_not_fresh", "repos_access_denied",
            "installation_scope_unavailable", "installation_selection_unknown",
        ):
            self.assertIn(f'{code}: "', api)
        self.assertIn("personal_worker_migration_required: [", self.modules["ui.js"])

    def test_get_transport_retry_is_envelope_aware(self):
        """NIGHT2-W20：GET-only 传输层重试只针对裸网关瞬态响应；应用信封
        的 5xx 与所有写操作都是终态，绝不在传输层重复提交。"""
        api = self.modules["api.js"]
        self.assertIn("TRANSIENT_STATUS = new Set([429, 502, 503, 504])", api)
        self.assertIn("RETRY_MAX_ATTEMPTS = 2", api)
        self.assertIn('const canRetry = options.method == null || String(options.method).toUpperCase() === "GET";', api)
        self.assertIn("responseCarriesAppError(response)", api)
        self.assertIn("Math.min(Math.max(jittered, retryAfter * 1000), 5000)", api)

    def test_player_keybind_pack_and_help_dialog(self):
        """NIGHT2-G-A / P16：J/L 大步跳转、</> 倍速档、? 键位帮助浮层——
        快捷键让位与播放器收权合同沿用既有守卫。"""
        player = self.modules["player-core.js"]
        self.assertIn('PLAYER_ALT_SEEK_SECONDS = 10', player)
        # N5PR-P2：步进集与 select 九档恒等（含 1.75）；长按临时倍速 3x 对齐 B 站
        self.assertIn("PLAYER_SPEED_STEPS = Object.freeze([0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3])", player)
        self.assertIn("PLAYER_HOLD_RATE = 3", player)
        self.assertIn('event.key === "j" || event.key === "J"', player)
        self.assertIn('event.key === "l" || event.key === "L"', player)
        self.assertIn('event.key === ">" || event.key === "<"', player)
        self.assertIn('event.key === "?"', player)
        self.assertIn('id="player-keys-dialog"', self.html)
        self.assertIn(".player-keys-rows {", self.css)

    def test_subtitle_ready_toasts_once_per_task(self):
        """NIGHT2-G-B / P32：字幕任务由进行中到 completed 轻播报恰一次；
        失败与首帧基线不播报，不跳转不打断。"""
        drawer = self.modules["tasks-drawer.js"]
        self.assertIn("announceNewlyCompletedSubtitleTasks", drawer)
        self.assertIn('toast("字幕已就绪，回到这一讲播放即可开启");', drawer)
        self.assertIn('taskBaselineSeeded = false;', drawer)
        self.assertIn('announcedCompletedTasks.add(id);', drawer)

    def test_privacy_section_retired_to_single_transparent_sentence(self):
        """CLOUD-TOGGLE-1 U3 + AS5 U3：隐私区三项重复入口（允许加密云端处理/
        记录本地学习统计/撤销云端授权行）退役，只留一句透明说明+《隐私与数据
        说明》查看入口；数据位置/保留期仍在页面上，可见范围/退出方式语义由
        文书承载（AS5PrivacyInsightPins 钉文书逐字恒等）。撤销云端授权迁入
        连接卡高级操作区。"""
        self.assertNotIn('id="processing-consent"', self.html)
        self.assertNotIn('id="analytics-consent"', self.html)
        self.assertNotIn('id="cloud-revoke-row"', self.html[self.html.find('id="settings-account-group"'):self.html.find('id="settings-ai-group"')], "隐私区不得再保留撤销云端授权行")
        # 一句要点仍讲清：存哪里/存多久；传什么/给谁/怎么退出在《隐私与数据说明》
        self.assertIn("你的学习资料只保存在这台电脑上", self.html)
        self.assertIn("云端最长保留 30 天", self.html)
        self.assertIn("查看《隐私与数据说明》", self.html)
        notice = (ROOT / "docs" / "privacy-notice.md").read_text(encoding="utf-8")
        self.assertIn("只有你勾选的课程会被处理", notice)
        self.assertIn("撤销云端授权，并删除云端凭据与状态", notice)
        # 撤销云端授权入口在唯一的云端处理控制面（连接卡高级操作与诊断）内保留
        self.assertIn('id="cloud-revoke-credentials"', self.html)
        revoke_at = self.html.find('id="cloud-revoke-row"')
        diagnostics_at = self.html.find('connection-diagnostics')
        self.assertGreater(revoke_at, diagnostics_at, "撤销云端授权应位于连接卡高级操作与诊断内")
        # 学习页指路注释同步新位置
        self.assertIn("撤销云端授权在「设置 → 网络与远程连接」的高级操作里", self.html)
        # 学生词汇：退出会话 → 退出登录（连接卡与账户菜单两处）
        self.assertNotIn(">退出会话<", self.html)
        self.assertIn('>退出登录</button>', self.html)
        self.assertIn('logout: "退出登录",', self.modules["ui.js"])

    def test_data_bulk_bar_explains_its_three_tiers(self):
        """NIGHT2-W12：数据管理批量条给可见三档标签与一句人话说明；
        仅文案+呈现，动作语义零变化。"""
        for label in ("随时可做", "释放空间", "不可恢复"):
            self.assertIn(f'<span class="data-group-tier">{label}</span>', self.html)
        self.assertIn('id="data-bulk-explain"', self.html)
        self.assertIn("「删除记录」不可恢复，仅删本机学习记录，课程和成绩不受影响。", self.html)
        self.assertIn('.data-group-tier {', self.css)

    def test_course_rows_support_keyboard_and_drag_reordering(self):
        """NIGHT2-W13：课程行支持拖拽与 Alt+↑/↓ 键盘重排；顺序只存本机
        视图偏好（专用键入存储清单），目录真值与既有点击行为零变化。"""
        study = self.modules["study.js"]
        self.assertIn('COURSE_ORDER_KEY = "courselens.course-order.v1"', study)
        self.assertIn("row.draggable = true;", study)
        self.assertIn('row.addEventListener("dragstart"', study)
        self.assertIn('row.addEventListener("drop"', study)
        self.assertIn('button.addEventListener("keydown"', study)
        self.assertIn('key !== "ArrowUp" && key !== "ArrowDown"', study)
        self.assertIn("function flipCourseRows(container, mutate)", study)
        self.assertIn('matchMedia("(prefers-reduced-motion: reduce)").matches', study)
        self.assertIn("courses = orderedCourses(courses);", study)

    # ---- search: single overlay, three states ----

    def test_search_is_one_overlay_with_three_states(self):
        palette = self.modules["search-palette.js"]
        self.assertIn('id="palette-root"', self.html)
        self.assertIn('id="palette-panel"', self.html)
        self.assertIn('id="palette-head"', self.html)
        self.assertIn('id="palette-answer-card"', self.html)
        # 唯一搜索入口：页面内不得再有第二个课程过滤输入框（旧 #catalog-query 已移除）
        self.assertNotIn('id="catalog-query"', self.html)
        self.assertNotIn('class="search-field"', self.html)
        self.assertIn("setMode(", palette)
        self.assertIn('paletteMode !== "compact"', palette)
        self.assertNotIn("courselens:search-page", palette)
        self.assertIn("search?q=${encodeURIComponent(query)}&limit=8", palette)
        self.assertIn("search?q=${encodeURIComponent(query)}&limit=50", palette)
        self.assertIn("search-index", palette)
        self.assertIn('postV3("search/answer"', palette)
        self.assertIn("course_ids", palette)
        self.assertIn("sub_id", palette)
        self.assertIn("SEARCH_INDEX_LABELS", palette)
        self.assertIn("没有匹配结果", palette)
        self.assertIn("检索失败", palette)

    def test_search_palette_keyboard_and_combobox_semantics(self):
        self.assertIn('role="combobox"', self.html)
        self.assertIn('aria-autocomplete="list"', self.html)
        self.assertIn('aria-controls="palette-listbox"', self.html)
        self.assertIn("aria-activedescendant", self.html)
        self.assertIn('role="listbox"', self.html)
        self.assertIn('setAttribute("role", "option")', self.modules["search-palette.js"])
        self.assertIn('setAttribute("role", "group")', self.modules["search-palette.js"])
        palette = self.modules["search-palette.js"]
        self.assertIn('event.key === "ArrowDown"', palette)
        self.assertIn('event.key === "ArrowUp"', palette)
        self.assertIn('event.key === "Enter"', palette)
        self.assertIn("renderOptions(", palette)

    def test_search_requests_abort_and_late_responses_are_isolated(self):
        palette = self.modules["search-palette.js"]
        self.assertIn("requestSeq", palette)
        self.assertIn("controller.abort()", palette)
        self.assertIn("AbortError", palette)
        self.assertIn("seq !== requestSeq", palette)

    def test_search_auth_gate_is_honest_with_actionable_entry(self):
        palette = self.modules["search-palette.js"]
        self.assertIn("登录后可搜索课程内容", palette)
        self.assertIn('courselens:open-login', palette)
        self.assertIn('store.auth?.state === "ready"', palette)

    def test_compact_palette_caps_results_and_keeps_actions(self):
        palette = self.modules["search-palette.js"]
        self.assertIn("8 - actions.length", palette)
        self.assertIn("GROUP_ORDER", palette)
        for group in ("课程", "字幕时间点", "课程讲次", "笔记 · 书签 · 资料", "操作"):
            self.assertIn(group, palette)
        self.assertIn('group: "课程"', palette)
        self.assertIn('dest: "学习 · 讲次列表"', palette)

    def test_ctrl_k_uses_the_visible_screen_trigger(self):
        palette = self.modules["search-palette.js"]
        self.assertIn("currentSearchTrigger", palette)
        self.assertIn('matches?.("[data-open-search]")', palette)
        self.assertIn('.page:not([hidden])', palette)

    # ---- playback / live recovery (behavior preserved) ----

    def test_playback_failures_have_closed_recovery_actions_without_raw_details(self):
        player = self.modules["player-core.js"]
        for element_id in (
            "player-recovery", "player-recovery-title", "player-recovery-impact", "player-recovery-actions",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        for code in (
            "media_authorization_required", "media_network_interrupted", "media_decode_failed",
            "media_source_unavailable", "media_source_unreachable", "media_format_unsupported",
        ):
            self.assertIn(code, player)
        for action in ("retry-media", "login"):
            self.assertIn(action, player)
        # N6L S1 U3：直播失败卡闭集整体移交 live-state.js（单源钉见
        # test_live_state_is_the_single_source_of_entry_semantics），学习桌
        # 播放器只保留回放失败码；live-refresh 事件由直播域派发
        self.assertNotIn("refresh-live", player)
        recovery_handlers = player.split("const showMediaFailure", 1)[1]
        self.assertNotIn("error.message", recovery_handlers)
        self.assertNotIn("manifestPath).textContent", recovery_handlers)

    def test_live_room_states_have_localized_reasons_and_legal_actions(self):
        live = self.modules["live-room.js"]
        state_source = self.modules["live-state.js"]
        self.assertIn('id="live-room-reason"', self.html)
        self.assertIn('id="live-room-capability"', self.html)
        self.assertIn('id="live-room-recheck"', self.html)
        for state in ("live", "upcoming", "ended", "denied", "offline", "stale", "unknown", "loading", "idle"):
            self.assertRegex(state_source, rf'{state}: Object\.freeze\(\{{ label: "[^"]+", reason: "[^"]+", action: "(?:enter|refresh)?" \}}\)')
        self.assertIn('ended: Object.freeze({ label: "直播已结束"', state_source)
        self.assertIn('denied: Object.freeze({ label: "无直播访问权限"', state_source)
        self.assertIn('action: ""', state_source)
        self.assertIn('$("enter-live-room").textContent = "进入直播";', live)
        self.assertIn('retry.textContent = "重新确认";', live)
        self.assertIn("liveCapabilityText", state_source)
        self.assertIn("直播状态待确认", state_source)
        self.assertIn('ENTRY_FAILURES[String(code || "")] ||', state_source)
        self.assertIn("live_playback_not_open", state_source)
        self.assertNotIn("error.message", live + state_source)
        self.assertNotIn("error.code}", live + state_source)

    def test_live_state_is_the_single_source_of_entry_semantics(self):
        """N6L S1：闭集同源钉——live-room/home-overview/live-page 三消费点
        都从 live-state.js 取状态语义与文案，页内不得有第二份定义。"""
        state_source = self.modules["live-state.js"]
        for module in ("live-room.js", "home-overview.js", "live-page.js", "live-player.js"):
            self.assertIn('from "./live-state.js"', self.modules[module])
        for code in (
            "live_authorization_denied", "live_authorization_revoked", "live_grant_invalid",
            "live_grant_identity_changed", "live_session_expired", "live_resource_expired",
            "live_upstream_rejected", "live_upstream_unreachable", "live_stream_unavailable",
            "live_playback_not_open",
        ):
            self.assertIn(code, state_source)
        # 跳转协议：卡片进入动作统一写 liveTarget+select-page live
        for module in ("live-room.js", "home-overview.js"):
            self.assertIn('store.set("liveTarget"', self.modules[module])
            self.assertIn('new CustomEvent("courselens:select-page", { detail: "live" })', self.modules[module])

    def test_live_view_bar_and_transcript_panel_contract(self):
        """直播二期（Prompt 42 乙2/乙3）：视角条+文稿侧板的前端契约钉。"""
        live_page = self.modules["live-page.js"]
        state = self.modules["live-state.js"]
        # 乙2：视角条纯模型唯一出处；切换=establishSession 带 view；防抖+回退
        self.assertIn("export function resolveViewBar", state)
        self.assertIn('DEFAULT_LIVE_VIEW = "student"', state)
        self.assertIn("const switchView = async (view)", live_page)
        self.assertIn("if (disposed || switchingView || !LIVE_VIEW_META[view]) return", live_page)
        self.assertIn("await establishSession(course.course_id, epoch, view)", live_page)
        self.assertIn("await switchView(DEFAULT_LIVE_VIEW)", live_page)  # 失败回退默认视角
        self.assertIn('postV3("live-room/sessions", { grant: grant.grant, view })', live_page)
        self.assertIn('aria-pressed', self.modules["live-page.js"])
        # 乙3：文稿侧板——双源归一/增量身份合并/闭集提示/零自动定时器
        self.assertIn("export function mergeTranscriptSegments", state)
        self.assertIn("export function resolveTranscriptOutcome", state)
        self.assertIn("apiV3(`transcript/segments?sub_id=${encodeURIComponent(subId)}${sinceQuery}`)", live_page)
        self.assertIn("void loadTranscript({ reset: true }); /* 乙3 触发源①：进直播 */", live_page)
        self.assertIn("/* 乙3 触发源②：手动开板 */", live_page)
        # 文稿侧板零自动轮询（LIVEEXP-1 起：全文唯一 setInterval=时间驱动重估
        # tick，非文稿/会话链；tick 本地零请求，网络仍只在相位翻转/边界临近时发生）
        transcript_section = live_page.split("乙3：文稿侧板", 1)[1].split("页面显隐与刷新事件", 1)[0]
        self.assertNotIn("setInterval", transcript_section)
        self.assertEqual(live_page.count("setInterval("), 1)
        self.assertIn("window.setInterval(handleRecheckTick", live_page)
        # 结构：视角条与文稿侧板元素在位；样式走 live.css
        for element_id in ("live-view-bar", "live-view-hint", "live-transcript", "live-transcript-toggle", "live-transcript-list"):
            self.assertIn(f'id="{element_id}"', self.html)
        css = self.css_by_name["live.css"]
        self.assertIn(".live-view-option[aria-pressed=\"true\"]", css)
        self.assertIn(".live-view-option.live-view-audio[data-suggest=\"true\"]", css)
        self.assertIn(".live-transcript-row", css)

    def test_live_entry_rechecks_on_time_passage(self):
        """LIVEEXP-1：直播入口时间驱动重估同源钉——课表相位纯函数唯一出处
        live-state.js，home 卡/学习页直播行/直播页三消费面共用；过窗措辞如实
        「已结束」（绝不把结束的课写成「开始」）；dispose 清重估定时器。"""
        state = self.modules["live-state.js"]
        home = self.modules["home-overview.js"]
        room = self.modules["live-room.js"]
        page = self.modules["live-page.js"]
        self.assertIn("export function schedulePhase", state)
        self.assertIn("export function scheduleBoundaryDistance", state)
        self.assertIn("export function liveRecheckDecision", state)
        self.assertIn("export const LIVE_RECHECK_INTERVAL_MS = 30000", state)
        self.assertIn("export function expirePastMeetingObservation", state)
        for module in (home, room, page):
            self.assertIn("schedulePhase", module)
            self.assertIn("const handleRecheckTick", module)
            self.assertIn("window.setInterval(handleRecheckTick", module)
            self.assertIn("window.clearInterval(recheckTimer)", module)
        # home 卡：注入时钟安装 + 过窗措辞 + 相位退出进行中时红点同源收敛
        self.assertIn("clock = () => new Date()", home)
        self.assertIn("按课表 ${meeting.start_time}–${meeting.end_time} 已结束", home)
        self.assertIn("expirePastMeetingObservation", home)
        self.assertIn("liveRecheckDecision", home)
        # 学习页直播行：headline 相位三分支（进行中/已结束/开始）
        self.assertIn("按课表进行中 · ${meeting.start_time}–${meeting.end_time}", room)
        self.assertIn("按课表 ${meeting.start_time}–${meeting.end_time} 已结束", room)
        self.assertIn("按课表 ${meeting.start_time} 开始", room)
        # 直播页：页内可见守卫 + 过窗措辞 + 播放/过渡态守卫（媒体链自治不被打扰）
        self.assertIn("pageVisible", page)
        self.assertIn("按课表 ${meeting.start}–${meeting.end} 已结束", page)
        self.assertIn('["playing", "buffering", "behind", "weaknet", "connecting", "recovering", "paused"].includes(phase.playback)', page)

    def test_unchanged_auth_poll_preserves_catalog_dom_and_focus(self):
        study = self.modules["study.js"]
        self.assertIn("if (fingerprint === lastAuthFingerprint) return", study)
        self.assertIn("if (fingerprint === lastCatalogFingerprint) return", study)
        self.assertIn("focus({ preventScroll: true })", study)
        self.assertIn('aria-busy="false"', self.html)

    def test_account_entry_uses_session_semantics_before_credentials(self):
        shell = self.modules["shell.js"]
        settings = self.modules["settings.js"]
        self.assertIn('$("account-text").textContent = ready ? "账户"', shell)
        self.assertIn('window.dispatchEvent(new CustomEvent("courselens:logout"))', shell)
        self.assertIn('window.addEventListener("courselens:logout", handleLogoutRequest)', settings)
        self.assertIn('"切换或重新认证"', shell + settings)
        self.assertIn('id="account-session-state"', self.html)
        self.assertIn('id="account-menu-settings"', self.html)
        self.assertIn('id="settings-theme-mode"', self.html)
        self.assertIn("applyThemePreference(event.target.value)", settings)
        self.assertIn('$("settings-theme-mode").value = readThemePreference()', settings)
        self.assertIn('window.requestAnimationFrame(() => $("login-student-id").focus({ preventScroll: true }))', shell)  # FOCUS-DRIFT-1: 程序化聚焦一律防跳滚

    def test_appearance_ui_font_tier_contract(self):
        """A11Y-IMPL-4（D14 P1-1 产品步）：界面字号三档——设置外观档位、根字号
        CSS 档位选择器、偏好闭集模块、装配接线与重置闭集键的跨文件合同。"""
        settings = self.modules["settings.js"]
        # 外观区档位控件：主题 select 同款形态（label>select[data-dropdown] 三 option 闭集）
        self.assertIn('<label>界面字号<select id="settings-ui-font" data-dropdown aria-label="界面字号">'
                      '<option value="default">默认</option><option value="large">大</option>'
                      '<option value="xlarge">特大</option></select></label>', self.html)
        self.assertIn("看不清可以把界面文字整体放大，选完立即生效；只保存在这台电脑上，重置应用时会一并清除。", self.html)
        # rem 地基：根字号档位是全站字号唯一总开关（默认档不落属性=与改前逐值同）；
        # ÷16 精确小数（16=2⁴）保证默认档像素等价
        tokens_css = self.css_by_name["tokens.css"]
        self.assertIn("html { font-size: 100%; }", tokens_css)
        self.assertIn('html[data-ui-font="large"] { font-size: 112.5%; }', tokens_css)
        self.assertIn('html[data-ui-font="xlarge"] { font-size: 125%; }', tokens_css)
        self.assertIn("font-size: 1.03125rem;", tokens_css)
        self.assertIn("button, input, select, textarea { font-size: 1rem; letter-spacing: 0; }", tokens_css)
        self.assertNotIn("font-size: 16.5px", tokens_css)
        self.assertNotIn("font-size: 16px", tokens_css)
        # 字幕域独立字号制：video::cue 与 --sub-font-size 不随界面档缩放（刻意双轨，
        # 字幕有自己的 xs-lg 四档；::cue 对 CSS 变量不可靠是记录在案的设计例外）
        self.assertIn("font-size: 17px;", self.css_by_name["components.css"])
        self.assertIn("font-size: var(--sub-font-size, 17px)", self.css)
        self.assertNotIn("0.82rem", self.css)  # 机械替换必须 ÷16 精确小数，禁近似值
        # 偏好闭集模块与装配接线（形态沿 P56-U1 主题三态先例：读闭集/写持久/应用回同步）。
        # 位置纪律：偏好写入方居 modules/ 顶层（ui-font.js）——settings 家族带
        # 「永不触碰浏览器本地存储」隐私钉（凭据毗邻面，test_remote_compute_ui
        # fail-closed 钉），浏览器侧偏好写入方依先例居顶层（主题=shell.js 同轨）。
        ui_font = self.modules["ui-font.js"]
        self.assertIn('UI_FONT_PREF_KEY = "courselens.ui-font.v1"', ui_font)
        self.assertIn('UI_FONT_TIERS = Object.freeze(["default", "large", "xlarge"])', ui_font)
        self.assertIn("export function readUiFontPreference(storage", ui_font)
        self.assertIn("export function applyUiFontAtStartup(storage", ui_font)
        self.assertIn("export function applyUiFontPreference(preference, storage", ui_font)
        self.assertIn("applyUiFontAtStartup();", settings)
        self.assertIn("applyUiFontPreference(event.target.value)", settings)
        self.assertIn('$("settings-ui-font").value = readUiFontPreference()', settings)
        self.assertIn('$("settings-ui-font").removeEventListener("change", handleUiFontChange)', settings)
        # 重置浏览器侧闭集键含界面字号（与 client-reset 清除面、index.html 文案三处同话）
        self.assertIn('"courselens.ui-font.v1"', settings)
        self.assertEqual(self.html.count("界面字号、课程排序、学期筛选"), 2)

    def test_settings_is_a_plain_page_with_progressive_disclosure(self):
        settings = self.modules["settings.js"]
        self.assertIn('id="settings-page"', self.html)
        self.assertIn('class="settings-nav"', self.html)
        self.assertIn('id="proxy-url-row" hidden', self.html)
        self.assertIn("自动（复旦服务直连）", self.html)
        self.assertIn("自动模式：复旦的网站直接连、不走你的代理软件", self.html)
        self.assertIn('auto: "自动（复旦服务直连）"', settings)
        self.assertIn('$("proxy-url-row").hidden = $("network-mode").value !== "manual";', settings)
        self.assertIn('event.detail === "settings"', settings)
        self.assertNotIn("openOverlay({ root: $(\"settings", settings)
        for group in ("settings-account-group", "settings-ai-group", "settings-network-group", "settings-update-group"):
            self.assertIn(f'id="{group}"', self.html)

    def test_media_error_card_settings_pointers_have_anchors(self):
        """MEDIA-UI-1：错误卡「设置页指路」锚点核对——细分卡文案指名的设置页
        开关必须同名实装（player-core.js 文案只读钉，防指路漂移）。"""
        player = self.modules["player-core.js"]
        # MEDIA-VPN-1 第三细分卡（vpn_present）指路的「媒体流走系统代理」开关。
        self.assertIn("可在设置开启「媒体流走系统代理」", player)
        self.assertIn("媒体流走系统代理", self.html)
        self.assertIn('id="media-stream-proxy"', self.html)
        # WEBVPN-AUTO-1 校外媒体区：用户开关已移除（全自动）。SETTINGS-UX-1 B3
        # （化身走查 F4）：说明收为「网络」节尾一行 hint（不再独占一节）——
        # 静态说明文案为稳定锚点；设定面回归钉反向钉死（零残留）。
        self.assertNotIn("<h3>校外媒体</h3>", self.html)
        self.assertIn("会自动尝试经学校 WebVPN 中转，无需设置", self.html)
        self.assertNotIn('id="media-webvpn-relay"', self.html)
        settings = self.modules["settings.js"]
        self.assertNotIn("set-media-webvpn-relay", settings)
        self.assertNotIn('id="media-webvpn-relay"', settings)

    def test_think_ladder_quality_chip_and_term_region_anchors(self):
        """THINK-LADDER-2：质量 chip 挂点 + 课程记忆候选复核区锚点与闭集文案钉。

        总结卡头部 chip 挂载点、findings 展开块、复习面复核区三个 DOM 锚
        必须同名实装；quality-chip.js 的三态文案、自动晋升人话标、动作闭集
        为跨模块稳定锚。"""
        self.assertIn('id="quality-chip-mount"', self.html)
        self.assertIn('id="quality-findings"', self.html)
        self.assertIn('id="term-candidate-region"', self.html)
        chip = self.modules["quality-chip.js"]
        # 抽检三态文案（passed/suspect/none）+ findings 诚实提示
        self.assertIn("抽检通过", chip)
        self.assertIn("抽检完成", chip)
        self.assertIn("未抽检", chip)
        self.assertIn("提醒仅供参考，内容本身可以直接用", chip)
        # 自动晋升人话标 + 闭集动作面（THINK-LADDER-1 撤销通道的消费点）
        self.assertIn("自动确认 · 信号 ×", chip)
        self.assertIn('"confirm_term_candidate"', chip)
        self.assertIn('"dismiss_term_candidate"', chip)
        self.assertIn('"course-review/actions"', chip)
        # 复核区渲染接线与空态整区隐藏（无空壳口径）
        review = self.modules["course-review.js"]
        self.assertIn("renderTermCandidateRegion", review)
        self.assertIn('$("term-candidate-region")', review)
        self.assertIn("normalizeTermCandidateView", review)
        study = self.modules["study.js"]
        self.assertIn("refreshQualityChip", study)

    def test_client_reset_danger_zone_contract(self):
        """CLIENT-RESET-1：设置危险区单入口 + typed 弹窗 + 两勾选项（默认不勾）。"""
        settings = self.modules["settings.js"]
        # 单入口分组 + 危险区卡 + typed 弹窗（与 mailbox 修复弹窗同构）
        self.assertIn('id="settings-reset-group"', self.html)
        self.assertIn('data-settings-target="settings-reset-group"', self.html)
        self.assertIn('class="danger-zone"', self.html)
        self.assertIn('id="client-reset-open"', self.html)
        self.assertIn('id="client-reset-dialog"', self.html)
        # 两勾选项默认不勾（checkbox 且无 checked 属性），文案为合同闭集
        self.assertIn('id="client-reset-delete-derived" type="checkbox">', self.html)
        self.assertIn('id="client-reset-delete-repos" type="checkbox">', self.html)
        self.assertIn("同时删除课程派生产物（字幕 / AI 产物 / 课件）", self.html)
        self.assertIn("同时删除 GitHub 专属仓库", self.html)
        # typed 门在客户端预置：恰为「重置」才启用，确认语原样随请求
        self.assertIn('confirmButton.disabled = input.value !== RESET_TEXT.confirmWord;', settings)
        self.assertIn('confirmWord: "重置",', settings)
        self.assertIn('confirm_typed: input.value,', settings)
        self.assertIn('postV3("client-reset/actions"', settings)
        self.assertIn('operation_id: operationId("client-reset")', settings)
        # 结果/阻塞态为闭集文案呈现，不渲染原始异常
        self.assertIn('RESET_TEXT.done', settings)
        self.assertIn('code === "reset_blocked" ? RESET_TEXT.blocked', settings)
        self.assertIn('id="client-reset-result" class="hint" role="status"', self.html)
        self.assertIn('id="client-reset-error" class="field-error" role="alert"', self.html)
        # 危险区按钮沿用既有 danger 样式；不新增秘密输入（全局 count=2 另有钉）
        self.assertIn('id="client-reset-open" class="danger" type="button"', self.html)
        self.assertIn(".danger-zone {", self.css)
        # NIGHT2-W1：警示框文字距左侧 3px 红饰条留出呼吸空隙（token spacing，占位不变）
        self.assertIn("padding: 10px var(--space-2) 10px var(--space-4);", self.css)

    def test_reset_manual_repo_deletion_is_closed_set_and_name_blind(self):
        """NIGHT-FRONT-1 T3 不可删类降级：权限类拒绝不中断本地重置；回执的
        repos_manual_deletion 走闭集过滤（settings 受信正则 + reason 闭集 +
        仓库名形状），链接直达 Settings 页、标签只用序号（仓库名单不回显），
        并带删后验证说明；非受信形态绝不渲染为链接。"""
        settings = self.modules["settings.js"]
        self.assertIn(
            "const GITHUB_REPO_SETTINGS_URL_RE = "
            "/^https:\\/\\/github\\.com\\/[A-Za-z0-9-]+\\/[A-Za-z0-9._-]+\\/settings$/;",
            settings,
        )
        self.assertIn("function validManualDeletions(result)", settings)
        self.assertIn('item.reason === "app_token_cannot_delete"', settings)
        self.assertIn("GITHUB_REPO_FULL_NAME_RE.test(item.repo)", settings)
        self.assertIn("GITHUB_REPO_SETTINGS_URL_RE.test(item.settings_url)", settings)
        self.assertIn("repos_manual_deletion", settings)
        self.assertIn("待手动删除仓库", settings)
        self.assertIn("删除专属仓库 ${index + 1}", settings)
        self.assertIn("确认仓库已删除", settings)
        # 后端：权限类拒绝降级为手删回执，瞬态失败仍中止
        source = (ROOT / "src" / "application.py").read_text(encoding="utf-8")
        self.assertIn("if exc.code != \"permission_denied\":", source)
        self.assertIn('"reason": "app_token_cannot_delete",', source)
        self.assertIn("repos_manual_deletion", source)

    def test_study_automation_table_covers_backend_codes_and_fake_mirror_enforces(self):
        """NIGHT-FRONT-1 T4：study.js 自动整理码表补齐 W1 六后端闭集码的可行动
        文案（每个码在 src/runtime/automation.py 真实存在）；FakeRemote 镜像对
        引擎自有语义面（DELETE 与仓库详情 GET）按真客户端闭集映射强制 expected，
        子路径探测的已知漂移显式记录为 blocker 而非静默放行。"""
        study = self.modules["study.js"]
        automation = (ROOT / "src" / "runtime" / "automation.py").read_text(encoding="utf-8")
        for code in (
            "automation_action_invalid", "operation_id_invalid", "cloud_artifact_empty",
            "cloud_result_invalid", "cloud_result_key_missing", "cloud_cleanup_pending",
        ):
            self.assertIn(f'{code}: "', study, f"study.js 码表缺 {code}")
            self.assertIn(f'"{code}"', automation, f"后端不存在码 {code}")
        self.assertIn("请重新运行一次自动整理", study)
        reset_tests = (ROOT / "tests" / "test_developer_first_install_reset.py").read_text(encoding="utf-8")
        self.assertIn("_STATUS_CODE_MAP = {", reset_tests)
        self.assertIn('404: "resource_missing",', reset_tests)
        self.assertIn("def _enforce_expected(method", reset_tests)
        self.assertIn('if method == "DELETE":', reset_tests)
        self.assertIn("worker_migration", reset_tests)

    def test_probe_repos_denied_pre_install_gets_dedicated_installation_guidance(self):
        """NIGHT-FRONT-1 T6 深分类收尾：未安装 + 仓库级 403 → 专用安装指引
        （解释「安装前读不了仓库属正常」），主按钮为安装语义，绝不「先创建
        仓库」；后端把闭集端点类挂进 installation 证据，五个下游组件不再以
        repos_access_denied 独立故障行出现。"""
        settings = self.modules["settings.js"]
        self.assertIn("function installationReposDenied(value)", settings)
        self.assertIn("const REMOTE_INSTALLATION_MISSING_REPOS_DENIED_GUIDANCE", settings)
        self.assertIn("evidence.repos_denied", settings)
        self.assertIn("REMOTE_INSTALLATION_MISSING_REPOS_DENIED_GUIDANCE;", settings)
        self.assertIn("const reposDenied = Boolean(installationReposDenied(value));", settings)
        self.assertIn("安装完成前，GitHub 会拒绝读取两个专属仓库", settings)
        connection = (ROOT / "src" / "remote" / "connection.py").read_text(encoding="utf-8")
        self.assertIn('evidence["repos_denied"] = repos_denied', connection)
        self.assertIn("if repos_denied and installed:", connection)
        self.assertIn("elif not repos_denied:", connection)

    def test_visual_contract_probe_radii_shadows_spacing_tokens_and_display_cjk(self):
        """NIGHT-FRONT-1 T8 美学合同自动化探针（全 styles 静态扫描）：
        ①字距闭集 {0, 0.01em 字标, 0.12em 小型大写}；②圆角闭集（两档 token、
        50%、微圆角 2px/1px、0——组合值拆分校验）；③阴影闭集（--shadow token
        【=none】、同色内嵌环、已钉聚焦环恒暗 chrome 族 --stage-*〔批β token 化〕）；④tokens.css 之外的十六进制色
        闭集（渐变/暗色覆盖等已裁定字面）；⑤display 衬线轨必须含 CJK 字形族
        （CJK 标题衬线拍板=方案 a 系统栈，防回退 Latin-only）。此后任何越界
        改动直接红。"""
        styles = self.css_by_name
        body = "\n".join(text for name, text in styles.items() if name != "tokens.css")
        spacings = {value.strip() for value in re.findall(r"letter-spacing:\s*([^;\n]+);", body)}
        self.assertTrue(
            spacings <= {"0", "0.01em", "0.12em"},
            f"字距越闭集: {sorted(spacings - {'0', '0.01em', '0.12em'})}",
        )
        radii = set()
        for text in styles.values():
            for value in re.findall(r"border-radius:\s*([^;\n]+);", text):
                radii.update(part.strip() for part in value.split())
        allowed_radii = {"var(--radius)", "var(--radius-panel)", "var(--radius-lg)", "50%", "2px", "1px", "0"}
        self.assertTrue(
            radii <= allowed_radii,
            f"圆角越闭集: {sorted(radii - allowed_radii)}",
        )
        for line in body.splitlines():
            stripped = line.strip()
            if "box-shadow:" not in stripped:
                continue
            ok = (
                "var(--shadow" in stripped
                or "inset" in stripped
                or "var(--stage-" in stripped
                # LIGHT-THEME-1 越族收口：conn-breath 呼吸光环自 --stage-accent-soft
                # （恒暗族）改主题 gold 同 alpha 洗色；主题 token 的 color-mix 与
                # stage 前缀同级合规（暗色 --gold=#D19E3F 与 stage 值逐位同源）。
                or "color-mix(in srgb, var(--gold)" in stripped
                or stripped == "box-shadow: none;"  # U1 三态焦点政策：指针态下键盘强调形的显式归零
            )
            self.assertTrue(ok, f"阴影越闭集: {stripped}")
        hex_values = {value.lower() for value in re.findall(r"#[0-9A-Fa-f]{3,8}\b", body)}
        allowlist = {
            "#071d33", "#151210", "#ece7db", "#efe9dc", "#ede7db",
            "#ffffff", "#d19e3f", "#f0c97e",
        }
        self.assertTrue(
            hex_values <= allowlist,
            f"新增非 token 色: {sorted(hex_values - allowlist)}",
        )
        # CJK 标题衬线拍板（方案 a：零文件系统栈）防回退钉
        self.assertIn("--font-display:", styles["tokens.css"])
        self.assertIn("KaiTi", styles["tokens.css"])
        self.assertIn("宋体", styles["tokens.css"])

    def test_connection_cards_use_closed_set_auto_connect_without_new_secret_inputs(self):
        settings = self.modules["settings.js"]
        # 偏好变更只走既有 settings/actions 闭集动作；被拒文案为可操作闭集
        self.assertIn('action: "set-auto-connect"', settings)
        self.assertIn("AUTO_CONNECT_ERROR_TEXT", settings)
        # 秘密输入闭集：登录密码 + DeepSeek Key 两个凭据输入不变；D12 数据
        # 主权 P0 新增的是搬家包口令三只（导出+确认+导入）——用户自设包口令，
        # 永不持久化、不进凭据面（credentials.json），不属「账户秘密输入」。
        self.assertEqual(self.html.count('type="password"'), 5)
        # GitHub 卡展示固定四阶段产品语义，而非蛇形组件行
        for phase in ("账号授权", "专属仓库", "App 安装", "加密通道"):
            self.assertIn(phase, self.html)
        # 自动连接开关为 role=switch 的既有复选样式
        self.assertIn('id="fudan-auto-connect" type="checkbox" role="switch"', self.html)
        self.assertIn('id="github-auto-connect" type="checkbox" role="switch"', self.html)
        # 设置页加载绝不自动触发远程动作（授权/安装/初始化只能由用户点击发起）
        load_remote_body = settings[
            settings.index("async function loadRemote"):settings.index("async function remoteAction")
        ]
        self.assertNotIn("remoteAction(", load_remote_body)
        # GitHub 卡反馈闭集：permission_denied 细分——仅身份/令牌端点被拒才与 revoked
        # 同族 force 重新授权（后端 github_app.py 同规则清授权）；安装/范围类 403
        # 保留授权，走端点类闭集细分指引，不再推进「重新授权还是报错」循环
        self.assertIn(
            'permission_denied: { action: "start-authorization", label: "重新授权并创建专属仓库", force: true }',
            settings,
        )
        self.assertIn("REMOTE_PERMISSION_GUIDANCE_BY_ENDPOINT", settings)
        for endpoint_class in ("user_identity", "token_endpoint", "user_installations", "user_repos", "repos_detail", "repos_actions"):
            self.assertIn(f'{endpoint_class}: "', settings)
        # permission_denied 端点类证据只认 authorization 组件的闭集 evidence 字段
        self.assertIn("function authorizationEndpointClass(", settings)
        self.assertIn("evidence.endpoint_class", settings)
        # 非身份/令牌端点类：有受信安装链接则引导安装，否则仅诊断，绝不 force 重新授权
        self.assertIn('endpointClass !== "user_identity" && endpointClass !== "token_endpoint"', settings)
        self.assertIn('{ action: "bootstrap", label: "完成 App 安装后继续初始化", withInstallLink: true }', settings)
        self.assertIn("REMOTE_ACTION_SUCCESS_TEXT", settings)
        self.assertIn("已重新诊断，结果见下方连接状态", settings)
        self.assertIn("诊断结果：", settings)

    def test_remote_card_flags_cross_account_stale_binding_with_closed_set_copy(self):
        # INSTALL-BINDING-OWNER-1：跨账号陈旧绑定读侧忽略——提示行只在
        # worker/mailbox 组件 evidence 携带 binding_owner_mismatch 真值时渲染；
        # 文案为闭集常量，绝不拼接账号名或仓库名；无标记场景按构造零受扰。
        settings = self.modules["settings.js"]
        self.assertIn(
            'const BINDING_OWNER_MISMATCH_TEXT = "检测到其他账号的仓库绑定，已忽略；请为当前账号创建专属仓库";',
            settings,
        )
        self.assertIn("function remoteBindingOwnerMismatch(value)", settings)
        self.assertIn('["worker_repository", "mailbox_repository"]', settings)
        # 标记只认组件 evidence 的闭集布尔真值形状
        self.assertIn("evidence.binding_owner_mismatch === true", settings)
        # 渲染点在 renderRemote 证据区：仅标记时追加一行
        render_body = settings[settings.index("function renderRemote"):settings.index("/* 设备码进度")]
        self.assertIn("if (remoteBindingOwnerMismatch(value)) {", render_body)
        self.assertIn("target.append(textElement(\"span\", BINDING_OWNER_MISMATCH_TEXT));", render_body)

    def test_settings_navigation_keeps_focus_on_compact_targets(self):
        settings = self.modules["settings.js"]
        # 指针与键盘激活都把焦点留在激活的导航按钮上；分组大容器/标题不再成为
        # 编程焦点目标（大容器 outline 根因），也不是全局禁用轮廓
        self.assertIn("group.scrollIntoView({ block: \"start\" });", settings)
        self.assertNotIn('group.querySelector("h2")?.focus?', settings)
        self.assertNotIn("group?.focus?.({ preventScroll: true });", settings)
        # 针对性保险而非全局禁用轮廓：容器抑制 + tabindex=-1 编程目标全局收敛
        self.assertIn(".settings-group:focus, .settings-group:focus-visible { outline: none; }", self.css)
        self.assertIn(
            ".settings-group > h2:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }",
            self.css,
        )
        # U1 三态焦点政策：标题环只在键盘来源（:focus-visible）；指针态由
        # data-input 追踪器归零，鼠标点击零环，Tab 恒环不变
        self.assertNotIn(".settings-group > h2:focus,", self.css)
        self.assertIn(':root[data-input="pointer"] :focus,', self.css)
        for heading in ("settings-account-heading", "settings-network-heading"):
            self.assertIn(f'id="{heading}" tabindex="-1"', self.html)

    def test_login_dialog_saved_account_selector_contract(self):
        settings = self.modules["settings.js"]
        # 选择器只消费非秘密元数据（student_id + requires_rotation），永不展示密码
        self.assertIn('id="login-saved"', self.html)
        self.assertIn('id="login-saved-options"', self.html)
        self.assertIn('id="login-use-saved"', self.html)
        self.assertIn('id="login-manual-mode" class="login-mode-link" type="button" aria-pressed="false"', self.html)
        self.assertIn('id="login-saved-status" class="hint" role="status" aria-live="polite"', self.html)
        # 不新增凭据秘密输入；搬家包口令三只见 D12（永不持久化，非凭据面）
        self.assertEqual(self.html.count('type="password"'), 5)
        # use-saved 一键免密：显式动作 + 不发送空密码 + 与手动登录同一进度轮询
        self.assertIn("async function refreshLoginSavedAccounts()", settings)
        self.assertIn("apiV3(\"accounts\")", settings)
        self.assertIn("action: \"use-saved\"", settings)
        self.assertIn("if (!account || account.requires_rotation) return; /* 空密码绝不发送 */", settings)
        self.assertIn("const handleLoginUseSaved", settings)
        use_saved_body = settings[settings.index("const handleLoginUseSaved"):settings.index("const handleLoginManualMode")]
        self.assertIn("startLoginPoll()", use_saved_body)
        self.assertNotIn("password", use_saved_body)
        # 恰一个可用账号预选；多个不代选；rotation 不可一键使用
        self.assertIn("usable.length === 1", settings)
        self.assertIn("useButton.disabled = !selectedUsable || loginInFlight;", settings)
        # 打开即刷新（保存/删除后下次打开即最新）；手动模式是显式动作
        self.assertIn("void refreshLoginSavedAccounts();", settings)
        self.assertIn('$("login-manual-mode").setAttribute("aria-pressed", "true");', settings)
        # onboarding 步进：焦点留在激活控件，步骤变化走既有 live region 播报
        onboarding = self.modules["onboarding.js"]
        self.assertNotIn("heading.focus", onboarding)
        self.assertIn("announce(`第 ${currentStep} 步：", onboarding)

    def test_login_dialog_is_one_identity_card_with_one_primary_path(self):
        """NIGHT-FRONT-1 T7（条目1定稿）：saved 与 manual 是同一张身份卡的两个
        状态——状态A=身份行（衬线学号）+ 唯一主按钮「一键登录」+ 渐进披露文字链；
        状态B=同卡展开手动表单（「← 返回已保存账号」）。登录请求面与账号 API
        消费语义零变化：use-saved 空密码门、同一进度轮询、rotation 预填不动。"""
        settings = self.modules["settings.js"]
        html = self.html
        # 状态A：一键主按钮 + 文字链；状态B：返回链 + 手动区容器/动作行
        self.assertIn('<button id="login-use-saved" class="primary login-oneclick" type="button" disabled>一键登录</button>', html)
        self.assertIn('id="login-back-saved" class="login-mode-link" type="button" hidden', html)
        self.assertIn('<div id="login-manual-fields" class="login-manual-fields">', html)
        self.assertIn('id="login-manual-actions"', html)
        self.assertNotIn('class="dialog-actions login-saved-actions"', html, "双主按钮排已退役")
        # Windows 加密说明收为 checkbox 次行小字（登录框独立段落已删；
        # 新手引导登录步骤仍保留同文提示，全局恰 1 处）
        self.assertIn('check-row"><input id="remember-account" type="checkbox">保存在本机<span class="hint check-hint">使用 Windows 加密保护', html)
        self.assertEqual(html.count("<p class=\"hint\">使用 Windows 加密保护"), 1)
        # 状态机：隐藏的必填输入同步禁用（HTML5 校验不检查 disabled 输入）
        self.assertIn("function renderLoginMode()", settings)
        self.assertIn("const identityMode = hasUsable && !loginManualMode;", settings)
        self.assertIn("section.hidden = loginManualMode;", settings)
        self.assertIn("node.disabled = identityMode;", settings)
        # rotation 选择即转手动态（预填学号不变）；返回链恢复身份卡
        self.assertIn("loginManualMode = true;\n    renderLoginMode();", settings)
        self.assertIn("const handleLoginBackSaved", settings)
        self.assertIn('$("login-back-saved").addEventListener("click", handleLoginBackSaved);', settings)
        # 身份行 radio 语义（aria-pressed 兼容钉保留）
        self.assertIn('option.setAttribute("role", "radio");', settings)
        self.assertIn('option.setAttribute("aria-checked", String(selected));', settings)
        # 视觉语法：身份行走 display 衬线轨、文字链 44px 触达、旧双按钮排样式退役
        self.assertIn(".login-saved-option-id {", self.css)
        self.assertIn(".login-oneclick { width: 100%; }", self.css)
        self.assertIn(".login-mode-link {", self.css)
        self.assertNotIn(".login-saved-actions {", self.css)

    def test_login_dialog_auto_connect_option_contract(self):
        """SMALL-POLISH-1①：登录框「开启后自动登录」复选——身份卡内入口，
        初始态对齐既有 auto_connect 偏好，勾选即走既有 set-auto-connect 闭集
        动作与前置拦截；文案闭集，视觉与登录卡一致（复选+次行小字）。"""
        settings = self.modules["settings.js"]
        # 身份卡内复选 + 次行说明小字（闭集文案，落在主按钮与文字链之间）
        self.assertIn(
            '<label class="check login-auto-connect-row"><input id="login-auto-connect" type="checkbox">开启后自动登录'
            '<span class="hint login-auto-connect-hint">使用所选账号，下次启动自动登录</span></label>',
            self.html,
        )
        self.assertLess(
            self.html.index("login-auto-connect"),
            self.html.index('id="login-manual-mode"'),
            "复选属于身份卡分区（手动态收拢时一并隐藏）",
        )
        # 与设置页开关同源：打开登录框即取设置快照，复选从 auto_connect 真值对齐
        self.assertIn("function syncLoginAutoConnectCheckbox()", settings)
        self.assertIn("async function refreshLoginAutoConnectPreference()", settings)
        self.assertIn("void refreshLoginAutoConnectPreference();", settings)
        self.assertIn('$("login-auto-connect").addEventListener("change", handleLoginAutoConnect);', settings)
        self.assertIn('$("login-auto-connect").removeEventListener("change", handleLoginAutoConnect);', settings)
        # 勾选即持久化既有偏好键：与设置页同一 set-auto-connect 动作、同一本地拦截
        self.assertIn('{ fudan: { enabled: checkbox.checked, account_id: accountId } }, checkbox, "fudan",', settings)
        self.assertIn("setLoginSavedStatus(AUTO_CONNECT_ERROR_TEXT.auto_connect_account_required);", settings)
        # 视觉语言：复选行与次行小字样式在 components.css（token 化，不发明新值）
        self.assertIn(".login-auto-connect-row {", self.css)
        self.assertIn(".login-auto-connect-row .login-auto-connect-hint {", self.css)

    def test_startup_landing_gate_keeps_greeting_visible(self):
        """SMALL-POLISH-1②：启动落地门——自动登录会话恢复完成后落在主页
        欢迎/问候面，绝不自动导航选择面/学习桌；显式导航才解除。仅改落地
        视图，登录/恢复请求链与目录自动选中（内存高亮）语义零变化。"""
        study = self.modules["study.js"]
        # 落地门是视图层开关：目录自动选中保留在内存，问候面不被顶掉
        self.assertIn("let studyLandingNavigated = false;", study)
        self.assertIn("studyMode = studyLandingMode(store, modeOverride, studyLandingNavigated);", study)
        # 目录渲染的自动选中包在 autoSelectingCatalog 标记内：不解除落地门
        self.assertIn("let autoSelectingCatalog = false;", study)
        # WP1-D4②：非自动选中订阅=解除落地门+完整应用链（courseChosen/按压态/renderLectures）
        self.assertIn("if (course && !autoSelectingCatalog) {", study)
        # 解除点=显式导航：课程行点击 / 讲次进入 / 字标返回 / 问候 CTA / 非自动选中
        # （N6L S1 U3：直播进入解除点随 live-only 学习桌模式退役）
        self.assertEqual(study.count("studyLandingNavigated = true;"), 5)

    # ---- tasks: backend-evidenced actions and precise course mapping ----

    def test_task_chip_and_actions_are_backend_evidenced(self):
        tasks = self.modules["tasks-drawer.js"]
        self.assertIn('id="task-chip-count"', self.html)
        self.assertIn('id="task-chip-dot"', self.html)
        self.assertIn('id="task-chip-failed"', self.html)
        self.assertIn('$("task-chip-count")', tasks)
        self.assertIn('$("task-chip-dot")', tasks)
        self.assertIn('$("task-chip-failed")', tasks)
        self.assertIn("(task.actions || [])", tasks)
        self.assertNotIn("function taskActions(task)", tasks)
        self.assertIn('id="task-counts"', self.html)
        self.assertIn('id="task-list"', self.html)

    def test_tasks_map_to_catalog_courses_precisely(self):
        tasks = self.modules["tasks-drawer.js"]
        self.assertIn("courseTitleOf", tasks)
        self.assertIn("task.course_id", tasks)
        self.assertIn("未关联课程", tasks)
        self.assertIn("store.courses", tasks)

    def test_task_cards_identify_kind_and_lecture_context(self):
        tasks = self.modules["tasks-drawer.js"]
        self.assertIn("TASK_KIND_LABELS", tasks)
        for kind in (
            "subtitle", "summary", "question", "document_import", "search_answer",
            "quiz", "review_plan", "document_alignment", "timeline_classification", "concept_analysis",
        ):
            self.assertIn(f'{kind}: "', tasks)
        self.assertIn("function lectureOf(store, task)", tasks)
        self.assertIn("task.sub_id", tasks)
        self.assertIn('textElement("span", context, "t-context")', tasks)
        self.assertIn(".task .t-context", self.css)
        # A11Y-IMPL-4：小字族 13.5px→0.84375rem（÷16 精确小数，随界面字号档缩放）
        self.assertIn("0.84375rem", self.css)

    def test_s09c_task_center_structure_is_declared(self):
        """S09-C 任务中心三层结构 + 语义阶段轨 + 学习材料父卡 + 证据条"""
        tasks = self.modules["tasks-drawer.js"]
        for element_id in ("task-completion-summary", "task-live"):
            self.assertIn(f'id="{element_id}"', self.html)
        # 三层抽屉：紧凑头部 / 任务分组列表（触发式告警在顶栏，无抽屉证据条）
        # 语义阶段轨：状态闭集 + 形状三通道 + 文本替代
        self.assertIn("PHASE_STATE_TEXT", tasks)
        self.assertIn('item.className = "t-rail-phase"', tasks)
        self.assertIn('item.dataset.state = ', tasks)
        self.assertIn('node.setAttribute("aria-hidden", "true")', tasks)
        self.assertIn('stateNode.className = "sr-only"', tasks)
        self.assertIn(".t-rail-phase[data-state=\"completed\"] .t-rail-node", self.css)
        self.assertIn(".t-rail-phase[data-state=\"active\"] .t-rail-node", self.css)
        self.assertIn(".t-rail-phase[data-state=\"skipped\"]", self.css)
        self.assertIn(".t-rail-phase[data-state=\"failed\"]", self.css)
        self.assertIn(".t-rail[data-stale=\"true\"]", self.css)
        # 学习材料父卡（仅展示层合并）+ 产出 chips 闭集
        self.assertIn('"学习材料"', tasks)
        self.assertIn("LEARNING_OUTPUT_LABELS", tasks)
        self.assertIn("renderLearningPack(", tasks)
        self.assertIn(".t-chip", self.css)
        # 排队卡零进度条语义存在：进度条只在可测量工作渲染
        self.assertIn("function appendProgressTrack(", tasks)
        self.assertIn("排队等待没有可测量进度", tasks)
        # 语义播报：唯一 task-live region，状态跃迁闭集
        self.assertIn('MEANINGFUL_TRANSITION_STATES', tasks)
        self.assertIn('$("task-live")', tasks)
        # 焦点恢复：SSE 重建后原位恢复（无焦点窃取）
        self.assertIn("function captureTaskFocus(", tasks)
        self.assertIn("function restoreTaskFocus(", tasks)
        # 新增 JS/HTML 零外部依赖：无 chart/图标/字体库（源级闭集锁）
        for banned in ("chart.js", "Chart(", "echarts", "d3.", "fontawesome", "googleapis", "sparkline"):
            self.assertNotIn(banned, tasks)
            self.assertNotIn(banned, self.html)

    def test_completed_tasks_collapse_into_one_history_details(self):
        tasks = self.modules["tasks-drawer.js"]
        self.assertIn('history.className = "task-history"', tasks)
        self.assertIn(
            "历史记录 · ${completed} 已完成 · ${canceled} 已取消 · 最近 ${formatTime(latest)}",
            tasks,
        )
        self.assertIn('textElement("h3", "正在处理")', tasks)
        self.assertIn("`需要处理 · ${failedTaskCount} 个失败任务`", tasks)
        self.assertIn("还有 ${groupItems.length - maxVisiblePerGroup} 个失败任务", tasks)
        self.assertIn("task.state === \"completed\"", tasks)
        self.assertIn(".task-history", self.css)
        self.assertIn(".task-history > summary", self.css)
        self.assertIn(".task-tier", self.css)
        self.assertIn(".task-overflow", self.css)

    def test_tasks_drawer_closes_via_real_scrim_attribute_and_shared_close(self):
        tasks = self.modules["tasks-drawer.js"]
        self.assertIn("data-tasks-scrim", self.html)
        self.assertIn("event.target?.dataset?.tasksScrim !== undefined", tasks)
        self.assertNotIn("dataset?.scrim !== undefined", tasks)
        self.assertIn("closeOverlay(drawerRoot)", tasks)
        # ARCH-DEBT-1：clear 随任务卡渲染族迁至 task-cards，closeOverlay 留门面
        # （scrim 关闭语义钉在门面导入形态上；tasks=家族并集文本）
        self.assertRegex(tasks, r'import \{[^}]*\bcloseOverlay\b[^}]*\} from "\./ui\.js"')
        self.assertRegex(tasks, r'import \{[^}]*\bclear\b[^}]*\} from "\.\./ui\.js"')
        self.assertIn('data-open-tasks aria-haspopup="dialog" aria-controls="tasks-root"', self.html)

    def test_task_failures_use_closed_set_guidance_with_technical_details(self):
        tasks = self.modules["tasks-drawer.js"]
        for code in (
            "timeout", "network_unavailable", "proxy_unavailable", "authorization_required",
            "authorization_revoked", "permission_denied", "rate_limited", "integrity_rejected",
            "remote_failed", "runtime_failed", "task_failed", "operation_failed",
            "remote_recovery_material_unavailable", "remote_recovery_cancel_not_safe",
            "task_already_active", "task_retry_not_supported", "task_not_found",
            "operation_already_running", "operation_id_conflict", "task_action_invalid",
            "task_action_rejected", "task_retry_requires_failed_state",
            "task_cancel_requires_active_state", "task_cancel_not_supported",
            "timeline_transcript_unavailable", "timeline_classification_failed",
            "alignment_transcript_unavailable", "alignment_failed",
            "concept_analysis_requires_two_courses", "question_explanation_not_configured",
            "document_import_failed", "document_type_unsupported", "document_payload_invalid",
            "document_empty", "document_too_large", "document_format_invalid",
            "document_password_required", "document_payload_not_recoverable",
        ):
            self.assertIn(f"{code}:", tasks)
        self.assertIn("TASK_FAILURE_GUIDANCE[task.error_code] || GENERIC_TASK_FAILURE", tasks)
        self.assertIn('if (task.state === "failed") {', tasks)
        self.assertIn('textElement("p", taskFailureGuidance(task), "t-failure")', tasks)
        self.assertIn("task-technical", tasks)
        self.assertNotIn("task.error}", tasks)
        self.assertIn(".task .t-failure", self.css)

    def test_wordmark_returns_to_study_selection_entry(self):
        shell = self.modules["shell.js"]
        study = self.modules["study.js"]
        self.assertIn('window.dispatchEvent(new Event("courselens:study-return-select"))', shell)
        self.assertNotIn("courselens:study-return-select", self.html)
        self.assertIn('window.addEventListener("courselens:study-return-select", handleStudyReturnSelect)', study)
        self.assertIn("window.removeEventListener(\"courselens:study-return-select\", handleStudyReturnSelect)", study)
        self.assertIn("const handleStudyReturnSelect = () => handleBackToSelect();", study)
        self.assertIn('$("wordmark").addEventListener("click", handleWordmark)', shell)
        self.assertIn('$("settings-close").addEventListener("click", () => selectPage("study"))', shell)

    def test_transcript_bookmark_uses_inline_svg_icon(self):
        study = self.modules["study.js"]
        self.assertNotIn('bookmark.textContent = "+"', study)
        self.assertIn("bookmark.append(renderBookmarkIcon())", study)
        self.assertIn("function renderBookmarkIcon()", study)
        self.assertIn('"http://www.w3.org/2000/svg"', study)
        self.assertIn('svg.setAttribute("viewBox", "0 0 24 24")', study)
        self.assertIn('path.setAttribute("d", "M7 4h10v16l-5-3.5L7 20z")', study)
        self.assertNotIn("innerHTML", study)
        self.assertIn("grid-template-columns: 56px minmax(0, 1fr) auto;", self.css)
        self.assertIn(".transcript-row .icon-button { justify-self: end; align-self: center; }", self.css)

    def test_login_dialog_gives_honest_busy_stage_and_failure_feedback(self):
        settings = self.modules["settings.js"]
        shell = self.modules["shell.js"]
        for element_id in ("login-status", "login-error", "login-submit", "toggle-password-visibility"):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('id="login-status" class="hint" role="status" aria-live="polite" hidden', self.html)
        self.assertIn('id="login-error" class="field-error" role="alert" hidden', self.html)
        self.assertIn('window.dispatchEvent(new Event("courselens:login-dialog-open"))', shell)
        self.assertIn("window.addEventListener(\"courselens:login-dialog-open\", handleLoginDialogOpen)", settings)
        self.assertIn('$("login-dialog").addEventListener("close", () => resetLoginDialog())', settings)
        self.assertIn("if (loginInFlight) return;", settings)
        self.assertIn("function resetLoginDialog()", settings)
        self.assertIn("apiV3(\"authentication\")", settings)
        self.assertIn('LOGIN_STAGE_TEXT[value?.state] || "正在登录，请稍候。"', settings)
        for code in (
            "fudan_login_failed", "fudan_credentials_rejected", "timeout", "network_unavailable",
            "fudan_session_expired", "fudan_login_required", "fudan_credentials_missing",
            "saved_account_unavailable", "authentication_request_invalid",
        ):
            self.assertIn(f"{code}:", settings)
        self.assertIn('LOGIN_FAILURE_TEXT[String(code || "")] || GENERIC_LOGIN_FAILURE', settings)
        self.assertIn("finishLoginFailure(loginFailureText(error?.code))", settings)
        # 诚实波动措辞 + 凭据被拒专属文案 + 阶段/尝试行闭集
        self.assertIn(
            "登录未完成：可能是网络或校园服务波动，系统已自动重试仍未成功。请稍候约 90 秒后再试。",
            settings,
        )
        self.assertIn("学号或密码不正确，请核对后重新输入。", settings)
        self.assertIn("LOGIN_STEP_TEXT", settings)
        self.assertIn("webvpn: \"校园网关\"", settings)
        self.assertIn("icourse: \"课程平台\"", settings)
        self.assertIn('loginStageText(value) || LOGIN_STAGE_TEXT[value?.state] || "正在登录，请稍候。"', settings)
        ui = self.modules["ui.js"]
        self.assertIn("fudan_credentials_rejected", ui)
        self.assertIn("请核对学号与密码后重新登录。", ui)
        self.assertIn("可能是网络或校园服务波动，请稍候约 90 秒后再试。", ui)
        self.assertIn("const LOGIN_POLL_MS = 2000;", settings)
        self.assertIn("function stopLoginPoll()", settings)
        self.assertIn("stopLoginPoll();", settings)
        self.assertIn("if (loginInFlight) setLoginStatus(", settings)

    def test_password_visibility_toggle_keeps_masked_default_and_metadata(self):
        settings = self.modules["settings.js"]
        self.assertIn('id="login-password" type="password" autocomplete="current-password"', self.html)
        self.assertIn('aria-label="显示密码" aria-pressed="false" aria-controls="login-password"', self.html)
        self.assertIn('data-eye-icon="on"', self.html)
        self.assertIn('data-eye-icon="off"', self.html)
        self.assertIn('class="password-field"', self.html)
        self.assertIn('input.type = visible ? "text" : "password";', settings)
        self.assertIn('toggle.setAttribute("aria-label", visible ? "隐藏密码" : "显示密码");', settings)
        self.assertIn('toggle.setAttribute("aria-pressed", String(visible));', settings)
        self.assertIn("input.setSelectionRange(start, end);", settings)
        self.assertIn("function applyPasswordVisibility(visible)", settings)
        self.assertIn("applyPasswordVisibility(false);", settings)
        self.assertIn(".password-field { position: relative; display: block; }", self.css)
        self.assertIn(".password-field input { width: 100%; padding-right: 48px; }", self.css)
        self.assertIn(".password-field .icon-button {\n  position: absolute;", self.css)
        self.assertIn(".password-field .icon-button:focus-visible", self.css)
        self.assertIn("svg[data-eye-icon][hidden] { display: none; }", self.css)
        self.assertIn(".password-field .icon-button { width: 44px; min-width: 44px; height: 44px; right: 2px; }", self.css)
        self.assertIn("handlePasswordToggle", settings)
        self.assertIn('$("toggle-password-visibility").removeEventListener("click", handlePasswordToggle)', settings)

    # ---- settings safe projections (kept verbatim, tested via node) ----

    def test_windows_update_errors_use_safe_closed_guidance(self):
        settings = self.modules["settings.js"]
        self.assertIn('id="update-error"', self.html)
        self.assertIn('role="alert"', self.html)
        for code in (
            "managed_install_required", "production_gates_incomplete", "download_redirect_untrusted",
            "current_version_below_security_floor", "manifest_key_epoch_rollback",
            "manifest_time_invalid", "rollback_unavailable", "startup_health_failed",
        ):
            self.assertIn(f"{code}:", settings)
        self.assertNotIn("`更新未继续：${value.error_code}`", settings)
        self.assertIn('$("update-error").textContent = GENERIC_UPDATE_GUIDANCE;', settings)
        update_action = settings.split("async function updateAction", 1)[1].split("async function loadAccounts", 1)[0]
        self.assertNotIn("error.message", update_action)
        # ARCH-DEBT-1 拆分后按名提取真源码实体（原「切片到 formatBytes」锚跨文件失效）
        helper = chr(10).join(
            family_entity("settings", name)
            for name in (
                "UPDATE_ERROR_GUIDANCE", "UPDATE_ERROR_CODES", "DATA_VALUE_FAILURE",
                "GENERIC_UPDATE_GUIDANCE", "closedMapValue", "updateErrorGuidance",
            )
        )
        script = "\n".join((
            helper,
            "console.log(JSON.stringify([",
            "  updateErrorGuidance('managed_install_required', 'policy_blocked'),",
            "  updateErrorGuidance('production_gates_incomplete', 'policy_blocked'),",
            "  updateErrorGuidance('manifest_key_epoch_rollback', 'policy_blocked'),",
            "  updateErrorGuidance('manifest_time_invalid', 'policy_blocked'),",
            "  updateErrorGuidance('rollback_unavailable', 'failed'),",
            "  updateErrorGuidance('unexpected_future_code', 'failed'),",
            "  updateErrorGuidance('', 'failed'),",
            "  updateErrorGuidance('', 'available'),",
            "  updateErrorGuidance('constructor', 'failed'),",
            "  updateErrorGuidance('toString', 'failed'),",
            "  updateErrorGuidance('__proto__', 'failed'),",
            "  updateErrorGuidance('valueOf', 'failed'),",
            "  updateErrorGuidance({ toString: () => 'managed_install_required' }, 'failed'),",
            "]));",
        ))
        result = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        guidance = json.loads(result.stdout)
        generic = "更新暂未继续。请稍后重新检查；若持续出现，可重新安装客户端。"
        self.assertEqual(guidance[5], generic)
        self.assertEqual(guidance[6], generic)
        self.assertEqual(guidance[7], "")
        self.assertEqual(guidance[8:], [generic, generic, generic, generic, generic])
        self.assertTrue(all(guidance[index] for index in range(5)))
        self.assertTrue(all("manifest_key_epoch_rollback" not in value for value in guidance if value))
        self.assertTrue(all(isinstance(value, str) for value in guidance))

    def test_windows_update_recovery_guidance_uses_only_confirmed_state(self):
        settings = self.modules["settings.js"]
        self.assertIn('id="update-recovery"', self.html)
        self.assertIn('role="status" aria-live="polite"', self.html)
        self.assertIn('$("update-recovery").textContent = recoveryGuidance;', settings)
        recovery = family_entity("settings", "updateRecoveryGuidance")
        self.assertNotIn("postV3", recovery)
        version_helper = family_entity("settings", "confirmedUpdateVersion")
        recovery_helper = recovery
        script = "\n".join((
            version_helper,
            recovery_helper,
            "console.log(JSON.stringify([",
            "  updateRecoveryGuidance({ state: 'ready_to_restart', rollback: { available: false } }),",
            "  updateRecoveryGuidance({ state: 'applying', rollback: { available: true, version: '1.0.0', state: 'awaiting_health' } }),",
            "  updateRecoveryGuidance({ state: 'applying', rollback: { available: false } }),",
            "  updateRecoveryGuidance({ state: 'rolled_back', current_version: '1.0.0', rollback: { available: true, version: '1.1.0', state: 'rolled_back' } }),",
            "  updateRecoveryGuidance({ state: 'rolled_back', rollback: { available: true, version: '1.1.0', state: 'rolled_back' } }),",
            "  updateRecoveryGuidance({ state: 'healthy', rollback: { available: true, version: '<img src=x>' } }),",
            "]));",
        ))
        result = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        guidance = json.loads(result.stdout)
        self.assertEqual(guidance, [
            "重启后将开始更新，并进行健康确认。",
            "正在等待健康确认；如未通过，可自动恢复到已确认版本 1.0.0。",
            "正在等待健康确认；恢复状态暂无法确认。",
            "已自动恢复到上一已确认版本 1.0.0。",
            "已自动恢复；恢复版本暂无法确认。",
            "",
        ])
        self.assertNotIn("1.1.0", guidance[3])
        self.assertTrue(all("<img" not in value for value in guidance))
        version_result = subprocess.run(
            [
                "node", "--input-type=module", "--eval", "\n".join((
                    version_helper,
                    "console.log(JSON.stringify([",
                    "  confirmedUpdateVersion('0.0.0'),",
                    "  confirmedUpdateVersion('1.2.3-rc.1'),",
                    "  confirmedUpdateVersion('01.2.3'),",
                    "  confirmedUpdateVersion('1.02.3'),",
                    "  confirmedUpdateVersion('1.2.03'),",
                    "]));",
                )),
            ],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(json.loads(version_result.stdout), ["0.0.0", "1.2.3-rc.1", "", "", ""])

    def test_client_update_diagnostics_are_closed_and_sanitized(self):
        settings = self.modules["settings.js"]
        projection = settings.split("export function safeUpdateDiagnostics", 1)[1].split(
            "export function updateRecoveryGuidance", 1
        )[0]
        self.assertNotIn("updateValue =", projection)
        self.assertNotIn("postV3", projection)
        self.assertIn("client_update: safeUpdateDiagnostics(updateValue)", settings)
        self.assertIn('toast("连接诊断暂时无法复制，请稍后重试。", "error")', settings)
        helper = chr(10).join(
            family_entity("settings", name)
            for name in (
                "updateLabels", "UPDATE_STATES", "ROLLBACK_STATES", "UPDATE_ERROR_GUIDANCE",
                "UPDATE_ERROR_CODES", "DATA_VALUE_FAILURE", "GENERIC_UPDATE_GUIDANCE",
                "isPlainRecord", "closedDiagnosticValue", "closedMapValue", "formatBytes",
                "confirmedUpdateVersion", "ownDataValue", "safeUpdateDiagnostics",
                "updateRecoveryGuidance", "renderUpdate", "updateAction", "updateValue",
            )
        )
        script = "\n".join((
            helper,
            "const normal = safeUpdateDiagnostics({",
            "  state: 'applying', error_code: 'startup_health_failed',",
            "  current_version: '1.1.0', available_version: '1.2.0',",
            "  rollback: { available: true, state: 'awaiting_health', version: '1.0.0', path: 'C:/secret' },",
            "  actions: ['install'], manifest: { signature: 'secret' },",
            "});",
            "const hostile = safeUpdateDiagnostics(JSON.parse('{\"state\":\"surprise\",\"error_code\":\"raw_error\",\"current_version\":\"<img src=x>\",\"available_version\":\"' + '9'.repeat(80) + '\",\"rollback\":{\"available\":true,\"state\":\"unknown_state\",\"version\":\"<script>secret</script>\",\"url\":\"https://secret\"},\"credential\":\"secret\"}'));",
            "const absent = safeUpdateDiagnostics(null);",
            "let getterReads = 0;",
            "const accessor = { error_code: 'network_unavailable' };",
            "Object.defineProperty(accessor, 'state', { get: () => { getterReads += 1; return 'healthy'; } });",
            "const inherited = Object.create({ state: 'healthy' });",
            "const withToJson = { state: 'healthy', error_code: '', current_version: '1.0.0', available_version: '', rollback: {}, toJSON: () => { throw new Error('must not run'); } };",
            "const throwingProxy = new Proxy({}, { getOwnPropertyDescriptor: () => { throw new Error('descriptor trap'); } });",
            "const guarded = [safeUpdateDiagnostics(accessor), safeUpdateDiagnostics(inherited), safeUpdateDiagnostics(withToJson), safeUpdateDiagnostics(throwingProxy)];",
            "console.log(JSON.stringify({ values: [normal, hostile, absent, ...guarded], getterReads }));",
        ))
        result = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        output = json.loads(result.stdout)
        normal, hostile, absent, accessor, inherited, with_to_json, throwing_proxy = output["values"]
        self.assertEqual(normal, {
            "available": True, "state": "applying", "error_code": "startup_health_failed",
            "current_version": "1.1.0", "available_version": "1.2.0",
            "rollback": {"available": True, "state": "awaiting_health", "version": "1.0.0"},
        })
        self.assertEqual(hostile, {
            "available": True, "state": "unknown", "error_code": "unknown",
            "current_version": None, "available_version": None,
            "rollback": {"available": True, "state": "unknown", "version": None},
        })
        self.assertEqual(absent, {
            "available": False, "state": None, "error_code": None,
            "current_version": None, "available_version": None,
            "rollback": {"available": False, "state": None, "version": None},
        })
        self.assertEqual(output["getterReads"], 0)
        self.assertEqual(accessor["state"], None)
        self.assertEqual(inherited["available"], False)
        self.assertEqual(with_to_json["state"], "healthy")
        self.assertEqual(throwing_proxy["available"], False)
        self.assertNotIn("secret", result.stdout)
        self.assertNotIn("<img", result.stdout)

    def test_diagnostic_copy_has_accessible_privacy_disclosure(self):
        settings = self.modules["settings.js"]
        for text in (
            "复制前的隐私说明",
            "有限的闭集状态、如有的计数和已净化的版本摘要",
            "不包含课程正文、课程 URL、账号标识、Cookie、凭据、媒体或本地路径。",
            "诊断摘要不是备份，不能用于恢复。",
        ):
            self.assertIn(text, self.html)
        self.assertIn('class="diagnostic-privacy"', self.html)
        self.assertIn('id="diagnostic-privacy-summary"', self.html)
        self.assertIn('id="copy-diagnostics" type="button" aria-describedby="diagnostic-privacy-summary"', self.html)
        self.assertIn("client_update: safeUpdateDiagnostics(updateValue)", settings)
        self.assertIn(".diagnostic-privacy", self.css)

    # ---- accessibility and responsive guardrails ----

    def test_accessibility_and_responsive_modes_are_kept(self):
        for token in (
            "@media (prefers-reduced-motion: reduce)",
            "@media (forced-colors: active)",
            ":focus-visible",
            "color-scheme",
            "aspect-ratio",
        ):
            self.assertIn(token, self.css)
        self.assertIn('id="workspace-main" tabindex="-1"', self.html)
        self.assertIn('aria-live="polite"', self.html)

    # ---- study content behavior (transcript / bookmarks, helpers via node) ----

    def test_transcript_following_uses_timed_segments_without_forcing_scroll(self):
        study = self.modules["study.js"]
        player = self.modules["player-core.js"]
        self.assertIn("row.dataset.startMs", study)
        self.assertIn("row.dataset.endMs", study)
        self.assertIn('row.setAttribute("aria-current", "true")', study)
        self.assertIn('row?.removeAttribute("aria-current")', study)
        self.assertIn('new CustomEvent("courselens:transcript-time"', player)
        self.assertIn('player.addEventListener("seeked", publishTranscriptTime)', player)
        self.assertIn('scrollIntoView?.({ block: "nearest" })', study)
        self.assertIn("transcriptManualScrollUntil", study)
        self.assertNotIn("transcriptAutoScrollUntil", study)
        # VTT-PERF 窗口化演进：跟随链本身仍零 scroll 监听（滚动只走 scrollIntoView，
        # 手动滚动保护=transcriptManualScrollUntil 不变）；唯一 scroll 监听属于
        # 文稿窗口化 reconcile（capture 被动语义，拆除对称），不得回流跟随链。
        follow_body = study.split("function followTranscript", 1)[1].split("function ensureTranscriptRowRendered", 1)[0]
        self.assertNotIn('addEventListener("scroll"', follow_body)
        self.assertEqual(study.count('document.addEventListener("scroll", onScroll, true)'), 1)
        self.assertEqual(study.count('window.addEventListener("resize", onScroll)'), 1)
        self.assertIn('removeEventListener("scroll", previous.onScroll, true)', study)
        self.assertIn('window.removeEventListener("resize", previous.onScroll)', study)
        for event_name in ("wheel", "touchstart", "pointerdown", "keydown"):
            self.assertIn(f'addEventListener("{event_name}", handleTranscriptUserIntent', study)
            self.assertIn(f'removeEventListener("{event_name}", handleTranscriptUserIntent)', study)
        self.assertGreaterEqual(study.count("resetTranscriptFollow()"), 3)
        helper = "function transcriptSegmentIndex" + study.split(
            "export function transcriptSegmentIndex", 1
        )[1].split("function transcriptTiming", 1)[0]
        intent_constants = "const TRANSCRIPT_SCROLL_KEYS" + study.split(
            "const TRANSCRIPT_SCROLL_KEYS", 1
        )[1].split("export function transcriptSegmentIndex", 1)[0]
        intent_helper = "function isTranscriptScrollIntent" + study.split(
            "export function isTranscriptScrollIntent", 1
        )[1].split("function followTranscript", 1)[0]
        script = "\n".join((
            intent_constants,
            intent_helper,
            helper,
            "const segments = [{ startMs: 0, endMs: 1000 }, { startMs: 1000, endMs: 2000 }];",
            "console.log(JSON.stringify([",
            "  transcriptSegmentIndex(segments, 0, 'lecture', 'lecture'),",
            "  transcriptSegmentIndex(segments, 999, 'lecture', 'lecture'),",
            "  transcriptSegmentIndex(segments, 1000, 'lecture', 'lecture'),",
            "  transcriptSegmentIndex(segments, 250, 'lecture', 'lecture'),",
            "  transcriptSegmentIndex(segments, 1000, 'other', 'lecture'),",
            "  isTranscriptScrollIntent({ type: 'scroll', target: { closest: () => null } }),",
            "  isTranscriptScrollIntent({ type: 'wheel', target: { closest: () => null } }),",
            "  isTranscriptScrollIntent({ type: 'keydown', key: 'PageDown', target: { closest: () => null } }),",
            "  isTranscriptScrollIntent({ type: 'keydown', key: 'Tab', target: { closest: () => null } }),",
            "  isTranscriptScrollIntent({ type: 'pointerdown', target: { closest: () => ({}) } }),",
            "]));",
        ))
        result = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(json.loads(result.stdout), [0, 0, 1, 0, -1, False, True, True, False, False])

    def test_learning_bookmarks_follow_the_active_lecture_safely(self):
        study = self.modules["study.js"]
        self.assertIn('id="bookmark-list"', self.html)
        self.assertIn("apiV3(`bookmarks?sub_id=${encodeURIComponent(subId)}`", study)
        self.assertIn("const epoch = bookmarkEpoch", study)
        self.assertIn('epoch !== bookmarkEpoch || String(store.activeLecture?.sub_id || "") !== subId', study)
        self.assertIn("bookmarkController?.abort()", study)
        self.assertIn("await loadBookmarks(store, { preserveOnFailure: true });", study)
        self.assertIn('timestamp.setAttribute("aria-label", `跳转到 ${timestamp.textContent}`)', study)
        self.assertIn('$("player-stage").currentTime', study)
        self.assertNotIn(
            "player.play()",
            study.split("function renderBookmarks", 1)[1].split("async function loadBookmarks", 1)[0],
        )
        self.assertIn('textElement("p", bookmark.note ?', study)
        self.assertNotIn("innerHTML", study)
        self.assertIn('"正在加载书签"', study)
        # EMPTY-STATES-1：空书签不再是裸「暂无书签」，给「没听懂」指路引导（文案钉）
        self.assertIn('"暂无书签。听课时在没听懂的地方点「没听懂」', study)
        self.assertIn('"书签暂时无法加载"', study)
        helper = "function sortBookmarks" + study.split(
            "export function sortBookmarks", 1
        )[1].split("function resetBookmarkLoading", 1)[0]
        script = "\n".join((
            helper,
            "const rows = sortBookmarks([",
            "  { bookmark_id: 'late', start_ms: 3000 },",
            "  { bookmark_id: 'same-first', start_ms: 1000, end_ms: 2500 },",
            "  { bookmark_id: 'early', start_ms: 1000, end_ms: 1500 },",
            "  { bookmark_id: 'same-second', start_ms: 1000, end_ms: 2500 },",
            "]);",
            "console.log(JSON.stringify(rows.map((row) => row.bookmark_id)));",
        ))
        result = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(json.loads(result.stdout), ["early", "same-first", "same-second", "late"])

    def test_empty_states_give_a_real_next_step(self):
        """EMPTY-STATES-1：恰域空态=一句话+真实动作钮，文案零技术词、零责备。"""
        study = self.modules["study.js"]
        # 统一形态：emptyStateWithAction 只挂既有 empty-state/empty-action 族
        self.assertIn('action.className = "empty-action";', study)
        # catalog/讲次空态动作复用既有 recovery 链路，绝不另造刷新通道
        self.assertIn('() => performRecoveryAction("refresh-catalog"),', study)
        self.assertIn("CATALOG_EMPTY_READY_TEXT", study)
        # 测验空态复用面板头部「生成测验」链路；未选讲次给选择引导不误报
        self.assertIn('() => $("generate-quiz")?.click(),', study)
        self.assertIn("选择讲次后，这讲的练习题会显示在这里。", study)
        # 错误空态保持既有 error-text 墨色合同，并带重试
        self.assertIn('"error-text");', study)
        self.assertIn("emptyStateWithAction(target, \"书签暂时无法加载\", \"重试\"", study)
        # 空书签文案给「没听懂」指路（动作在播放器，不造死按钮）
        self.assertIn("「没听懂」", study)

        review_family = self.modules["course-review.js"]
        # 课程复习三面板空态动作=既有「更新课程知识」请求（busy 防重在 data.js）
        self.assertIn("emptyStateWithAction(", review_family)
        self.assertEqual(review_family.count('"更新课程知识",'), 3, "三面板空态动作文案闭集")
        # 负面核对：合同失配提示不再出现「后端/不受支持」类技术词
        self.assertNotIn("后端返回了不受支持", review_family)
        self.assertIn("更新客户端后就能看到", review_family)

    def test_transcript_requests_are_isolated_and_replace_rows_atomically(self):
        study = self.modules["study.js"]
        self.assertIn("let transcriptController = null;", study)
        self.assertIn("let transcriptEpoch = 0;", study)
        self.assertIn("transcriptController?.abort()", study)
        self.assertIn("{ controller: transcriptController }", study)
        self.assertIn("target.replaceChildren(fragment);", study)
        self.assertIn('"字幕暂时无法加载"', study)
        # SIMPLIFY-AUDIT-1 S2：手动「刷新字幕」处理器随钮退役，自动链（进讲次
        # 载入 + courselens:transcript-refresh 热重读）承载全部更新路径。
        self.assertNotIn("handleReloadTranscript", study)
        self.assertIn("resetTranscriptLoading();", study)
        helper = "function isCurrentTranscriptLoad" + study.split(
            "export function isCurrentTranscriptLoad", 1
        )[1].split("function clearTranscriptFollow", 1)[0]
        script = "\n".join((
            helper,
            "console.log(JSON.stringify([",
            "  isCurrentTranscriptLoad(2, 2, 'A', 'A', 4, 4),",
            "  isCurrentTranscriptLoad(1, 2, 'A', 'A', 4, 4),",
            "  isCurrentTranscriptLoad(2, 2, 'A', 'B', 4, 4),",
            "  isCurrentTranscriptLoad(2, 2, 'A', 'A', 4, 5),",
            "]));",
        ))
        result = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(json.loads(result.stdout), [True, False, False, False])

    def test_bookmark_review_actions_wait_for_backend_confirmation(self):
        study = self.modules["study.js"]
        self.assertIn('id="bookmark-action-state"', self.html)
        self.assertIn('postV3("bookmarks/actions", { bookmark_id: bookmarkId, action }, { controller: actionController })', study)
        self.assertIn("if (button.disabled || !bookmarkId || !subId", study)
        self.assertIn("button.disabled = true;", study)
        self.assertIn("button.disabled = false;", study)
        self.assertIn("button.textContent = label;", study)
        self.assertIn('"复习状态暂时无法更新"', study)
        self.assertIn("await loadBookmarks(store);", study)
        self.assertLess(
            study.index('await postV3("bookmarks/actions"'),
            study.index('await loadBookmarks(store, { preserveOnFailure: true });', study.index("async function updateBookmarkReview")),
        )
        self.assertIn("const confirmed = confirmedBookmarkReview(result.bookmark, bookmarkId, subId, action);", study)
        self.assertIn('"状态已更新，列表暂未刷新"', study)
        self.assertIn('"状态可能已更新，请重新加载"', study)
        self.assertIn('renderBookmarks($("bookmark-list"), bookmarkItems);', study)
        self.assertIn("bookmarkActionIsCurrent(epoch, bookmarkActionEpoch, subId, store.activeLecture?.sub_id)", study)
        self.assertIn("bookmarkActionControllers.forEach((actionController) => actionController.abort())", study)
        self.assertIn('bookmarkList.addEventListener("click", handleBookmarkAction)', study)
        self.assertIn('bookmarkList.removeEventListener("click", handleBookmarkAction)', study)
        action_helper = "function bookmarkReviewAction" + study.split(
            "export function bookmarkReviewAction", 1
        )[1].split("export function bookmarkActionIsCurrent", 1)[0]
        current_helper = "function bookmarkActionIsCurrent" + study.split(
            "export function bookmarkActionIsCurrent", 1
        )[1].split("export function confirmedBookmarkReview", 1)[0]
        confirmation_helper = "function confirmedBookmarkReview" + study.split(
            "export function confirmedBookmarkReview", 1
        )[1].split("function resetBookmarkLoading", 1)[0]
        script = "\n".join((
            action_helper,
            current_helper,
            confirmation_helper,
            "console.log(JSON.stringify([",
            "  bookmarkReviewAction({ bookmark_id: 'open', resolution_status: 'open' }),",
            "  bookmarkReviewAction({ bookmark_id: 'resolved', resolution_status: 'resolved' }),",
            "  bookmarkReviewAction({ bookmark_id: 'unknown', resolution_status: 'pending' }),",
            "  bookmarkActionIsCurrent(3, 3, 'A', 'A'),",
            "  bookmarkActionIsCurrent(3, 3, 'A', 'B'),",
            "  bookmarkActionIsCurrent(3, 4, 'A', 'A'),",
            "  confirmedBookmarkReview({ bookmark_id: 'open', sub_id: 'A', resolution_status: 'resolved' }, 'open', 'A', 'resolve')?.resolution_status,",
            "  confirmedBookmarkReview({ bookmark_id: 'open', sub_id: 'B', resolution_status: 'resolved' }, 'open', 'A', 'resolve'),",
            "  confirmedBookmarkReview({ bookmark_id: 'open', sub_id: 'A', resolution_status: 'open' }, 'open', 'A', 'resolve'),",
            "]));",
        ))
        result = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(json.loads(result.stdout), [
            {"bookmark_id": "open", "action": "resolve", "label": "标记已复习"},
            {"bookmark_id": "resolved", "action": "reopen", "label": "重新加入复习"},
            None, True, False, False, "resolved", None, None,
        ])

    # ---- evidence navigation (UX-2): identity metadata, evidence seek, citations ----

    def test_evidence_identity_follows_the_contract_or_stays_legacy(self):
        study = self.modules["study.js"]
        self.assertIn("const evidenceId = segmentEvidenceId(segment);", study)
        self.assertIn("if (evidenceId) row.dataset.evidenceId = evidenceId;", study)
        # 身份只进 DOM 元数据；绝不进入可见文本或 innerHTML
        self.assertNotIn("textContent = evidenceId", study)
        self.assertNotIn("innerHTML", study)
        # 既有陈旧加载守卫与手动滚动让位守卫保持原样
        self.assertIn(
            "isCurrentTranscriptLoad(requestEpoch, transcriptEpoch, subId, store.activeLecture?.sub_id, instanceEpoch, studyInstanceEpoch)",
            study,
        )
        self.assertIn("transcriptManualScrollUntil", study)
        helper = "const EVIDENCE_ID_PATTERN" + study.split(
            "const EVIDENCE_ID_PATTERN", 1
        )[1].split("function resetTranscriptLoading", 1)[0]
        script = "\n".join((
            helper,
            "console.log(JSON.stringify([",
            '  isContractEvidenceId("slevt:0123456789ab"),',
            '  isContractEvidenceId("seg:aaaaaaaaaaaa"),',
            '  isContractEvidenceId("slevt:0123456789"),',
            '  isContractEvidenceId("slevt:0123456789AB"),',
            '  isContractEvidenceId("unknown:0123456789ab"),',
            '  isContractEvidenceId(""),',
            "  isContractEvidenceId(null),",
            "  isContractEvidenceId(42),",
            '  segmentEvidenceId({ evidence_id: "seg:aaaaaaaaaaaa" }),',
            '  segmentEvidenceId({ evidence_id: "bogus" }),',
            "  segmentEvidenceId({}),",
            "  segmentEvidenceId(null),",
            "  evidenceAnchorMs({ start_ms: 65000 }),",
            "  evidenceAnchorMs({ start_ms: 0 }),",
            "  evidenceAnchorMs({ start_ms: 1250.9 }),",
            "  evidenceAnchorMs({ start_ms: -5 }),",
            '  evidenceAnchorMs({ start_ms: "abc" }),',
            "  evidenceAnchorMs({}),",
            "  evidenceAnchorMs(null),",
            "]));",
        ))
        result = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(json.loads(result.stdout), [
            True, True, False, False, False, False, False, False,
            "seg:aaaaaaaaaaaa", "", "", "",
            65000, 0, 1250, None, None, None, None,
        ])

    def test_quiz_and_review_evidence_actions_seek_without_autoplay(self):
        study = self.modules["study.js"]
        # DATA-DELETE-REPAIR-1：零调用的通用 renderItems 退役（测验/复习各自
        # 内联建行），evidence 动作契约钉不受影响。
        self.assertIn("const anchor = quizEvidenceAnchor(item, lecture);", study)
        self.assertIn("const anchor = reviewStepEvidenceAnchor(plan, step, lecture);", study)
        self.assertIn("const action = anchor == null ? null : evidenceJumpButton(store, lecture.sub_id, anchor);", study)
        self.assertIn("if (action) row.append(action);", study)
        self.assertIn('button.type = "button";', study)
        self.assertIn('button.textContent = "查看依据";', study)
        self.assertIn('button.setAttribute("aria-label", `查看依据，跳转到 ${bookmarkTime(anchorMs)}`);', study)
        # 点击时讲次仍须一致；seek 走字幕面板 + 绝对毫秒锚点，不自动播放
        self.assertIn('if (String(lecture?.sub_id || "") !== String(subId)) return;', study)
        self.assertIn('selectMaterialTab("transcript");', study)
        self.assertIn('$("player-stage").currentTime = anchorMs / 1000;', study)
        self.assertIn('new CustomEvent("courselens:transcript-time"', study)
        self.assertIn('detail: { sub_id: String(subId || ""), time_ms: anchorMs },', study)
        action_block = "export function quizEvidenceAnchor" + study.split(
            "export function quizEvidenceAnchor", 1
        )[1].split("async function loadDocuments", 1)[0]
        self.assertNotIn(".play(", action_block)
        self.assertIn(".evidence-jump-button {", self.css)
        self.assertIn(".citation-action {", self.css)
        helpers = "const EVIDENCE_ID_PATTERN" + study.split(
            "const EVIDENCE_ID_PATTERN", 1
        )[1].split("function resetTranscriptLoading", 1)[0]
        action_helpers = "export function quizEvidenceAnchor" + study.split(
            "export function quizEvidenceAnchor", 1
        )[1].split("async function loadDocuments", 1)[0]
        script = "\n".join((
            helpers,
            action_helpers,
            "const lecture = { course_id: 'c1', sub_id: 's1' };",
            "console.log(JSON.stringify([",
            "  quizEvidenceAnchor({ course_id: 'c1', sub_id: 's1', evidence: { start_ms: 65000 } }, lecture),",
            "  quizEvidenceAnchor({ course_id: 'c1', sub_id: 's2', evidence: { start_ms: 65000 } }, lecture),",
            "  quizEvidenceAnchor({ course_id: 'c1', sub_id: 's1', evidence: {} }, lecture),",
            "  quizEvidenceAnchor({ course_id: 'c1', sub_id: 's1' }, lecture),",
            "  quizEvidenceAnchor({ course_id: '', sub_id: 's1', evidence: { start_ms: 5 } }, lecture),",
            "  reviewPlanEvidenceAnchor({ scope: { course_id: 'c1', sub_id: 's1' }, steps: [{ evidence: {} }, { evidence: { start_ms: 120000 } }] }, lecture),",
            "  reviewPlanEvidenceAnchor({ scope: { course_id: 'c1', sub_id: 's9' }, steps: [{ evidence: { start_ms: 1 } }] }, lecture),",
            "  reviewPlanEvidenceAnchor({ scope: { course_id: 'c1' }, steps: [{ evidence: { start_ms: 1 } }] }, lecture),",
            "  reviewPlanEvidenceAnchor({ scope: { course_id: 'c1', sub_id: 's1' }, steps: [] }, lecture),",
            "  reviewStepEvidenceAnchor({ scope: { course_id: 'c1', sub_id: 's1' } }, { course_id: 'c1', sub_id: 's1', start_ms: 30000, evidence: null }, lecture),",
            "  reviewStepEvidenceAnchor({ scope: { course_id: 'c1', sub_id: 's1' } }, { evidence: { start_ms: 45000 } }, lecture),",
            "  reviewStepEvidenceAnchor({}, { course_id: 'c1', sub_id: 's2', start_ms: 30000 }, lecture),",
            "  reviewStepEvidenceAnchor({}, { course_id: 'c2', sub_id: 's1', start_ms: 30000 }, lecture),",
            "  reviewStepEvidenceAnchor({}, { course_id: 'c1', sub_id: 's1', start_ms: -5 }, lecture),",
            "  reviewStepEvidenceAnchor({}, { course_id: 'c1', sub_id: 's1' }, lecture),",
            "  reviewStepEvidenceAnchor({}, {}, lecture),",
            "]));",
        ))
        result = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(json.loads(result.stdout), [
            65000, None, None, None, None, 120000, None, None, None,
            30000, 45000, None, None, None, None, None,
        ])

    def test_grounded_answer_citations_map_only_when_reachable(self):
        palette = self.modules["search-palette.js"]
        self.assertIn("const mapped = citationSeekTarget(store, citation);", palette)
        self.assertIn('cites.append(textElement("span", label));', palette)
        self.assertIn('button.className = "citation-action";', palette)
        self.assertIn('button.type = "button";', palette)
        self.assertIn("executeItem({", palette)
        self.assertIn("jumpTo(store, mapped.target);", palette)
        self.assertIn('$("player-stage").currentTime = mapped.startSeconds;', palette)
        # P3-IMPL-PKGC-1：引用渲染抽为 renderCitations（快速/深度同构复用），
        # 块边界随之改为该函数体（防 .play( 自动播放的钉意图不变）
        cite_block = palette.split("(citations || []).forEach", 1)[1].split(
            "function setAnswerCardFace", 1
        )[0]
        self.assertNotIn(".play(", cite_block)
        jump_helper = "function jumpTarget" + palette.split(
            "function jumpTarget", 1
        )[1].split("function jumpTo", 1)[0]
        cite_helper = "export function citationSeekTarget" + palette.split(
            "export function citationSeekTarget", 1
        )[1].split("function catalogJumpItems", 1)[0]
        script = "\n".join((
            jump_helper,
            cite_helper,
            "const store = { courses: [{ course_id: 'c1', lectures: [{ sub_id: 's1' }] }] };",
            "const mapped = citationSeekTarget(store, { course_id: 'c1', sub_id: 's1', start_seconds: 12.5 });",
            "console.log(JSON.stringify([",
            "  mapped?.startSeconds,",
            "  mapped?.target.lecture.sub_id,",
            "  citationSeekTarget(store, { sub_id: 's1', start_seconds: 12.5 }),",
            "  citationSeekTarget(store, { course_id: 'c1', start_seconds: 12.5 }),",
            "  citationSeekTarget(store, { course_id: 'c9', sub_id: 's1', start_seconds: 12.5 }),",
            "  citationSeekTarget(store, { course_id: 'c1', sub_id: 's1' }),",
            "  citationSeekTarget(store, { course_id: 'c1', sub_id: 's1', start_seconds: -3 }),",
            "  citationSeekTarget(store, { course_id: 'c1', sub_id: 's1', start_seconds: 'x' }),",
            "]));",
        ))
        result = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(json.loads(result.stdout), [
            12.5, "s1", None, None, None, None, None, None,
        ])

    def test_study_install_disposes_every_listener_idempotently(self):
        study = self.modules["study.js"]
        self.assertIn('const unsubscribeActiveLecture = store.subscribe("activeLecture", handleActiveLecture)', study)
        self.assertIn("unsubscribeActiveLecture();", study)
        self.assertNotIn('store.subscribe("activeLecture", () =>', study)
        for element, event, handler in (
            # SIMPLIFY-AUDIT-1 S2：手动「刷新字幕」钮退役，装卸对随之移除
            ("artifact-kind", "change", "handleArtifactKind"),
            ("generate-quiz", "click", "handleGenerateQuiz"),
            ("analyze-concepts", "click", "handleAnalyzeConcepts"),
            ("document-input", "change", "handleDocumentInput"),
        ):
            self.assertIn(f'$("{element}").addEventListener("{event}", {handler})', study)
            self.assertIn(f'$("{element}").removeEventListener("{event}", {handler})', study)
        self.assertNotIn('handleReloadTranscript', study)
        self.assertIn("if (disposed) return;", study)
        self.assertIn("disposed = true;", study)
        self.assertIn("resetBookmarkLoading();", study)
        self.assertIn("resetBookmarkActions();", study)

    def test_mobile_player_keeps_essential_controls_and_44px_targets(self):
        # 一方控制台取代原生 controls：视频元素不再携带 controls（下载出口随之消失），
        # nodownload/noremoteplayback 与 disableremoteplayback 仅作浏览器 hint 兜底
        self.assertIn('<video id="player-stage" preload="metadata"', self.html)
        self.assertNotIn('<video id="player-stage" controls', self.html)
        self.assertIn('controlslist="nodownload noremoteplayback"', self.html)
        self.assertIn("disableremoteplayback", self.html)
        self.assertIn('kind="subtitles"', self.html)
        self.assertIn("@media (max-width: 719px)", self.css)
        self.assertIn("width: 44px; height: 44px; min-height: 44px; padding: 0; justify-content: center;", self.css)
        self.assertIn(".player-pane video", self.css)
        self.assertIn("@media (pointer: coarse)", self.css)
        self.assertIn("min-height: 44px", self.css)
        # 窄屏控制台：触控目标 44px、影院（低优先级）收进响应式呈现之外、
        # 时间轴保持 overlay 顶行 44px 命中；音量经静音按钮的点击式音量面触达
        self.assertIn(".player-ctrl-button { min-width: 44px; min-height: 44px; }", self.css)
        self.assertIn(".player-ctrl-theatre { display: none; }", self.css)
        self.assertIn(".player-timeline { height: 44px; }", self.css)
        self.assertIn(".player-timeline { --player-track-offset: 16px; }", self.css)
        self.assertIn(".player-ctrl-timeline::-webkit-slider-thumb { margin-top: 31px; }", self.css)

    # ---- 顶栏更新小组件（FEATURE-FRONTEND-UPDATE-DATA-1：UX-1 A1-A4/E1 冻结） ----

    def test_update_widget_slot_and_glyphs_are_declared(self):
        html = self.html
        # 槽位：theme-toggle 之后、status-capsule 之前；常驻 DOM + hidden；不在胶囊内
        toggle_index = html.index('id="update-toggle"')
        self.assertGreater(toggle_index, html.index('id="theme-toggle"'))
        self.assertLess(toggle_index, html.index('id="status-capsule"'))
        self.assertIn(
            # N10B-1（夜10-A 落地）：初始态具名（aria-label+title），动态渲染会覆写。
            'id="update-toggle" class="icon-button update-toggle" type="button" data-update-state="idle" aria-label="客户端更新" title="客户端更新" hidden',
            html,
        )
        self.assertIn('data-update-icon="install"', html)
        self.assertIn('data-update-icon="restart"', html)
        self.assertIn('class="update-dot" data-update-dot hidden', html)
        self.assertIn("svg[data-update-icon][hidden] { display: none; }", self.css)
        self.assertIn('id="update-live" class="sr-only" role="status" aria-live="polite"', html)
        # 44×44 稳定槽位：pages.css 后加载覆写 .icon-button 34px，状态原位换装
        update_css = self.css.split(".update-toggle {", 1)[1].split("}", 1)[0]
        self.assertIn("width: 44px", update_css)
        self.assertIn("min-width: 44px", update_css)
        self.assertIn("height: 44px", update_css)
        # 双主题 token：金/danger 点 + forced-colors 映射（components.css）
        self.assertIn('.update-toggle[data-update-state="available"] .update-dot', self.css)
        self.assertIn(".update-dot { forced-color-adjust: none; }", self.css)

    def test_update_widget_state_mapping_is_frozen(self):
        widget = self.modules["update-widget.js"]
        settings = self.modules["settings.js"]
        # 十三态词表与 settings updateLabels 同词表（逐串一致，A2）
        for state_label in (
            'idle: "尚未检查"', 'checking: "正在检查"', 'offline: "离线"',
            'up_to_date: "已是最新"', 'available: "发现更新"', 'downloading: "正在下载"',
            'verifying: "正在验证"', 'ready_to_restart: "等待重启"', 'applying: "正在应用"',
            'healthy: "更新成功"', 'rolled_back: "已自动回滚"', 'failed: "更新失败"',
            'policy_blocked: "策略阻止"',
        ):
            self.assertIn(state_label, settings)
            self.assertIn(state_label, widget)
        # C1/A3：唯一编排动作 update_now + confirmed:true；fire 后 ≤2s 快照轮询
        self.assertIn('action: "update_now", confirmed: true', widget)
        self.assertIn("UPDATE_POLL_INTERVAL_MS = 2000", widget)
        # E1：仅用户激活过的失败可见；ambient 失败/策略阻止不进顶栏
        self.assertIn('USER_FAILURE_STATES = new Set(["failed", "rolled_back"])', widget)
        self.assertIn("armed = true", widget)
        # A2：busy 全程 disabled+aria-busy；ready_to_restart disabled（重启由 launcher 收口）
        self.assertIn('button.disabled = busy || state === "ready_to_restart"', widget)
        self.assertIn('button.setAttribute("aria-busy", "true")', widget)
        # A3：failed/rolled_back 单击跳设置并定位更新组（UPDATE-UX-1 起 mac 宿主
        # 顶栏单击同走此跳转=「查看下载」，符号更名 openUpdateGroupInSettings，
        # 旧名 openRecoveryInSettings 钉死不得回潮）
        self.assertIn("openUpdateGroupInSettings", widget)
        self.assertNotIn("openRecoveryInSettings", widget)
        self.assertIn('"settings-update-group"', widget)
        # 阶段词进度：无 raw error（不内联 error.message）；UPDATE-UX-1 三律
        # 修订——下载在途的进度进可读名（aria/title），全模块唯一 % 即此模板
        self.assertIn("正在下载更新… ${downloadPercent}%", widget)
        self.assertEqual(widget.count("%"), 1)
        self.assertNotIn("error.message", widget)
        self.assertIn("发现新版本", widget)
        self.assertIn("更新失败，点击进入恢复", widget)
        self.assertIn("已自动回滚，点击查看详情", widget)
        self.assertIn("更新已就绪，即将重启并完成健康确认", widget)
        # UPDATE-UX-1：mac 检查道三态纳入映射合同——update-mac.js 检查态闭集
        # （idle/checking/available/up_to_date/unavailable），降级=诚实失败，
        # 绝不误导成「已是最新」（update-mac.js 降级错误码同钉）
        mac = self.modules["update-mac.js"]
        for phase in ("idle", "checking", "available", "up_to_date", "unavailable"):
            self.assertIn(f'phase: "{phase}"', mac)
        self.assertIn("mac_release_list_unavailable", mac)
        self.assertIn("current_version_unknown", mac)
        # 顶栏 mac 面：仅 available 可见（复用 Windows available 装束），其余安静隐藏
        self.assertIn("function renderMacWidget()", widget)
        self.assertIn('const visible = state.phase === "available"', widget)
        self.assertIn("点击查看下载", widget)
        # 检查道三态回音闭集：available 带版本 / up_to_date 安静确认 / 其余诚实降级
        self.assertIn("检查完成：已是最新版本。", widget)
        self.assertIn("检查没有完成：稍后再试，或直接打开下载页确认。", widget)
        # 设置面板 mac 三态映射（settings 家族并集含 update-panel.js）：
        # available=发现更新+三步安装展开 / up_to_date=安静「已是最新」/
        # unavailable=「手动下载」诚实降级
        self.assertIn('idle: "手动下载", checking: "正在检查", available: "发现更新",', settings)
        self.assertIn('up_to_date: "已是最新", unavailable: "手动下载",', settings)
        self.assertIn('$("update-mac-steps").hidden = state.phase !== "available"', settings)
        self.assertIn("检查没有完成：暂时连不上更新检查服务", settings)

    def test_settings_update_card_subscribes_and_owns_preference(self):
        settings = self.modules["settings.js"]
        widget = self.modules["update-widget.js"]
        # 唯一轮询者合同：settings 不再直接 GET client-update，改为订阅 store `update`
        self.assertIn('store.subscribe("update", renderUpdate)', settings)
        self.assertNotIn('apiV3("client-update")', settings)
        self.assertIn('apiV3("client-update")', widget)
        # C4/D3：偏好开关消费冻结键 update_background_checks（缺省=开），
        # 走 settings 动作闭集，无本地持久化
        self.assertIn('id="update-background-checks" type="checkbox" role="switch"', self.html)
        self.assertIn("value.update_background_checks !== false", settings)
        self.assertIn('action: "set-update-background-checks"', settings)
        self.assertNotIn("localStorage", self.modules["update-widget.js"])
        # C3：guidance 闭集表新增唯一条目 update_restart_blocked（409 retriable）
        self.assertIn("update_restart_blocked:", settings)
        self.assertIn("请等待任务完成后再试", settings)

    # ---- 数据管理工作区（FEATURE-FRONTEND-UPDATE-DATA-1：UX-1 B1-B6 + Q1-Q5） ----

    def test_data_page_entry_is_account_menu_only(self):
        # E2：唯一入口 = 账户菜单项；无顶栏入口、无设置页跳转按钮
        self.assertIn('id="account-menu-data" class="menu-item" type="button"', self.html)
        self.assertIn(">数据管理</button>", self.html)
        self.assertIn('id="data-page" class="page" data-page="data" hidden', self.html)
        course_data = self.modules["course-data.js"]
        self.assertIn('closeOverlay($("account-menu"))', course_data)
        self.assertIn('selectPage("data")', course_data)
        # 凭据域排除：本页无账户管理/密码/令牌入口，无本地持久化
        self.assertNotIn("github", course_data.lower())
        self.assertNotIn("deepseek", course_data.lower())
        self.assertNotIn("password", course_data.lower())
        self.assertNotIn("localStorage", course_data)

    def test_data_page_structure_and_closed_copy(self):
        course_data = self.modules["course-data.js"]
        for element_id in (
            "data-evidence", "data-search", "data-category-filter", "data-refresh",
            "data-summary", "data-recovery", "data-recovery-title", "data-recovery-impact",
            "data-recovery-actions", "data-bulk-bar", "data-selection-count", "data-select-all",
            "data-action-rebuild-search", "data-action-purge-derived", "data-action-remove-copies",
            "data-action-export", "data-action-delete-records", "data-clear-orphans",
            "data-clear-selection", "data-result", "data-course-list", "data-detail",
            "data-confirm-dialog", "data-confirm-form", "data-confirm-title", "data-confirm-hint",
            "data-confirm-input", "data-confirm-label", "data-confirm-cancel", "data-confirm-confirm",
            "data-orphans-filter", "data-bulk-hint", "data-bulk-actions",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        # D-20261009-07：确认标签文案必须住专用 span——对 label 赋 textContent
        # 会抹掉 label 内的确认输入框（typed 确认全族死亡的根因）
        self.assertIn('id="data-confirm-label-text"', self.html)
        # B3/B4 文案闭集（Object.freeze 表；无感叹号、无 raw 异常）
        for copy in (
            "正在统计数据…", "暂无课程数据", "统计生成于 ", "未统计", "孤儿", "在册",
            "可重建", "不可重建", "依赖远端", "清除全部孤儿", "清空选择",
            "将删除所选 ", " 项，共 ", "，删除后不可恢复。", "输入课程名「", "输入课程编号「", "」确认：",
            "输入任意文字确认",
            "将清除以下课程留下的残留数据：",
            "本课文字存量约 ", "（进度、字幕文本等，按存进学习库的内容计）",
            "移除本地副本后，原件需重新导入。", "远端图片可能已过期，之后可能无法重建。",
            "自动化规则可能在之后重新生成同类数据。",
            "先勾选课程，再选下方操作", "重建搜索索引，不改动课程数据", "导出全库数据清单",
            "删除记录一次仅支持一门课程", "永久删除观看进度、书签、测验作答",
        ):
            self.assertIn(copy, course_data)
        self.assertNotIn("error.message", course_data)
        self.assertNotIn("！", course_data)
        # B3：typed 危险确认 = 原生 dialog；确认按钮逐课程名精确匹配后启用
        self.assertIn("pendingConfirm.names.length > 0", course_data)
        # 详情面板纯只读：类别栅格与明细不再携带任何动作按钮
        self.assertNotIn("data-category-action", course_data)
        self.assertNotIn("data-detail-actions", course_data)
        # stale 不自动刷新：证据行显示统计生成时间，刷新按钮显式
        self.assertIn("renderEvidence", course_data)

    def test_data_page_categories_actions_and_schemas_frozen(self):
        course_data = self.modules["course-data.js"]
        # 类别闭集（B 冻结 13 类）与三 schema 名
        for key in (
            "progress", "transcript", "ppt", "artifacts", "documents", "references",
            "timeline", "search", "bookmarks", "quizzes", "review", "tasks", "automation",
        ):
            self.assertIn(f"{key}: ", course_data)
        for schema in (
            "courselens.course-data-summary.v1",
            "courselens.course-data-lecture-page.v1",
            "courselens.course-data-action-result.v1",
        ):
            self.assertIn(schema, course_data)
        # 动作闭集与阻塞码闭集
        for action in ("rebuild-search", "purge-derived", "remove-copies", "delete-records", "export"):
            self.assertIn(f'"{action}"', course_data)
        for blocker in (
            "active_task", "active_remote_run", "active_automation_import",
            "automation_rule", "cleanup_pending",
        ):
            self.assertIn(blocker, course_data)
        # Q5：ppt 恒为依赖远端档；Q3：AI 件（artifacts）为可重建档
        self.assertIn('ppt: "remote"', course_data)
        self.assertIn('artifacts: "rebuildable"', course_data)
        # 分页闭集：讲次 ≤50/页；课程 ≤200/页
        self.assertIn("LECTURE_PAGE_LIMIT = 50", course_data)
        self.assertIn("SUMMARY_PAGE_SIZE = 200", course_data)
        self.assertIn("course-data/lectures?course_id=", course_data)
        self.assertIn("include_orphans=true", course_data)
        self.assertIn("confirm_typed", course_data)
        self.assertIn('operationId("course-data")', course_data)

    def test_data_page_layout_and_budget(self):
        css = self.css
        # 分栏镜像 course-layout（400px+1fr），999px 收单列（窄窗行内 details）
        self.assertIn(".data-layout {", css)
        self.assertIn(".data-bulk-bar {", css)
        bulk_css = css.split(".data-bulk-bar {", 1)[1].split("}", 1)[0]
        self.assertIn("position: sticky", bulk_css)
        self.assertIn(".data-row-narrow", css)
        # 意图分组（安全→释放空间→不可逆）与空态提示行
        self.assertIn(".data-action-group", css)
        self.assertIn(".data-bulk-hint", css)
        self.assertIn('data-bulk-group="safe"', self.html)
        self.assertIn('data-bulk-group="space"', self.html)
        self.assertIn('data-bulk-group="irreversible"', self.html)
        # 375px 预算：既有紧凑规则保持；数据页搜索输入窄窗收缩不溢出
        self.assertIn(".topbar { gap: 2px; padding: 0 2px; }", css)
        self.assertIn(".topbar-actions { margin-left: auto; gap: 2px; }", css)
        self.assertIn(".header-actions .data-search { min-width: 0; width: 100%; }", css)

    def test_no_horizontal_overflow_guards_at_375(self):
        self.assertIn("min-width: 320px", self.css)
        self.assertIn("@media (max-width: 719px)", self.css)
        self.assertIn(".home-cards-row { grid-template-columns: minmax(0, 1fr); }", self.css)
        self.assertIn(".desk-columns { grid-template-columns: minmax(0, 1fr); }", self.css)

    def test_mobile_topbar_fits_375(self):
        # 新增主题键后仍保留所有 44×44 控件；窄屏仅收紧容器、控件间距和字标。
        self.assertIn(".topbar { gap: 2px; padding: 0 2px; }", self.css)
        self.assertIn(".topbar-actions { margin-left: auto; gap: 2px; }", self.css)
        self.assertIn(
            "width: 44px; height: 44px; min-height: 44px; padding: 0; justify-content: center;",
            self.css,
        )

    def test_academic_visual_tokens_are_applied(self):
        self.assertIn("--canvas: #F7F3EB", self.css)
        self.assertIn("--canvas: #1B1815", self.css)
        self.assertIn('--font-editorial: Georgia, "Noto Serif SC", "Noto Serif CJK SC", "Source Han Serif SC", "Songti SC", "STSong", "SimSun", "NSimSun", "宋体", serif', self.css)
        self.assertNotIn("#0F6C52", self.css)
        self.assertNotIn("#0f6c52", self.css)
        # 全站唯一允许的 gradient 是播放器控制台的功能性可读性 scrim（S09-D）：
        # 中性深色向上淡出，不带任何装饰性渐变
        self.assertEqual(self.css.count("gradient"), 1)
        self.assertIn("linear-gradient(to top, rgb(7 29 51 / 88%) 0%, rgb(7 29 51 / 55%) 55%, rgb(7 29 51 / 0%) 100%)", self.css)
        # A11Y-IMPL-4：正文基准 16.5px→1.03125rem（body 于 tokens.css，根字号档总开关）
        self.assertIn("1.03125rem", self.css)
        self.assertIn("line-height: 1.75", self.css)
        self.assertIn("font-variant-numeric: tabular-nums", self.css)

    def test_desktop_dual_pane_starts_at_1000px(self):
        self.assertIn("@media (min-width: 1000px)", self.css)
        self.assertIn("grid-template-columns: minmax(0, 1.7fr) minmax(360px, 1fr);", self.css)

    def test_native_cue_styling_matches_editorial_tokens(self):
        self.assertIn("video::cue", self.css)
        self.assertIn('font-family: Georgia, "Noto Serif SC", "Noto Serif CJK SC", "Source Han Serif SC", "Songti SC", "STSong", "SimSun", "NSimSun", "宋体", serif', self.css)
        self.assertIn("rgba(7, 29, 51, 0.78)", self.css)
        self.assertIn("--navy-deep #071D33", self.css)  # 注释记录 ::cue 内字面量例外
        self.assertIn("line-height: 1.5", self.css)

    def test_subtitle_overlay_replaces_unstylable_native_cues(self):
        # 合成 Chromium 证据：video::cue 完全不可控且原生多块白底不可接受；
        # track 保持 hidden 模式复用解析与时间轴，自绘 overlay 是唯一渲染者。
        player = self.modules["player-core.js"]
        self.assertIn('id="player-stage-shell"', self.html)
        self.assertIn('id="player-subtitle-track" kind="subtitles"', self.html)
        self.assertRegex(self.html, r'id="player-subtitle-overlay"[^>]*aria-hidden="true"')
        self.assertIn('textTrack.mode = "hidden"', player)
        self.assertIn('textTrack.addEventListener("cuechange", renderSubtitleOverlay)', player)
        # 真实 Chromium 中 TextTrack 模式变化不派发任何事件：禁止事件拦截死代码，
        # 既有字幕开关（原生 CC 按钮）必须由 rAF 模式看护逐 tick 感知
        self.assertNotIn('addEventListener("modechange"', player)
        self.assertIn("function applySubtitleMode", player)
        self.assertIn('subtitleTextTrack.mode === "showing"', player)
        self.assertIn('subtitleTextTrack.mode === "disabled"', player)
        self.assertIn("window.requestAnimationFrame(watchSubtitleModeStep)", player)
        self.assertIn("startSubtitleWatcher()", player)
        # 看护在讲次卸载/切换与清理时停止（定义之外至少 3 个停止点；
        # N6L S1 U3：直播/refresh-live 停止点随直播段移交 live-player）
        self.assertGreaterEqual(player.count("stopSubtitleWatcher()"), 3)
        self.assertIn("cues ? cues.length : 0", player)  # track.cues === null 安全
        # overlay 样式：不劫持指针与可达性、两行封顶、半透明深海军底 + 暖白字
        overlay_block = self.css.split(".player-subtitle-overlay {", 1)[1].split("}", 1)[0]
        self.assertIn("pointer-events: none", overlay_block)
        self.assertIn("-webkit-line-clamp: 2", overlay_block)
        self.assertIn("rgb(7 29 51 / var(--sub-bg-alpha, 0.78))", overlay_block)
        self.assertIn("color: #EFE9DC", overlay_block)  # 暗色主题 --ink 象牙白字面量，恒不随主题反转
        # D3 面板三变量必须被 CSS 真实消费（甲4：字号/背景/贴底档位不再是死设置）
        self.assertIn("font-size: var(--sub-font-size,", overlay_block)
        self.assertIn("var(--sub-bottom,", overlay_block)
        self.assertIn('overlay.style.setProperty("--sub-font-size"', player)
        self.assertIn('overlay.style.setProperty("--sub-bg-alpha"', player)
        self.assertIn('overlay.style.setProperty("--sub-bottom"', player)
        # 全屏直达：一方按钮在用户手势内同步对 shell 请求（异步等待会丢瞬态激活），
        # fullscreenchange 仅同步按钮状态；旧的 video 全屏"退出重进"重定向保持删除
        self.assertIn('document.addEventListener("fullscreenchange", handleFullscreenChange)', player)
        self.assertIn("document.fullscreenElement === shell", player)
        self.assertNotIn("document.fullscreenElement !== player", player)
        self.assertIn("document.exitFullscreen?.()", player)
        self.assertIn("Promise.resolve(shell.requestFullscreen()).catch", player)
        # S09-D 沉浸式 overlay：控制台 DOM 嵌在 fit 内部（视频矩形底缘），video/
        # overlay/controls 依次排列且控制台是 fit 的最后一个子层
        self.assertIn('id="player-stage-fit"', self.html)
        shell_block = self.html.split('id="player-stage-shell"', 1)[1].split('id="player-recovery"', 1)[0]
        self.assertIn('id="player-stage-fit"', shell_block)
        self.assertIn('<video id="player-stage"', shell_block)
        self.assertIn('id="player-subtitle-overlay"', shell_block)
        self.assertRegex(
            self.html,
            re.compile(r'id="player-controls".*?</div>\s*</div>\s*<p id="player-ctrl-status"', re.S),
        )  # 控制台之后连收两层（fit+shell）再出现播报区 = 嵌套在 fit 内
        self.assertIn(".player-stage-fit { position: relative; width: min(100%, calc(56vh * 16 / 9)); }", self.css)
        # 全屏单区 contain-fit：不预留 80px 控制台行（overlay 已在 fit 内贴底），
        # 禁止 width:auto + max-* 的塌缩写法（Chromium/152 实测不满屏）
        self.assertIn(".player-stage-shell:fullscreen {", self.css)
        fullscreen_shell = self.css.split(".player-stage-shell:fullscreen {", 1)[1].split("}", 1)[0]
        self.assertIn("place-items: center", fullscreen_shell)
        self.assertNotIn("grid-template-rows", fullscreen_shell)  # 不再预留底部控制台行
        self.assertIn("min(100%, calc(100vh * 16 / 9))", self.css)
        self.assertNotIn("100vh - 80px", self.css)
        self.assertNotIn("place-self: center", self.css)
        # 字幕安全区由 shell[data-chrome] 统一驱动（普通/影院/全屏共用），不再由
        # 全屏特化 80px；PLAYER-UX-1② 后 visible=72px=轨带上缘（旧 104px 悬空过高）
        self.assertIn('.player-stage-shell[data-chrome="visible"] { --subtitle-overlay-safe: 72px; }', self.css)
        self.assertIn('.player-stage-shell[data-chrome="hidden"] { --subtitle-overlay-safe: 20px; }', self.css)
        self.assertNotIn("--subtitle-overlay-safe: 80px", self.css)
        fullscreen_video = self.css.split(".player-stage-shell:fullscreen video {", 1)[1].split("}", 1)[0]
        self.assertNotIn("width: auto", fullscreen_video)  # 塌缩写法禁止回归
        # 全屏控制台贴屏幕底缘（fixed 锚 fullscreen 根，B 站式整屏控制台）：
        # 不随画面 letterbox 悬浮在画面底缘
        self.assertIn(".player-stage-shell:fullscreen .player-controls {", self.css)
        fullscreen_controls = self.css.split(".player-stage-shell:fullscreen .player-controls {", 1)[1].split("}", 1)[0]
        self.assertIn("position: fixed", fullscreen_controls)
        self.assertIn("bottom: 0", fullscreen_controls)

    def test_first_party_controls_replace_native_player_surface(self):
        # 一方控制台：原生 controls 与其 Download 菜单一并移除；hint 仅是纵深防御，
        # 主控制是"没有原生控制条"。没有下载路由、没有新的播放器依赖。
        player = self.modules["player-core.js"]
        shell_block = self.html.split('id="player-stage-shell"', 1)[1].split('id="player-recovery"', 1)[0]
        self.assertIn('controlslist="nodownload noremoteplayback"', self.html)
        self.assertIn("disableremoteplayback", self.html)
        self.assertNotIn("player-download", self.html + self.javascript)
        http_api = (ROOT / "src" / "runtime" / "http_api.py").read_text(encoding="utf-8")
        # 媒体路由面无任何 download 痕迹；D12 搬家包下载路由是文件带走面
        # （attachment 直存，非媒体回放面），其出现不破此约束。
        self.assertNotIn("player-download", http_api.lower())
        for line in http_api.splitlines():
            lowered = line.lower()
            if "download" not in lowered:
                continue
            for media_route in ("/api/v3/media", "/api/v3/subtitles/file",
                                "/api/v3/courseware-pdf/file", "/api/v3/materials/file"):
                self.assertNotIn(media_route, lowered,
                                 f"media route gained download semantics: {line}")
        self.assertEqual(self.html.count("<script"), 3)  # Hls.js vendor + 应用入口 + 问候空态装配入口（外置 greeting-boot.js）
        # 控制台整体在 shell 内（全屏时同屏可见），语义控件与播报区域齐备
        for control_id in (
            "player-controls", "player-ctrl-play", "player-ctrl-elapsed", "player-ctrl-timeline",
            "player-timeline-fill", "player-ctrl-duration", "player-ctrl-mute", "player-ctrl-volume",
            "player-ctrl-speed", "player-ctrl-subtitle", "player-ctrl-pip", "player-ctrl-theatre",
            "player-ctrl-fullscreen", "player-ctrl-status",
        ):
            self.assertIn(f'id="{control_id}"', shell_block)
        self.assertIn('role="group" aria-label="播放控制"', self.html)
        self.assertIn('id="player-ctrl-timeline" class="player-ctrl-timeline" type="range"', self.html)
        self.assertIn('id="player-ctrl-status" class="sr-only" role="status"', self.html)
        # USEROPS-1：字幕钮带长按样式入口提示（title），与快捷键表「长按『字幕』钮」行同链
        self.assertIn('id="player-ctrl-subtitle" class="player-ctrl-button player-ctrl-text" type="button" aria-pressed="true" aria-label="关闭字幕" title="点按开关字幕；长按可调字幕样式" hidden', self.html)
        # 属性级兜底：JS 再关一次原生 controls；不自动播放
        self.assertIn("player.controls = false", player)
        self.assertNotIn("autoplay", self.html)
        # PiP 保留（用户明确要求）：不禁用、能力探测显隐、状态同步
        self.assertNotIn("disablepictureinpicture", self.html)
        self.assertIn("requestPictureInPicture", player)
        self.assertIn("pictureInPictureEnabled", player)
        self.assertIn('player.addEventListener("enterpictureinpicture", syncPipButton)', player)
        self.assertIn('player.addEventListener("leavepictureinpicture", syncPipButton)', player)
        # 快捷键：可交互元素让位 + 修饰键让位；左右 5 秒；按住右方向临时 2 倍速并在
        # keyup/blur/讲次切换恢复；seek 钳制在 seekable 与时长之内
        self.assertIn("target.closest(PLAYER_SHORTCUT_EDITABLE)", player)
        self.assertIn('window.addEventListener("keydown", handlePlayerKeydown)', player)
        self.assertIn('window.addEventListener("keyup", handlePlayerKeyup)', player)
        self.assertIn('window.addEventListener("blur", handlePlayerWindowBlur)', player)
        self.assertIn('window.removeEventListener("keydown", handlePlayerKeydown)', player)
        self.assertIn('window.removeEventListener("keyup", handlePlayerKeyup)', player)
        self.assertIn('window.removeEventListener("blur", handlePlayerWindowBlur)', player)
        self.assertIn("PLAYER_SEEK_SECONDS = 5", player)
        self.assertIn("PLAYER_HOLD_THRESHOLD_MS = 400", player)
        self.assertIn("player.playbackRate = PLAYER_HOLD_RATE", player)
        self.assertIn("player.playbackRate = userRateBeforeHold", player)
        self.assertIn("seekableEnd(player)", player)
        self.assertIn("const canSeekPlayer = () => playbackKind === \"lecture\" && finiteDuration(player) > 0;", player)  # 直播不可 seek
        # 时间轴同步覆盖：元数据/时长变化/seeking/音量/倍率事件全量接线并成对解除
        for listener in (
            'player.addEventListener("durationchange", syncPlayerTimeline)',
            'player.addEventListener("seeking", syncPlayerTimeline)',
            'player.addEventListener("volumechange", syncVolumeControls)',
            'player.addEventListener("ratechange", handleRateChange)',
        ):
            self.assertIn(listener, player)
            self.assertIn(listener.replace("addEventListener", "removeEventListener"), player)
        # 影院模式：shell 级 class、可逆、状态由 aria-pressed 承载；不触碰浏览器全屏
        self.assertIn('shell.classList.toggle("theatre", enable)', player)
        self.assertIn('.player-stage-shell.theatre .player-stage-fit { width: min(100%, calc(78vh * 16 / 9)); }', self.css)
        self.assertIn(".player-pane:has(.player-stage-shell.theatre) { grid-column: 1 / -1; }", self.css)
        # 播报区域承载 seek / 临时倍速 / 全屏拒绝等诚实反馈
        self.assertIn("announcePlayerStatus", player)
        self.assertIn("浏览器拒绝了全屏请求", player)
        # U②：3× 提示文案与延时分化（840ms=600ms×1.4）
        self.assertIn("${PLAYER_HOLD_RATE.toFixed(1)}×播放中", player)
        self.assertIn("PLAYER_OSD_HOLD_HINT_MS = Math.round(PLAYER_OSD_HIDE_MS * 1.4)", player)

    def test_immersive_player_overlay_contract(self):
        # S09-D 沉浸式控制台：结构、显隐引擎、音量组件、快捷键收权、紧凑焦点
        player = self.modules["player-core.js"]
        css = self.css
        # ---- 结构：音量组件（静音+滑杆一体）、三态音量图标、timeline 在 overlay 顶行
        self.assertIn('id="player-volume" class="player-volume"', self.html)
        self.assertIn('id="player-volume-surface" class="player-volume-surface"', self.html)
        self.assertRegex(self.html, r'id="player-ctrl-volume"[^>]*aria-label="音量"')
        self.assertIn('data-volume-icon="low"', self.html)
        # 平台事实锁（S09-Q 390px 发现）：Chromium UA [hidden] 不隐藏 SVG，
        # 非激活音量图标必须由显式规则收起，否则三图标同显（静音键 72px）
        # 在 390px 把全屏按钮挤出视口
        self.assertIn("svg[data-volume-icon][hidden] { display: none; }", self.css)
        self.assertRegex(self.html, r'id="player-stage-shell"[^>]*data-chrome="visible"')
        self.assertIn('id="player-controls" class="player-controls" role="group" aria-label="播放控制" data-chrome-state="visible" hidden', self.html)
        self.assertIn('class="player-controls-row"', self.html)
        self.assertIn('class="player-controls-spring" aria-hidden="true"', self.html)
        # timeline 是 overlay 第一个子行（顶行），按钮行在其下
        controls_block = self.html.split('id="player-controls"', 1)[1].split("player-controls-row", 1)[0]
        self.assertIn('class="player-timeline"', controls_block)
        self.assertIn('id="player-ctrl-timeline"', controls_block)
        # ---- 显隐引擎：单一 1.5s 计时器（S10-A 契约值）+ 钉住集合 + 到点复核
        self.assertIn("const PLAYER_IDLE_HIDE_MS = 1500;", player)
        self.assertIn("function chromePinned()", player)
        # PLAYER-UX-1①：暂停不再是钉住条件（与播放共用空闲计时）；
        # deckHover=悬停控制台豁免面。
        for pin in ("mediaBuffering", "keyboardFocusInsidePlayer", "volumeOpen", "timelineScrubbing", "deckHover"):
            self.assertIn(pin, player.split("function chromePinned()", 1)[1].split("}", 1)[0])
        self.assertIn("setChromeVisible(chromePinned());", player)  # 到点复核不误收起
        self.assertIn("function clearIdleTimer()", player)
        for event in ("pointerenter", "pointermove", "pointerleave", "pointercancel", "focusin", "focusout"):
            self.assertIn(f'shell.addEventListener("{event}"', player)
            self.assertIn(f'shell.removeEventListener("{event}"', player)
        self.assertIn('shell.addEventListener("pointerup", handleShellPointerUp)', player)
        self.assertIn('event.pointerType !== "touch" && event.pointerType !== "pen"', player)  # 粗指针点按切换
        self.assertIn('target.closest(".player-controls")', player)  # 点在控制台上不是切换
        self.assertIn('player.addEventListener("waiting", handleBufferingStart)', player)
        self.assertIn('player.addEventListener("playing", handleBufferingEnd)', player)
        self.assertIn('player.addEventListener("canplay", handleBufferingEnd)', player)
        self.assertIn('document.addEventListener("visibilitychange", handleDocVisibilityChange)', player)
        self.assertIn('document.addEventListener("pointerdown", handleDocPointerDown, true)', player)
        self.assertIn('document.removeEventListener("pointerdown", handleDocPointerDown, true)', player)
        self.assertIn('document.removeEventListener("visibilitychange", handleDocVisibilityChange)', player)
        self.assertIn('addEventListener("keydown", handleVolumeGroupEscape)', player)
        # 讲次/直播切换确定性复位
        self.assertIn("mediaBuffering = false;", player)
        self.assertIn("pokeChrome();", player)
        # ---- 音量：真实音量与静音分离、非零记忆、诚实 0-100 可达值
        self.assertIn("let lastAudibleVolume = 0;", player)
        self.assertIn('return volume <= 0.5 ? "low" : "sound-on";', player)
        self.assertIn("player.volume = lastAudibleVolume > 0 ? lastAudibleVolume : 0.5;", player)
        self.assertIn("player.muted = true;", player)  # 静音只改 muted，不动 volume
        self.assertIn('slider.setAttribute("aria-valuetext", `${Math.round(volume * 100)}%`);', player)
        self.assertIn("if (!Number.isFinite(requested)) return; /* 畸形值防御：不动媒体音量 */", player)
        self.assertIn("volumeOpen = volumeHover || volumeFocus || volumeClickPin;", player)
        self.assertIn('$("player-volume").dataset.open = volumeOpen ? "true" : "false";', player)
        # ---- 快捷键收权（PLAYER-INTERACT-REPAIR-1 修正）：按「焦点在哪」判定，
        # 不要求播放器先被聚焦；可交互元素让位 + 学习桌不可见让位
        self.assertIn("if (target.closest(PLAYER_SHORTCUT_EDITABLE)) return false;", player)
        self.assertIn('const desk = $("study-desk");', player)
        self.assertIn("return Boolean(desk) && desk.hidden === false;", player)
        self.assertIn("if (!playerOwnsKeyEvent(event) || !shortcutOwnsEvent(event)) return;", player)
        # ---- 长按右方向（B 站式 keyup 裁决）：按下不立即 seek，短按松开才快进
        self.assertIn("pendingRightSeek = true;", player)
        self.assertIn("if (!canSeekPlayer()) return;", player)
        self.assertIn("event.preventDefault(); /* 仅在实际消费快捷键时阻止默认 */", player)
        # ---- CSS：overlay 贴底、显隐过渡、音量面、紧凑焦点
        self.assertIn("linear-gradient(to top, rgb(7 29 51 / 88%)", css)  # 功能性 scrim（唯一 gradient）
        self.assertIn('.player-controls[data-chrome-state="hidden"]', css)
        self.assertIn(".player-volume-surface {", css)
        self.assertIn('.player-volume[data-open="true"] .player-volume-surface', css)
        # U③：音量面渐显/渐隐走派生慢速 token（150ms→300ms）
        self.assertIn("--motion-volume: calc(var(--motion-fast) * 2);", css)
        self.assertIn("transition: opacity var(--motion-volume) ease, visibility 0s linear var(--motion-volume);", css)
        self.assertIn(".player-ctrl-timeline:focus-visible { outline: none; }", css)
        self.assertIn(".player-ctrl-timeline:focus-visible::-webkit-slider-thumb", css)
        self.assertIn("box-shadow: 0 0 0 2px var(--stage-bg), 0 0 0 4px var(--stage-ink);", css)  # 恒暗 chrome 族（批β token 化）
        # S10-A 时间轴单一几何模型：thumb 尺寸是唯一常量，填充端缘按同一常量内缩，
        # 宽度 = (100% − thumb) × 进度比；JS 只写 --play-ratio，不写像素宽度
        self.assertIn("--player-thumb-size: 14px;", css)
        self.assertIn("left: calc(var(--player-thumb-size) / 2);", css)
        self.assertIn("width: calc((100% - var(--player-thumb-size)) * var(--play-ratio, 0));", css)
        # 播放条贴近按钮行（2026-09-21 用户反馈）+ 等距视觉平衡（2026-09-22 用户
        # 拍板，二次微调两侧≈11px 呼吸感）：可见轨/填充/旗标经 --player-track-offset
        # 下移（32px 带盒 10px / 44px 命中区 16px），thumb 同步（基准 19px / 44px 31px）
        # 补充E②：控制钮悬浮指示=金 accent 高对比色（黑底可辨），数字验收：
        # --stage-accent-strong 对 --stage-bg 的 WCAG 对比度必须 ≥4.5:1，
        # 且 hover 不再落回 --navy-ink（黑底≈1.6:1 不可辨的根因）
        self.assertIn(".player-ctrl-button:hover:not([disabled]) {", css)
        hover_block = css.split(".player-ctrl-button:hover:not([disabled]) {", 1)[1].split("}", 1)[0]
        self.assertIn("color: var(--stage-accent-strong);", hover_block)
        self.assertNotIn("--navy-ink", hover_block)
        tokens_css = (FRONTEND / "styles" / "tokens.css").read_text(encoding="utf-8")

        def _luminance(hex_color: str) -> float:
            red, green, blue = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
            def _channel(value: float) -> float:
                return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)

        accent = re.search(r"--stage-accent-strong:\s*(#[0-9A-Fa-f]{6})", tokens_css).group(1)
        stage_bg = re.search(r"--stage-bg:\s*(#[0-9A-Fa-f]{6})", tokens_css).group(1)
        ratio = (max(_luminance(accent), _luminance(stage_bg)) + 0.05) / (min(_luminance(accent), _luminance(stage_bg)) + 0.05)
        self.assertGreaterEqual(ratio, 4.5, f"悬浮金 accent 对黑底对比度不足: {ratio:.2f}:1")
        self.assertIn("--player-track-offset: 10px;", css)
        self.assertIn("top: calc(50% + var(--player-track-offset));", css)
        self.assertIn("margin-top: 19px;", css)
        self.assertIn('fill.style.setProperty("--play-ratio"', player)
        self.assertNotIn("fill.style.width", player)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)  # 全局降级仍在
        self.assertIn("@media (forced-colors: active)", css)
        self.assertIn(".player-volume-surface { border: 1px solid ButtonText; background: Canvas; }", css)
        # ---- 边界：无 Bilibili 资产/命名、无新依赖、全局 accessibility 样式表未被削弱
        self.assertNotIn("bilibili", (self.html + css + player).lower())
        self.assertEqual(self.html.count("<script"), 3)
        accessibility = (FRONTEND / "styles" / "accessibility.css").read_text(encoding="utf-8")
        # S10-A 焦点契约：紧凑 2px 键盘环；tabindex="-1" 编程目标（壳/分组标题/
        # 浮层根/roving 选项卡）针对性抑制容器级大环；SVG 平台事实全局收敛
        self.assertIn(":focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }", accessibility)
        self.assertIn('[tabindex="-1"]:focus, [tabindex="-1"]:focus-visible { outline: none; }', accessibility)
        self.assertIn("svg[hidden] { display: none; }", accessibility)
        self.assertEqual(accessibility, accessibility)  # 锚定读取成功（文件不因本包改变）

    def test_live_playback_leaves_the_study_page(self):
        """N6L S1 U3（还债减法反向钉）：live-only 学习桌模式与直播播放模式键
        整体退役——直播动线=header 钮/卡片跳转 → data-page=live 独立页
        （行为钉见 frontend_live_page_behavior.mjs），学习页/学习桌播放器
        对直播协议零残留。"""
        player = self.modules["player-core.js"]
        study = self.modules["study.js"]
        self.assertNotIn("livePlayback", player + study + self.modules["store.js"])
        self.assertNotIn("courselens:live-play", self.javascript)
        self.assertIn('data-page="live"', self.html)

    def test_search_esc_steps_back_one_level(self):
        palette = self.modules["search-palette.js"]
        self.assertIn("onEscape: () => {", palette)
        self.assertIn('if (paletteMode !== "compact") {', palette)
        self.assertIn('setMode("compact");', palette)
        self.assertIn("return true;", palette)
        self.assertIn("return false;", palette)

    def test_compact_short_query_skips_network(self):
        palette = self.modules["search-palette.js"]
        self.assertIn('if (query.length < 2) {', palette)
        self.assertIn("继续输入以搜索全文，或直接跳转。", palette)
        guard_block = palette.split("if (query.length < 2) {")[1].split("}")[0]
        self.assertNotIn("apiV3(", guard_block)

    def test_blocked_overlay_produces_no_side_effects(self):
        ui = self.modules["ui.js"]
        tasks = self.modules["tasks-drawer.js"]
        palette = self.modules["search-palette.js"]
        shell = self.modules["shell.js"]
        self.assertIn("if (overlayStack.length) return null;", ui)
        self.assertIn("if (!closeDrawer) return; /* 单浮层守卫被挡：不产生任何副作用 */", tasks)
        self.assertIn("if (!opened) return;", palette)
        self.assertIn('trigger.setAttribute("aria-expanded", "false");', shell)
        self.assertGreaterEqual(shell.count("if (!opened) return;"), 2)
        self.assertIn('const opened = openOverlay({', shell)

    def test_settings_entry_only_from_account_menu(self):
        self.assertIn('id="account-menu-settings"', self.html)
        self.assertNotIn("去设置", self.html)
        self.assertNotIn("open-settings-from-drawer", self.html + self.javascript)
        shell = self.modules["shell.js"]
        self.assertNotIn("data-conn-goto", shell + self.html)

    def test_overlays_declare_aria_modal(self):
        for overlay_id in ("task-drawer", "palette-panel", "conn-menu", "account-menu"):
            self.assertRegex(
                self.html,
                rf'id="{overlay_id}"[^>]*role="dialog"[^>]*aria-modal="true"|id="{overlay_id}"[^>]*aria-modal="true"[^>]*role="dialog"',
            )

    def test_tabler_icons_are_vendored_with_license(self):
        icons = FRONTEND / "assets" / "icons"
        self.assertTrue((icons / "LICENSE.txt").is_file())
        self.assertIn("Tabler Icons", (icons / "LICENSE.txt").read_text(encoding="utf-8"))
        # R4 P-21 载荷卫生：只保留真实使用的两枚（index.html 直接引用），
        # 零引用的 bootstrap 预备资产已随树整理删除（git 历史可恢复）。
        self.assertEqual(
            sorted(path.name for path in icons.glob("*.svg")),
            ["book.svg", "search.svg"],
        )

    # ---- AI 学习工作台：结构化总结 / 测验揭示 / 复习步骤 / 考试上下文 ----

    def test_ai_learning_workspace_behavior_harness_passes(self):
        harness = ROOT / "tests" / "frontend_ai_learning_workspace_behavior.mjs"
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for frontend behavior tests")
        result = subprocess.run(
            [str(node), str(harness)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"ai learning workspace behavior failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("frontend ai learning workspace behavior passed", result.stdout)

    def test_summary_view_is_structured_with_honest_states(self):
        self.assertIn('data-material-tab="notes" aria-controls="panel-notes" aria-selected="false" tabindex="-1">总结</button>', self.html)
        self.assertIn("<h2>本讲总结</h2>", self.html)
        for element_id in (
            "artifact-structured", "artifact-overview-title", "artifact-overview",
            "artifact-takeaways-title", "artifact-takeaways", "artifact-chapters-title",
            "artifact-chapters", "artifact-source", "artifact-key-moments", "artifact-key-moment-list",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        study = self.modules["study.js"]
        # 结构化优先、legacy 回退；内容为空时诚实说明，绝不 JSON.stringify 原始载荷
        self.assertIn("function structuredNotes(artifact)", study)
        self.assertIn("function legacyNotesText(artifact)", study)
        self.assertIn("function artifactStatusNote(artifact)", study)
        self.assertNotIn("JSON.stringify(content", study)
        # Lecture IR 是纯增强：contract 不匹配或空数组一律隐藏
        self.assertIn('const IR_CONTRACT = "evidence.v1";', study)
        self.assertIn("if (content.contract !== IR_CONTRACT) return null;", study)
        self.assertIn("return keyMoments.length ? { keyMoments } : null;", study)
        # 请求守卫：总结与 IR 各自 epoch/AbortController；dispose 时收口
        self.assertIn("artifactController = new AbortController();", study)
        self.assertIn("irController = new AbortController();", study)
        self.assertIn("artifactController?.abort();", study)
        self.assertIn("irController?.abort();", study)
        self.assertIn("if (epoch !== artifactEpoch) return;", study)
        self.assertIn("if (epoch !== irEpoch) return;", study)
        # 生成来源行不暴露技术哈希
        self.assertNotIn("input_hash", study)
        self.assertIn(".structured-summary", self.css)
        self.assertIn(".chapter-row {", self.css)

    def test_quiz_answer_reveal_is_collapsed_by_default(self):
        """C⑨：提交前答案不可见；「提交并核对」单向揭示答案+原文。"""
        study = self.modules["study.js"]
        self.assertIn('reveal.setAttribute("aria-expanded", "false");', study)
        self.assertIn('reveal.setAttribute("aria-controls", `quiz-answer-${index}`);', study)
        self.assertIn('reveal.textContent = "提交并核对";', study)
        self.assertIn('reveal.textContent = "已核对";', study)
        self.assertIn('reveal.disabled = true;', study)
        self.assertIn("answer.hidden = true;", study)
        self.assertIn(".answer-reveal-button {", self.css)
        self.assertIn(".quiz-answer {", self.css)

    def test_exam_context_renders_only_for_active_with_validated_scope(self):
        self.assertIn('<section id="exam-context" class="exam-context" hidden>', self.html)
        for element_id in ("exam-context-row", "exam-today-title", "exam-today-list", "exam-today-state"):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn("今天建议", self.html)
        study = self.modules["study.js"]
        # 渲染门：exam_state=active 且 course_scope 非空；不一致（时间缺失/已过/>720h）fail closed
        self.assertIn('if (String(plan?.exam_state || "") !== "active") return false;', study)
        self.assertIn("return Array.isArray(plan?.course_scope) && plan.course_scope.length > 0;", study)
        self.assertIn("if (diffMs < 0 || diffMs > 720 * 3600 * 1000) return null;", study)
        # 今天建议只消费后端顺序前缀：不重排、不打分、无“必考/押题”文案
        self.assertNotIn("必考", study + self.html)
        self.assertNotIn("押题", study + self.html)
        self.assertIn("const suggested = [];", study)
        # passed 的计划保留普通复习并显示明确状态
        self.assertIn('String(plan?.exam_state || "") === "passed"', study)
        self.assertIn("考试已结束，保留历史步骤。", study)
        self.assertIn(".exam-context-row {", self.css)

    def test_course_relations_never_claim_evidence_without_evidence(self):
        study = self.modules["study.js"]
        self.assertIn('if (Array.isArray(edge?.evidence) && edge.evidence.length) return "有证据";', study)
        self.assertIn('return "证据状态未提供";', study)
        self.assertIn('if (String(edge?.status || "") === "stale") return "证据待确认";', study)
        # 不再有 || "有证据" 的默认兜底
        self.assertNotIn('|| "有证据"', study)

    def test_ask_about_lecture_reuses_the_search_palette(self):
        self.assertIn('<button id="ask-lecture" class="text-button" type="button">针对本讲提问</button>', self.html)
        study = self.modules["study.js"]
        palette = self.modules["search-palette.js"]
        self.assertIn('window.dispatchEvent(new CustomEvent("courselens:ask-lecture"', study)
        self.assertIn('if (!lecture) return toast("请先选择讲次", "error");', study)
        self.assertIn('window.addEventListener("courselens:ask-lecture", handleAskLecture);', palette)
        self.assertIn("function openPalette(trigger, scope = null)", palette)
        self.assertIn("function lectureScopeHint(store)", palette)
        # 证据回答继续只带活动讲次范围，不新增第二套问答通道
        self.assertIn('sub_id: store.activeLecture?.sub_id || ""', palette)
        self.assertIn('window.removeEventListener("courselens:ask-lecture", handleAskLecture);', palette)
        self.assertIn(".notes-header-actions {", self.css)
        self.assertIn(".notes-panel-header { flex-wrap: wrap;", self.css)

    def test_review_steps_show_minutes_status_reason_and_evidence(self):
        study = self.modules["study.js"]
        self.assertIn("const REVIEW_STEP_KIND_LABELS = Object.freeze({", study)
        self.assertIn("function reviewStepMinutes(step)", study)
        self.assertIn("function lectureLabel(store, courseId, subId)", study)
        self.assertIn("function renderReviewPlans(store, target, plans, lecture)", study)
        # 步骤状态/理由原样呈现；分钟数优先新合同字段、回退 legacy
        self.assertIn("if (step?.status) metaParts.push(String(step.status));", study)
        self.assertIn("if (step?.reason) row.append(textElement(\"span\", `理由：${step.reason}`, \"review-step-reason\"));", study)
        self.assertIn(".review-step {", self.css)
        self.assertIn(".review-step-reason {", self.css)

    # ---- courseware PDF: explicit action, bounded polling, path-checked download ----

    def test_courseware_pdf_surface_information_architecture_and_copy(self):
        # IA：资料面板 = h2 资料 → h3 本讲课件 → h3 全部资料（统一文件中心，
        # DATA-DELETE-REPAIR-1）；不新增顶层 tab
        self.assertIn("<h2>资料</h2>", self.html)
        self.assertIn('id="courseware-title"', self.html)
        self.assertIn(">本讲课件</h3>", self.html)
        self.assertIn('id="imported-documents-title"', self.html)
        self.assertIn(">全部资料</h3>", self.html)
        self.assertIn('id="document-list"', self.html)
        # 本讲课件在资料面板内部，而不是播放器按钮旁
        surface = self.html[self.html.index('id="courseware-surface"') : self.html.index('id="imported-documents-title"')]
        self.assertIn('id="generate-courseware-pdf"', surface)
        self.assertIn("生成课件 PDF", surface)
        self.assertIn('id="courseware-pdf-state"', surface)
        self.assertIn('role="status"', surface)
        self.assertIn('aria-live="polite"', surface)
        self.assertIn('aria-atomic="true"', surface)
        self.assertIn('id="courseware-pdf-detail"', surface)
        self.assertIn('id="courseware-pdf-download"', surface)
        self.assertIn('id="courseware-notes"', surface)
        self.assertIn("整理说明", surface)
        self.assertIn("有改动或批注的版本会全部保留", surface)
        self.assertIn("不等于原始课件页序", surface)
        # 可见文案不含 em-dash/en-dash
        for dash in ("—", "–"):
            self.assertNotIn(dash, surface)

    def test_courseware_pdf_states_are_evidence_driven_and_bounded(self):
        study = self.modules["study.js"]
        # artifact/operation 两个独立事实；互不覆盖
        self.assertIn("const artifact = status?.artifact", study)
        self.assertIn("const operation = status?.operation", study)
        self.assertIn("opActive", study)
        # 动作语义来自后端 operation 事实，绝不从点击推断成功
        self.assertIn('postV3("courseware-pdf/actions"', study)
        self.assertIn('operation?.state === "paused" ? "resume" : "generate"', study)
        # 整段活动操作期间禁用（不止 POST 请求期间）
        self.assertIn("if (button.disabled) return;", study)
        self.assertIn("COURSEWARE_PDF_ACTIVE_STATES.has(String(operation.state", study)
        self.assertIn(
            "renderCoursewarePdf(\n        coursewarePdfLastStatus,\n        lastKnownCoursewareOperationActive() && coursewarePdfStatusRetries >= COURSEWARE_PDF_MAX_STATUS_RETRIES\n          ? { stale: true, manualRetry: true }\n          : {},\n      );",
            study,
        )
        # 百分比只在后端 measured 计数下渲染
        self.assertIn("operation.percent_measured", study)
        # 失败/暂停文案走闭集映射；颜色之外有文字含义（data-tone）
        self.assertIn("COURSEWARE_PDF_ERROR_TEXT", study)
        self.assertIn("ppt_record_storm", study)
        self.assertIn('infoNode.dataset.tone = tone', study)
        # 轮询单一且在讲次切换/卸载时失效
        self.assertIn("stopCoursewarePdfPolling", study)
        self.assertIn("loadCoursewarePdf(store),", study)
        self.assertIn('pollCoursewarePdf(activeStore, String(lecture.sub_id || ""))', study)
        self.assertIn('$("generate-courseware-pdf")?.addEventListener("click", handleGenerateCoursewarePdf)', study)
        self.assertIn('$("generate-courseware-pdf")?.removeEventListener("click", handleGenerateCoursewarePdf)', study)
        # 瞬时状态错误保留已知成品
        self.assertIn("状态暂时无法确认", study)
        # 整理说明仅在成品存在时出现
        self.assertIn('notes.hidden = !notesVisible', study)
        # 任务抽屉显示闭集中文标签，绝不出现原始 kind 标识
        drawer = self.modules["tasks-drawer.js"]
        self.assertIn('courseware_pdf: "课件 PDF"', drawer)
        # 甲-1b：课件任务活跃→完成跃迁广播 materials-refresh，学习页资料区
        # （本讲课件状态行+全部资料）即刻换新，sub_id 定向不误刷
        self.assertIn("courselens:materials-refresh", study)
        self.assertIn("courselens:materials-refresh", drawer)
        # SWEEPFIX-R2 W1（D-20261009-15①）：字幕任务完成跃迁广播
        # transcript-refresh，学习页文稿热重读（sub_id 定向+已有时轴事实不重读），
        # 笔记按钮随 transcriptHasTiming 热启用，学生不再「重进讲次」
        self.assertIn("courselens:transcript-refresh", study)
        self.assertIn("courselens:transcript-refresh", drawer)

    def test_materials_import_flow_and_kind_filter_layout(self):
        """MATERIALS-DECLUTTER-1 甲-2：导入流程成组（类型→导入→课程级选项），
        列表筛选默认「全部类型」；空态收形（筛选/课程级选项退场、导入钮
        升为主操作）由 data-empty 驱动，控件语义零变化。"""
        self.assertIn('id="document-kind-filter"', self.html)
        self.assertIn('<option value="all">全部类型</option>', self.html)
        self.assertIn('data-empty="true"', self.html)
        flow = self.html[self.html.index('id="import-flow"') : self.html.index('id="document-list"')]
        self.assertIn('id="document-type-select"', flow)
        self.assertIn('aria-label="导入类型"', flow)
        self.assertIn('id="document-input"', flow)
        self.assertIn('id="document-scope-course"', flow)
        self.assertIn("作为课程级资料（不挂讲次）", flow)
        self.assertIn('role="group"', flow)
        self.assertIn('.imported-documents[data-empty="true"] .document-kind-filter { display: none; }', self.css)
        self.assertIn('.imported-documents[data-empty="true"] .document-scope-option { display: none; }', self.css)
        self.assertIn('.imported-documents[data-empty="true"] .file-button {', self.css)
        # 甲-4：两区块既有 token 细线分隔，间距不发明新值
        self.assertIn(".imported-documents { border-top: 1px solid var(--line); padding-top: var(--space-3); }", self.css)

    def test_declutter_conditional_display_family(self):
        """N6L §二十七 乙-2：折叠条件显示族——内容一字不删，只收视觉。
        #14 播放器提示条件化 / #15 任务占位行收起 / #22 书签状态行空文案收起 /
        #33 洞察说明随入口钮 / #38 安装阶段折叠 / #50 数据页说明折叠；
        #41/#42 终态树已合规（行级 hidden + 诊断 details），只核对。"""
        player = self.modules["player-core.js"]
        study = self.modules["study.js"]
        self.assertIn("hint.hidden = Boolean(available) && !autoOn && timed;", player)
        self.assertIn('id="player-task-state" class="status-row" data-state="unknown" hidden', self.html)
        self.assertIn("taskStateRow.hidden = false;", player)
        self.assertIn('id="bookmark-action-state" class="hint" role="status" hidden', self.html)
        self.assertIn("function setBookmarkActionState(node, text)", study)
        self.assertIn("node.hidden = !text;", study)
        self.assertIn('id="insight-hint" class="hint" hidden', self.html)
        self.assertIn('insightHint.hidden = playbackKind !== "lecture";', player)
        self.assertIn('id="github-phases-details"', self.html)
        self.assertIn("安装阶段详情", self.html)
        for phase_label in ("账号授权", "专属仓库", "App 安装", "加密通道"):
            self.assertIn(phase_label, self.html)
        self.assertIn('id="data-bulk-explain-details"', self.html)
        self.assertIn("六个按钮各做什么", self.html)  # STUDY-STATS-M3：+导出学习统计
        self.assertIn('id="data-action-export-study-stats"', self.html)
        self.assertIn("可直接用 Excel 打开", self.html)
        self.assertIn("「删除记录」不可恢复，仅删本机学习记录，课程和成绩不受影响。", self.html)
        self.assertIn('id="remote-mailbox-reconcile" class="status-row mailbox-reconcile-card" hidden', self.html)
        self.assertIn('<div id="remote-rotate-row" hidden>', self.html)
        self.assertIn(
            ".github-phases-details > summary { width: fit-content; cursor: pointer; font-size: 0.84375rem; line-height: 1.6; color: var(--muted); }",
            self.css,
        )
        self.assertIn(
            ".data-bulk-explain-details > summary { width: fit-content; cursor: pointer; font-size: 0.84375rem; line-height: 1.6; color: var(--muted); }",
            self.css,
        )

    def test_declutter_guidance_fade_family(self):
        """N6L §二十七 乙-3：挪引导+首用淡出族——一次性教学文字迁入引导，
        常驻行删除/收起；内容零删除。"""
        boot = self.modules["greeting-boot.js"]
        greeting = self.modules["greeting.js"]
        # #6 问候行首用淡出：boot 计数（本机 UI 偏好键，零课程身份）+ 淡出判定
        self.assertIn('APP_STARTS_KEY = "courselens:app-starts"', boot)
        self.assertIn("GREETING_FADE_STARTS = 5", boot)
        self.assertIn("installGreeting(store, { startCount });", boot)
        self.assertIn("if (startCount > fadeStarts) greetingEl.hidden = true;", greeting)
        # #11 周对话框教学行迁入引导第 3 步（常驻 span 删除）
        self.assertNotIn(">点按课程块可选中对应目录课程；Esc 关闭浮层。</span>", self.html)
        step3 = self.html[self.html.index('id="onboarding-step-3"') : self.html.index('id="onboarding-step-4"')]
        self.assertIn("点按课程块可选中对应目录课程；Esc 关闭浮层。", step3)
        # #26 资料教学文迁入引导第 5 步（资料空态只留一句短引导）
        step5 = self.html[self.html.index('id="onboarding-step-5"') : self.html.index('id="onboarding-action-error"')]
        self.assertIn("生成的课件、导入的资料和完成的 AI 总结会自动出现在课程的「资料」页。", step5)
        self.assertNotIn("生成的课件、导入的资料和完成的 AI 总结都会自动出现在这里，可以逐条导出或删除。", self.html)
        # #45 设置页指路行删除，引导第 5 步承接原文
        self.assertNotIn('id="settings-timetable-evidence"', self.html)
        self.assertIn("高频周切换与会议冲突提示在「学习」页的「今日与本周安排」中。", step5)

    def test_courseware_pdf_download_is_anchor_only_with_no_automatic_fetch(self):
        study = self.modules["study.js"]
        # 下载只通过带 href 的锚点发生；状态/动作路径绝不 fetch 文件路由
        self.assertIn('const downloadHref = `/api/v3/courseware-pdf/file?sub_id=${encodeURIComponent(subId)}`;', study)
        self.assertIn('downloadNode.setAttribute("href", downloadHref)', study)
        self.assertNotIn('apiV3(`courseware-pdf/file', study)
        # 下载文件名来自后端消毒结果，锚点 download 属性传递
        self.assertIn('downloadNode.setAttribute("download", name)', study)
        self.assertIn('!name.includes("..")', study)
        # 就绪文案由成品计数构成，诚实不夸大
        self.assertIn("个课堂画面整理为", study)
        self.assertIn("批注版本已保留", study)
        # CSS：复用令牌，窄屏堆叠，动作区 44px
        self.assertIn(".courseware-row {", self.css)
        self.assertIn(".courseware-actions {", self.css)
        self.assertIn("min-height: 44px", self.css)
        self.assertIn('courseware-info[data-tone="paused"] { border-left-color: var(--gold); }', self.css)
        self.assertIn('courseware-info[data-tone="failed"] { border-left-color: var(--danger); }', self.css)

    def test_greeting_boot_tail_local_first_contract(self):
        # AUTOLOGIN-LOCAL-FIRST-1①：boot 本地优先尾句（恢复期课量不缺席）
        greeting = self.modules["greeting.js"]
        # 端点闭集键拉取 + 同源键映射（未知/无数据一律退基础问候，绝不自造文案）
        self.assertIn('fetch("/api/v3/greeting-context"', greeting)
        self.assertIn("export function tailTextForKey", greeting)
        for tail_text in ("今日无课。", "今日课毕。", "今日课毕，辛苦了。", "今日课满。", "今日课满，已过半。"):
            self.assertIn(tail_text, greeting)
        # 快照缺位才生效；深夜档不叠加；真实快照到场后本地尾句退役
        self.assertIn("todayMeetings === null && !band.deepNight", greeting)
        self.assertIn("bootTailText = null;", greeting)
        # AUTOLOGIN-LOCAL-FIRST-1②：恢复态文案弱化——偏好经既有 settings 快照
        # auto_connect 闭集读出（零新端点）；checking+自动登录开=常规主文案
        self.assertIn('fetch("/api/v3/settings"', greeting)
        self.assertIn("export function buttonTextFor(authState, { autoLoginResume = false } = {})", greeting)
        # FA1⑬c：checking 恒不占主按钮（RESUMING 通道保留但不再上主按钮）
        self.assertIn('autoLoginResume ? BUTTON_TEXT_READY : BUTTON_TEXT_LOGIN', greeting)
        self.assertIn('BUTTON_TEXT_RESUMING = "正在恢复会话…"', greeting)
        self.assertIn('fudan?.enabled === true && fudan?.status === "ready"', greeting)

    def test_session_restoring_frontend_split_contract(self):
        # AUTOLOGIN-LOCAL-FIRST-1③：恢复期闭集新码的前端分流合同
        api = self.modules["api.js"]
        study = self.modules["study.js"]
        # api.js 闭集映射：新码=瞬态中文提示（唯一文案来源，页面绝不自造）
        self.assertIn('fudan_session_restoring: "正在登录，请稍候…"', api)
        # study.js：动作失败 toast 分流——新码走 checking 非错误形态，其余既有错误
        self.assertIn("function toastActionError(error)", study)
        self.assertIn('String(error?.code || "") === "fudan_session_restoring"', study)
        self.assertIn('restoring ? "checking" : "error"', study)
        # 7 = 既有 6 处 + 资料中心删除失败分流（DATA-DELETE-REPAIR-1）
        self.assertEqual(study.count("toastActionError(error);"), 7, "全部动作失败 toast 走分流")

    def test_wp1_audit_cards_subtitle_safe_zone_and_dropdown_sync(self):
        """WP1-D2/D1 源码锚：字幕安全区加算语义不被同选择器后加载击穿；
        异步填充的下拉在程序化设值后显式同步自绘触发钮文案。"""
        pages = self.css_by_name["pages.css"]
        components = self.css_by_name["components.css"]
        # WP1-D2：两文件的 .player-subtitle-overlay bottom 必须同为
        # 「安全区+档位」加算（chrome visible=104px 档 / hidden=20px 档，
        # player-core 恒写 --sub-bottom，绝对替换会让兜底永不落安全区）。
        additive = "calc(var(--subtitle-overlay-safe, 104px) + var(--sub-bottom, 0%))"
        self.assertIn(additive, components)
        pages_block = pages.split(".player-subtitle-overlay {", 1)[1].split("}", 1)[0]
        self.assertIn(additive, pages_block)
        self.assertNotIn("var(--sub-bottom, var(--subtitle-overlay-safe", pages)
        # WP1-D1：触点程序化填充后补 syncDropdown（目录学期筛选/周课表学期），
        # 否则触发钮初始只有箭头没有当前值。SIMPLIFY-AUDIT-1 S1 后设置页学期
        # 镜像触点已随「课表」组退役，仅余两处。
        study = self.modules["study.js"]
        timetable = self.modules["timetable.js"]
        self.assertIn('import { syncDropdown } from "./dropdown.js";', study)
        self.assertIn("select.value = courseTermFilter;", study)
        self.assertIn("syncDropdown(select);", study)
        self.assertIn('import { syncDropdown } from "./dropdown.js";', timetable)
        self.assertIn("semester.value = selectedSemester;", timetable)
        self.assertIn("syncDropdown(semester);", timetable)
        self.assertNotIn("settingsSemester", timetable)


class _GreetingTailService:
    """greeting-context 端点窄桩：只含端点触达的成员，最小授权面。"""

    def __init__(self, auto_connect=None, store=None):
        self.auto_connect = auto_connect
        runtime = SimpleNamespace(store=store) if store is not None else None
        self.auth_catalog = SimpleNamespace(identity_scope=lambda: "greeting-scope")
        self.settings = SimpleNamespace(privacy_snapshot=lambda: {
            "auto_connect": dict(self.auto_connect) if self.auto_connect is not None else None,
        })
        self.timetable = SimpleNamespace(runtime=runtime)


class HttpDisconnectSanitizationTests(unittest.TestCase):
    """FRONTEND-SMOOTH-1 卫生④：连接中止族（WinError 10053/10054）单行脱敏，
    未知错误保留全栈。实弹钉：半途 RST 的原始套接字请求 → stderr 恰一行
    脱敏提示、无 Traceback；服务器对后续正常请求保持健康。"""

    def setUp(self):
        self.service = _GreetingTailService()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(self.service, FRONTEND),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _health_ok(self):
        with urlopen(f"{self.base}/api/health") as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read())

    def test_disconnect_mid_request_logs_single_sanitized_line(self):
        # 前置：服务器正常服务
        self.assertTrue(self._health_ok()["ok"])
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            client = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3)
            try:
                client.sendall(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\n")
                client.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                )
            finally:
                client.close()  # 带.SO_LINGER(1,0)的关闭=RST，模拟页面刷新瞬间的连接中止
            deadline = time.time() + 3.0
            while (
                "http: client connection ended mid-request" not in stderr_capture.getvalue()
                and time.time() < deadline
            ):
                time.sleep(0.05)
        captured = stderr_capture.getvalue()
        self.assertIn("http: client connection ended mid-request", captured, "连接中止族输出恰一行脱敏提示")
        self.assertNotIn("Traceback", captured, "连接中止族不打印全栈")
        # 后置：服务器对正常请求保持健康
        self.assertTrue(self._health_ok()["ok"])


class GreetingContextApiTests(unittest.TestCase):
    """AUTOLOGIN-LOCAL-FIRST-1①：GET /api/v3/greeting-context 行为钉。

    无会话 200 + 恰闭集键 {tail_key}；无保存账号/未启用自动登录=诚实无数据；
    本地缓存正反例。零学校网络、零个人数据。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="greeting-context-")
        self.store = TimetableStore(Path(self.tmp) / "timetable.db")
        self.auto_connect = {
            "enabled": True, "account_id": "saved-account", "status": "ready",
        }
        self.service = _GreetingTailService(auto_connect=self.auto_connect, store=self.store)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(self.service, FRONTEND),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.closed_keys = {"none", "done", "done-long", "full", "full-half", "no-data"}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _get(self):
        try:
            with urlopen(f"{self.base}/api/v3/greeting-context") as response:
                self.assertEqual(response.status, 200)
                return json.loads(response.read())
        except HTTPError as exc:
            self.fail(f"greeting-context returned HTTP {exc.code}")

    def _put_semester(self, *, start_offset_days, weekdays, courses=1, observed_at=None):
        today = date.today()
        anchor = today + timedelta(days=start_offset_days)
        start = anchor - timedelta(days=anchor.isoweekday() - 1)
        payload = {
            "observed_at": observed_at if observed_at is not None else time.time(),
            "semesters": [{"semester_id": "2026-1", "label": "2026-1", "is_default": True,
                           "start_date": start.isoformat()}],
            "semester": {"semester_id": "2026-1", "label": "2026-1",
                         "start_date": start.isoformat()},
            "courses": [
                {
                    "timetable_course_id": f"course-{index}",
                    "semester_id": "2026-1",
                    "semester_start_date": start.isoformat(),
                    "week_indexes": [1],
                    "meetings": [
                        {"weekday": weekday, "start_unit": 1, "end_unit": 2}
                        for weekday in weekdays
                    ],
                }
                for index in range(courses)
            ],
        }
        self.store.put("greeting-scope", "synthetic", "2026-1", payload)

    def test_no_session_returns_closed_tail_key_only(self):
        # 无会话/无身份快照要求：端点 200，响应恰一个闭集键，零信封零个人数据
        payload = self._get()
        self.assertEqual(set(payload.keys()), {"tail_key"})
        self.assertIn(payload["tail_key"], self.closed_keys)
        self.assertNotIn("schema", payload)

    def test_without_saved_auto_login_account_is_honest_no_data(self):
        # 无保存账号/未启用自动登录：原硬门原样，诚实无数据键
        for variant in (
            None,  # 快照缺 auto_connect
            {"enabled": False, "account_id": "saved-account", "status": "off"},
            {"enabled": True, "account_id": "", "status": "account_missing"},
        ):
            self.service.auto_connect = variant
            self.assertEqual(self._get()["tail_key"], "no-data", f"variant={variant}")

    def test_with_saved_account_but_empty_cache_is_no_data(self):
        self.assertEqual(self._get()["tail_key"], "no-data")
        # rotation_required 仍是本机已保存账号（密码待更新）：零敏感尾句键同属可信读据
        self.service.auto_connect = {"enabled": True, "account_id": "saved-account", "status": "rotation_required"}
        self.assertEqual(self._get()["tail_key"], "no-data")

    def test_with_local_cache_returns_closed_non_none_key_for_loaded_day(self):
        # 本地正例：本周内含今日课量 → 闭集键且绝不误报「今日无课」
        today = date.today()
        self._put_semester(start_offset_days=0, weekdays=[today.isoweekday()], courses=1)
        self.assertNotEqual(self._get()["tail_key"], "none")

    def test_with_local_cache_and_free_today_reports_none(self):
        # 本地正例：缓存确知今日无课（课程只在未来周）→ none（深夜档除外）
        self._put_semester(start_offset_days=7, weekdays=[1], courses=1)
        self.assertIn(self._get()["tail_key"], {"none", "no-data"})

    def test_stale_cache_is_no_data(self):
        # 缓存超新鲜度（31 天前 > STALE_SECONDS）：诚实无数据（与 snapshot 同语义）
        today = date.today()
        self._put_semester(
            start_offset_days=0, weekdays=[today.isoweekday()], courses=1,
            observed_at=time.time() - 31 * 24 * 3600,
        )
        self.assertEqual(self._get()["tail_key"], "no-data")


class GreetingTailKeyPhaseTests(unittest.TestCase):
    """AUTOLOGIN-LOCAL-FIRST-1①：闭集键相位判定矩阵（纯函数，零时钟依赖）。"""

    def test_phase_matrix(self):
        self.assertEqual(_greeting_tail_key_from_ends(["18:00"], 120), "no-data", "深夜档不叠加")
        self.assertEqual(_greeting_tail_key_from_ends(["18:00"], 299), "no-data", "深夜档上界")
        self.assertEqual(_greeting_tail_key_from_ends([], 600), "none", "缓存确知今日无课")
        self.assertEqual(_greeting_tail_key_from_ends(["bad"], 600), "no-data", "钟点全坏诚实留白")
        self.assertEqual(_greeting_tail_key_from_ends(["08:00", "09:00"], 600), "done", "1–4 节已毕")
        self.assertEqual(_greeting_tail_key_from_ends(["11:15"], 600), "no-data", "1–4 节未开始留白")
        self.assertEqual(_greeting_tail_key_from_ends(["10:30", "11:15"], 600), "no-data", "1–4 节进行中留白")
        self.assertEqual(
            _greeting_tail_key_from_ends(["09:15", "10:15", "11:15", "12:15", "13:15"], 600),
            "full", "≥5 节未过半",
        )
        self.assertEqual(
            _greeting_tail_key_from_ends(["07:45", "08:45", "09:45", "10:45", "11:45"], 600),
            "full-half", "≥5 节已过半",
        )
        self.assertEqual(
            _greeting_tail_key_from_ends(["05:45", "06:45", "07:45", "08:45", "09:45"], 600),
            "done-long", "≥5 节已毕",
        )


class _GateStateService:
    """三态门窄桩：会话态 + auto_connect 可调；读路由 handler 的最小本地成员。"""

    def __init__(self, auth_state="action_required", auto_connect=None):
        self.auth_state = auth_state
        self.auto_connect = auto_connect
        self.auth_catalog = SimpleNamespace(
            authentication_snapshot=lambda: {
                "state": self.auth_state, "source": "local", "observed_at": 1.0,
                "expires_at": 0.0, "code": "synthetic", "actions": ["login"],
            },
            identity_scope=lambda: "gate-scope",
        )
        self.settings = SimpleNamespace(privacy_snapshot=lambda: {
            "auto_connect": dict(self.auto_connect) if self.auto_connect is not None else None,
        })
        self.learning = SimpleNamespace(list_review_plans=lambda: [])
        self.tasks = SimpleNamespace(repository=SimpleNamespace(
            get_app_state=lambda key, default=None: default,
        ))


class CourseSessionGateApiTests(unittest.TestCase):
    """AUTOLOGIN-LOCAL-FIRST-1③：读族/动作族三态门行为钉。

    restoring-trusted（checking + 已保存账号 + 自动登录启用）下读族 200 本地
    数据、动作族 401 闭集新码 fudan_session_restoring；无账号 checking/
    rotation_required/degraded 仍现状 401 fudan_login_required；ready 态零变化。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="course-gate-")
        self.service = _GateStateService()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(self.service, FRONTEND),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _get(self, route, expected=200):
        try:
            with urlopen(f"{self.base}{route}") as response:
                self.assertEqual(response.status, expected)
                return json.loads(response.read())
        except HTTPError as exc:
            self.assertEqual(exc.code, expected)
            return json.loads(exc.read())

    def _post(self, route, body, expected):
        request = Request(
            f"{self.base}{route}", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urlopen(request) as response:
                self.assertEqual(response.status, expected)
                return json.loads(response.read())
        except HTTPError as exc:
            self.assertEqual(exc.code, expected)
            return json.loads(exc.read())

    def _set_restoring_trusted(self):
        self.service.auth_state = "checking"
        self.service.auto_connect = {"enabled": True, "account_id": "a", "status": "ready"}

    def test_restoring_trusted_read_routes_serve_local_cache(self):
        self._set_restoring_trusted()
        # 静态闭集路由 + 本地存储读路由均 200（无会话也出本地数据）
        references = self._get("/api/v3/references")
        self.assertIn("supported_sources", json.dumps(references))
        plans = self._get("/api/v3/review-plans")
        self.assertEqual(plans["data"]["plans"], [])
        schedule = self._get("/api/v3/schedules")
        self.assertIn("schedule", json.dumps(schedule))

    def test_restoring_trusted_action_routes_reject_with_new_code(self):
        self._set_restoring_trusted()
        payload = self._post("/api/v3/review-plans/actions", {"action": "x"}, expected=401)
        self.assertEqual(payload["error_code"], "fudan_session_restoring")
        self.assertEqual(payload["actions"], ["login"])

    def test_checking_without_saved_account_keeps_current_rejection(self):
        self.service.auth_state = "checking"
        self.service.auto_connect = None
        payload = self._get("/api/v3/review-plans", expected=401)
        self.assertEqual(payload["error_code"], "fudan_login_required")
        action = self._post("/api/v3/review-plans/actions", {"action": "x"}, expected=401)
        self.assertEqual(action["error_code"], "fudan_login_required")

    def test_degraded_keeps_current_rejection(self):
        # 恢复已确认失败（degraded）：绝不报「正在登录」新码，既有文案链原样
        self._set_restoring_trusted()
        self.service.auth_state = "degraded"
        payload = self._get("/api/v3/review-plans", expected=401)
        self.assertEqual(payload["error_code"], "fudan_login_required")
        action = self._post("/api/v3/review-plans/actions", {"action": "x"}, expected=401)
        self.assertEqual(action["error_code"], "fudan_login_required")

    def test_rotation_required_checking_keeps_current_rejection(self):
        # 已保存密码待旋转：自动登录无法恢复会话，不构成「正在登录」语境
        self.service.auth_state = "checking"
        self.service.auto_connect = {"enabled": True, "account_id": "a", "status": "rotation_required"}
        payload = self._get("/api/v3/review-plans", expected=401)
        self.assertEqual(payload["error_code"], "fudan_login_required")

    def test_ready_state_read_and_action_zero_change(self):
        self.service.auth_state = "ready"
        plans = self._get("/api/v3/review-plans")
        self.assertEqual(plans["data"]["plans"], [])
        schedule = self._get("/api/v3/schedules")
        self.assertIn("schedule", json.dumps(schedule))
        # ready 下动作族照常进入派发（该路由门后无 handler → 404，证明门未拒绝）
        action = self._post("/api/v3/review-plans/actions", {}, expected=404)
        self.assertNotEqual(action.get("error_code"), "fudan_session_restoring")


class AS5PrivacyInsightPins(unittest.TestCase):
    """AS5 隐私三件套+F1（第卅八案）：热点默认开/显式 off 被尊重、抹除移课次
    两击臂只抹本讲、《隐私与数据说明》入仓+内置入口逐字恒等、任务占位行沉默、
    散落隐私说明收敛为一句要点+查看入口。"""

    @classmethod
    def setUpClass(cls):
        cls.html = (FRONTEND / "index.html").read_text(encoding="utf-8")
        # ARCH-DEBT-1：被拆模块的值=家族并集（门面+子目录模块），内容钉检索
        # 家族而非门面单文件；键集仍为顶层文件名（布局闭集钉不受影响）。
        cls.modules = {
            path.name: family_text(path.stem)
            for path in sorted((FRONTEND / "modules").glob("*.js"))
        }
        cls.player = cls.modules["player-core.js"]
        cls.settings = cls.modules["settings.js"]
        cls.notice = (ROOT / "docs" / "privacy-notice.md").read_text(encoding="utf-8")

    def test_insight_default_on_with_explicit_off_respected(self):
        """热点默认开启（2026-09-23 拍板反转原「默认关」隐私钉）；唯一保留的
        关闭通道=曾显式写入的本地偏好 "off"（零采集零请求），设置项 UI 整体移除。"""
        self.assertIn('localStorage.getItem(INSIGHT_SWITCH_KEY) !== "off"', self.player)
        self.assertNotIn('id="insight-switch"', self.html)
        self.assertNotIn("在进度条上标出我的回看热点", self.html)
        self.assertIn("回看热点默认记录在这台电脑上", self.html)

    def test_erase_is_two_step_and_scoped_to_current_lecture(self):
        """抹除移出洞察对话、藏进每个课次操作区：两击臂（同 tasks-drawer
        armTwoStepButton 语义）+ 只抹当前讲次（sub_id 域）+ 人话确认/结果文案。"""
        self.assertIn(">抹掉本讲热点</button>", self.html)
        self.assertNotIn("抹掉我的热点记录", self.html)
        self.assertIn('postV3("watch-events/clear", { sub_id: subId })', self.player)
        self.assertIn("再点一次，抹掉本讲热点", self.player)
        self.assertIn("已抹掉本讲回看热点；字幕、课件和学习记录都不受影响", self.player)
        self.assertIn("resetInsightEraseArm()", self.player)
        self.assertIn('insightErase.hidden = playbackKind !== "lecture" || !insightEnabled();', self.player)

    def test_task_placeholder_row_stays_silent(self):
        """F1：占位行不常驻——空消息隐藏任务行（保持沉默），技术腔占位
        文案「后端尚未返回任务状态」从加载链消失。"""
        self.assertNotIn("后端尚未返回任务状态", self.player)
        self.assertIn("taskStateRow.hidden = true;", self.player)
        self.assertIn("taskStateRow.hidden = false;", self.player)
        self.assertIn('id="player-task-state" class="status-row" data-state="unknown" hidden', self.html)

    def test_privacy_notice_doc_is_embedded_verbatim(self):
        """《隐私与数据说明》入仓 docs/privacy-notice.md，客户端入口渲染同文：
        settings.js 常量与仓内文档逐字恒等（改文书两处同笔，防漂移）。"""
        embedded = re.search(r"const PRIVACY_NOTICE_MARKDOWN = `([\s\S]*?)`;", self.settings)
        self.assertIsNotNone(embedded, "PRIVACY_NOTICE_MARKDOWN 常量在场")
        self.assertEqual(embedded.group(1), self.notice)
        for required in ("privacy-doc-dialog", "privacy-doc-open", "privacy-doc-body"):
            self.assertIn(f'id="{required}"', self.html)
        self.assertIn("renderPrivacyDoc(body)", self.settings)

    def test_privacy_notice_states_code_facts(self):
        """文书口径=现行代码事实（不夸大）：外联闭集四服务（network.py
        SERVICE_PROBES）、DPAPI、GitHub App 仅两受管仓库、零遥测、云端最长
        30 天/状态记录 90 天=现行产品文案口径。"""
        for fact in (
            "CourseLens 没有遥测",
            "icourse.fudan.edu.cn",
            "webvpn.fudan.edu.cn",
            "api.github.com",
            "api.deepseek.com",
            "Windows 数据保护接口（DPAPI）",
            "仅两个受管仓库",
            "云端最长保留 30 天",
            "版本：v1.2",
            "2026-10-02 与 README「安全与隐私架构」同口径更新",
        ):
            self.assertIn(fact, self.notice)

    def test_scattered_privacy_lines_converge_to_notice(self):
        """设置页隐私节/帮助节/首跑引导的散落隐私说明收敛为一句要点+
        「查看《隐私与数据说明》」入口。"""
        self.assertIn("查看《隐私与数据说明》", self.html)
        self.assertNotIn("不经任何第三方服务器中转（详见 README「安全与隐私架构」）", self.html)
        self.assertIn("设置页「隐私」节的《隐私与数据说明》", self.html)
        self.assertIn("都写在《隐私与数据说明》里（设置页「隐私」节可查看）", self.html)


if __name__ == "__main__":
    unittest.main()
