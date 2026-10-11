# Academic UI 稳定化交接

## 状态、范围与基线

这是一次**有边界的稳定化批次**的唯一执行合同，源于一次被拒绝的验收；它不是下一轮重设计，也不授权持续扩范围。只在 `private/main` 工作，遵守根 `AGENTS.md`：root/controller 先定范围；一个有边界的 worker 独占编辑和定向自测；完成后独立 reviewer 审查。risk auditor 只为凭据、加密、签名、信任边界、发布、迁移或真实运行时等高风险证据门禁服务，且不授权被门禁的动作。

已核对的当前事实：`HEAD` 为 `f597b36`；`origin/main` 为 `76e8c85`，当前领先 2 个提交。历史的 pre-doc 用户状态是 **18 个 tracked modified + 6 个 untracked**，不得相信旧报告的“16”计数；本任务开始时，前两份稳定化文档已存在，实际状态是 **18 个 tracked modified + 8 个 untracked**。这两份新增文档不是用户基线。preflight 必须验证完整的精确路径名称，而不是只报计数；随后只评估本批增量。绝不 `reset`、`stash`、`restore`、`checkout`、覆盖或清理。

禁止真实服务、Git 操作、发布、Secrets、签名、GitHub 写入、取消、破坏性清理、`runtime/data` 读取或人工修改。不得复制真实内容、凭据、URL、Cookie、账号或 API key 到文件、命令、日志、截图、报告或环境转储。

## Preflight 与授权路径

先完整阅读根 `AGENTS.md`、`docs/handoff.md`、本文件、`docs/ui-academic-editorial-product-migration-handoff.md`、当前 allowlist 文件和相关测试。对每个将改文件记录：修改前 `git status --short`、`git diff -- <path>`、SHA-256；结束后重复。若某目标在本增量外出现意外变化，停止该增量并报告，不覆盖它。

本批的**唯一可编辑 allowlist**如下；无任何范围升级。既有 icons 一律只读，如确有必要只能写最小 inline SVG。`docs/ui-design-system.md` 仅在代码行为变化且需事实同步时编辑；不得更新 `docs/handoff.md`。

```text
frontend/index.html
frontend/modules/search-palette.js
frontend/modules/tasks-drawer.js
frontend/modules/shell.js
frontend/modules/ui.js
frontend/modules/player-core.js
frontend/modules/study.js
frontend/modules/timetable.js
frontend/styles/layout.css
frontend/styles/components.css
frontend/styles/pages.css
frontend/styles/accessibility.css
docs/ui-design-system.md
tests/test_frontend_workbench.py
tests/frontend_academic_ui_behavior.mjs
runtime/cache/ui-academic-editorial-product-acceptance-20260906/product-select-schedule-collapsed-1440x900.png
runtime/cache/ui-academic-editorial-product-acceptance-20260906/product-select-schedule-expanded-1440x900.png
runtime/cache/ui-academic-editorial-product-acceptance-20260906/product-desk-1440x900.png
runtime/cache/ui-academic-editorial-product-acceptance-20260906/product-desk-1000x720.png
runtime/cache/ui-academic-editorial-product-acceptance-20260906/product-search-compact-1440x900.png
runtime/cache/ui-academic-editorial-product-acceptance-20260906/product-search-full-1440x900.png
runtime/cache/ui-academic-editorial-product-acceptance-20260906/product-search-answer-1440x900.png
runtime/cache/ui-academic-editorial-product-acceptance-20260906/product-settings-1440x900.png
runtime/cache/ui-academic-editorial-product-acceptance-20260906/product-tasks-drawer-1440x900.png
runtime/cache/ui-academic-editorial-product-acceptance-20260906/product-mobile-select-375x812.png
runtime/cache/ui-academic-editorial-product-acceptance-20260906/product-mobile-desk-375x812.png
runtime/cache/ui-academic-editorial-product-acceptance-20260906/product-mobile-search-375x812.png
runtime/cache/ui-academic-editorial-product-acceptance-20260906/product-desk-1440x900-dark.png
runtime/cache/ui-academic-editorial-product-acceptance-20260906/product-search-compact-1440x900-dark.png
runtime/cache/ui-academic-editorial-product-acceptance-20260906/acceptance-matrix.json
runtime/cache/ui-academic-editorial-product-acceptance-20260906/real-e2e-report-amended.json
```

新建并执行 `tests/frontend_academic_ui_behavior.mjs` 是强制条件。因此没有意外用户变化时，最终状态预期为原有 **18 tracked modified + 9 untracked**：任务起始的 8 个 untracked 加上该测试文件；截图/cache 为 gitignored，不计入该 Git 计数。不得以新增文档或测试倒算为用户脏基线。

### 路径到批准用途

