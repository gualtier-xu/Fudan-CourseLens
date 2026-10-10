# CourseLens 首次使用分步引导交接合同

Last updated: 2026-09-07

## 0. 目标与授权

在 `private/main` 为首次使用者实现一个可跳过、可重新打开、自动读取真实后端状态的分步页面。它必须帮助用户完成最短学习路径，但不得强迫用户配置所有功能，也不得把“看完教程”伪装成“服务已经可用”。

本合同授权修改产品代码、对应测试和事实文档，但不授权真实登录、真实课程访问、真实课表刷新、远程任务、GitHub 授权/写入、Secrets 变更、发布、签名、取消、删除、Git commit/push/PR 或人工读写 `runtime/data`。

## 1. 开工与基线保护

完整读取：

- 工作区根 `AGENTS.md`；
- `docs/handoff.md`；
- `docs/ui-design-system.md`；
- `docs/student-onboarding.md`；
- 本合同；
- `frontend/app.js`、`frontend/index.html`、`frontend/modules/{shell,study,settings,ui,store}.js`；
- `src/application.py` 中 `onboarding_snapshot()`、`app_shell_snapshot()`、`tutorials_snapshot()`；
- `src/runtime/http_api.py` 中 `onboarding`、`app-shell`、认证和设置路由；
- 相关前端行为测试、API 测试和合成服务器。

先记录 fresh `git status --short --branch`、`git diff --stat`、HEAD/origin 和拟触碰文件的现有 diff。当前工作树包含大量用户未提交成果，必须逐文件保留，不得 reset、clean、stash、restore、checkout、覆盖或顺手整理其他批次。

## 2. 核心产品决定

### 2.1 页面，不是强制浮层

- 新手引导是现有无路由 SPA 中的普通 `data-page="onboarding"` 页面，保留顶栏；
- 不使用全屏 modal、coach mark、自动轮播或遮挡式热点；
- 首次安装只自动进入一次；用户可随时跳过；
- 已登录账户菜单、“设置 → 更新与帮助”以及未登录学习空态都提供“新手引导”入口；未登录时账户按钮仍沿用直接打开登录 dialog 的现有行为，不为教程改变；
- 手动打开时记录来源页面，退出后返回该页面；首次自动打开的返回目标为“学习”；
- 不增加新的一级导航、URL/hash/history 路由或底部导航。

### 2.2 教程状态与产品状态必须分离

教程记录只保存：

```text
version: student-onboarding.v1
disposition: new | dismissed | completed
auto_opened: boolean
updated_at: number
```

- `dismissed` 仅表示不再自动弹出当前版本；
- `completed` 仅表示用户走完引导；
- `auto_opened=true` 表示当前版本已经自动展示过，即使用户随后直接关闭程序，也不会在每次启动反复抢占页面；
- 两者均不得改变认证、课程目录、远程连接、同意设置或 API Key 状态；
- 实际 readiness 永远从后端证据重新计算；
- 后续若升级 guide version，旧版本记录不得自动冒充新版本完成；新版本是否重新展示由新的 version 常量明确决定；
- 恢复出厂清空应用状态后，该记录自然消失，下一次启动重新显示引导；
- 禁止用 `localStorage` 保存教程状态。现有约束仍是 `localStorage` 只保存主题。

### 2.3 复用已有真实状态聚合

`Application.tutorials_snapshot()` 已汇总 runtime、GitHub App、GitHub Device、Worker/Mailbox、复旦登录、课程目录、课表、DeepSeek、调度和 analytics；`app_shell_snapshot()` 已暴露 `tutorials`、authentication、catalog、remote、settings 等证据。

优先扩展这条现有链路，不新建重复的 readiness aggregator，不从 DOM 文案反推状态，不用前端猜测“已经连接”。允许的最小后端扩展：

