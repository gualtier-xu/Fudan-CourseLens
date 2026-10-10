# 统一个人 Worker 与公开 Release 承载迁移交接合同

Last updated: 2026-09-07

> **2026-09-30 定稿注记（RR-DOC-2）**：官方发布仓定稿为 `gualtier-xu/Fudan-CourseLens-Worker`；本文正文的 `gualtier-xu-co/...` 均为迁移历程旧称，沿革见 `docs/namespace-migration-ledger.md`，发布面以现行为准。

## 0. 本轮裁决与目标

本文件是本轮唯一执行合同。用户已经作出两项产品架构裁决：

1. **所有用户完全一致**：开发者账号也使用自己的公开
   `Fudan-CourseLens-Worker` 与私有 `Fudan-CourseLens-Mailbox`，不再把
   `gualtier-xu-co/Fudan-CourseLens-Worker` 当作真实课程任务执行器；
2. 客户端安装包、签名 manifest、SHA256 与公开发布说明今后放到
   `gualtier-xu-co/Fudan-CourseLens-Worker` 的 GitHub Releases；停止把
   `gualtier-xu-co/Fudan-CourseLens-Releases` 作为长期分发仓库。

目标终态：

| 仓库 | 可见性 | 终态职责 |
| --- | --- | --- |
| `gualtier-xu-co/Fudan-CourseLens-Private` | private | 客户端、Worker 源码、协议、发布控制面的唯一可编辑源 |
| `gualtier-xu-co/Fudan-CourseLens-Worker` | public | 签名 Worker 模板、公开文档与客户端 GitHub Release 资产；**不执行任何用户真实课程任务** |
| `OWNER/Fudan-CourseLens-Worker` | public | 每位用户自己的 GitHub Actions 执行器；开发者账号无特例 |
| `OWNER/Fudan-CourseLens-Mailbox` | private | 每位用户自己的密封任务/结果信箱 |
| `gualtier-xu-co/Fudan-CourseLens-Releases` | private | 新公开分发链验收和观察期后归档；删除不在本轮范围 |

普通用户拥有两个 CourseLens 仓库；公共模板/产品门户只需公开读取，不属于用户
GitHub App 安装的任务仓库。开发者账号额外拥有 Private 和公共模板，因此最终有四个
现役仓库；旧 Releases 先归档保留回退证据，不删除。

本轮不合并 Git 历史，不把二进制提交到 `main`，不删除任何仓库，不擦除 PR、Actions、
Release 或审计历史，不重新设计协议，不新建服务或数据库。

## 1. 必读、来源优先级与工作区并发门禁

开始前完整读取：

- 工作区根 `AGENTS.md`；
- 当前 `docs/handoff.md`；
- 本合同；
- `docs/adr/0004-signed-client-updates.md`；
- `docs/adr/0005-signed-template-direct-worker.md`；
- `docs/signed-template-direct-migration.md`、`docs/process-canary.md`；
- `docs/client-update-operations.md`、`docs/technical/README.md`；
- `docs/worker-budget-eta-integration-handoff.md` 及当时已经完成的相关报告；
- `src/remote/{github_app,connection,worker_migration}.py`；
- `src/update/service.py`、`config/client-update-trust.json`；
- `.github/workflows/{worker-mirror-release,client-release}.yml`（按实际文件名读取）；
- `scripts/{signed_template_direct_preflight,finalize_signed_template_direct,verify_github_process_canary,bootstrap_asset_host,check_client_release_gates}.py`；
- 上述模块的全部调用方和测试。

若本合同与过期 handoff 的动态事实冲突，以 fresh 证据为准；若与安全边界冲突，按更
严格者执行。只允许编辑 `private/main`。`public/worker-mirror` 是 CI 生成镜像，
`managed/personal-worker` 与 `managed/mailbox` 是客户端托管状态，禁止手工修改。
不得人工读取、移动或修改 `runtime/data`，不得读取或回显任何 Secret/凭据值。

### 1.1 其他任务仍在写入时不得开工

