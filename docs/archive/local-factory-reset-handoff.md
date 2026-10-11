# CourseLens 本地恢复出厂交接合同

Last updated: 2026-09-07

## 0. 本合同的权限边界

本文件只授权低级模型进行**只读盘点、设计重置清单并向用户申请逐项确认**。它本身不授权删除、移动、覆盖、Git 写入、GitHub 写入、真实登录、真实任务、取消任务、Secrets 变更、发布或重新固定镜像。

目标不是“一条命令清空所有东西”，而是把下列互不等价的状态分开处理：

1. CourseLens 应用数据与会话；
2. 本机保存的复旦账号和 DeepSeek Key；
3. 可重建的缓存、日志、报告和遥测；
4. 本地源码、未提交改动与 Git 历史；
5. GitHub 上的仓库、Actions、Worker/Mailbox、PR 与审计记录。

任何一类只能在用户针对该类给出 fresh GO 后执行。一个 GO 不得推导为其他类的授权。

## 1. 开工前必须完整读取

- 工作区根 `AGENTS.md`；
- `docs/handoff.md`（动态状态入口）；
- `docs/project-overview.md`；
- `docs/local-data-recovery.md`；
- 与账户、凭据、任务和清理有关的当前源码及测试。

先执行 fresh、只读 preflight：

- `git -C private/main status --short --branch`；
- 记录 `HEAD`、`origin/main`、二者 ahead/behind；
- 列出各仓库和 worktree，但不修改；
- 只通过代码、配置和公开 API 确认数据根与删除语义；
- 检查本地服务是否运行，以及本地与远端是否存在 active/queued/running/paused/leased/canceling/downloading 任务；`unknown`、`orphaned` 或查询失败均不算“已经归零”，不得为“清空”而自动取消真实任务。

2026-09-07 的已知起点仅供核对，不可当作新事实：`HEAD=f597b36`，`origin/main=76e8c85`，本地领先 2 个提交，工作树约为 27 个 tracked modified + 27 个 untracked。若 fresh preflight 不同，以 fresh 结果为准。

## 2. 状态面与默认处置

### 2.1 应用持久数据

当前文档表明：

- `runtime/data/state.db`：课程目录、课表、任务、operation、run、lease 等；
- `runtime/data/learning.db`：字幕、文档、搜索、对齐、测验、复习和本地分析；
- `runtime/data/credentials.json`：平台保护后的凭据密文；
- `runtime/cache`、`runtime/logs`、`runtime/reports`：可重建缓存、脱敏日志和验收证据；媒体和 migration backup 必须作为不同子项列出，不能随数据库一起默认删除。

不得在 preflight 中直接读取 `runtime/data` 的数据库、行、凭据或课程内容。只允许从代码和文件系统元数据确认精确路径、存在性、数量与大小；输出不得包含账号、课程名、URL、Cookie、Token、API Key 或正文。

### 2.2 本机外部凭据与驱动

