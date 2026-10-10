# Academic UI 并行批次收口审计合同

## 目的、边界与来源

本合同只授权对 Academic UI stabilization 与 usability/auth followup 两个并行批次做**只读收口审计**。先完整阅读根 `AGENTS.md`、`docs/handoff.md`、`docs/ui-academic-editorial-stabilization-handoff.md`、`docs/ui-usability-auth-followup-handoff.md`，以及两批最终报告/证据索引。若报告未落盘，只能将用户消息视为声明并标记 `unavailable`；不得猜测、补写或把声明提升为证据。

禁止修改产品、测试、文档、`runtime/data`、`public/`、`managed/`；禁止真实网络、认证、任务、GitHub、发布、Secrets、取消、删除及任何 Git 写操作（包括 `add`、`commit`、`stash`、`reset`、`restore`、`checkout`、`push`、PR）。全部现有改动均属用户。唯一可选写入是 Git 忽略的 `runtime/cache/ui-parallel-batches-closeout-audit-20260906/audit-report.json` 和 `audit-report.md`，且仅脱敏审计报告、仅在必要时、必须用 `apply_patch`；也可不创建而只最终回复。

## Fresh preflight 与基线

在 `private/main` 运行并保留脱敏输出：

```powershell
git status --short
git diff --name-status
git diff --numstat
git diff --stat
git diff --check
```

建立逐路径 preflight 基线：状态、name-status、numstat、是否命中两批 allowlist、重叠批次、相关测试。以路径和 diff 内容判断来源/归属；18/19 tracked、8/10/13 untracked 等计数漂移必须逐路径解释。mtime 只可辅助排序，绝不是作者或批次归属证据。

## 审计检查表

### 1. Diff、allowlist 与组合行为

- 将最终 diff 映射到每批 allowlist，列出未归属、超范围、重叠文件。
- 对重叠文件检查重复实现、冲突、死代码、过期注释和相互覆盖的行为/测试。
- 审查组合后的任务抽屉、palette/overlay、字标返回、字幕书签、登录提交/轮询/晚到响应、密码显隐；不能以单批报告替代最终代码/测试证据。
- 对每一项标记 `verified`、`unverified` 或 `conflict`，并给出精确路径/测试依据。

### 2. 测试组合态

`641 pass / 6 skip` 只能是组合态结论，不能伪称任一批次独立结论。在运行任何 pytest（尤其全量）前，先以测试标记、代码和既有约定确认其只使用离线或合成 fixture、没有真实外联；不能确认则不运行，标记 `unavailable`，禁止借测试突破真实网络边界。通过门禁后独立运行：changed-module Node check、两个 `.mjs` harness、两批定向 pytest 集和全量 pytest；记录每个命令的结果、已知环境失败与新增失败的区别。不要把一个批次的结果归因给另一个。

建议 PowerShell 验证矩阵：

```powershell
$modules = @('frontend/modules/search-palette.js','frontend/modules/tasks-drawer.js','frontend/modules/shell.js','frontend/modules/ui.js','frontend/modules/player-core.js','frontend/modules/study.js','frontend/modules/timetable.js','frontend/modules/settings.js')
$modules | ForEach-Object { node --check $_ }
node --test tests\frontend_playback_recovery_behavior.mjs tests\frontend_academic_ui_behavior.mjs
.\.venv-client-py310\Scripts\python.exe -B -m pytest -q tests\test_frontend_workbench.py tests\test_frontend_shell_security.py tests\test_frontend_playback_recovery_behavior.py tests\test_timetable.py tests\test_unified_task_center.py tests\test_remote_compute_ui.py tests\test_documentation.py tests\test_text_encoding.py
.\.venv-client-py310\Scripts\python.exe -B -m pytest -q
```

若路径不存在或命令环境不可用，如实标 `unavailable`；不安装依赖、不改环境。

### 3. 运行时、认证与脱敏 artifact

禁止读取或写入 `runtime/data`。仅从报告、代码合同和 Git 忽略的脱敏 artifact 得出结论，并明确区分“agent 直接访问运行时数据”（禁止）与“客户端真实登录正常产生的运行时写入”（不是 agent 人工写入，但仍不能读取其原始内容）。

凭据默认完全不碰；只能检查允许的非敏感状态。不得读取、打印、复制账号/密码/API key/token/Cookie/DPAPI 内容，不得运行 `set-deepseek`、登录、授权、断开、撤销或任何凭据变更。对“`remember=false` 删除既有 DeepSeek key、外部 store 可恢复”仅审计证据链；不能安全确认恢复时标记“需用户确认”，绝不自行恢复。

真实认证 artifact schema/脱敏性只能读取明确位于 `runtime/cache` 的报告文件。先以路径、大小和非敏感文本扫描确认；不打开截图中的真实数据。发现可能泄密即停止该读取和审计，不在输出中回显可疑内容。

### 4. 提交拆分建议

只提出建议，不执行 Git 操作。若重叠代码能可靠分离，建议产品代码、测试、文档分别提交；若两个批次在同一文件不可可靠归因，建议“一个产品代码提交 + 一个测试提交 + 一个文档提交”，或单一原子提交，并写明追溯风险。不得用 `git patch` 逆向重写工作树以制造拆分。

## Findings、结论与停止条件

分级：P0 = 秘密/数据泄露、破坏性或安全边界；P1 = 认证/任务/overlay 等核心合同破坏；P2 = 可访问性、回归或显著可用性缺口；P3 = 文档、可维护性或建议性跟进。结论只能是 `ACCEPT`、`ACCEPT_WITH_FOLLOWUPS`、`REJECT`；任一未关闭 P0/P1 必须为 `REJECT`。`ACCEPT_WITH_FOLLOWUPS` 仅适用于已明确接受的 P2/P3 与已写明的剩余风险。

立即停止：发现泄密迹象、需要访问 `runtime/data` 或真实网络/认证、需要任何外部或破坏性写入、无法安全读取 artifact、或用户基线受到意外改变。输出已完成的只读证据与停止原因。

## 报告模板

1. 来源可用性：两批报告/证据索引的路径、`verified`/`unavailable`；
2. preflight：完整状态摘要、路径基线与计数漂移解释；
3. allowlist/重叠矩阵：来源、冲突/死代码/覆盖、结论；
4. 组合行为与测试矩阵：命令、结果、环境与新增失败；
5. runtime/凭据/artifact 边界：只读证据、未验证项、是否安全停止；
6. P0–P3 findings、提交拆分建议、最终 `ACCEPT`/`ACCEPT_WITH_FOLLOWUPS`/`REJECT`。