当前工作区已有大量用户改动，且 `worker-budget-eta`、课表/字幕/直播、onboarding 等任务
可能仍在其他对话中运行。全部视为用户所有，不得 reset、clean、stash、restore、
checkout、覆盖或用旧 patch 回放。

先做只读盘点；满足以下条件后才能写产品文件：

1. 工具可见的其他写任务已完成；无法观察的并行对话由 fresh 文件变化判定；
2. 对 `git status --short`、目标文件大小/mtime/SHA256 连续取三次快照，每次间隔 30 秒，
   三次完全一致；
3. 工作区没有未结束的构建、测试或本地服务正在改写目标文件；
4. 明确记录当前 HEAD、origin/main、ahead/behind、tracked/untracked 清单和本轮 allowlist；
5. 仅指定一个 write-capable worker 顺序实施，不并行写同一仓库。

快照只记录路径和摘要，不生成整仓 patch 副本。若仍在漂移，继续只读调查和等待；不得
通过抢先提交来“固定”基线。

## 2. 已核实的起点与必须 fresh 重验的事实

2026-09-07 只读调查看到：

- `private/main` HEAD 为 `f597b36`，origin/main 为 `76e8c85`，且工作树高度 dirty；
- `gualtier-xu-co/Fudan-CourseLens-Worker` 为公开、未归档模板仓库；
- `gualtier-xu-co/Fudan-CourseLens-Worker-Worker` 为公开、已归档仓库；
- `gualtier-xu-co/Fudan-CourseLens-Releases` 为私有、未归档且当时无默认分支；
- 对公共仓库和 Releases 仓库的 release 只读枚举当时未返回条目；
- 当前公共签名模板 pin 在旧 handoff 中记录为 commit `4e8f161`、tree `e34f82a`；
- `src/remote/github_app.py` 仍含 owner-only `signed-template-direct` 分支，并把 App 安装
  仓库硬编码为模板、Worker、Mailbox 三仓；
- 客户端更新配置/服务/测试/runbook 仍硬编码
  `gualtier-xu-co/Fudan-CourseLens-Releases`，生产更新仍 fail closed；
- 旧 personal Worker 的旧密钥组合与本地回退检查点已终结，取消归档本身不足以恢复。

这些都是会漂移的快照。任何 GitHub 写入前必须重新只读验证：仓库可见性/归档状态、
默认分支、当前模板 pin/tree、Actions 与任务零活动、Release/tag/assets 清单、App 安装
selection、环境名称与 **Secret 名称集合（只列名，不读值）**、本地绑定布尔状态。

### 2.1 已完成且不得重做/覆盖的并行增量

两批此前未提供的工作现已报告完成且代码已出现在共享工作树中。把其当前实现和测试视为
用户所有的已接受基线，不得当成迁移清理对象：

1. **首次使用分步引导**：普通 `data-page="onboarding"` 页面复用
   `student_onboarding_guide` app state 与幂等的
   `POST /api/v3/onboarding/actions`，可跳过、重开并由真实后端快照驱动。personal Worker
   迁移必须复用这一引导，不得新建第二套 wizard/readiness/localStorage。GitHub 步骤最终
   只解释 Worker + Mailbox 的精确安装范围；公开 Release 下载不得显示为 App 授权要求。
2. **课表、字幕 overlay 与合成直播**：课表刷新已保留来源级 `partial_failures`；Chromium
   字幕由产品自己的 `#player-subtitle-overlay` 渲染；纯直播学习桌由
   `store.livePlayback` 保持；合成 shell 已有本地 VTT/media/HLS fixture。本迁移不得重新
   设计这些功能，必须保留对应执行式测试。

两份报告中的 client 726/728 测试计数只代表各自检查点，不是当前基线。收到报告后的
fresh 只读快照已变为 **36 tracked modified + 40 untracked**，说明工作树仍在变化。因此
§1.1 稳定门禁继续生效，不得用旧报告的 72 行 status 或测试计数作为开工依据。

已知测试卫生边界：强杀 synthetic shell 可能留下媒体临时文件。必须使用 fixture 的优雅
关闭/清理路径并复跑现有 acceptance audit；不得用广域删除 cache/media 来收口。

