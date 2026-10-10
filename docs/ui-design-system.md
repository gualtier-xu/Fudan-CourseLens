# CourseLens 桌面工作台设计系统（Academic Editorial，已落地）

记录 `frontend/` 实际采用的视觉与交互规则。只记录已实现的规则；不包含未落地的意图。
当前实现为 Academic Editorial 方向（`docs/ui-academic-editorial-ia-guidance.md`）：
**学习是唯一持久主场景**，搜索是全局动作（单一浮层），课表是选课上下文（渐进披露），
任务是右抽屉，设置只从账户菜单进入。旧的方向（Fluent、绿色品牌、四稳定页面、
两页三浮层、Minimal Swiss）已全部失效并被替换。

技术栈保持原生 HTML、CSS、ES modules；不引入框架、CSS 框架、在线字体、CDN、
图标包或动画库；图标只用 vendored Tabler SVG（`assets/icons/`，含 LICENSE.txt）。

## 信息架构（实际落地）

- 顶栏：CourseLens 字标（点击返回学习，并稳定回到课程/讲次选择入口，保留已选课程与
  讲次）→ 仅在真实层级中显示位置（学习桌时显示“课程 · 讲次”面包屑）→ 右侧 `Ctrl K`
  搜索、复旦+GitHub 连接状态簇、任务、账户。
- 学习页三态：未选课空态 → 课程/讲次选择（含“今日与本周安排”渐进披露）→ 学习桌。
- 学习桌 ≥1000px 左右双栏：左 = 播放器、播放恢复、生成动作、任务证据、直播状态；
  右 = 字幕 / 总结 / 资料 / 复习（书签在字幕内；课程关系与学习回顾是复习内二级入口）。
- 学习内容四标签（AI 学习工作台，已落地）：
  - 总结标签（原“笔记”更名）：结构化 `artifact.content` 优先渲染概览（阅读衬线、
    保留换行）、核心结论（非空才显示）、章节（时间戳按钮 = 合法锚点才可跳字幕 seek，
    不自动播放）；legacy `content_markdown`/纯文本安全回退，内容为空显示诚实空态，
    绝不 `JSON.stringify` 原始载荷、不暴露哈希。生成来源压成一行（模型 + 更新时间）。
  - Lecture IR（`kind=lecture_ir`）是纯增强：`contract=evidence.v1` 且 `key_moments`
    含合法毫秒锚点才显示“关键时刻”（时间 + 页码，不从 ID 猜标题）；缺失/为空/畸形
    只隐藏增强。总结与 IR 各自 epoch + AbortController 守卫，切换讲次/视图丢弃过期响应。
  - 测验：答案默认折叠，原生按钮 `aria-expanded`/`aria-controls` 揭示，揭示后不自动
    打分；“查看依据”仅对同讲次合法锚点出现。
  - 复习：计划渲染有序步骤（类型闭集映射 + 未知原词回退、预计分钟、后端原词状态、
    理由、逐条依据动作）；`exam_state=passed` 的计划显示“考试已结束，保留历史步骤”。
  - 考试上下文（只读附注，非第二模式）：仅 `exam_state=active` 且后端已验证非空
    `course_scope` 才渲染安静一行「距考试 … · 来源 … · 今日预算 N 分钟」+ 复习列表
    顶部的“今天建议”分组（只消费后端顺序前缀，按 `daily_minutes` 预算截取，不重排、
    不打分、无“必考/押题”文案）；时间缺失/已过/与 `active` 不一致（>720h）一律
    fail closed 隐藏；窗口外/不可用/已过保持普通复习。
  - 课程关系：文本关系行 + 课程标签；证据状态只来自后端（有证据数组才写“有证据”，
    `stale` 写“证据待确认”，否则“证据状态未提供”），绝不默认“有证据”。
  - 总结头“针对本讲提问”：打开既有搜索浮层并带讲次范围提示（状态行文案），回答
    范围仍由 `store.activeLecture` 决定，不新增问答通道。
  - 触达补偿：`pointer: coarse` 下本内容面紧凑按钮（揭示/依据/时间戳/本讲提问）恢复
    ≥44px（`components.css` 的 `button{min-height:32px}` 加载顺序晚于 `layout.css`
    全局 coarse 规则并覆盖之，属既有全局事实，本包交互按相同媒体条件局部恢复）。
- 搜索 = 单一 overlay 三态（紧凑 ≤8 项 → 完整检索 limit=50 → 证据回答），Esc 逐级退出；
  无搜索页、无路由。
