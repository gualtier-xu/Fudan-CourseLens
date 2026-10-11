# Worker 保护额度、任务 ETA 与全项目夜间收口交接合同

Last updated: 2026-09-07 (revision 2)

> **2026-09-14 部分取代（COMPUTE-GUARD-RETENTION-1）**：本文档中关于“常驻顶栏
> 额度 pill / 百分比 / 圆环 / 额度 popover / `worker_budget` 常驻载荷 /
> GitHub Billing usage API 与 `Plan: read` 权限”的条款已被取代。现行合同：
> 顶栏只有一个**触发式**保护告警（`courselens.worker-budget-alert.v1`，仅在
> 本地每日闸门新鲜地 warning/blocked 时出现，闭集状态 + 北京时间重置时刻，
> 无任何剩余分钟或百分比；正常/stale/未知不渲染任何内容）；任务载荷不再携带
> 常驻 `worker_budget`；客户端不调用任何 Billing 接口，App 清单不再请求
> `plan: read`。本地原子每日入队闸门、任务成本预留/结算与任务 ETA 设计仍然
> 有效，但文中提及的旧字幕模式矩阵已被取代：
> live 契约只有唯一 `automatic` 策略（DeepSeek Key 配置时 AI 校对，否则非 AI
> 回退；进度/预算内部按键 proofread/fallback 行为命名，不再是模式标签）。

Revision 2 closes four defects in the first draft: the available-budget formula now
accounts for active consumption; the UI no longer depends on the drawer-scoped SSE;
budget reservations must be concurrency-safe and idempotent; and the post-push real
test/remediation loop has a finite, risk-bounded completion rule.

## 0. 任务目标与执行顺序

本合同是本轮唯一执行入口。先在 `private/main` 完成两个产品目标：

1. 建立可解释、可执行的 CourseLens Worker 保护额度体系，并在 Academic Editorial 前端顶栏加入一个精致、紧凑、随任务事件更新的额度组件；
2. 诊断并改进任务中心中进行中任务的预计剩余时间，降低明显跳变、虚假精确和排队时间混入计算时间的问题。

产品增量完成并经 reviewer 接受后，才依次执行：确认没有并行写入者 → 最终组合态全功能验收 → 风险门禁 → 分组提交并非强制推送 → 风险门禁 → 使用用户已授权的测试身份做有界真实 E2E → 修复可复现缺陷 → 复验并追加提交/推送。

不得跳过顺序，也不得因为用户离线而降低门禁。遇到一个高风险阻塞时，记录该阻塞并继续处理不依赖它的安全项目；不得用猜测填补证据。

## 1. 必读、仓库边界与脏基线

完整读取：

- 工作区根 `AGENTS.md`；
- `docs/handoff.md`；
- 本合同；
- `docs/ui-design-system.md`；
- `docs/student-workspace-login-combined-closeout-handoff.md`；
- `docs/timetable-subtitle-live-followup-handoff.md`；
- `docs/student-onboarding-guide-handoff.md`；
- `src/runtime/{automation,task_store,estimator,progress,http_api}.py`；
- `src/application.py` 中任务入队、进度、历史样本、远程运行与自动化预算调用链；
- `src/remote/{coordinator,github_app,github_client}.py` 中 workflow/run/job 状态读取路径；
- `frontend/modules/tasks-drawer.js`、`frontend/modules/ui.js`、`frontend/modules/shell.js`、`frontend/index.html` 与相关样式、测试。

只允许编辑 `private/main`。`public/worker-mirror` 是 CI 生成的只读镜像；`managed/personal-worker`、`managed/mailbox` 是托管状态；均不得手改。不得人工读取、移动或修改 `runtime/data`。

开工先记录 fresh：

- `git status --short --branch`；
- HEAD、上游、`origin/main`；
- `git diff --stat` 与目标路径 diff/hash；
- 当前活跃 agent/任务（工具可用时）；
- 现有测试入口和离线性；
- 本轮预计 allowlist。

当前工作树已包含大量用户和其他会话的未提交修改。它们全部属于用户基线。禁止 `reset`、`clean`、`stash`、`restore`、`checkout --`，禁止覆盖、删除或通过旧 patch 反向重写。只允许一个 write-capable worker 顺序写入；如目标文件正在被其他任务修改，先只读调查并等待稳定。

## 2. 已知事实与不得误称的内容

