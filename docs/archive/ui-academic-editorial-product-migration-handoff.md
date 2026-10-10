# 学术编辑风产品迁移交接

## 1. 状态、授权与优先级

状态：**夜间持续执行合同，也是正式本地迁移唯一入口**。先完成 Academic UI 迁移；随后在可编辑的 `private/main` 内，重复“一次只审计、修复、验证一个有边界的问题”，直到用户明确说停止。迁移完成、一次测试全绿或一次干净审计都不是终点。

本文件授权本地实现、测试、文档同步和 CourseLens 所需的正常认证端到端测试；不授权 Git 操作（`add`、`commit`、`push`、分支、合并）、发布/签名/镜像发布、GitHub 写入、Secrets 变更、真实取消、破坏性操作、`runtime/data` 读取或人工修改、以及 managed Worker/Mailbox 状态的人工修改。`runtime/cache/ui-academic-editorial-product-acceptance-20260906/` 只可作为 Git 忽略的截图证据目录，不能承载产品代码或运行时数据。

优先级：安全、隐私、认证、API、任务生命周期、DPAPI/凭据、无障碍和现有测试合同高于本文件；本文件高于旧 UI 意图和旧设计文档。冲突时停止，不用视觉迁移绕过更严格合同。

当前工作树已有脏改动是**用户基线**。先记录其完整 `git status --short`，之后只审查本增量；绝不 `reset`、`checkout`、`restore`、`stash`、覆盖、清理或重排这些改动。`public/worker-mirror` 是生成物且只读。

真实测试仅允许沿用现有 DPAPI secret store/session injection，且一律 `remember=false`。授权包络仅限提供的测试账户/API 上的登录/会话、正常 GET/读取行程、播放、搜索与 search-answer、课表读取，以及验证所必需的最少普通任务创建/派发/结果导入行程。任何变更状态请求必须是既有产品动作、适用时使用新的 `operation_id`、非破坏且次数最少；经产品 UI 的设置改变须在测试结束前恢复。真实取消、删除、批量修改、人工 `runtime/data` 操作、Secrets、发布/签名、Git/GitHub 写入仍然禁止。若不能确定某动作是否破坏性、昂贵或超出正常测试行程，跳过它并审计另一安全区域。

绝不把用户提供或读取到的明文凭据、API key、Cookie、课程 URL/正文或账号标识写入文件、命令、日志、截图、报告、提交或环境转储；正常认证流程产生的既有短暂状态不等于授权读取或人工修改 `runtime/data`。

## 2. 初始 allowlist、受控升级与 preflight

迁移第一增量只可触碰以下路径：

```text
frontend/index.html
frontend/modules/ui.js
frontend/modules/shell.js
frontend/modules/search-palette.js
frontend/modules/study.js
frontend/modules/tasks-drawer.js
frontend/modules/settings.js
frontend/modules/player-core.js
frontend/modules/timetable.js
frontend/styles/tokens.css
frontend/styles/layout.css
frontend/styles/components.css
frontend/styles/pages.css
frontend/styles/accessibility.css
docs/ui-design-system.md
tests/test_frontend_workbench.py
tests/frontend_playback_recovery_behavior.mjs
```

`player-core.js` 与 `tasks-drawer.js` 是条件修改：只有为满足本文件指定的移动播放器控制、任务精确映射或既有合同回归所必需时才改。允许对整个 `private/main` 做只读审计；但后续编辑仅可在满足全部条件时扩大到 root AGENTS 路由的精确最小文件集：已确认可复现的 defect 或明确既有合同缺口；先读全调用链；不触及 `runtime/data`、generated/managed 目录、发布/签名/Secrets/GitHub、破坏性操作或需要独立 GO 的边界；并在下一检查点写明“问题、证据、精确文件、为何初始 allowlist 不足”。不得为猜想功能、纯重构或纯装饰性 churn 升级范围。

