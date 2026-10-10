# CourseLens 前端 v3 设计方案 —— “静墨”完全重设计版

> **已被取代（2026-09-10）：** 本设计方案的信息架构与视觉方向已被 [docs/ui-academic-editorial-ia-guidance.md](../ui-academic-editorial-ia-guidance.md)（单一“学习”主场景、学术编辑风）取代；本文件仅作历史记录，不再作为实现依据。

状态：设计定稿，待实现（由执行模型按本文件实施）  
编写：2026-09-05（ui-ux-pro-max 技能库检索 + 现有前端逐文件分析）  
**本文件取代同日早版（保守换皮版）：这是完全重新设计**——HTML 结构、模块分解、交互模型、前端测试全部重建；不变的只有 §2 的产品级不变量  
适用仓库：`private/main`；可改 `frontend/**` 与 `tests/test_frontend_*.py`、`tests/frontend_*.mjs`、`tests/synthetic_shell_server.py`（仅当元素 ID 变化时同步）；**后端 `src/**`、`worker/**`、API v3 语义一律不动**

---

## 1. 完全重设计的边界：什么重建、什么不变

| 层 | 处置 |
| --- | --- |
| `frontend/index.html` | **全部重建**（新结构、新 ID 命名体系） |
| `frontend/styles/*` | **全部重写**（§3–§5 视觉系统） |
| `frontend/modules/*` | **重新分解**（§6 新模块架构；其中四块逻辑语义原样搬运，见 §6.3） |
| `frontend/app.js` | 重建（组合根，保持 ≤300 行、无 fetch 的既有约束精神） |
| `tests/test_frontend_workbench.py` | **按新设计重写**（安全断言逐条移植，见 §7——禁止弱化） |
| `tests/frontend_playback_recovery_behavior.mjs` | **按新模块重写**（全部行为场景逐一对应，见 §7.2） |
| `tests/test_frontend_shell_security.py`、其余后端测试 | **不动**（全部必须原样通过） |
| `src/**`、`worker/**`、API v3 | **不动** |

## 2. 产品级不变量（重建中任何时候不得弱化）

1. **技术栈**：vanilla HTML/CSS/ES modules 单页应用；不引入任何前端/CSS 框架、在线字体、CDN、图标包、动画库；图标沿用 vendored Tabler SVG（`frontend/assets/icons/` 含 `LICENSE.txt`，测试硬性校验）。仍由 `make_handler(…, PROJECT_ROOT / "frontend")` 静态托管。
2. **API 契约**：仅 `/api/health` 与 `/api/v3/*`；请求字段、闭集错误码、`operation_id` 幂等语义、后端权威状态机不变。`api-client.js` 的错误映射表（`ERROR_MESSAGES`）原样保留。
3. **凭据与隐私**：学号/密码/DeepSeek Key 只进 `type="password"`（带 `autocomplete`）；`localStorage` 仅允许写 `courselens.theme.v1` 一处；普通界面不出现上游 URL、路径、内部错误详情、原始 JSON——只在脱敏"复制诊断"里出现；诊断披露三段文案语义保留。
4. **诚实状态**：不伪造成功/进度/在线/可恢复；暂停/继续/重试/取消只由后端返回的 actions 决定；取消请求发出≠已取消，终态后才显示已取消；进度条仅在有真实分子分母时渲染。
5. **无障碍底线**：全键盘可用、Tab 序=视觉序、焦点环可见、dialog 焦点圈闭并归还、异步状态 `aria-live`、`prefers-reduced-motion` 全禁非必要动效、`forced-colors` 保留文字边框/高亮、`pointer: coarse` 下 ≥44px 触达、正文对比 ≥4.5:1、控件边界 ≥3:1、深浅双主题。
6. **生命周期纪律**（既有真实缺陷修复的沉淀）：`frontend-session` open/heartbeat/close（close 走 `sendBeacon`）；任务 SSE（`/api/v3/events`）随工作区清理关闭；每类异步请求有 `AbortController` 竞态防护；`pagehide` 反序清理全部监听。
7. **能力不减**：后端已暴露的能力（目录/播放/直播、字幕/笔记/资料/书签/测验/课程关系、搜索与 AI 回答、任务与远程连接、课表、账户/网络/隐私/更新设置）在新区中得到等价或更好的入口。