### 2.1 GitHub 与“额度”

- 标准 GitHub-hosted runner 在公开仓库中不消耗包含分钟；私有仓库或计费 runner 才涉及账户计费分钟。参考：<https://docs.github.com/en/billing/concepts/product-billing/github-actions>。
- GitHub Billing usage API 返回账户计费用量，不是通用的“剩余算力”，个人账户还要求 GitHub App user access token 的 `Plan: read`，且 Enhanced Billing 可用性不一。参考：<https://docs.github.com/en/rest/billing/usage>。
- 本任务**不增加** GitHub App `Plan`/Billing/organization Administration 权限，不改变 App registration，不要求现有用户重新授权，也不读取账户级账单。
- UI 必须称为“CourseLens 保护额度”或“今日计算预算”，不得冒充 GitHub 官方余额。公共标准 Runner 状态可说明“不计费”，但不得声称无限、保证可用或不会限流。

### 2.2 当前本地能力

- `automation_budget_ledger` 已有 `runner_minutes`、`reserved_runner_minutes` 和默认 `max_runner_minutes=300`；先确认它是否只覆盖 cloud automation。不得未经证据把它直接改名为所有手动任务的真实总账。
- 任务公开模型已有 `remaining_seconds`、`elapsed_seconds`、`progress_confidence`；`EvolvingEstimate` 已使用 median/MAD、EMA、历史样本和区间；`ProgressTracker` 已包含字幕媒体时长、RTF、阶段权重和重启恢复。
- 当前前端只显示单点“预计剩余”，没有显示上下界、置信度、依据或队列/计算时间的区别。
- `processed_media_seconds`、signed progress、阶段样本、GitHub run/job 起止时间是否完整参与当前 ETA，必须沿真实调用链验证，不得假定。

## 3. Worker 保护额度：产品语义

### 3.1 额度的目的

额度首先用于防止学生一次派发过多长课程，使队列拥堵或意外消耗大量 runner 时间；不是账单系统。必须同时提供：

- `limit_minutes`：北京时间当天的 CourseLens 保护上限；默认复用已批准的 300 分钟，除非证据证明需要迁移；
- `settled_minutes`：已终结运行且已结算的可验证 runner 用时；
- `active_consumed_minutes`：当前运行任务截至 `observed_at` 已经消耗的活动时间；
- `reserved_minutes`：已排队/运行任务尚需的保守预测；
- `committed_minutes = settled + active_consumed + reserved`；
- `available_minutes = max(0, limit - committed)`；
- `queued_count`、`running_count`；
- `reset_at`：下一个 Asia/Shanghai 自然日边界；
- `confidence`、`observed_at`、`expires_at`、`stale` 和明确的数据来源。

`reserved_minutes` 只表示尚未发生的预测消耗，绝不能包含
`active_consumed_minutes`；任务终态结算时以实际值替换该任务剩余预留，不能再追加一份总耗时。任务重试、暂停/续跑、客户端重启、重接、导入失败和终态重复通知必须幂等。活动任务允许完成；额度不足时阻止**新的自动/批量远程派发**。对手动单任务采用现有产品策略中最严格且最少意外的一种：默认阻止并给出“调整今日保护额度/等待明日重置”的明确动作；不得静默超额。

额度判断和任务入队之间不得有 TOCTOU 窗口。两个并发 enqueue 不能都在同一份
余额上通过。优先在 TaskStore 的同一锁/SQLite 事务内完成“检查可用额度 → 建立任务
预留 → 入队”；若现有调用链不能低风险地合并事务，至少用应用级单进程锁包住持久化
预留与入队，并留下 `ponytail:` 注释说明多进程上限。不得继续使用当前
`get_automation_budget()` 后另开连接 `update_automation_budget()` 的读改写模式承担并发门禁。

### 3.2 估算与核算

按最小梯子实现：

1. 优先复用现有 task、remote run、estimate sample 和 automation ledger 的计算与字段语义；
2. 先证明 `automation_budget_ledger` 的作用域。当前调用证据表明它主要服务 cloud automation，不能直接把历史行解释成所有手动任务的全局余额；
3. 若 aggregate ledger 无法对单任务幂等预留，增加最小的按 `task_id`/attempt 标识的 reservation/accounting 记录，并用唯一约束或事务阻止重复结算；
4. 禁止新增服务、第三方依赖、遥测平台或账户 Billing 权限。