## 3. 统一 personal-worker 的设计合同

### 3.1 不变量

- `gualtier-xu-co/Fudan-CourseLens-Worker` 只作为受签名 pin 保护的模板和公开发布门户；客户端永不
  向其 dispatch 真实课程 workflow；
- 每个账号只向 `ACCOUNT/Fudan-CourseLens-Worker` 派发，并只通过
  `ACCOUNT/Fudan-CourseLens-Mailbox` 交换密封载荷；
- 所有账号走同一 `personal-worker` 状态机、错误闭集、UI 文案、保护额度和 ETA 语义；
- 不允许 owner 名称、仓库 ID 或 template ownership 触发 direct 特例；
- Worker 完整树必须由当前签名模板重建/修复，并验证 pin、tree、工作流、trust epoch；
- Worker/Mailbox 的密钥、installation ID、repo ID、owner 必须成套绑定，切换原子且可回滚；
- 切换期间 `remote_enabled=0`，不得出现 personal/direct 双派发或静默 fallback；
- 公共模板仓库不得保存个人执行密钥，也不得具有可执行真实任务的有效 Secret 组合。

### 3.2 最小代码方向

先追踪端到端调用后再改。优先复用现有 `personal-worker` 创建、repair、密封通道、pin
校验和 process canary；不要另写第二套 bootstrap。

1. 删除或封闭 owner-only direct 选择条件，使所有新安装、重登、重启和恢复都选择
   `personal-worker`；旧本地 direct 状态应被识别为“需要迁移”，不能继续 dispatch；
2. 保留公共模板的签名/pin 校验；把“模板可信”与“模板可执行任务”彻底分开；
3. 将安装仓库契约从硬编码三仓改为账号自己的 Worker + Mailbox。**不要直接把常量 3
   改成 2 就结束**：必须先画出现有 OAuth/App installation/bootstrap 的顺序，处理目标
   仓库尚未创建时的鸡生蛋问题；
4. GitHub 官方 add/remove installation repository API 不接受 GitHub App user access
   token 或 installation token，只接受 classic PAT。不得新增 PAT、不得扩大为“所有仓库”
   来伪造全自动。若平台限制确实需要一次用户选择，应把它变成最短、可恢复、可验证的
   安装步骤，并在 onboarding 中只要求精确勾选 Worker + Mailbox；
5. 旧 direct migration/preflight/finalize 工具不得误报成功。可最小改为只读识别、反向
   迁移/退役提示，或由新的明确脚本替代；不要维护两套活跃迁移框架；
6. `connection`、任务派发、process canary、保护额度来源和前端 GitHub 状态全部读取当前
   personal Worker，不能继续把模板仓库当 executor；
7. 新增 ADR（建议 `0006-uniform-personal-worker-and-public-releases.md`）记录裁决，并把
   ADR 0005 标记为 superseded；历史 ADR 不得伪装成从未发生。

### 3.3 开发者 Worker 反向迁移

以下是高风险外写序列，必须由 `risk_auditor` 在 fresh 零活动证据后放行：

1. 确认本地任务、Mailbox 消息、远程 workflow/run/job、结果导入、租约和清理全部为零；
   unknown 不等于 zero；
2. 取消归档 `gualtier-xu-co/Fudan-CourseLens-Worker-Worker`；归档仓库只读，GitHub 官方也要求先
   unarchive 才能修改；
3. 不信任旧树，按当前签名模板 commit/tree 做完整 repair/rebuild 并复验；
4. 生成全新的密封/签名密钥组合。值只能在受控进程内流转，禁止进入命令行、日志、
   shell history、截图、报告或仓库；
5. 仅向 personal Worker 的受保护 Environment 写入所需的新 Secret；先核对名称集合，
   不读旧值；
6. 在本地 DPAPI 状态中原子写入新的 Worker/Mailbox/repo/installation/public-key 绑定，
   `remote_enabled` 仍保持关闭；
7. 执行加密通道测试，确保 `legacy_channel_test → channel_test_required → ready` 重新闭环；
8. 开启 remote 后只做一条最小非破坏性 smoke，再做 process canary；验证运行仓库必为
   `gualtier-xu-co/Fudan-CourseLens-Worker-Worker`，公共模板仓库同期零真实任务 run；