## 3. 视觉系统（沿用已批准的"静墨"方向）

### 3.1 设计选型（ui-ux-pro-max 检索结论）

- **主风格：Minimalism & Swiss Style**（style 域精确命中；product 域对"专业工具"与"Study Together"双推荐）。
- **辅风格：Flat Design 微交互**（无阴影、无渐变、150–200ms、排版优先）。
- **配色：Study Together / Virtual Coworking 调色板**——Focus Blue `#2563EB` + 冷纸 `#F8FAFC` + 墨字 `#0F172A`；原绿色品牌降级为 success 语义色。
- **技能库抛弃项**（不得引入）：Hero 落地页模式、红色品牌系、Atkinson Google 字体（禁在线字体）、GSAP（禁新依赖）、玻璃拟态/Bento/渐变装饰。

### 3.2 Token（双主题全量，照抄）

| Token | 亮色 | 暗色 | 用途 / 对比度目标 |
| --- | --- | --- | --- |
| `--bg` | `#F8FAFC` | `#0F172A` | 页面底 |
| `--surface` | `#FFFFFF` | `#1E293B` | 面、控件底 |
| `--surface-muted` | `#F1F5F9` | `#334155` | 悬停、次级填充 |
| `--text` | `#0F172A` | `#E2E8F0` | 正文（≥14:1） |
| `--text-muted` | `#475569` | `#94A3B8` | 辅助（≥7:1） |
| `--border` | `#E2E8F0` | `#334155` | 发丝分隔（装饰） |
| `--border-strong` | `#64748B` | `#94A3B8` | 可交互描边（≥3:1） |
| `--accent` | `#2563EB` | `#60A5FA` | 品牌重音/主按钮/链接 |
| `--accent-strong` | `#1D4ED8` | `#93C5FD` | hover/强调 |
| `--accent-soft` | `#EFF6FF` | `#172554` | 选中填充 |
| `--accent-text-on-soft` | `#1E40AF` | `#93C5FD` | 选中文字（≈8:1） |
| `--focus` | `#1D4ED8` | `#93C5FD` | 焦点环 |
| `--success` / `--success-soft` | `#047857` / `#ECFDF5` | `#34D399` / `#064E3B` | 仅状态 |
| `--warning` / `--warning-soft` | `#B45309` / `#FFFBEB` | `#FBBF24` / `#451A03` | 仅状态 |
| `--danger` / `--danger-soft` | `#DC2626` / `#FEF2F2` | `#F87171` / `#450A0A` | 仅危险/错误 |
| `--shadow` | `none` | `none` | 卡片无阴影 |
| `--shadow-raised` | `0 8px 24px rgb(15 23 42 / 14%)` | `0 8px 24px rgb(0 0 0 / 50%)` | 仅 dialog/toast |

规则：组件内禁裸色值；蓝色只用于品牌/交互/信息，绿橙红只用于状态语义；大面积填充仅限 `--bg`/`--surface`/`--surface-muted`/`--accent-soft`。

### 3.3 字体 / 间距 / 圆角 / 动效

```css
font-family: system-ui, "Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif;
--font-mono: ui-monospace, "Cascadia Mono", Consolas, monospace;
```

- 字阶：h1 1.25rem/700；分区标题 0.95rem/600；正文 0.93rem/1.6；**阅读正文 1.05rem/1.75、`max-width:68ch`**；辅助 0.84rem（全站最小，禁 <12px）；task id / 闭集 code 用 `--font-mono`。
- 间距 4px 基（4/8/12/16/24/32）；圆角：控件 6px、面板 8px、dialog/toast 10px。
- 动效：颜色/底 150ms ease-out；开合/toast 180ms；**目的地切换无动画**；`prefers-reduced-motion` 全禁。

## 4. 组件语言（去卡片化）