预留估计：

- 字幕：媒体时长 × 同模式/同 source-kind 的稳健 end-to-end RTF；无历史时使用当前 bootstrap RTF；
- 摘要：区分 include-ppt 与 transcript-only，优先同 pipeline/profile 历史；有可用字幕长度、分段数或幻灯片数量时可作为有界修正，但不得读取或保存正文；
- 未知任务：使用经测试的保守上界或标为不可估，不得伪造 0；
- 排队等待不计入 runner 消耗，但必须单独展示。

核算优先级：已验证 GitHub job/run 起止时间或 Worker 终态 elapsed → signed progress 的活动时间 → 本地保守估算。若 GitHub 计费语义用于显示，Linux 每 job 向上取整分钟与 runner multiplier 必须明确；如果只是保护预算，可以保留秒级核算，但字段名不得叫“GitHub 已计费”。

每天北京时间重置；跨午夜运行的任务必须按一个明确、可测试的规则归属。最低复杂度规则为：已发生的活动秒数按观测时所在日期结算，未发生的剩余预留转入新日；若现有状态无法可靠切分，则整次 attempt 按启动日归属并在代码加 `ponytail:` 注释说明上限与升级条件。必须覆盖 23:59→00:01、系统时钟回拨、重复终态、恢复后再结算。

恢复出厂后的新用户没有本地历史样本。此时额度预留必须使用现有 bootstrap
profile 并标为 `low` confidence；不能显示成“精确余额”。历史样本达到可证明的最小
数量后再升级置信度。预算数据不可用、迁移失败或快照过期时，enqueue 应 fail closed，
但本地播放、浏览和已运行任务的导入不得被连带阻止。

### 3.3 读取与更新通道

优先把 budget snapshot 附在现有 tasks snapshot/EventSource 载荷中，让顶部组件、任务 chip 和任务抽屉消费同一事实源。当前 SSE 由 `tasks-drawer.js` 持有且仅在抽屉打开时存活；若 fresh 调查仍如此，应把**这一条既有订阅**提升到应用级 store/app 生命周期，并让抽屉只消费共享快照，不能为额度组件再建第二条 SSE。额度随任务事件、signed progress、终态与恢复事件更新；应用启动和打开 popover 时各允许一次有界 GET，跨北京时间零点允许一个精确 `setTimeout` 触发刷新。不得新增常驻轮询、重复 SSE 或每秒请求。

允许前端在相邻事件之间只更新“多久前确认”，不得本地捏造后台进度。SSE 断线时保留最后一次快照并在 TTL 后标 stale；重连先 GET 对账。语义状态变化只使用一个 `role=status`/atomic live region，只在 normal→warning、warning→blocked、stale/恢复等阈值变化时播报，不能让每个数字成为竞争播报。

## 4. 顶栏额度组件

使用 `ui-ux-pro-max` 做针对性 UX 检索，但 Academic Editorial 设计系统优先。若本地检索无匹配结果，按其 accessibility/touch/layout 默认规则执行并如实说明，不得编造搜索结论。组件应像 Codex 的紧凑用量入口，而不是仪表盘卡片：

- 桌面位于 GitHub 状态簇与“任务”之间，采用一个低噪声 usage pill：18–20px SVG 圆环 + `今日预算` + `余 214 分钟`；百分比只作为辅助，不同时堆满三个主数字；
- 移动端保留 44×44px 触达，以 SVG 仪表图标 + 简短百分比/状态呈现，不能挤压顶栏；
- 点击打开唯一的锚定 popover，与账户菜单、搜索、任务抽屉遵守现有单浮层守卫；
- popover 显示“今日预计剩余”“已使用”“已预留”“排队/运行”“北京时间重置”“估算可信度”“数据来源”；
- 公共标准 Runner 显示“标准公共 Runner 不计费；这里是 CourseLens 防过量保护额度”；其他拓扑显示“计费状态未读取”，不得猜测；
- 正常为 navy，接近阈值为 gold，耗尽/阻止派发为 danger，并始终配文字/形状，不能只靠颜色；
- 无绿色、无渐变、无 emoji、无在线字体、无新图标依赖；复用现有 Tabler 风格内联 SVG 和 token；
- 亮暗主题、375/720/1000/1440、200% zoom 零横向溢出；键盘、Esc、外点关闭、焦点归还、`aria-expanded`、`aria-controls`、`aria-haspopup` 完整；
- stale/unknown 时显示“额度待确认”，不能显示 0 或 100%；负数、NaN、超过上限和时间逆序一律归入诚实异常态；
- 动画只用于不改变布局的短过渡；`prefers-reduced-motion` 下直接进入终态。