先完整读取工作区根 `AGENTS.md`、`docs/handoff.md`、本文件、`docs/ui-academic-editorial-ia-guidance.md`、最终原型 `runtime/cache/ui-academic-editorial-prototype-20260905/index.html` 和其 `ia-revision/` 截图、旧设计文档、相关调用点与测试。原型和截图只作视觉/IA 参考，不能复制演示 JS、合成数据或其绕过产品合同的实现。只读 preflight 必须确认：当前脏基线；`git status --short`、`git diff --stat`、`git diff --name-only` 与本增量 diff；现有路由/导航、`openOverlay`、搜索 API、任务抽屉、课表、播放器和设置入口；测试命令可用；没有读取 `runtime/data`、凭据或真实课程资料。每个增量对将触碰的路径记录前后精确基线（状态及 diff 或 hash）；若目标在增量外出现意外变化，立即停止该增量，改审计另一安全区域或报告。不得保存秘密或原始课程数据。对要改的共享函数先检索所有调用者，修改根因而非单一路径症状。

## 3. 目标 IA

```text
学习（唯一持久主场景）
├─ 课程/讲次选择态：今日与本周安排（渐进披露）
├─ 学习桌：播放器 + 字幕 / 笔记 / 资料 / 复习
├─ 单一搜索浮层：紧凑 → 完整检索 / 证据回答
├─ 任务：右侧抽屉
└─ 账户菜单 → 设置（独立页面，不引入路由）
```

搜索是全局动作，不是一级目的地；课表是选课上下文，不是一级目的地。不得保留“学习 / 搜索 / 课表 / 设置”四项顶栏或移动底部导航，也不得新建搜索页、搜索路由或新框架。

## 4. Shell、连接状态与学习层级

桌面顶栏：可返回学习的字标；仅在真实层级中展示当前位置/面包屑；右侧依次为 `Ctrl K` 搜索、复旦+GitHub 状态簇、任务、账户。账户菜单进入设置，主题留在设置。移动顶栏同样保留返回或字标、搜索、状态簇、任务、账户，删除四项底部导航；浏览器返回必须按学习层级可预测地退回，不能跳到无关页面。不得引入任何新路由。

状态簇中的复旦状态只消费 `store.auth`；GitHub 状态只消费既有 `GET /api/v3/remote-connection`。两者均使用批准原型的精确四态语义并配状态文字：已连接为 navy 实心点；需操作为金色空心环；失败为红点加白色 `!`；检查中/未知为灰实心点。GitHub 可在安装时一次 GET、打开 popover 时刷新，及消费既有状态事件；不得新增 `interval`、SSE 或“实时”声称。状态簇是读取现有状态的呈现，不得扩展认证或远程连接合同。

课程、讲次与课表是学习层级，不是“连接簇”：选择课程后进入讲次；有数据时课表项进入或选中相应课程；学习桌返回上一级选择态。不得伪造数据关联或真实服务可用。

## 5. 单一浮层与搜索三态

搜索只有一个由共享 `ui.js`/`openOverlay` 根因语义承载的 overlay，不能拆为路由、页面或多个相互独立的 modal。账户菜单、状态 popover、搜索与任务抽屉同时最多打开一个；其他浮层已开时 `Ctrl K` 不得叠开搜索，打开设置前必须先关闭浮层。它必须保持背景 `inert`、`Esc`、关闭后焦点归还和 `hidden` 的真实隐藏语义。

1. **紧凑态**：学术编辑风居中 command palette，打开时输入获取焦点并有 autocomplete。合并本地目录和 `GET /api/v3/search?q={q}&limit=8` 的已有全文结果，首屏最多 8 项，分为字幕时间点、课程讲次、笔记/书签/资料、操作；每项有足够上下文和唯一可达目的地。
2. **完整检索态**：仍在同一 overlay，保留 `GET /api/v3/search-index`、`GET /api/v3/search?q={q}&limit=50`、索引状态、空态、失败态和全文结果。桌面展开为稳定阅读面，移动近全屏。
3. **证据回答态**：仍在同一 overlay，承载既有 `POST /api/v3/search/answer`、回答证据及不可用/失败状态；请求携带现有 `course_ids`/`sub_id` 上下文，回答不得冒充检索事实。

