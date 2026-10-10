# UI 可用性与认证后续交接

## 目标与边界

本文件是一次受限后续修复的唯一执行合同。目标仅有五项：任务抽屉关闭/信息层级；CourseLens 字标返回课程与讲次主入口；字幕阅读区视觉与交互；复旦登录慢/失败的端到端根因诊断和最小修复；密码显示/隐藏。不得把它扩展为重设计、路由改造、认证重写或功能清单。

先完整阅读工作区根 `AGENTS.md`、`docs/handoff.md`、现有 Academic migration/stabilization 文档及相关调用链。每次开始 fresh preflight：`git status --short`、`git diff --stat`、目标路径 diff/hash、当前调用者/测试。所有脏改动是用户基线；绝不 `reset`、`stash`、`restore`、`checkout`、覆盖或清理。

主代理决定单一问题、范围和风险；同一仓库只由一个 bounded worker 写入并运行 focused self-test；完成后由独立 reviewer 验收。真实认证前，risk_auditor 必须只读审查精确 journey、状态变更、秘密处理、成本/破坏性与停止条件；它不授权门禁外操作。

禁止读/写 `runtime/data`，禁止修改 `public/worker-mirror`、`managed/` 状态，禁止 Git commit/push/PR、发布、签名、Secrets、真实取消、删除或其他破坏性动作。不得在文件、命令、日志、截图、DOM 导出、报告或环境转储中出现账号、密码、API key、Cookie、真实课程内容或 URL。

## 只读追踪与 allowlist

先追踪五项的实际入口、共享 helper、所有调用者和测试，再选最小 allowlist。默认候选仅为 `frontend/index.html`、`frontend/modules/tasks-drawer.js`、`shell.js`、`study.js`、`player-core.js`、`settings.js`、`ui.js`、必要 styles 和相关 tests。任何文件不必需就不改。

只有证据证明登录根因在 `src/` 时，才可扩入**精确单一路径**，并记录症状、复现、调用链、为何前端不足和对应验证。不得以“也许更快”扩大范围。

## 1. 任务抽屉

- 统一关闭路径：关闭按钮 ×、遮罩、`Esc` 都能关闭；没有叠层或死浮层。
- 关闭后恢复 `hidden`、背景 `inert`、`aria-expanded="false"` 与触发器焦点；打开时为 `true`，焦点进入抽屉。
- 资源所有权明确：关闭只清理**本次打开创建**的 EventSource 与抽屉级资源；全局 timer/触发器监听只由模块 disposer 清理。验收打开→关闭→重开无重复订阅，且任务 chip 继续更新。
- 按课程紧凑分组；无 `course_id`/无法精确映射者进入“未关联课程”，绝不按标题猜测。
- 技术详情默认折叠。将 raw `task_failed` 转为闭集、可行动的中文用户提示；技术码只在详情中显示。

## 2. 字标、学习层级与设置返回

CourseLens 字标稳定返回课程与讲次选择态：不登出、不丢当前选择。桌面在学习桌保留清晰、可键盘到达的返回路径；设置完成后恢复到进入前的学习态。不得引入路由框架或破坏浏览器返回的可预测性。

## 3. 字幕阅读区

保留 ivory/navy/gold 与 `"Times New Roman", "SimSun", "宋体", serif`。阅读文字约 16–17px、行高 1.65–1.75；时间戳是窄栏，当前行用淡蓝底与金线，其他行安静分隔。移除孤立的 `+`；书签按钮有明确 SVG 图标、可见名称或等效可访问名称、正确 `aria`、键盘焦点和 coarse pointer ≥44px。

保留自动跟随、用户手动滚动、书签和播放恢复语义；避免字幕区内再嵌套独立滚动容器。不得用颜色单独表达当前、失败或状态。

## 4. 登录慢/失败：先测量，后修根因

先以合成/脱敏方式按阶段记录 client submit、本地 API、IdP、catalog、auth poll 的耗时与错误类别；不打印真实正文或秘密。每个获准真实尝试的受限脱敏 artifact/schema 只可包含：阶段名、开始/结束或 `duration_ms`、闭集 `outcome`/`error_category`、安全时的 HTTP status、`operation_id` 的 SHA-256 短投影、attempt 序号、是否成功。绝不记录请求/响应正文、URL query、headers、cookies、账号或课程。成功定义为认证状态 `ready` 且目录可用（不采集内容）；失败定义为明确闭集阶段失败或超时；缺字段或没有真实门禁尝试必须标记“未验证”。

仅有合成证据不得宣称已修复 IdP/catalog 的真实根因；无充分证据不得修改认证核心，只可改善诚实反馈。根据充分证据修根因，禁止盲目缩短 timeout 或无限重试。

修复必须包含：防重复提交；busy 和当前阶段状态；具体但不泄密的失败说明与重试入口；保留既有约 90 秒临时失败退避、keepalive/轮询、账户隔离、DPAPI、`remember` 与 2FA 安全语义。

真实登录只在合成测试通过和 risk_auditor 通过后进行：沿用 `C:\Users\admin\.courselens-secrets\` 中既有 `p0_session.py`、`remember=false`、fresh `operation_id`，最少尝试（建议最多 2 次）且重试遵守现有退避。不得复制参数或秘密；真实认证期间禁止截图、DOM/网络/存储导出。若不确定一次行为是否安全或在范围内，跳过真实认证。

## 5. 密码显示/隐藏

使用现有 SVG，不引入 emoji/在线资源。按钮在 `password`/`text` 间切换时不得改变值、光标、`autocomplete` 或 remember 语义；`aria-label` 精确为“显示密码”/“隐藏密码”，并维护 `aria-pressed`、键盘焦点和 ≥44px 粗指针目标。默认、关闭再打开及重新渲染后均为遮罩状态。

## 验收与工作流

每个单一修复遵循：只读追踪 → 可复现证据 → 最小补丁 → focused test → 合成浏览器断言 → reviewer。行为测试应实际运行 DOM/ESM，不只检索源字符串。验证含 changed-module `node --check` 与 ESM import、相关 `.mjs`/pytest、全量 pytest、合成浏览器 1440/1000/375 亮暗截图与交互断言、零横向溢出。已知环境失败必须在当次 fresh run 重新确认，不能硬编码或宣称绝对全绿。

合成浏览器只用已有 fixture/stub；截图前断言目标状态、抽屉互斥、必要元素可见、合成 auth；失败不截图。截图不得含真实数据。若浏览器工具不可用，停止浏览器部分并如实报告，不安装新包。

## 非目标、停止条件与报告

非目标：真实任务派发/重试/取消、真实数据导出、协议变更、账户迁移、依赖/框架替换、视觉重做、无证据功能扩张。

停止并报告：需要 allowlist 外文件而无已证实根因；需要真实认证但 risk gate 未通过；需要秘密/真实内容；需要 runtime/public/managed 操作；需要发布/Git/Secrets/破坏性动作；或无法保留脏基线。

最终报告模板：

1. 目标与已确认根因；
2. 修改文件与为何最小；
3. 逐项验收（任务/字标/字幕/登录/密码）；
4. 命令、测试、合成截图与溢出结果；
5. 真实认证是否未运行/仅脱敏结论及 gate；
6. reviewer 结论、剩余风险与完整 `git status --short` 基线对比。