- **无框列表行**：`border-bottom: 1px var(--border)`；hover `--surface-muted`；激活 `--accent-soft` 底 + 左 2px 重音线 + `--accent-text-on-soft` 文字。
- **安静状态行**：6px 状态圆点（`::before`，色由 `data-state` 语义映射）+ 文字；**左色条仅用于 recovery/警示面板**。
- **按钮**：主=实心 `--accent`；标准=1px `--border-strong` 描边；次要/低频=无框文字按钮（hover 底色）；danger=红描边、确认态实心红。
- **分区**=标题+发丝顶线+留白，不装卡片；仅更新面板、dialog 等离散对象用面。
- 空状态=一句话+单一下一步按钮；六态（空/加载/成功/失败/需操作/禁用）可区分且不靠颜色独证。
- **浮层三件套**：右抽屉（scrim 点击关闭）、全屏覆盖页（Esc 返回）、popover 菜单（账户）；统一 `role="dialog"`+`aria-modal="true"`、打开焦点入内、Esc/关闭归还触发元素、Tab 圈闭、打开期间背景主区置 `inert`。

## 5. 新信息架构：两页 + 三浮层（"一张学习桌"）

进一步收敛：**只有"学习"和"课表"是页面；任务、搜索、设置都是浮层**。侧导航栏整体取消——两个页面用顶栏分段控件切换，把全部水平空间还给内容（这也是对"课程+学习合并"的彻底化：App 的默认态就是学习桌）。

```
┌ 顶栏 52px ──────────────────────────────────────────────────────┐
│ CourseLens   [ 学习 | 课表 ]      ⌘K 搜索   ●任务(3)   ◐主题   账户▾ │
└──────────────────────────────────────────────────────────────────┘
  页面1 学习（默认页）：选课 → 选讲次 → 学习桌面；App 即学习桌
  页面2 课表：周视图（浏览型参考面，需要整页宽度，故保留为页）
  浮层1 任务抽屉：右侧 400px；顶栏任务 chip 全局唤起
  浮层2 ⌘K 搜索：紧凑态=跳转/快搜面板 → 展开态=全屏结果+AI 回答
  浮层3 设置浮层：账户菜单进入的全屏覆盖页
```

**为什么这样分**（实现模型不得擅自加回更多页面或侧栏）：

- **学习**是唯一"长时间停留"的场景 → 独占默认页。
- **课表**回答"这周上什么课"，是整周浏览，周视图需要整页宽度与 parity → 第二页面；做成抽屉会毁掉周视图，故不并入浮层。
- **任务**是被动监控：发起任务后在原地得到"已提交"反馈，平时靠 chip 徽标一瞥，需要时才开抽屉 → 不是页面。**远程连接的配置类动作（GitHub 授权/Worker 初始化/修复/加密测试/设备授权轮询）从任务区移入设置浮层"网络与连接诊断"**——配置归设置、监控归抽屉；抽屉里只保留"连接异常 → 去设置"链接。
- **搜索**是随时唤起的检索 → ⌘K 两级浮层（紧凑跳转 + 展开阅读），不占页面位。
- **设置**是低频配置 → 账户菜单进入的覆盖页，不占页面位。

页面与浮层内容：

1. **学习（默认页，三态渐进披露）**
   - 未选课：单一空状态（"从选择课程开始" + 课程选择入口）。
   - 已选课未选讲次：左侧课程/讲次两级主-从列表（课程 300px 无框行 → 讲次列表）。
   - 已选讲次（学习桌面）：**播放器为舞台**（16:9、≤56vh），其下双栏——左=字幕阅读列（68ch、跟随高亮、行内书签），右=资料面板（笔记/资料/复习/课程关系/回顾，下划线 tabs）；直播入口与任务证据为播放器下缘的安静状态行；生成字幕/笔记动作在播放器动作条。
2. **课表**：桌面 7 列发丝网格，事件=soft 底+左重音线，冲突=左红线+`!` 字标；≤820px 纵向日程；原生日期输入与 ICS 导出保留。
3. **任务抽屉**（400px 右抽屉，全局）：任务行=名称+圆点状态+2px 真实数据进度条+证据次行+折叠技术详情；chip 徽标=活动任务数（有失败无活动时红点）；**抽屉开→SSE 订阅、关→断开**（生命周期纪律不变）；发起任务后原地"已提交，查看进度"提示可唤起抽屉。
4. **⌘K 搜索**（两级浮层）：紧凑态=输入即搜（目录跳转 + 全文结果前 8 条，纯前端对已加载目录做匹配，不新增 API）；"查看全部"→展开态=全屏浮层、68ch 阅读列、四态（索引未就绪/无结果/失败/有结果）、结果内"生成回答"。Esc 逐级退出（展开→紧凑→关闭），焦点圈闭、关闭归还触发按钮。
5. **设置浮层**（全屏覆盖，内容 1080px 居中）：分组=账户与隐私 / AI 与处理 / 网络与连接诊断（含设备授权流与原任务区的远程动作）/ 更新与帮助；v2 的删除二次确认、busy/失败恢复、隐私回读语义全部保留；更新面板为唯一突出面。