认证未 ready 时展示诚实门禁和既有可执行入口，不能假称索引、搜索或回答完成。每次新搜索、关闭或上下文改变须 abort 旧请求并隔离晚到响应，不能以过期的 `course_ids`/`sub_id` 覆盖当前结果。键盘上下键移动结果、`Enter` 打开活动项、`Esc` 每次只退一级（完整/回答→紧凑→关闭）。使用 `dialog`、`aria-modal`、`combobox`、`aria-autocomplete="list"`、命名 `listbox`/`option`、`aria-activedescendant` 和 `aria-live`；一个 option 只对应一个动作。保持背景 `inert`、焦点圈闭、关闭后归还触发元素、44px 粗指针目标和可见焦点环。

## 6. 课表、学习桌、任务与设置

课程/讲次选择态标题下加入默认折叠的“今日与本周安排”，摘要可为“今天 2 节 · 下一节 15:25 合成课程”。展开后按时间排序，不做七列教务表。桌面可为“今天 / 本周稍后”双列，窄屏单列。读取保持既有 `GET /api/v3/timetable?semester_id=…&week=…` query；动作保持 `POST /api/v3/timetable/actions` 且每次动作生成新的 `operation_id`；ICS 保持 `semester_id`。展开区承接高频周切换、课程会议、冲突、刷新；低频学期起始日、ICS 进入“课表设置”二级披露或设置页课表分组。未认证清空、缓存 stale、需起始日、空、加载、失败、冲突均须诚实呈现；冲突不得只用颜色。

保留学习桌播放器、字幕、笔记、资料、复习的结构、恢复、时间点、书签和复习语义。≥1000px 左右同屏；375px 必须可见播放、当前/总时长、CC、全屏，次要控件可隐藏，但不得只留下进度条或裁掉控制栏。任务继续为右抽屉：保持 `GET /api/v3/tasks`、`POST /api/v3/tasks/actions` 的新 `operation_id`，只显示后端 `task.actions` 允许的动作；保持现有 EventSource topics、重连和关闭清理。按 `course_id` 精确映射 `store.courses`，缺失映射进入“未关联课程”，禁止猜测标题。设置仍是独立页面，只从账户菜单进入；不引入路由。会话、代理、更新、危险操作确认、DPAPI/凭据和隐私/诊断脱敏不得弱化。

## 7. Academic 视觉和响应式约束

- 字体仅 `"Times New Roman", "SimSun", "宋体", serif`；无在线字体、CDN、新依赖或 emoji 图标。
- 亮色：canvas `#F7F3EB`，surface `#FFFCF7`/`#FFFFFF`，ink `#172033`，navy `#1E3A5F`/`#071D33`/`#E9EFF5`，gold `#B7791F`/`#B45309`，muted `#667080`，line `#DED6C9`，line-strong `#8A8272`，danger `#B4232D`。
- 暗色：canvas `#111927`，surface `#182230`/`#1D2A3C`，text `#EFE9DC`，title `#A9C4E0`，button `#2E5285`，gold `#D19E3F`/`#DCA742`，stage `#0C1626`。
- 禁绿色和渐变；正文 `16.5px/1.75`，控件 16px，辅助文字至少 13.5px，数值采用 tabular numerals，coarse pointer 控件至少 44px。375px 不得有水平溢出。

## 8. 测试迁移与精确验证命令

先迁移/更新 allowlist 内现有测试的断言，使其保护新 IA 及既有合同；不要删除测试来“变绿”，不要新建宽泛测试框架。至少覆盖：一级导航消失、状态簇四形状/未知、搜索单浮层与三态/`Esc`/abort 晚到隔离、认证门禁、结果键盘路径、焦点归还、课表状态与非颜色冲突、任务精确映射、移动播放器控制栏和零溢出。实际落地完成后，同步更新 `docs/ui-design-system.md`：删除旧 Fluent、绿色品牌、四稳定页面等已失效说明，只记录实际落地后的规则，不预写未来意图。

