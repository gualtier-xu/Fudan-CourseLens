# 学生工作台后续执行交接

## 前置、调度与边界

本合同只覆盖课表、字幕能力、任务抽屉、本地保存语义和单一搜索五项。先读根 `AGENTS.md`、`docs/handoff.md` 与当前相关 migration/stabilization/usability/auth handoff。做 fresh preflight：`git status --short`、目标调用链、测试与 diff/hash。若 `fudan-login-stability` 或其他写任务仍运行、或工作树持续漂移，只做只读调查，不写；等其终态后重建基线。所有既有改动均属用户，绝不 `reset`/`stash`/`restore`。

主代理按单一 worker 顺序切片；凭据语义或真实课程访问前需 risk_auditor；完成后 reviewer。禁止读/写 `runtime/data`、public/managed，禁止真实任务/取消、Git add/commit/push/PR/release/Secrets，禁止输出账号/API key/课程正文。

先追踪实际调用链再定最小 allowlist。预期前端候选为 `frontend/index.html`、`player-core.js`、`study.js`、`search-palette.js`、`tasks-drawer.js`、`settings.js`、`timetable.js` 与必要 styles；只有证据证明需要时才扩到 `src` 的精确 application/runtime timetable/catalog/credentials 文件及对应 tests。

## 当前代码观察（执行时 fresh 验证）

- `catalog_payload_invalid` fallback 在 `application.py`，属于课程目录，不等于 timetable。
- timetable 首次 load 读本地 snapshot，显式 refresh 才抓源。
- ASR（2026-09-14 取代）：唯一内部 `automatic` 策略——配置 DeepSeek Key 时为双引擎+AI 校对，未配置时自动走非 AI 回退（Paraformer 精修）；旧模式标签不再是任何 live 载荷、规则或测试的一部分。
- remember 值已有事件绑定，但 DeepSeek `remember=false` 当前会删除持久 key。
- 顶栏 Ctrl K 是全局 palette；catalog query 是课程过滤，二者用途不同。

## 1. 课表

分别追踪 catalog refresh 与 timetable snapshot/refresh 两链。复现时只记录闭集 code、content-type、redirect 和 schema shape；不记录课程值/正文。修真实根因（schema 漂移、SSO handoff、HTML 授权页、或前端未呈现 `partial_failures`），禁止将空态当成功。

首次 timetable load 保持本地 snapshot；只有显式刷新抓源。前端显示具体课表来源失败与下一步，并与课程目录错误分开。先用合成 fixture；真实只读课表刷新需本任务内 fresh 用户 GO + risk gate、复用现有会话，且禁止 runtime/data、截图和课程内容输出。

## 2. 字幕能力自适应

用户只看到一个统一入口，不选择任何模式。（2026-09-14 取代：该段描述的旧能力语义已由唯一 `automatic` 策略实现——配置 DeepSeek Key 时为双引擎+AI 校对，未配置时自动走非 AI 回退（Paraformer 精修），任务卡在回退时如实标注“无大模型校对（自动降级）”，不称失败或低质量。）

> **2026-09-14 取代（COMPUTE-GUARD-RETENTION-1 scope expansion）**：产品未发布，历史兼容不再保留——上一段所述旧模式内部兼容、协议枚举保留与“旧 mode”测试已全部移除；live 契约只有唯一 `automatic` 策略（行为依 DeepSeek Key 自动选择）。

## 3. 任务抽屉

根因是按课程分组后仍平铺 failed/canceled 与重复元信息。改为三层：进行中（展开、最高优先级）、需要处理（失败、紧凑可操作）、历史记录（completed+canceled 默认折叠）。层内保留精确 `course_id` 分组与未知桶，并按 `updated` 倒序。

每项主行只显示任务类型、状态、关键进度；课程/讲次/时间为次级小字；失败给一句可操作文案；技术详情折叠。不要重复“任务失败·失败”、无意义进度或掩盖可重试失败。失败多时可最多展示最近项，但必须显示总数和展开入口。保持既有关闭、焦点、EventSource、overlay 合同。验收 1440/1000/375 亮暗、零横溢、44px 触达。

## 4. 本地保存

账号和 DeepSeek 的“加密保存”文案改为“保存在本机”或“本地保存”，旁注“使用 Windows 加密保护，仅当前 Windows 用户可用”。绝不改为明文、`localStorage` 或浏览器存储；DPAPI 与账户隔离保持。

修“按了没效果”：checkbox 仍随登录/保存提交，但给清晰 helper 与保存后 inline 状态：已保存在本机 / 仅本次启动使用 / 保存失败。以 accounts API 的 `saved` 布尔为证据。统一语义：unchecked 仅会话使用，**不得**静默删除既有持久 DeepSeek key；删除仅由显式“移除本机保存”完成。账号既有未勾选不删旧保存语义保持。测试空 key、busy、成功、失败、刷新后状态；实施前 risk gate，任何凭据值不得输出。

## 5. 单一搜索

顶栏 Ctrl K 是唯一可见全局 palette；页面内 catalog-query 仅是课程过滤。移除课程选择页第二输入框，把课程/教师检索纳入同一 palette 的课程结果组或课程范围动作：紧凑结果 ≤8、可进入完整结果，保留 auth/index 门禁、键盘、Esc 逐级、焦点归还、abort/sequence、单浮层。不得新建搜索页或路由。

## 验证、视觉与报告

先确认测试离线/合成 fixture、无真实外联，再运行 changed-module `node --check`、三项相关 mjs harness、相关 pytest（timetable、credentials、identity settings、unified task center、worker ASR）与全量 client/worker。已知环境失败每次 fresh 复核，不能硬编码。合成浏览器验收五项状态与 1440/1000/375 亮暗；无横向溢出。视觉保持 Academic ivory/navy/gold、Times New Roman+SimSun，不重设计。

停止：需要真实课表刷新但无 fresh GO/risk PASS；需要凭据/真实课程；需要 worker/protocol/public 发布授权；需要超范围文件而无根因；或无法保留脏基线。

最终报告：五项根因与结果、文件和最小性、离线测试/截图、真实状态（未运行或脱敏）、reviewer、风险/未验证项与 `git diff`/status。