移动端（≤820px）：顶栏单行（字标+搜索按钮+任务 chip+主题+账户）；底部导航 3 项：**学习 | 课表 | 任务**；设置仍从账户菜单进入。

## 6. 新模块架构

### 6.1 模块清单

```
app.js            组合根：安装各模块、pagehide 反序清理（≤300 行、无 fetch）
api.js            ≈ 现 api-client.js 原样搬运（契约胶水，含 ERROR_MESSAGES）
store.js          ≈ 现 state.js 原样搬运
ui.js             $/toast/operationId/textElement/时间格式 + evidenceDetails 闭集文案映射（原样搬运，可增新键）+ 浮层基座（焦点圈闭/归还、Esc 栈、inert 管理）
shell.js          顶栏、两页切换（学习/课表）、主题、账户菜单、globalStatus、frontend-session 心跳
tasks-drawer.js   任务抽屉 + 顶栏 chip 徽标 + SSE 订阅生命周期
search-palette.js ⌘K 两级搜索浮层（紧凑/展开）
settings.js       设置覆盖页（含原任务区的远程连接动作与设备授权流，请求语义不变）
study.js          学习主场景：课程/讲次选择 + 播放器 + 字幕列 + 资料面板
player-core.js    媒体/直播回放、错误→闭集恢复码映射、进度保存、transcript-time 事件（行为搬运，见 6.3）
live-room.js      直播状态/进入/竞态防护（行为搬运）
timetable.js      课表页
```

### 6.2 ID 命名体系（新）

统一 `区域-元素` 小写连字符（如 `study-course-list`、`player-stage`、`task-list`、`settings-privacy-section`）；登录对话框、密码字段、隐私披露、诊断复制等安全关键元素的语义与属性按 §2.3 重建。`synthetic_shell_server.py` 不依赖元素 ID（只挂路由），无需改。

### 6.3 语义原样搬运清单（真实缺陷修复的沉淀，逐块搬运、只换 DOM 触点）

| 逻辑块 | 来源 | 必须保留的语义 |
| --- | --- | --- |
| 播放恢复状态机 | `player.js` | 9 个闭集恢复码及文案、错误码映射、恢复动作、live HLS 错误映射、恢复面板动作不泄漏 `error.message`/manifestPath |
| 直播竞态防护 | `live-room.js` | requestEpoch/isCurrentCourse、迟到的 status/grant/session 不得覆盖当前课程、失败闭集文案、cleanup 冻结 |
| 字幕加载与跟随 | `learning.js` 字幕部分 | epoch/controller 竞态防护、原子替换行、手动滚动 2.5s 抑制跟随、键盘滚动意图识别 |
| 书签动作确认 | `learning.js` 书签部分 | 后端确认才更新、epoch 校验、失败恢复文案、事件委托+清理 |
| 目录防抖动 | `catalog.js` | auth/catalog/lecture 指纹短路、焦点保持、`aria-busy` |
| 更新安全投影 | `settings.js` | `safeUpdateDiagnostics`/`updateErrorGuidance`/`updateRecoveryGuidance` 三函数与其闭集映射原样保留（现有 node 内嵌测试直接复用） |
| 远程连接动作 | `tasks.js` | diagnose/start-authorization/bootstrap/repair-worker/test-channel 的请求体、闭集结果文案与设备授权轮询语义原样保留；**仅入口移至设置浮层**；任务抽屉只显示连接总体状态+"去设置"链接 |

## 7. 测试重建协议（完全重设计的最大风险点）

### 7.1 `tests/test_frontend_workbench.py` 重写规则