在 `private/main` 用 PowerShell 运行，保留输出：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
git status --short
.\.venv-client-py310\Scripts\python.exe -B scripts\check_markdown_links.py
.\.venv-client-py310\Scripts\python.exe -B scripts\check_text_encoding.py
$modules = @('frontend/modules/ui.js','frontend/modules/shell.js','frontend/modules/search-palette.js','frontend/modules/study.js','frontend/modules/timetable.js','frontend/modules/tasks-drawer.js','frontend/modules/settings.js','frontend/modules/player-core.js')
$modules | ForEach-Object { node --check $_ }
.\.venv-client-py310\Scripts\python.exe -B -m pytest -q tests\test_frontend_workbench.py tests\test_frontend_shell_security.py tests\test_frontend_playback_recovery_behavior.py tests\test_timetable.py tests\test_unified_task_center.py tests\test_remote_compute_ui.py tests\test_documentation.py tests\test_text_encoding.py
.\.venv-client-py310\Scripts\python.exe -B -m pytest -q
node --test tests\frontend_playback_recovery_behavior.mjs
git diff --check -- frontend/index.html frontend/modules/ui.js frontend/modules/shell.js frontend/modules/search-palette.js frontend/modules/study.js frontend/modules/timetable.js frontend/modules/tasks-drawer.js frontend/modules/settings.js frontend/modules/player-core.js frontend/styles docs/ui-design-system.md tests/test_frontend_workbench.py tests/frontend_playback_recovery_behavior.mjs
git status --short
```

若解释器、Node 或某一已有测试因环境/既有基线失败，记录准确命令、失败类别和与本增量的关系，不更改环境或扩大范围。

## 9. 浏览器矩阵、认证端到端测试与独立 review

视觉矩阵必须硬隔离：只在未认证 fixture/stub 中运行。在 `private/main` 以以下命令启动合成服务器：

```powershell
.\.venv-client-py310\Scripts\python.exe tests\synthetic_shell_server.py --port 0
```

浏览器只能使用该命令 stdout 给出的 loopback origin；断言合成 auth identity、`remember=false`，以及没有真实 login/refresh/remote/task action。没有 OS 级 egress lock；因此 origin 或 fixture 断言任一失败时，不得捕获截图。认证测试期间禁止截图、导出、DevTools network/storage 捕获、DOM dump 或任何内容提取。报告仅可在既有政策允许时给出计数、状态、耗时和散列化标识符，绝不包含真实内容。所有正式截图写入 Git 忽略的 `runtime/cache/ui-academic-editorial-product-acceptance-20260906/`，固定为以下 14 个文件：

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

另以几何检查覆盖 900、720 与 200% 缩放，无需额外截图。截图不得含调试控件、真实课程/URL/账号、凭据、API key、Cookie、正文或可识别的运行时资料。

首次认证端到端测试前，以及其动作范围改变前，必须由只读 **risk_auditor** 对精确 journey、所有状态变更调用、成本/破坏性、秘密处理和停止条件做 preflight。用户当前 GO 只覆盖本文件已记录的测试包络；若 auditor 通过且范围未变，不需要重复征求用户 GO。否则跳过真实测试或请求新的 GO。不得发明真实 E2E 命令；只使用现有外部 `p0_session.py`/`p0_keepalive.py` 机制，不复制其参数或任何秘密。

正常认证端到端测试已获授权，但只能使用现有 DPAPI secret store/session injection 与 `remember=false`；测试应限于验证本增量所需的正常 CourseLens 行程，并以最小次数完成。不得打印、复制、截图或报告任何秘密/真实内容，不能把 session 注入脚本改造成长期存储、环境导出或批量抓取工具。真实取消仍需单独 GO；发布、签名、镜像/仓库发布、GitHub 写入、Secrets 变更、破坏性操作、`runtime/data` 读取或人工修改始终分别需要单独 GO。

记录每张截图的 viewport、主题、状态和关键元素 bounding boxes；检查控制台为 0 error、375 零水平溢出、1000px 学习桌双栏、键盘、焦点归还、状态演示和对比度/触达。每个增量都运行与风险相称的定向自测；共享流程、认证、任务、课表或安全改动必须再跑全量相关检查。

**独立 reviewer**不必重复审查每个微小修复，但在 Academic 迁移里程碑、一个多修复批次拟被接受前、以及有重大回归/安全风险的增量时必须只读审查。重点查 IA 偏离、合同回归、a11y、移动裁切、测试缺口、脏基线误伤和扩范围正当性；先修复明确问题，再重跑受影响验证。只有凭据、加密、签名、信任边界、发布、迁移或真实运行时等真正高风险门禁才使用 risk auditor；它只给出门禁意见，不授权被门禁的操作。reviewer/risk auditor 都不得修改文件、泄露真实数据或扩大授权。

## 10. 夜间审计循环与检查点

root/controller 负责为每个增量定范围、证据和风险级别；一个有边界的 worker 独占同一仓库的编辑与定向自测，不在同一仓库并行写入。reviewer 仅按第 9 节用于里程碑、批次或重大风险；risk_auditor 仅用于真正高风险门禁和上述认证 preflight，均不授权被门禁的操作。

按下列优先级持续工作：

1. 本迁移的阻塞缺陷、合同回归和测试失败；
2. 安全、隐私、认证、数据泄露、任务生命周期和可靠性缺陷；
3. 可访问性、键盘/焦点、移动布局与错误/空态诚实性；
4. 有复现证据的产品 bug、测试缺口、文档与实际落地不一致；
5. 小且证据支持、与现有合同兼容的功能改善。

每轮只取一个最小问题：读取证据和调用链 → 写出假设与精确范围 → 最小修复 → 定向自测 → 必要时认证端到端最小 smoke → 检查点。普通微小修复可累积为有边界批次，再按第 9 节进行独立 review；不要把多个无关问题捆绑，不发明功能，不以“看起来可以更好”代替证据。一个审计 lens 干净时，转到下一个优先级 lens；完整清洁循环后，只复查未闭合 TODO、test skips、错误路径和当前增量风险，不随机挑选功能。

每个完成的增量留一条简短检查点记录在本任务连续上下文中（不要为此新建产品文件）：问题与证据、修改文件、定向验证/截图或真实测试的脱敏结论、若本轮触发则记录 review/risk-auditor 结论、剩余风险、与初始 allowlist 不同的文件及理由。检查点后立即选择下一个优先级最高且已证实的问题继续；持续目标/continuation 可用时必须使用它保持执行。

## 11. 停止/暂停条件与最终报告

仅在以下闭合条件停止或暂停：用户明确说停止/暂停；没有安全工作可继续且所有剩余工作都被闭合 blocker 阻止；出现安全 blocker（需要明文秘密、`runtime/data` 读取/人工修改、真实取消、发布/签名/镜像或 GitHub/Secrets 写入、破坏性操作）；安全、隐私、API 或生命周期合同不清楚且无法从现有代码/文档确认；无法保持既有脏基线；或需要不能按受控升级规则正当化的范围。迁移完成、一次 clean audit、没有立即发现问题、测试全绿或等待普通测试结果都不是停止理由；继续从优先级列表选择下一项。

只有用户明确停止，或没有安全工作可继续且已到闭合 blocker 时，才交付最终报告。平台强制的 turn boundary 只输出检查点，保持 persistent goal 未完成，并在 continuation 可用时继续。最终报告固定为：

1. 修改文件及每项意图；
2. 测试迁移与验证命令/结果；
3. 合成浏览器矩阵、截图和 bounding-box 结果；
4. 独立 review 结论及修复；
5. 保留的合同、未验证项与停止边界；
6. preflight 与最终 `git status --short` 的基线对比。

真实认证端到端测试如已运行，只报告已验证的正常行程和脱敏结果，不宣称未覆盖的搜索、回答、课表、任务或取消行为；绝不包含秘密或真实课程数据。