`C:\Users\admin\.courselens-secrets\` 同时可能包含 DPAPI store、真实验收驱动和 keepalive 工具。默认**全部保留**，不得递归删除该目录，不得读取或输出任何值。

若用户要删除本机保存的账号或 DeepSeek Key，优先使用产品已有的 logout / delete-saved-credentials / delete-deepseek 操作，并以 API 返回的 `saved/configured` 布尔状态验收。删除某个已保存秘密不代表可以删除整个外部工具目录。

### 2.3 恢复快照

`docs/handoff.md` 记录的两个 sidecar recovery snapshot 为 retain-only；`private/runtime-convergence` 为 frozen。它们不属于普通应用恢复出厂范围，未经单独点名授权不得读取、移动或删除。

### 2.4 源码与本地 Git

默认不改源码和 Git。以下选择结果完全不同，模型不得代选：

- A（推荐）：只清应用状态，保留当前源码、2 个本地提交和全部未提交工作；
- B：先把当前产品工作正式收口/提交，再从批准的 commit 新建干净 clone；
- C：把现目录回退到指定 commit/ref；这是不可逆地丢弃本地工作的独立破坏性操作；
- D：删除整个本地 clone 后重拉；同样需要确认仓库、分支、未提交文件和外部证据是否都已安全保留。

禁止使用 `git reset --hard`、`git clean -fdx`、`git checkout --`、`git restore`、`stash` 或删除 clone，除非用户在看到 exact diff/文件清单后明确批准精确目标和命令。

### 2.5 其他仓库与 GitHub

- `public/worker-mirror` 只读；
- `managed/personal-worker` 与 `managed/mailbox` 是生成/托管状态，不得手工编辑；
- 本合同不授权修改 GitHub 仓库、Actions、Artifacts、PR、Issues、Releases、分支、Secrets、GitHub App、Worker、Mailbox 或签名 pin。

## 3. Phase 1：只读盘点与提案

模型必须先交付一张不含秘密的 reset manifest，至少列出：

| 范围 | 精确目标 | 当前状态（仅计数/大小） | 推荐动作 | 是否可恢复 | 所需 GO |
| --- | --- | --- | --- | --- | --- |
| 应用数据库 | 经源码确认的 data root | 不读内容 | 暂停服务后隔离 | 是 | GO-DATA |
| 保存凭据 | 产品删除 API | 仅 saved/configured | 逐账号/逐 key 删除 | 通常否 | GO-CREDS |
| 缓存/日志/报告 | 精确目录 | 数量/大小 | 清理或保留证据 | 视选择 | GO-CACHE |
| 活动任务 | 只读 API | 状态计数 | 必须先自然结束；取消另需 GO | 不适用 | GO-TASK |
| 本地源码 | exact Git status | 文件数/diff stat | 默认保留 | 是/否 | GO-GIT |
| GitHub | 只读资源清单 | 数量/状态 | 本轮不处理 | 不适用 | 未来独立 GO |

盘点报告只能出现在聊天中；不要把包含本机状态的中间 patch、数据库副本或凭据副本写入仓库。不得为了盘点安装依赖、启动真实登录或访问真实课程。

然后停止，向用户逐项询问：

1. 是否清空应用数据库和媒体/字幕历史；
2. 是否删除本机保存的复旦账号；
3. 是否删除本机保存的 DeepSeek Key；
4. 是否清理 cache/logs/reports/登录遥测；
5. 是否保留外部 secrets 目录中的验收驱动与 DPAPI store（推荐保留）；
6. 是否只重置应用，还是还要处理源码；若处理源码，必须给出 exact commit/ref；
7. 是否先做可恢复隔离，验收后再永久删除（推荐）。

没有完整答案时不得进入 Phase 2。

## 4. Phase 2：可恢复的本地重置（仅在对应 GO 后）

由 `risk_auditor` 对 exact targets、活动任务、凭据语义和恢复路径做 fresh gate。PASS 后才可执行：

1. 正常停止 CourseLens 服务并确认相关进程已退出；
2. 通过只读 API 确认没有 active/queued/paused/leased 任务；若有，停止并报告，不得隐式取消；
3. 把批准的应用数据目标原子移动到用户批准的、访问受限、位于工作区和活动数据根之外的时间戳隔离目录；不要复制或打印内容；
4. 对“只删凭据而不整体隔离 data root”的选择，使用产品已有动作注销会话并删除用户明确点名的保存凭据；整体隔离 data root 时不得再打开或改写隔离副本；
5. 重新启动客户端，验证它进入首次使用/未登录状态；
6. 不自动登录、不刷新真实课程、不派发任务；
7. 报告隔离目录、可恢复性和仍保留的范围，随后停止。

Windows 文件操作必须在 PowerShell 中端到端完成，使用已解析且逐个核验的绝对路径和 `-LiteralPath`。禁止广域目标、glob、`~`、`$HOME`、环境变量拼接、跨 shell 删除或递归清理工作区根。

## 5. Phase 3：永久删除（第二次独立 GO）

只有用户在首次使用状态验收通过后再次明确批准，才可永久删除隔离目录。执行前再次打印 exact target、大小、SHA/清单摘要和“删除后不可恢复”，再由 `risk_auditor` 复核。

永久删除不得顺带处理源码、外部 secrets、sidecar snapshot、managed/public 仓库或任何 GitHub 资源。

## 6. 验收标准

- 客户端显示首次使用/未登录状态；
- `saved=false`、`configured=false`（按用户批准的账号/key 范围）；
- 课程、课表、字幕、书签、搜索索引、任务历史均为空；
- 无活动或排队远程任务，未触发取消；
- 重启一次后仍为空；
- 源码工作树与 preflight 一致，除非用户另行批准 GO-GIT；
- `public/`、`managed/`、sidecar snapshots 和 GitHub 均未修改；
- 报告仅含路径、计数、布尔、状态码和哈希，不含秘密或课程内容。

## 7. 停止条件

遇到任一情况立即停止并报告：目标路径无法精确解析；路径越出批准根；服务或任务无法静止；凭据删除语义不确定；工作树与用户批准的基线不符；需要取消真实任务；需要 Git/GitHub 写入；需要触碰 retain-only/frozen 状态；risk gate 非 PASS。

## 8. 关于未来“清除全部 PR 记录”

本轮不实现。建议不要为视觉整洁重写或删除当前开发仓的审计历史。更稳妥的发布收口是：

1. 保留现有私有开发仓及 PR/Actions 作为审计档案；
2. 在首个测试版验收后，从批准的 release snapshot 创建新的干净发布仓，只放一个初始快照提交；
3. 在新仓重新配置最小分支保护、Actions environment、GitHub App、Secrets 和发布权限；
4. 重新建立签名镜像 pin、通道测试与客户端信任链后再切换；
5. 旧仓改为私有只读/归档，而不是销毁。

普通 Git 历史重写不能可靠抹去 GitHub 的 PR 引用；`refs/pull/*` 为 GitHub 只读，旧 clone/fork/cache 也可能保留对象，同时会改变 commit SHA、破坏签名和 PR diff。只有发生真实秘密泄露时，才应先轮换/撤销秘密，再按 GitHub 敏感数据清除流程与 Support 协作。Actions run/artifact 可以单独永久删除，但仍是未来独立、不可逆的远程写入门禁。

## 9. 最终报告模板

- fresh preflight（HEAD/origin、dirty counts、服务与任务计数）；
- 用户逐项 GO 原文与 exact scope；
- 实际移动/删除目标（路径、计数、大小，不含内容）；
- 凭据布尔状态与是否可恢复；
- 首次使用状态和重启验证；
- 未触碰项（源码、public/managed、snapshots、GitHub）；
- risk_auditor 结论；
- 遗留风险和下一门禁。