- 课表 = 选择态标题下默认折叠的“今日与本周安排”：今天/本周稍后按时间列表、周切换、
  刷新；低频学期起始日/ICS 在“课表设置”二级披露与设置页课表分组；无七列表格。
- 任务 = 全局右抽屉（覆盖当前屏，scrim/inert/Esc/焦点归还）；动作只来自后端
  `task.actions`；行按 `course_id` 精确映射 `store.courses` 课程名，缺失映射进
  “未关联课程”组，不猜测标题；每张卡片渲染一行 13.5px 辅助上下文
  「{类型}任务 · {讲次日期} {讲次标题}」（类型为闭集 `TASK_KIND_LABELS`，未知 kind
  诚实回退原始字符串；讲次同法按 `course_id → sub_id` 精确映射，未解析时只显示类型）；
  同组已完成任务折叠为单个 `details.task-history`（摘要「已完成 N 个 · 最近 {时间}」，
  展开前不渲染完整卡片）；失败任务追加一行闭集处置文案（键 = 后端
  `TASK_ERROR_CODES`，未知代码走统一兜底），技术代码只保留在折叠的“技术详情”内。
- 设置 = 普通页面，仅从账户菜单进入，带“返回学习”；不引入路由。
- 新手引导 = 普通页面（`data-page="onboarding"`），保留顶栏；五步内容用安静步骤目录 +
  约 720px 阅读列，移动端只显示“第 N 步，共 5 步”和当前标题。当前步骤用
  `aria-current="step"` + navy-soft 底 + 左 2px 金线；状态行复用 `data-state` 圆点语义；
  步骤 4 的可选能力三行为中性“可稍后设置”（灰点），不用失败红；教程状态与服务
  readiness 分离，状态只读后端证据。入口 = 已登录账户菜单“新手引导”、“设置 →
  更新与帮助”帮助行、未登录学习空态文字按钮；从引导进入设置时临时显示
  “返回新手引导”，引导关闭即隐藏。
- 浏览器返回键：本阶段未集成 history（合同禁止路由）；返回会离开应用，
  属已知边界，产品化阶段再确认承载方式。
- 无浏览器底部导航；无“学习/搜索/课表/设置”一级导航。

## 颜色 token（`styles/tokens.css`，双主题）

| Token | 亮色 | 暗色 |
| --- | --- | --- |
| `--canvas` 页面底 | `#F7F3EB` | `#111927` |
| `--surface` / `--surface-strong` | `#FFFCF7` / `#FFFFFF` | `#182230` / `#1D2A3C` |
| `--ink` 正文 | `#172033` | `#EFE9DC` |
| `--navy` / `--navy-ink` 标题与主色 | `#1E3A5F` | `#2E5285` / `#A9C4E0` |
| `--navy-deep` 播放器舞台 | `#071D33` | `#0C1626` |
| `--navy-soft` 选中底 | `#E9EFF5` | `#22334A` |
| `--gold` / `--gold-strong` 强调 | `#B7791F` / `#B45309` | `#D19E3F` / `#DCA742` |
| `--muted` 次文字 | `#667080` | `#93A0B4` |
| `--line` / `--line-strong` | `#DED6C9` / `#8A8272` | `#2E3A4E` / `#76839A` |
| `--danger` | `#B4232D` | `#E0705F` |

规则：无绿色、无渐变；组件内禁裸色值；大面积填充仅 canvas/surface/surface-strong/
navy-soft。深色主按钮 `#2E5285`、标题 `#A9C4E0`、舞台 `#0C1626`。

## 字体与字号

全局仅 `"Times New Roman", "SimSun", "宋体", serif`（`--font-editorial`），无在线字体。
正文 16.5px/1.75；控件 16px；辅助文字 13.5px/1.6（最小）；标题 h1 1.5rem/700；
数值 `font-variant-numeric: tabular-nums`；中文正文 `letter-spacing: normal`。

## 连接状态簇（复旦 + GitHub）

- 语义四态：**已连接 = navy 实心点**；**需操作 = 金色空心环**；**失败 = 红点 + 白色 `!`**；
  **检查中/未知 = 灰实心点**。形状与文字辅助，不只靠颜色。
- 复旦状态只消费 `store.auth`；GitHub 状态只消费 `GET /api/v3/remote-connection`
  （安装时一次 + 打开 popover 时刷新）；无 interval、无新增 SSE、无“实时”声称。