9. 重启客户端、重新登录，并用恢复出厂/新安装 synthetic 流证明 owner 与普通用户选择
   同一模式；
10. 观察一个明确窗口后，再经独立 gate 清理公共模板上已失效的 direct 执行 Secret 和
    临时 DPAPI 回退检查点。若无法证明 personal Worker 稳定，关闭 remote 并回滚绑定，
    不允许 fallback 双跑。

真实取消、批量课程、课程数据读取、历史清理和仓库删除不属于验证手段。任何真实 smoke
只能用会话内注入、`remember=false`、fresh operation_id；不得回显凭据或课程内容。

## 4. 将客户端 Release 承载合并到公共仓库

### 4.1 保留的信任边界

公开下载不等于可信。必须保留并分别验证：

- 离线 root key → online release key → 签名 manifest；
- manifest 中的版本、channel、package SHA256、大小和反回滚状态；
- Windows Authenticode/受管安装器校验；
- Worker 模板签名/pin 与客户端发布签名使用不同根、不同 Environment、不同工作流；
- 客户端发布只能由 private monorepo 的受保护工作流产生并发布，不得让公共仓库自行
  构建私有客户端，不得复用学生 GitHub App token 或 Worker Secret；
- 公开仓库的源代码分支和 Release 资产是两个发布面，二进制只进入 Release assets。

建议受保护 Environment 分工：

- `worker-mirror-release`：仅发布签名模板；
- `client-release-production`：仅签名并发布客户端资产；
- 每个个人 Worker 自己的运行 Environment：仅课程任务运行密钥。

不要只改一个仓库字符串，也不要用 `PRIVATE_ASSET_AUTH_IMPLEMENTED=True` 绕过门禁。

### 4.2 最小实现

1. 把 `config/client-update-trust.json`、`src/update/service.py`、release gate、asset-host
   bootstrap、workflow、runbook、测试和文档统一指向
   `gualtier-xu-co/Fudan-CourseLens-Worker`；
2. 将 private-repository/private-asset-auth 特有门禁替换为 public GitHub Release
   discovery/download 门禁；公共读取不得要求用户把模板仓库加入 App installation，
   也不得要求客户端保存 GitHub 下载 token；
3. 继续默认 `enabled=false`，直至 root/release key、manifest、Authenticode、installer、
   clean-machine rollback 全部通过。迁移仓库地址不能顺手开启生产自动更新；
4. 版本标签分命名空间：客户端使用 `client-v<semver>`；Worker 继续使用签名 commit pin/
   trust epoch，不与客户端共享裸 `vX.Y.Z`；
5. 发布流程为 draft → 上传全部资产 → 独立验证 manifest/SHA/signature/Authenticode →
   publish。仓库支持时启用 immutable releases；发布后不得替换资产；
6. 使用专用、最小权限的 publisher 身份向公共仓库写 Release。先证明当前 GitHub App/
   workflow 权限模型是否足够；不得扩大面向学生的 App 权限；
7. 不合并两个仓库的 Git 历史；旧 Releases 若 fresh 仍为空，无需迁移资产。若出现任何
   tag/release/asset，停止并重新制定兼容清单。

### 4.3 下载重定向是安全必测项

GitHub Release asset 下载可能经过重定向，而现有 updater 据旧 ADR 会拒绝重定向。先对
公开的非敏感测试资产做只读网络观测，记录 host、协议、跳数和状态，不记录 token/query。
实现最小的逐跳验证：

- 起点只能是精确 GitHub API/repository allowlist；
- 只允许 HTTPS/443、有限跳数、无 userinfo、无 fragment；
- 每一跳重新解析并拒绝 loopback、私网、link-local 和非全局地址；
- 目标 host 必须来自基于 fresh 官方行为建立的精确 allowlist，不能 `*.github.com` 泛放；
- 不把 Authorization 转发到不同 host；公共资产默认匿名下载；
- 最终仍以签名 manifest、SHA256、大小和 Authenticode 判定，不信任 URL 本身。