不要创建独立“额度页面”，不要添加图表库。一个紧凑入口和一个简洁 popover 已足够。

## 5. ETA 根因调查与改进要求

先用合成进度轨迹复现当前误差，不得先改常数。至少区分：

- GitHub 排队/runner allocation；
- payload/授权准备；
- ASR 按 chunk 推进；
- proofread/LLM；
- OCR/幻灯片获取；
- remote result/import；
- 暂停、重启接管、重试与 stale heartbeat。

### 5.1 预测模型

保持简单、稳健、可解释，不增加 ML 依赖：

1. 排队阶段不输出处理 ETA；显示“等待 GitHub Runner · 已等待 X”，开始执行后再估算。
2. 基线来自同类任务的 median/MAD：至少区分 kind、subtitle mode、include-ppt、source kind；仅当 pipeline/worker 版本显著影响耗时时再纳入 profile key。
3. 字幕优先使用真实媒体时长和 end-to-end RTF；在至少两个稳定 chunk 后，将 live processed-media pace 与历史 RTF 有界融合。确认并修复 `processed_media_seconds` 只存不使用之类的真实缺口（如果存在）。
4. 当前 `ProgressTracker._observe_estimator()` 的 duration 路径主要按 wall-time 推进，且有历史时会忽略 live RTF；fresh 代码若仍如此，以此作为首要根因假设验证。建议最小公式为 `remaining_media × blended_rtf + remaining_tail`：历史稳健 RTF 为先验，live RTF 的权重随有效 chunk 数/已处理比例增长并设上下限。只能相加确实串行的尾部；重叠引擎/阶段取关键路径而不是机械求和。
5. 对 proofread、OCR、结果导入等非线性尾部使用阶段剩余模型或阶段历史，避免 ASR 完成后 ETA 错误归零。阶段切换时继承已消耗活动时间，不能重置为一份全新总时长。
6. 历史不足时显示范围而非假精确；异常样本用现有 median/MAD/winsorization 处理，保留最近有界样本。不同 Worker/pipeline 版本只有在基准证明分布显著不同时才拆 profile，避免冷启动碎片化。
7. 允许 ETA 因真实变慢而上调，但要有界平滑；不得强制单调下降而撒谎。UI 只在友好舍入后的分钟值或区间跨档时更新，避免每个 heartbeat 抖动。
8. heartbeat stale、进度倒退、分母变化、重启恢复、暂停或没有可测总量时，冻结旧预测并降级为“正在估算/进度确认中”；暂停时间不计入 active elapsed。
9. queue elapsed 与 active elapsed 必须来自明确时间锚。GitHub queued→in_progress 之前只累计等待；重试的新 attempt 单独计时，任务级展示可累计历史 attempt 但 ETA 只能针对当前 attempt。

### 5.2 公共 API 与 UI

在保持兼容的前提下，任务公开模型应能表达：

- `remaining_seconds`（兼容现有消费者）；
- remaining 的 lower/upper 或等价区间；
- `progress_confidence`；
- `estimate_basis`（闭集、非敏感）；
- `observed_at`/stale；
- queue elapsed 与 active elapsed 的明确区分。

同时为额度组件公开一个版本化、闭集的 `worker_budget` snapshot。数值全部由后端计算，前端不得自行拼公式；至少包含 limit/settled/active/reserved/available/committed、队列与运行数量、reset/observed/expires/stale、confidence、source、enforcement_state。旧客户端忽略该字段仍能工作。

前端规则：

- high：`预计约 9 分钟`；
- medium/low：`预计约 8–12 分钟`；
- 资料不足：`正在估算 · 已运行 3 分钟`；
- 排队：`等待 GitHub Runner · 已等待 2 分钟`；
- stale：`进度确认中`。

时间可以按分钟友好舍入；不得显示不断抖动的秒级倒计时。技术详情可以显示 basis/confidence，但不暴露 raw GitHub 响应、课程内容或内部秘密。

### 5.3 精度验收