- popover 列出两行状态（复旦/GitHub）与指引文字“详细诊断在设置 → 网络与远程连接”；
  `aria-controls`/`aria-expanded`/`aria-live` 同步。

## 顶栏计算保护告警（触发式）

GitHub 连接簇与「任务」之间的一条小状态文本（`#protection-alert`）+ 唯一
live region（`#protection-live`）：CourseLens 本地防过量保护的**触发式**提示。
正常、过期或未知状态一律不渲染任何内容——顶栏没有常驻额度 pill、没有
popover、没有剩余分钟数或百分比。

- 数据通道：`GET /api/v3/tasks` 载荷中的 `protection_alert`
  （`courselens.worker-budget-alert.v1`）——后端仅在本机日常保护闸门**新鲜地**
  处于 `warning`/`blocked` 时给出，闭集键 = schema/state/reset_at/
  observed_at/expires_at/action，绝无 limit/used/available 分钟或百分比；
  其余情况后端直接给 `null`，前端不渲染。
- 文案 = 闭集状态词 + 北京时间重置时刻：`今日计算接近上限，新任务可能被暂停`
  / `今日计算已达上限，新任务已暂停`，可附「北京时间 HH:mm 重置」。前端只做
  闭集映射（`tasks-drawer.js` `PROTECTION_STATE_TEXT`），不拼任何数值公式。
- 新鲜度以时间为根：`expires_at`（缺省 `observed_at`+20s）已过且无新载荷 →
  60s 纯 DOM ticker 或 SSE `onerror` 快路径把告警隐藏（渲染空），零网络；
  重连/下一条消息先 GET 对账。跨北京时间零点用一个精确 `setTimeout` 触发
  一次刷新，让 blocked/warning 在自然日切换后尽快消失；旧预算体系的多档
  pill/popover/百分比降级文案全部移除。
- 状态着色仅 `warning` 金 / `blocked` 红，且状态词始终在文案内，不只靠颜色；
  语义播报经唯一 `#protection-live`（sr-only `role="status"`），仅在告警状态
  跃迁时播报一次，隐藏即清空语义。
- 任务入队被本地保护拦截（`budget_exhausted`）时，后端错误文案直接说明
  「今日计算保护已暂停新任务，约 HH:mm 自动重置」，由既有提交动作的 toast
  通道原样呈现。

- 任务 ETA 友好文案与保护告警同文件（`tasks-drawer.js` `taskEtaText` 闭集映射，
  合同 §5.2）：分钟友好舍入、无秒级倒计时，仅在友好字符串变化时更新 DOM。

  | 场景 | 文案 |
  | --- | --- |
  | stale / heartbeat 失效 | `进度确认中` |
  | 排队（`estimate_basis: queue`） | `等待 GitHub Runner` / `等待 GitHub Runner · 已等待 N 分钟` |
  | 区间（lower+upper） | `预计约 8–12 分钟` |
  | 点估计（仅 `remaining_seconds`） | `预计约 9 分钟`；后端明确 0 剩余 → `预计剩余 0 秒`，不向上捏造 |
  | 资料不足 | `正在估算` / `正在估算 · 已运行 N 分钟` |

## 浮层纪律（单一浮层）

`ui.js openOverlay` 统一入口带**单浮层守卫**：任一浮层已打开时禁止叠开（先 Esc 关闭再
打开）。浮层集合 = 搜索 palette、任务抽屉、连接 popover、账户菜单、登录 dialog。
全部保持背景 `inert`、`Esc`（palette 逐级：完整/回答→紧凑→关闭）、Tab 圈闭、关闭后
焦点归还触发元素、`aria-modal`。

登录 dialog 结构（一张身份卡，一条主路径，已落地）：saved 与 manual 不再是两个分区，而是同一张身份卡的两个状态，主按钮永远只有一个。状态 A（有可用已保存账号）= 身份行（学号走 `--font-display` 衬线轨，右侧为「已保存密码/需要更新密码」闭集徽记，多账号即 radio 语义选择器）+ 全宽唯一主按钮「一键登录」+ 文字链「使用其他账号或手动输入 →」渐进披露。状态 B = 同卡展开的手动表单（学号/密码/保存在本机；Windows 加密说明收为 checkbox 次行小字），「← 返回已保存账号」返回身份卡。选择需要更新密码的账号直接转手动态并预填学号；身份卡收起时隐藏的必填输入同步 `disabled`（HTML5 校验安全）。登录请求面与账号 API 消费语义零变化。