若无法建立稳定而封闭的下载策略，保持 updater disabled；可以公开发布手工下载的 beta，
但不得宣称自动更新已投产。

## 5. 两条迁移的执行顺序

不得并行迁移。推荐顺序：

1. 等其他工作区任务完成，冻结 fresh dirty 基线；
2. **离线实现**统一 personal-worker，跑定向/全量测试并 reviewer；
3. **外写 Gate W1**：提交、push、PR、CI、合并后，执行开发者 Worker 反向迁移；
4. 观察并证明公共模板零真实任务，personal Worker/Mailbox 稳定；
5. **离线实现**公共 Release host，保留 updater disabled，跑安全负例与全量测试；
6. **外写 Gate R1**：提交、push、PR、CI、合并；准备 public release dry-run；
7. 等用户给出首个测试版的确切 version/channel/distribution 裁决后，才创建并发布真正的
   `client-v...` Release；无该裁决时停在“代码与工作流 ready、updater disabled”；
8. clean-machine/Hyper-V 安装、升级、签名失败、下载失败、回滚验收；
9. 观察没有消费者访问旧 Releases 后，归档 `Fudan-CourseLens-Releases`；删除必须另行 GO；
10. 刷新 `docs/handoff.md` 为唯一动态状态入口，并明确所有用户拓扑一致。

如果现有 worker-budget/ETA 增量已经落地，必须把额度来源改为当前账号的 personal Worker，
并验证 owner 不再读公共模板的 run；不得回退或覆盖其共享 SSE、预算或 ETA 改动。

## 6. 授权矩阵与强制暂停点

本合同记录的是用户已经批准的**架构方向与迁移实施准备**。执行模型仍须遵守下表：

| 动作 | 本合同处理方式 |
| --- | --- |
| 本地代码/测试/持续文档修改 | 工作区稳定后允许 |
| commit/push/PR/merge | fresh diff、reviewer、risk gate、CI 后允许；禁止 force 和改写历史 |
| 取消归档旧 Worker | W1 risk gate 后允许 |
| 修复 Worker 树、轮换其运行 Secret、更新本地 DPAPI 绑定 | W1 risk gate 后允许；值绝不落盘到报告 |
| 最小通道 test/smoke/process canary | W1 risk gate 后允许；不得真实取消或批量测试 |
| 修改 GitHub App registration 权限或改成 all repositories | **未授权，必须停下请示** |
| 创建/发布真实客户端 Release、启用 production updater | 缺 version/channel/发布裁决，**必须停下请示** |
| 归档旧 Releases | 新公开版本完整验收与观察后允许 |
| 删除任何仓库、历史、tag、Release、Actions、PR | **未授权，必须另行 GO** |
| 清理 public/managed/runtime/data/凭据 | **未授权** |

GitHub App 安装的仓库选择若无法通过现有授权安全完成，不得索取 classic PAT 或扩大权限；
输出一个可点击的 GitHub 安装设置步骤并暂停等待用户完成，然后继续验证。

## 7. 验收矩阵

### 7.1 personal-worker

至少覆盖：

- owner 与非 owner 都返回 `personal-worker`；public template 永不成为 dispatch target；
- 新安装、既有 direct 状态升级、重启、重新登录、断线重接、恢复出厂；
- onboarding 首次自动打开、跳过/重开/完成、invalid persistence 与 legacy-user 行为；
  GitHub 步骤只依据后端事实，不自行捏造 ready；
- Worker/Mailbox 缺失、已存在、归档、改名、额外仓库被选、installation scope 不精确；
- pin/tree/signature/trust epoch 不符时 fail closed；repair 后恢复；
- channel test required、旧 operation_id、重复 enqueue、重复结果、runner failure；
- 同一任务只有一个 personal Worker run；公共模板同期零真实 task run；
- 保护额度、ETA、任务抽屉和连接状态从 personal Worker 获取且不重复订阅；
- Secret 名称完整、值级扫描零泄露；公共模板没有有效个人执行 Secret 组合。

### 7.2 public releases