建立当前算法 baseline，再用同一组合成轨迹比较新算法。使用按任务切分的回放/留出样本，不能把同一任务的阶段点同时放进拟合和评估：

- 首次可用 ETA 的时间；
- 中位绝对百分比误差 + 中位绝对误差；短任务/接近终态以绝对误差为主，避免百分比爆炸；
- 系统性高估/低估偏差；
- 预测区间覆盖率；
- ETA 大幅跳变次数；
- 终态归零与 stale 降级正确性。

不得为了指标只拟合一条轨迹。覆盖短/长字幕、AI 校对开启与自动回退两条行为路径、摘要含/不含 PPT、queue stall、慢 chunk、proofread 尾部和重启恢复。验收目标不是写死一个可被投机的百分比：新模型的 median absolute error 与大幅跳变次数应不劣于 baseline，区间覆盖率应与标称置信度方向一致；若无法证明点估计更准，必须证明诚实性提升——错误 ETA 被替换成范围或“待估算”，且无现有准确场景回退。

## 6. 预计实现范围与测试

先按调用链确定最小 allowlist。候选范围：

- `src/runtime/{estimator,progress,task_store,http_api,automation}.py`；
- `src/application.py`；
- 仅在需要已有 GitHub run/job 起止证据时修改 `src/remote/{coordinator,github_client}.py`；
- `frontend/index.html`；
- `frontend/modules/tasks-drawer.js`，必要时 `shell.js`/`ui.js`；
- `frontend/styles/{components,layout,pages,accessibility}.css`；
- 对应现有测试，新增最多一个 ETA/预算 Python 测试文件和一个前端执行式 mjs；
- `tests/synthetic_shell_server.py`；
- `docs/ui-design-system.md` 与必要事实文档。

不需要就不要修改。默认不改 Worker/protocol；若现有 signed progress 不足，先报告为何必须改变协议。Worker 改动会触发签名镜像发布边界，本合同没有发布授权。

验证顺序：

1. 改动 Python `py_compile`、JS `node --check`；
2. estimator/progress/task-store/API 定向 pytest；
3. 前端执行式 mjs 与 unified task center/workbench 测试；
4. 合成浏览器：额度 100%/正常/临界/耗尽/stale，任务 queue/high/medium/low/stale/恢复，亮暗和响应式；另用执行式测试证明抽屉关闭时组件仍从唯一共享 SSE 更新、断线转 stale、重连 GET 对账、跨日刷新且无重复订阅；
5. fresh client 全量 pytest；Worker 未改时跑最小 smoke 或说明；Worker 改动时跑全量 Worker；
6. markdown links、encoding、`git diff --check`；
7. reviewer 审查当前增量；涉及预算持久化、GitHub run 核算或权限边界时先/再由 `risk_auditor` 审查。

后端必须有最小回归用例覆盖：两个并发 enqueue 争抢最后余额只能一个成功、重复终态不重复结算、retry/恢复保持同一 reservation 身份、跨日、factory-reset 冷启动、未知耗时 fail closed、活动任务完成不被中断。

截图与矩阵只放 gitignored `runtime/cache/worker-budget-eta-20260907/`，必须为合成数据。

## 7. 工作区稳定轮询

本产品增量 reviewer 接受后，不立即提交。进入只读稳定观察：

1. 每次记录 HEAD、上游、`git status --porcelain=v2`、tracked diff 的摘要哈希、untracked 路径及内容哈希；不得写 patch 快照。
2. 工具可用时检查其他 agent/任务是否仍 running/needs-attention；不打断它们。
3. 以不超过 30 秒的单次等待轮询；不向用户播报“无变化”。
4. 只有连续 3 个、间隔约 30 秒的快照完全一致，且没有已知 write-capable agent 仍运行，才认定稳定；若已有明确的 agent 完成清单，三次足够，避免无意义等待。
5. 任一变化都重新开始计数并重新检查本轮目标文件是否被覆盖。

若无法观测其他 agent，只能报告“文件系统稳定”，不能声称“所有任务已完成”。

## 8. 最终组合态全功能验收

稳定后重做 fresh preflight，以最终工作树为唯一事实。先由 reviewer 做重叠/死代码/合同冲突审计，再运行：