1. `tutorials_snapshot()` 增加上述 `guide` 记录；
2. 新增本地、幂等的 `POST /api/v3/onboarding/actions`，只接受 `mark-opened`、`dismiss` 与 `complete`；
3. 将记录写入既有 TaskStore app state，例如 `student_onboarding_guide`；
4. action/version 非闭集时返回稳定错误码；
5. 不要求 `operation_id`，因为这是单机幂等偏好写入；不得借机改认证、远程或凭据协议。

若代码审查发现已有等价持久化接口，必须复用并在报告中说明，不得并存两套状态。

最小稳定 API 合同：

```json
POST /api/v3/onboarding/actions
{"action":"mark-opened|dismiss|complete","version":"student-onboarding.v1"}
```

成功返回 HTTP 200 的 v3 envelope，payload 为：

```json
{
  "guide": {
    "schema": "courselens.onboarding-guide.v1",
    "version": "student-onboarding.v1",
    "disposition": "new|dismissed|completed",
    "auto_opened": true,
    "persistence": "ready",
    "source": "stored|default|legacy_existing_user",
    "updated_at": 0
  }
}
```

- 客户端必须发送当前常量 version，后端不接受任意版本；版本不匹配返回 HTTP 409 + `onboarding_version_conflict`，同时给出当前 guide snapshot；
- action/schema 无效返回 HTTP 400 + `onboarding_action_invalid`；
- `mark-opened` 只把当前版本的 `auto_opened` 设为 true，disposition 仍为 `new`；
- `dismiss` 写 `dismissed + auto_opened=true`；`complete` 写 `completed + auto_opened=true`；重复调用结果相同；
- GET 时若 app-state 缺失且存在已保存账号、已确认课程目录或历史任务等明确旧用户证据，规范化为 `dismissed + auto_opened=true + source=legacy_existing_user`，不得在版本上线后突然打断既有用户；完全空白状态规范化为 `new + auto_opened=false + source=default`；
- app-state 类型、字段或 disposition 损坏时返回 `persistence=invalid`，禁止自动打开，但手动入口仍可用；不得把损坏记录猜成 completed，也不得静默覆盖；
- 自动打开条件必须同时满足：当前版本、`disposition=new`、`auto_opened=false`、`persistence=ready`。客户端先成功 POST `mark-opened`，再切换页面；写入失败则不强制抢占页面，保留手动入口并诚实报告状态。

## 3. 信息架构与文案

总计五步。桌面端用窄步骤目录 + 约 680–760px 阅读列；移动端只显示“第 N 步，共 5 步”和当前标题，不做横向滚动步骤条。

### 步骤 1：先了解工作方式

目的：让用户知道哪些是本机状态，哪些动作会连接学校或远程 Worker。

必须说明：

- 播放、课程目录、字幕/笔记任务各自依赖的连接不同；
- 用户明确点击登录、刷新或生成前，不自动发起对应真实动作；
- 学号、密码和 API Key 只在已有受保护表单中输入；
- 是否允许加密云端处理以真实 consent 状态显示，不在引导中默认勾选或代用户同意。

本步只有“继续”和全局“跳过引导”，不堆叠长篇隐私政策。

### 步骤 2：连接复旦课程平台

数据源：`authentication`/`store.auth`，使用既有闭集 state/code/step/attempt。

状态和动作：

- ready：显示“复旦课程平台已连接”，可继续；
- checking：显示既有阶段反馈，不可重复提交；
- action_required/degraded/error：显示现有闭集恢复文案；
- unknown/请求失败：显示“暂时无法确认”，提供重试状态读取；
- 主操作调用既有 `courselens:open-login`，复用现有登录 dialog、密码显隐、保存语义和轮询；绝不在引导中复制账号密码字段。

状态从未就绪变为 ready 时只更新页面和 `aria-live="polite"`，不得自动跳步或抢走焦点。

### 步骤 3：确认课程目录

数据源：`app-shell.catalog` 及既有课程 store。必须以 catalog 的 `state/code/course_count` 判断，不能用 `courses.length === 0` 直接认定失败，因为零课程可能是合法结果。