至少覆盖：

- 正确 draft/release/tag/channel/manifest/package 成功；
- 错误签名、错误 root/release key、SHA/大小不符、包截断、版本降级、重复版本、channel
  混用、manifest 重放全部 fail closed；
- 公开匿名 discovery/download，不请求学生安装 token；
- 0/1/多跳 redirect、跨 host auth stripping、HTTP downgrade、userinfo、fragment、私网/
  loopback/DNS rebinding、超跳数全部有负例；
- immutable publish 后资产/标签不可替换的流程检查；
- `Fudan-CourseLens` 模板分支 pin 不因客户端 tag/release 被误选；
- updater disabled 与 production enabled 两种配置的门禁差异；
- Windows clean machine 安装、旧版升级、重启、离线、坏包、签名失败、回滚；
- 首次安装只引导用户创建/授权个人 Worker + Mailbox，不出现 Releases 仓库授权。
- onboarding 的 completed/dismissed 继续幂等；Release host 改址不能让旧用户意外重开
  引导，除非明确升级 guide version 并有迁移测试。

运行 `node --check`、相关 node harness、定向 pytest、client 全量、Worker 全量、markdown/
encoding/link 检查。涉及签名、密钥、GitHub、发布、迁移和真实 canary 的里程碑必须分别由
`risk_auditor` 审核；组合态完成后由 reviewer 审核。P0/P1 未关闭不得外写或合并。

同时运行现有 onboarding-guide、timetable、playback-recovery、usability/auth 与 live
synthetic harness。即使迁移无意修改对应模块，这些已接受的 DOM/API 合同也是回归门禁。

## 8. 回滚

### 8.1 Worker

- 切换前建立**仅 DPAPI 加密、带时限**的 direct 绑定回退检查点；报告只写存在性/hash；
- personal Worker 任一门禁失败：立刻 `remote_enabled=0`，停止新派发，保留终态导入，回滚
  到上一组完整绑定；不得同时启用两个 executor；
- 观察期通过后再独立 gate 删除回退检查点和公共模板 direct Secret；
- 无法证明零活动时不得切换或回滚。

### 8.2 Release

- 在首个公开版本前 updater 保持 disabled，回滚即恢复 disabled 配置；
- 已发布 immutable 版本不得替换，问题通过新版本修复；
- 旧 Releases 仓库在观察期只读保留，不作为自动 fallback；private asset auth 本来未实现，
  不得把它描述为可靠回滚链；
- 新公开链失败时停止更新发布，不删除公共资产、不删旧仓库、不修改历史。

## 9. 停止条件与交付报告

出现以下任一情况立即停止相应高风险阶段并报告：

- 工作区仍在漂移或无法唯一归属改动；
- active/queued/cleanup/unknown 任一不能证明为零；
- App 安装必须扩大到 all repositories、索取 PAT 或新增账户级权限；
- Secret 值进入输出，或无法证明 public template 不可执行真实任务；
- public release 下载需要放宽到通配 host/任意重定向；
- 签名/Authenticode/反回滚/clean-machine 任一失败；
- 首个发布 version/channel 未决；
- reviewer/risk_auditor 给出未关闭 P0/P1。

最终报告必须分开写“代码已实现”“GitHub 外写已执行”“真实验证已执行”和“尚待用户
门禁”，并包含：fresh/终态 Git 状态、实际修改文件、PR/commit/run/release 标识、Worker
与 Mailbox 仓库角色、App 安装实际人工步骤、测试结果、脱敏证据位置、回滚状态、旧
Releases 是否仍存在/是否归档。不得声称未执行的发布或真实验证已完成。

## 10. 官方依据

- GitHub 归档仓库为只读，修改前必须取消归档：
  <https://docs.github.com/en/repositories/archiving-a-github-repository/archiving-repositories>
- App 安装 add/remove repository API 的 token 限制：
  <https://docs.github.com/en/rest/apps/installations>
- GitHub Releases 用于发布二进制资产：
  <https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases>
- immutable release 推荐 draft → 上传资产 → publish，发布后资产和 tag 受保护：
  <https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases>