- 所有前端模块 `node --check` 与 ESM import/执行式加载（`node --check` 不能发现所有浏览器模块组合错误）；
- 全部仓库内 mjs harness；
- client 全量 pytest（使用 `.venv-client-py310\Scripts\python.exe`）；
- Worker 全量测试（`PYTHONPATH=worker;.`）；
- markdown links、encoding、`git diff --check`、冲突标记和禁止秘密扫描；
- synthetic shell 的核心矩阵：首次引导、登录、课程/讲次、课表、回放/字幕、直播入口、搜索、设置、任务抽屉、额度组件、ETA、亮暗、375/720/1000/1440/200%。

验收还必须核对当前 `docs/handoff.md`。它是唯一动态状态入口，但当前文件头仍是
2026-09-05，明显早于工作树中的后续批次；提交前必须按 fresh 事实刷新，删除“尚未完成”
但已经完成的指示，并保留仍有效的签名、发布、取消与 runtime 安全边界。不得把旧测试
计数或会话报告直接抄成当前事实。

报告 fresh 实际计数，不沿用历史 604/641/700/702 等数字。合成验收不等于真实服务成功。

## 9. 分组提交与推送授权

用户已明确授权在组合态通过后，将全部应入库成果分组提交并推送。执行前必须由 `risk_auditor` 做 fresh Git/凭据/远端门禁并得到 PASS；该授权不包含 force-push、历史重写、删除、merge、tag、Release、镜像发布、GitHub App 权限变更或 Secrets 管理。

提交规则：

- 先 `fetch` 并确认远端身份、分支、祖先关系和无远端漂移；
- 只用显式路径 `git add`，禁止 `git add -A`；
- `runtime/**`、凭据、缓存、截图、trace、浏览器 profile、真实数据和备份永不提交；
- 对 staged diff 做仅计数的秘密模式扫描和人工路径审查，禁止回显命中内容；
- 先把现有未跟踪文件逐一分类为：产品/测试、当前有效文档、已被取代的执行提示、gitignored 证据。只提交产品代码及测试、用户文档、`docs/handoff.md` 和仍有持续价值的设计/运维文档；不要为了“全部推送”把已完成任务的一次性 prompt、重复 handoff、缓存或报告全部塞进仓库。无法安全删除的未跟踪历史文件可保留并在报告列出；
- 按可真实追溯的功能原子提交：产品代码及其测试应同提交；重叠 hunk 无法可靠拆分时合并成一个 integration commit，禁止伪造批次归属；事实文档最后一笔；
- 每笔提交后跑最小相关检查，全部提交后再跑组合 smoke；
- 仅非强制 push。优先推送新的 `codex/pre-release-integration-20260907` 分支，避免把两笔既有本地提交和多批脏工作树未经分支审查直接写入受保护 main；只有 fresh gate 明确证明当前 main 允许且历史/保护规则均符合时才可 fast-forward push。不得 force、自动 merge、开 PR、覆盖远端或改写历史；PR/merge 需要另一份明确授权。

## 10. 有界真实 E2E 授权与秘密处理

推送成功后，用户已授权使用其测试复旦身份和 DeepSeek API Key 做全项目真实 happy-path 验收。**明文秘密不得写入本文件、任何仓库文件、命令行、环境转储、日志、报告、截图、trace 或 agent 消息。**