- ready 且有课程：只显示课程数量，不显示课程名、教师、URL 或真实内容；
- ready 且为零：诚实显示“当前账号暂未发现可访问课程”；
- stale/checking/degraded：复用课程目录闭集解释与下一步；
- 刷新必须是用户点击既有 refresh-catalog 动作后才发生，不自动访问学校来源；
- 本步不要求用户立即选择具体课程。最终完成引导后再进入现有课程选择页。

### 步骤 4：准备生成能力

这是能力检查，不是强制配置。用三行状态清楚区分：

1. 加密云端处理 consent；
2. GitHub/Worker/Mailbox 远程连接；
3. DeepSeek Key。

规则：

- 远程连接只读取 `app-shell.remote`/`tutorials.evidence`，不得因打开引导自动开始 Device Flow、bootstrap、repair 或 channel test；
- 需要处理时提供“打开远程连接设置”，跳到既有设置分组；
- DeepSeek Key 是可选增强。没有 Key 时仍可按现有产品语义生成高质量 ASR 字幕，并标注“无大模型验证”；摘要、证据回答等依赖关系必须先按当前代码核实再写文案；
- 不在引导中新增 API Key 输入框，使用既有设置表单；
- 未配置项目要显示“可稍后设置”，不得用失败红色或阻止用户完成引导。

### 步骤 5：开始学习

用后端证据生成简短总结：

- “基础学习已可用”：复旦会话和课程目录已就绪；
- “还需连接复旦账号”或“课程目录仍需处理”：核心条件未满足；
- “字幕与笔记生成可用/尚需准备远程连接”；
- “DeepSeek 已配置/字幕将使用无大模型验证模式”。

主按钮统一叫“完成引导”。点击后先写 `completed`：

- catalog ready 时进入现有课程/讲次选择态；
- 否则返回学习空态，由现有登录/恢复入口继续；
- 即使可选项未配置，也允许完成；不得显示“所有设置已完成”。
- `complete` 写入失败时留在本页，保留用户当前步骤与后端状态，行内给出“重试”；不得在失败后跳转或在前端假装 completed。

## 4. 导航、跳过与重新打开

- 页面顶部显示标题“开始使用 CourseLens”、进度文字和“跳过引导”；
- 底部提供“上一步”“下一步”；第一步隐藏/禁用上一步，第五步主操作为“完成引导”；
- 允许点击步骤目录查看任一步，但不把未完成步骤锁住；
- `Esc` 不直接跳过或完成普通页面；
- 跳过时 POST `dismiss`，成功后回来源页；
- 如果 dismiss 写入失败，先留在引导页并行内说明“引导状态未能保存，下次启动仍会显示”，同时提供“重试保存”和“仅本次退出”；只有用户再次选择“仅本次退出”才回来源页，不得只 toast；
- 手动重新打开不会先把 `dismissed/completed` 改回 `new`；只有再次点击完成才写 `completed`；
- 已完成后重开默认落在步骤 5；已跳过后重开默认落在第一个未就绪的核心步骤；全新状态从步骤 1 开始；
- 进入设置完成配置后，用户通过设置页中明确的“返回新手引导”返回。该按钮只在从引导进入设置时显示，不改变设置页正常返回逻辑。

## 5. 状态刷新与生命周期

- 打开引导时执行一次 `GET /api/v3/app-shell`；
- 订阅既有 `store.auth` 与课程 store 更新；
- 引导可见且窗口重新获得焦点时允许一次有界刷新，用于读取外部 GitHub 授权后的状态；
- 不新增 interval、SSE 或后台自动外联；
- 使用单个 AbortController/sequence 隔离迟到响应；关闭页面或 `pagehide` 时清理 listener 和在途请求；
- 后端读取失败时保留上一份已确认状态并标记 stale/unknown，不把失败渲染成“未配置”；
- 所有状态变化必须有文字和形状/图标，颜色不能单独承载语义。

## 6. 视觉与无障碍