登录 dialog 反馈（已落地）：提交防重复（进行中禁用提交并标记 `aria-busy`）；约 2 秒
轮询 `GET /api/v3/authentication` 渲染闭集阶段行；检查中快照带 `step/attempt/
max_attempts` 时渲染「正在验证 · 第 N/M 次 · 校园网关/课程平台」（闭集步骤映射，
未知步骤省略，缺失字段回通用阶段文案）；失败为闭集中文文案 + 可重试，dialog 保持
打开——`fudan_login_failed` 用诚实的网络/服务波动措辞（约 90 秒后再试），新增
`fudan_credentials_rejected` 明确指向学号或密码核对；打开/关闭即复位（提交可用、
状态与错误清空、密码回掩码）。密码框内置 inline SVG 显示/隐藏切换：默认掩码，
翻转只改 `type`，不改值、光标与 `autocomplete`；切换按钮绝对定位于输入框右缘内侧
（输入框 `padding-right` 让位），`pointer: coarse` 下 ≥44px 并保留 focus ring；
`aria-label` 在“显示密码/隐藏密码”间同步，`aria-pressed`/`aria-controls` 同步。

## 组件语言

- 列表行无框：发丝底线 + hover `surface` + 激活 `navy-soft` 底 + 左 2px 金线 +
  `aria-current="true"`。
- 按钮：主操作 = navy 实心（`--accent-contrast` 文字）；次 = 1px line-strong 描边；
  文字按钮无框；danger 红描边、确认态实心红。
- recovery/警示面板用左 4px 金色条；状态行用 6px 圆点 + 文字（`data-state` 映射）；
  冲突 = 红左线 + “！” + “冲突”文字 + 图标 + 重叠说明，不只靠颜色。
- 课表会议行 = navy-soft 底 + 左 2px 金线；已结束淡出 + “已结束”文字；“下一节”金字。
- 字幕行书签 = inline SVG 书签图标按钮（与行内文本同排；`pointer: coarse` 下 ≥44px），
  `aria-label="加入书签"`。
- 原生字幕 track 的 `video::cue` = 编辑体衬线（`--font-editorial` 同栈）+ 白字 +
  半透明 navy-deep 底，行高 1.5、字号 `17px`/`2.2vh` 双声明。**记录例外**：`::cue`
  内对 `var()` 支持不可靠，底色必须写字面量 `rgba(7, 29, 51, 0.78)`（即 `--navy-deep`
  `#071D33`），白字同为字面量。
- 阴影只给浮层（抽屉/palette/popover/dialog/toast）；列表行不投影。
- 云控制面卡（学习页，NIGHT2-W9）：单行状态字（复用 `data-state` 色彩）+ 右侧动作组；
  动作集完全来自快照 `actions` 闭集，未知键忽略、空集整卡隐藏；危险动作（撤销云端
  授权/抹除云端数据）= danger 描边 + 两击确认（`confirming` 实心红）。
- 数据管理批量条按意图分三档可见标签：随时可做 / 释放空间（金）/ 不可恢复（红），
  附一句人话说明（NIGHT2-W12）。课程列表行支持拖拽与 Alt+↑/↓ 键盘重排，顺序入
  `courselens.course-order.v1`（视图偏好键，NIGHT2-W13）。

## 响应式与触达

- ≥1000px 学习桌双栏（`1.7fr / minmax(360px,1fr)`）；720–999px 上下堆叠；
  <720px 顶栏四控件 44×44（任务按钮图标化，保留 `aria-label="任务"`）。
- `pointer: coarse` 控件 ≥44px；375px 零水平溢出；Tab 序 = 视觉序；
  `prefers-reduced-motion` 全禁动效；`forced-colors` 保留边框/高亮/状态形状。
- 播放器 375px：播放、当前/总时长、CC、全屏必须可见（原生 controls + 字幕 track），
  次要控件可隐藏但不能裁掉控制栏。

## 合同要点

`localStorage` 仅写 `courselens.theme.v1`、`courselens.catalog-term.v1`、
`courselens.course-order.v1` 三个视图偏好键；凭据只进密码框；路由仅 `/api/health` 与
`/api/v3/*`；任务动作/进度/结果全部后端证据；课表动作带新 `operation_id`；
认证未就绪时搜索/课表给诚实门禁与可执行入口；删除类操作二次确认；
诊断脱敏三段文案不变。