只允许通过现有 `C:\Users\admin\.courselens-secrets\` DPAPI store 与 `p0_session.py`/`p0_keepalive.py` 注入，`remember=false`，每次登录使用 fresh `operation_id`。若安全存储没有所需秘密，停止真实 E2E 并请求用户在新对话安全表单/消息中重新提供；不得从 Git 历史、shell history 或缓存寻找。

真实动作前由 `risk_auditor` fresh gate，至少确认：

- 精确账户与 GitHub App installation/repository scope；
- 无活动任务、无孤儿 run、无待清理 token lease；
- 当前签名 Worker pin 与 channel test ready；
- 操作预算、预计 runner 分钟和 LLM token 上限；
- 日志/截图/trace 全部关闭或脱敏；
- `runtime/data` 只由产品正常路径访问，agent 不人工读取。

真实测试采用“破坏性/高成本分支用 synthetic，低风险 happy path 用真实服务”的完整功能矩阵。用户本消息已经授权本任务内的测试身份认证，不必等用户醒来再次询问；risk gate 仍必须通过，安全存储缺值则停止真实部分。

真实测试顺序：

1. 最多两次复旦登录，首次瞬时失败后按既有约 90 秒退避一次；
2. 走一次首次引导/跳过/重新打开、账户与密码可见性、本地保存文案和状态；真实凭据仍只用安全驱动，不把明文交给浏览器自动化或截图；
3. 只读目录、课表 refresh、媒体回放、字幕阅读/书签、直播入口/无直播诚实态、搜索 readiness、连接状态、额度组件和任务抽屉；
4. 选择不输出名称/内容的最短合适讲次，顺序执行一条代表性字幕 → 笔记 → 摘要/幻灯片 → 导入 → 搜索/证据回答链；一次只运行一个远程任务；
5. 验证 retry/pause/resume/cancel/额度耗尽/凭据拒绝/保存删除/恢复出厂等破坏性或高成本分支时只用 synthetic harness；不把“全功能”解释成真实执行所有危险动作；
6. 优先控制在 60 runner 分钟和 100,000 LLM tokens 内；预计会超出时停止真实链但继续合成审计；
7. 只记录布尔、闭集 code、计数、耗时、哈希化标识和 notices；不记录课程标题、正文、URL、字幕、回答或秘密；
8. 不运行真实取消；不批量处理课程；不更改 GitHub App 权限；不发布镜像；不删除运行时数据。

若 Worker 源码已改变而公开签名镜像尚未发布，真实 E2E 只能验证当前 pin，必须标记新 Worker 代码 `unverified`；不得把推送私有 main 当成镜像发布。

## 11. E2E 后缺陷修复循环

对真实或组合验收发现的问题：

- 先复现并追到共享根因和所有调用者；
- 一次只修一个有证据的 P0/P1/P2；对“非常值得增加”的功能先列收益、用户频率、复用点、风险和验收标准，只允许实现最多 3 个无需新权限/协议/依赖/迁移、能在本地和 synthetic 明确验证的高价值小缺口；其余只进入建议清单；
- 不做“也许以后有用”的功能，不引入新依赖/服务/权限；
- 每个增量由单一 worker 修改、定向测试，里程碑 reviewer；信任边界变更先 risk gate；
- 修复后重复相关 synthetic/real 验证，并按 §9 追加非强制提交和推送；
- 需要 release、签名镜像、App permission、真实取消、删除或数据迁移时记录 blocker，继续下一个安全项，不自行扩权。

每轮修复最多 3 个增量；达到上限后必须先做 reviewer 复核和组合回归，再决定是否确有下一轮。禁止用“继续完善”无限扩张范围。

结束条件：最终工作区再次稳定；组合测试无未归因失败；reviewer 无未关闭 P0/P1；P2 已修复或有明确接受理由；真实链已完成或被明确安全门禁阻止；所有允许提交已推送；剩余项仅为需用户新授权的高风险门禁或如实标记的 unavailable。

## 12. 最终报告

报告必须包含：

1. fresh 基线与并发漂移；
2. 额度语义、数据来源、保护行为和 UI 截图；
3. ETA baseline/new 指标、范围/置信度和异常降级；
4. 实际改动文件及最小性；
5. 定向、组合、浏览器和全量测试的 fresh 计数；
6. 稳定轮询证据；
7. commit SHA、分组、push 目标及任何远端阻塞；
8. 真实 E2E gate、预算、动作、脱敏结果与未验证项；
9. reviewer/risk_auditor 结论；
10. 最终 `git status --short --branch` 和 remaining follow-ups。

不得泄露秘密或真实课程内容，不得把估算称为官方 GitHub 余额，不得把 synthetic 结果称为真实验收。

## 13. Documented limitations（已记录局限）

额度账本存在两项已知局限，方向均为保守或自愈，记录在案以免重复排查：

- **降级数据的 TOCTOU**：并发 enqueue 门禁在同一锁/事务内完成，但快照数据本身可能在观测与使用之间降级（历史样本过期、后台刷新滞后、confidence 下降）。该窗口由 `observed_at`/`stale` 如实标注，快照过期时按 §3.2 fail closed，随后刷新即自愈，不持久化错误的额度状态。
- **同日重试 over-count**：同一北京日内任务重试或重复终态的保守核算可能把已用额度计得高于实际消耗；只会多计、不会少计，因此额度门禁只会更严格，不会放行超额派发。跨北京日重置后自然修复。

两项不影响额度门禁的保守正确性；进一步精确化需要另行授权的最小增量，不属于本合同范围。