严格沿用当前 Academic Editorial 设计系统：象牙 canvas、白色阅读面、海军蓝、金色强调、Times New Roman + SimSun；无绿色、无渐变、无在线字体、无新依赖、无新的卡片海洋。

- 页面像一本简短的“开始使用”章节：清晰标题、金色细线、安静步骤目录；
- 当前步骤用 `aria-current="step"`、navy-soft 底与金线共同表示；
- 状态采用已有 navy 实心/金环/红叹号/灰点语义并配文字；
- 正文不少于 16px/1.5，辅助文字不少于现有 13.5px；
- 所有控件为原生 button/link/input，移动端和 coarse pointer 触达至少 44×44px；
- 标题层级连续；步骤变化后聚焦当前步骤 `h2[tabindex=-1]`；
- 后端错误使用 `role="alert"`，普通状态更新使用 polite live region；
- 375/720/1000/1440、200% zoom、亮暗主题均无水平溢出或固定元素遮挡焦点；
- `prefers-reduced-motion` 下无非必要过渡；不得用 emoji，图标复用现有 vendored SVG 风格。

`ui-ux-pro-max` 的通用检索曾返回 Minimal Swiss、青绿色与在线 Lora/Raleway，这与项目已批准的 Academic Editorial、无绿色、系统字体和零外部依赖约束冲突，因此只采纳其中的用户自由、进度可见、错误播报、44px 触达和焦点可见原则，不改变既有视觉方向。

## 7. 建议最小实现范围

先验证调用链，再锁定 allowlist。预计：

- `frontend/index.html`；
- 新增 `frontend/modules/onboarding.js`；
- `frontend/app.js`；
- `frontend/modules/shell.js`（账户菜单入口/来源返回事件，若确有需要）；
- `frontend/modules/settings.js`（仅引导来源返回入口，若确有需要）；
- `frontend/styles/pages.css`、必要时 `components.css`/`accessibility.css`；
- `src/application.py`；
- `src/runtime/http_api.py`；
- `tests/test_frontend_workbench.py`；
- 新增一个最小的执行式前端行为 mjs 及 pytest wrapper，或扩展现有最匹配 harness；
- 对应 API/状态测试；
- `tests/synthetic_shell_server.py`（仅合成状态，不得真实外联）；
- `docs/student-onboarding.md`、`docs/ui-design-system.md`。

不需要就不要修改；不得扩入 Worker、协议、任务调度、认证实现、凭据格式、remote coordinator、`runtime/**`、`public/**` 或 `managed/**`。

## 8. 行为测试矩阵

至少执行式覆盖：

1. `guide=new`：首次 bootstrap 自动进入引导且只进入一次；
2. 旧安装无 guide 记录但已有账号/目录/任务证据：不得自动打开，手动入口仍可用；损坏记录同样不得自动抢占；
3. dismiss 成功：退出、刷新后不自动打开；已登录账户菜单、设置帮助和未登录学习空态均可重开；
4. dismiss 失败：先留在页面显示行内错误；“仅本次退出”可离开，刷新后仍会出现；
5. complete：记录完成，缺少可选 DeepSeek/remote 时仍可完成，但状态不被伪造成 ready；写入失败不得跳转；
6. completed 后真实状态退化：重开仍显示当前 degraded/action_required，而不是沿用“已完成”；
7. 认证按钮复用既有登录 dialog；双击不重复提交；登录成功只更新状态、不自动跳步；
8. catalog ready-zero、ready-nonzero、checking、stale、degraded、unknown 分支；
9. remote/consent/DeepSeek 只读展示，打开页面不产生远端动作 POST，不启动 GitHub 授权；
10. 从 study/settings 打开与退出均回到正确来源；从引导进入 settings 后可返回引导；
11. 重开默认步骤规则；步骤目录、上一步、下一步、Tab、Shift+Tab 和焦点移动；
12. 请求迟到/Abort/快速切页不覆盖新状态；所有 listener/timer/request 清理；
13. API 覆盖 mark-opened/dismiss/complete 幂等、无效 action、版本冲突、损坏记录和 legacy_existing_user 规范化；
14. `localStorage` 仍只有 theme 写入；密码、Key、账号和课程内容不进入 guide DOM、日志或 fixture；
15. 恢复出厂后的空 app state 会重新得到 `guide=new + auto_opened=false`。