| 路径 | 仅批准用途 |
| --- | --- |
| `frontend/modules/search-palette.js` | 搜索生命周期、焦点与 jump |
| `frontend/modules/tasks-drawer.js` | 任务分组、ARIA、结构图标 |
| `frontend/modules/shell.js` | 状态形状与 ARIA |
| `frontend/modules/ui.js` | 仅在已证明时修共享 overlay 根因 |
| `frontend/modules/player-core.js` | 播放器进度触发器语义 |
| `frontend/modules/study.js`、`frontend/modules/timetable.js` | 仅替换 warning glyph |
| `frontend/index.html` | 仅 inline SVG 与触发器语义 |
| `frontend/styles/layout.css` | 状态形状布局 |
| `frontend/styles/components.css` | 任务/palette 样式 |
| `frontend/styles/pages.css` | 13.5px 辅助文字与图标样式 |
| `frontend/styles/accessibility.css` | forced-colors 与焦点 |
| `docs/ui-design-system.md` | 条件性的事实同步 |
| `tests/test_frontend_workbench.py`、`tests/frontend_academic_ui_behavior.mjs` | 行为覆盖；后者必须新增并执行 |
| allowlist 的 `runtime/cache/...` 文件 | 仅合成验收截图、matrix 和脱敏 evidence note |

任何批准文件若本增量不需要，则保持不动；表外用途不获授权。

## P1：必须修复的搜索与浮层合同

1. `currentSearchTrigger` 找不到局部触发器时回退到全局搜索按钮。
2. 跳转课程、讲次或完整结果前先关闭 palette；恢复 `inert` 与焦点到有意义目标。
3. 重构 `openPalette`：被其他浮层阻挡时零搜索、零 timer、零 DOM 副作用。
4. 关闭 palette 时清 timer，并 abort **每一个** in-flight request。
5. `search-answer` 使用 `AbortController`，并在任何 DOM 或 event 副作用前检查 sequence 与 mode，隔离晚到响应。
6. 定义有效 query、索引状态与证据的门禁；禁止空 query 或未门禁的 POST answer。
7. **本批新增稳定契约**：`setMode` 必须在 `#palette-panel`（或 palette root）写入 `data-mode="compact|full|answer"`；它不是 pre-fix 已存在行为，必须由 `tests/frontend_academic_ui_behavior.mjs` 执行式锁定。

共享浮层仍以 `ui.js/openOverlay` 为根因位置：palette、账户、状态 popover 与任务抽屉同时最多一个；`hidden` 真实隐藏，背景 `inert`，`Esc` 和焦点归还不回归。

## P2：任务、可访问性与图标一致性

- 任务按 `course_id` 分组；缺失或未知课程进入明确的未知桶，绝不按标题猜测。
- 任务触发器的 `aria-expanded` 必须在开/关为 `true`/`false`；播放器进度触发器也保持相同语义。
- ready 与 checking 必须在颜色之外可区分；辅助文字不小于 13.5px。
- 在现有 scope 可用时，以现有一致 SVG 替换用作结构图标的 warning/refresh/close glyph；不引入 emoji、在线资源或新依赖。

## 测试与行为验收

行为测试必须实际执行 DOM 逻辑，而不只是源字符串断言。必须最小新增并执行 `tests/frontend_academic_ui_behavior.mjs`，聚焦 P1/P2 行为，不建新框架。运行定向测试、Node 语法检查、相关 Python 测试及全量 pytest。preflight 观察到 `tests/test_sign_client_release` 的 4 个 PowerShell module/AuthentiCode 环境失败及 **630 pass / 6 skip**；每次全量 pytest 都必须重新验证该基线并单独记录，UI 验收要求定向绿且无新失败，禁止把环境问题包装成“634 全绿”。