- **安全断言逐条移植**（改 ID 不改语义）：密码输入类型与 autocomplete；`localStorage.setItem` 全站仅主题一处；路由仅 `/api/health|/api/v3/*`；无已下线功能 ID；更新诊断闭集与 node 内嵌三函数测试（可原样保留）；图标 vendored+LICENSE；a11y token（reduced-motion/forced-colors/:focus-visible/color-scheme/aspect-ratio）；移动端 toast 避让与顶栏双行语义（字符串按新 CSS 重写）。
- **结构断言按新设计重写**：两页切换（学习/课表）；任务抽屉、⌘K 搜索、设置浮层的 dialog 语义（`aria-modal`、焦点圈闭、Esc 逐级退出并归还）；抽屉开→SSE 连/关→断；任务 chip 徽标随活动数更新；学习主场景三态；ID 命名规范。
- **禁止**：删除安全类断言、放宽为子串模糊匹配、跳过 node 行为测试。

### 7.2 `tests/frontend_playback_recovery_behavior.mjs` 重写规则

现有全部行为场景在新模块上逐一等价重建（场景清单，缺一即未通过）：

- 直播 7 态文案/动作/按钮可见性；进入直播 grant→session 链；迟到的 status/grant/session 响应不覆盖当前 UI；grant 失败显示闭集文案且界面不出现原始失败文本/manifest 路径/错误码；cleanup 后一切冻结。
- 播放器：媒体错误码→恢复码映射与恢复动作按钮序列；`loadedmetadata/playing` 清除恢复面板；讲次切换清面板并换源；直播 HLS fatal 网络/媒体错误映射；live-play 后 store 讲次清空；cleanup 后冻结且进度/任务零调用。

### 7.3 必须原样通过的既有测试

`test_frontend_shell_security.py`（后端）、`test_documentation.py`（README 截图>10KB——完成后用新 UI 刷新 `docs/assets/readme/course-workspace.png`）、其余全部客户端与 Worker 套件。

## 8. 实施切片与验收（每片全绿再继续）

- **A 骨架**：token / 顶栏+两页切换 / 浮层基座（抽屉、⌘K 面板、设置覆盖页空壳，含焦点管理与 inert）/ 主题 + **先重写** `test_frontend_workbench.py` 新结构断言（安全断言此刻移植）。
- **B 学习主场景**：三态流、player-core/live-room 行为搬运 + **重写** `.mjs` 行为 harness。
- **C 学习内容**：字幕列、资料面板、书签（竞态语义搬运）。
- **D 任务抽屉 + ⌘K 搜索 + 课表页 + 设置浮层**：远程连接动作迁入设置（请求语义不变）；settings 三安全函数与其测试保留；删除确认/隐私回读语义保持；抽屉 SSE 生命周期接入。
- **E 收口**：4 视口 × 双主题、forced-colors、reduced-motion、44px、全量套件、README 截图刷新、更新 `docs/ui-design-system.md` 为 v3 实际值。

每片命令：

```powershell
.\.venv-client-py310\Scripts\python.exe -m pytest -q tests/test_frontend_workbench.py tests/test_frontend_shell_security.py tests/test_frontend_playback_recovery_behavior.py tests/test_documentation.py
node --check frontend\app.js
Get-ChildItem frontend\modules\*.js | ForEach-Object { node --check $_.FullName }
.\.venv-client-py310\Scripts\python.exe -m pytest -q
```

浏览器验收（`tests/synthetic_shell_server.py` 合成数据）：375/768/1024/1440 × 亮/暗——两页零意外横向滚动；Tab 序=视觉序、焦点环可见；抽屉/搜索/设置浮层焦点圈闭、Esc 逐级退出并归还触发元素；对比度复核；六态可区分；删除有确认、失败不显示已删除；⌘K 键盘导航完整。截图入临时目录不入库。

## 9. 明确禁止项

不引入框架/在线字体/CDN/图标包/动画库；不用 emoji 作图标；不新增营销 hero 或"首页"；不改 API v3 与后端；不在视觉层伪造任何状态；不删测试、不放宽断言、不吞异常来让测试变绿；不动 `runtime/`；不修改 `synthetic_shell_server.py` 的服务行为（仅当确需时可同步元素选择器，须在报告中说明）。