## 9. 合成浏览器验收

使用现有 loopback synthetic shell，所有 auth/catalog/remote/AI 状态必须是合成数据，禁止真实登录和外联。至少截图并用 DOM 断言绑定：

- 1440 亮色：步骤 1、步骤 2 已连接、步骤 4 部分未配置、步骤 5 总结；
- 1000 亮色：步骤目录 + 阅读列无裁切；
- 375 亮色：步骤 2 与步骤 5；
- 1440 深色：步骤 4；
- 720 viewport-equivalent 的 200% 场景；
- dismiss 失败行内恢复态；
- 设置页“返回新手引导”临时入口。

逐张确认：零水平溢出、触达 ≥44px、焦点可见、当前步骤不只靠颜色、状态文字与后端 fixture 一致、控制台零 JS 异常。截图和矩阵只放 gitignored `runtime/cache/student-onboarding-guide-20260907/`，不得包含真实账号、课程或网络证据。

浏览器协议：

- 复用仓库此前 Academic UI 验收使用的 Playwright CLI/现有浏览器工具；先确认工具已安装，不得为截图下载浏览器、增加 npm 依赖或改系统；
- 用 `tests/synthetic_shell_server.py` 在 loopback 临时端口启动，记录 PID、命令、origin 与 fixture 模式；浏览器 profile、trace 和截图仅放上述 gitignored 目录；
- 每个场景先断言当前页面、guide state、可见文字、后端请求计数和几何，再截图；不得用截图代替行为断言；
- 使用新的隔离浏览器上下文，不复用真实登录 session、Cookie 或用户 profile；严禁请求拦截后转发真实端点；
- 收尾只终止本轮记录的精确 PID，禁止 broad `taskkill`；
- 若既有浏览器工具不可用，离线 DOM/API 测试仍继续，但视觉验收必须明确标为 `BLOCKED_UNVERIFIED`，不得安装替代工具或声称通过，并交由用户决定是否补跑。

## 10. 验证与审查

按风险从小到大：

1. `node --check` 所有改动模块；
2. 新/现有 mjs 行为 harness；
3. 定向 API、workbench、identity/settings、shell security 测试；
4. 合成浏览器矩阵；
5. client 全量 pytest；
6. 若 Worker 未改，只跑既有最小 smoke 或明确说明未跑，不为数字扩范围；
7. `scripts/check_markdown_links.py`、`scripts/check_text_encoding.py`、`git diff --check`；
8. reviewer 审查当前增量的状态真实性、首次启动判定、跳过持久化、生命周期、可访问性和脏基线保护。

任何涉及真实账号、凭据值、真实 GitHub 授权或远端状态变更的测试都必须另获用户 fresh GO，并先过 `risk_auditor`。本合同预期无需真实测试。

## 11. 完成标准

- 首次安装自动打开一次，跳过后不再自动打开；
- 账户菜单和设置帮助都能重新打开；
- 五步内容、返回路径与手动导航完整；
- 教程 disposition 与服务 readiness 明确分离；
- 所有状态来自既有后端证据，失败不被降格为“未配置”；
- 引导不收集秘密、不自动登录、不自动刷新真实来源、不自动操作 GitHub；
- 现有登录、课程选择、设置、单浮层、任务和搜索合同无回归；
- 合成矩阵和离线测试通过；
- 未执行任何 Git 写操作、发布、远端或真实数据操作；只读 Git preflight 与 diff 检查已完成。

## 12. 最终报告

报告：根因与设计决定、实际文件、后端状态来源、教程持久化语义、五步逐项验收、测试命令与结果、截图清单、reviewer 结论、未验证项、fresh git status 与边界声明。不得只报截图或字符串断言，必须包含真实 DOM 行为测试证据。