在 `private/main` 运行以下命令：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
git status --short
.\.venv-client-py310\Scripts\python.exe -B scripts\check_markdown_links.py
.\.venv-client-py310\Scripts\python.exe -B scripts\check_text_encoding.py
$modules = @('frontend/modules/search-palette.js','frontend/modules/tasks-drawer.js','frontend/modules/shell.js','frontend/modules/ui.js','frontend/modules/player-core.js','frontend/modules/study.js','frontend/modules/timetable.js')
$modules | ForEach-Object { node --check $_ }
node --test tests\frontend_playback_recovery_behavior.mjs tests\frontend_academic_ui_behavior.mjs
.\.venv-client-py310\Scripts\python.exe -B -m pytest -q tests\test_frontend_workbench.py tests\test_frontend_shell_security.py tests\test_frontend_playback_recovery_behavior.py tests\test_timetable.py tests\test_unified_task_center.py tests\test_remote_compute_ui.py tests\test_documentation.py tests\test_text_encoding.py
.\.venv-client-py310\Scripts\python.exe -B -m pytest -q
git diff --check -- frontend/index.html frontend/modules/search-palette.js frontend/modules/tasks-drawer.js frontend/modules/shell.js frontend/modules/ui.js frontend/modules/player-core.js frontend/modules/study.js frontend/modules/timetable.js frontend/styles/layout.css frontend/styles/components.css frontend/styles/pages.css frontend/styles/accessibility.css docs/ui-design-system.md tests/test_frontend_workbench.py tests/frontend_academic_ui_behavior.mjs
git status --short
```

## 只用合成浏览器验收

仅用可用的 Playwright/browser skill，禁止安装包或编造依赖；开始前必须阅读其 `SKILL.md`。仅当已有 bundled wrapper 或 global command 可调用时继续；否则停止截图部分。不得认证或访问真实服务。在 `private/main` 用准确命令启动合成服务器：

```powershell
.\.venv-client-py310\Scripts\python.exe tests\synthetic_shell_server.py --port 0
```

浏览器只能打开 stdout 给出的 loopback origin。使用工具已支持的**命名隔离 session**（不猜测参数；若现有 wrapper/global command 不支持，停止截图部分）。操作协议固定为：`open <stdout-origin>` → `snapshot` → `resize W H` → 以最新 snapshot refs 交互 → significant UI 变化后重新 `snapshot` → 仅用 `eval` 产出下列 pre-capture assertion object → `screenshot` → 对新 artifact 做 hash 并复制到精确目标。不得新增 capture script。每张截图前断言失败则不得写截图。

`eval` assertion object 必须至少含 `page`、`mode`、`visible`、`paletteVisible`、`drawerVisible`、`fixtureAuth`、`overflow`、`bboxes`、`consoleErrorCount`，并按状态包含以下 selector 期望：选择态 `#study-select`；展开课表 `#schedule-box[open]`；学习桌 `#study-desk`、`#player-stage`、`.materials-tabs`；palette 的 `#palette-root` 与本批新增的 `#palette-panel[data-mode="compact|full|answer"]`（或同等 palette root 属性）；任务态 `#tasks-root` 可见且 `#palette-root` 隐藏；设置态 `#settings-page` 可见。Matrix 的 capture trace 写入实际执行命令/动作列表，而非不稳定 refs。

200% 必须使用工具支持的 browser zoom/deviceScale 并记录实际方式；若工具不能设置，使用 CSS viewport 等价（1440→720、1000→500），在 matrix 标记 `viewport_equivalent`，绝不声称原生缩放。重写以下 14 个文件到 `runtime/cache/ui-academic-editorial-product-acceptance-20260906/`：

```text
product-select-schedule-collapsed-1440x900.png
product-select-schedule-expanded-1440x900.png
product-desk-1440x900.png
product-desk-1000x720.png
product-search-compact-1440x900.png
product-search-full-1440x900.png
product-search-answer-1440x900.png
product-settings-1440x900.png
product-tasks-drawer-1440x900.png
product-mobile-select-375x812.png
product-mobile-desk-375x812.png
product-mobile-search-375x812.png
product-desk-1440x900-dark.png
product-search-compact-1440x900-dark.png
```

学习桌截图必须有 active lecture、播放器和 tabs；移动学习桌必须在 viewport 内证明播放、当前/总时长、CC、全屏；任务文件必须显示 drawer。`full` 与 `answer` 的 hash 不同只是最低检查，不能替代每个状态断言。

持久化合成 `acceptance-matrix.json`，其 schema 必须绑定 server command、safe loopback origin、fixture assertions，以及每个 PNG 的 filename、SHA-256、viewport、theme、expected state、assertions、overflow、bboxes、console error count、capture command/action trace；还须覆盖 1440/1000/900/720/375 与 200%。控制台 JavaScript error 必须为 0。

合成服务器清理仅授权：先枚举 8799/8945 listeners，只有 command line **完全识别** `private/main/tests/synthetic_shell_server.py` 的进程才可停止；不匹配不得 kill。

## 真实证据边界

不得重新登录、派发、重试、取消或读取 `runtime/data`，也不得伪造缺失的历史证明。唯一证据 note 是 `runtime/cache/ui-academic-editorial-product-acceptance-20260906/real-e2e-report-amended.json`，仅可由既有脱敏 artifact 派生，且每一结论均有 `verified`、`unverified`、`source` 字段；删除没有证据的 GitHub-window 因果说法。若当前安全的 public/client 状态可在不认证、不读取原始内容下证明零 active，则只记录计数；否则保留“未验证”。

## Review、停止条件与交付

独立 reviewer 必须审查本批增量；最终验收条件是 reviewer 无 P0/P1，P2 已解决或被明确接受，并准确区分历史 **18 tracked + 6 untracked**、任务起始 **18 tracked + 8 untracked**、以及无外部变化时预期最终 **18 tracked + 9 untracked**。若发现高风险证据缺口，risk auditor 只读判定风险，不触发真实动作。

停止并报告：需要真实服务/Git/发布/Secrets/取消/破坏性动作、超出最小范围、无法保留基线、合成 fixture 不能证明预期状态，或 P0/P1 未闭合。最终报告列出：改动文件、逐路径基线、P1/P2 结果、命令与结果、截图/hash/matrix、环境失败与新失败区分、脱敏证据结论、review 结论、最终完整 `git status --short`，并明确历史 **18+6**、任务起始 **18+8**、无外部变化时预期 **18+9**；实际偏差必须逐路径解释。
