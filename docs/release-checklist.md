# CourseLens 发布就绪清单（Runbook 骨架）

面向发布日执行者。当前项目状态是受控试用、非公开生产发布；本清单假设的目标是 2026-10 国庆窗口的干净仓重建发布（删除现有个人仓库、以无历史新仓重新初始化）。**每一节都是先只读核对、再动手**；任何一步验证不过就停在那里，不要跳步。

## 0. 灾备快照（删库/重初始化之前必做，不可跳过）

1. 冻结写入窗口：确认没有在途执行会话、在飞 GitHub Actions 与未导入的云端任务（任务中心清零，或显式记录哪些任务将放弃）。
2. 本地数据快照：整包备份 `runtime/data`（含 `artifacts/`、任务库、学习记录库与日志）。这是唯一的学生数据真值。
3. 凭据说明（不是拷贝）：DPAPI 加密文件绑定当前 Windows 用户，换机不可解密；快照只需记录「哪些账号保存过凭据、哪天需要重新登录」，不要把密文当成可恢复备份宣传。
4. 仓库快照：对 `Fudan-CourseLens`（公开镜像）、`Fudan-CourseLens-Private`（主开发仓）、`Fudan-CourseLens-Worker`（co 仓）各打一个归档（`git bundle` 或 `git clone --mirror` 到离线介质），并记录三个仓当时的 HEAD 哈希。
5. 信任物料快照：导出最近一次发布的 `manifest.json`、签名、`root`/`trust`/`trust.sig` 与 `allowlist` 五件套，离线保存（回滚与事故排查都要用）。
6. 快照自检：在另一台目录里用 bundle 恢复一次 `Fudan-CourseLens-Private` 并跑 `git log -1`，确认快照可用。

## 1. 仓库名与渠道 URL 引用清单（发布前必须逐项裁决）

用户拍板约束：**渠道 URL 禁止硬编码当前仓名**。~~当前源码树仍以 `gualtier-xu/<仓>` 硬编码~~ → 2026-09-28 夜批10-B 曾全量迁移至中间过渡账号（历史见 `docs/namespace-migration-ledger.md`）→ **2026-09-30 用户拍板定稿：官方发布仓=`gualtier-xu/Fudan-CourseLens-Worker`**，客户端发布面已按此翻转（RR-FACE-1 T1，`src/update/service.py` `DISTRIBUTION_REPOSITORY`）；**2026-10-02 改名定稿（WINIT-1 案1）：官方发布仓=`gualtier-xu/Fudan-CourseLens-Worker-Release`**（`-Release` 后缀标记发布门户角色；GitHub 侧 Rename 与 reclaim 归 PKG-WINIT1-C 外写硬门；过渡期旧名直链经 GitHub 转发存活，reclaim 后死链）；下列分布为迁移历程存档：

**2026-10-04 起定稿=总仓 `gualtier-xu/Fudan-CourseLens`（REBUILD-9 重指；此后 R2/R5 两轮同名删仓重建仓名不变、id 更替，2026-10-09 晨 R6 第三轮同名删仓重建预排在册——下方 -Release 定稿行与迁移历程均为历史存档）**，与 update-chain-values.md §1.1 互链；发布日勾「渠道清单」以本行为准。

**运行时/发布链（高危，改错会断更新或镜像）：**

| 文件 | 引用数 | 内容 |
| --- | --- | --- |
| `src/update/service.py` | 2 | `DISTRIBUTION_REPOSITORY`（:62）——**更新渠道 URL 的根**，releases/download 前缀由它拼出；2026-09-30 起定稿值为 `gualtier-xu/Fudan-CourseLens-Worker` |
| `scripts/build_client_update.py` | 1 | 更新包构建（PKG1 闭集文件，改动须走闭集门） |
| `scripts/release_worker_mirror.py` | 2 | Worker 镜像发布目标仓 |
| `scripts/check_client_release_gates.py` | 1 | 发布门校验 |
| `scripts/configure_github_actions.py` | 2 | Actions 配置目标仓 |
| `scripts/bootstrap_asset_host.py` | 3 | 资产托管初始化 |
| `scripts/register_github_app.py` | 2 | GitHub App 注册清单 |

**文档面（低危，随发布一并更新）：** `docs/client-update-operations.md`（7）、`docs/handoff.md`（2）、`docs/project-overview.md`（1）、`docs/technical/README.md`（1）、`docs/namespace-migration-ledger.md`、`docs/adr/0004`、`docs/adr/0006` 等历史记录按「历史材料」原则不回改，只在新文档里声明新仓名。

裁决项（发布日勾选）：
- [x] **官方发布仓定稿（2026-09-30 用户拍板）**：`gualtier-xu/Fudan-CourseLens-Worker`；客户端发布面已按此翻转（RR-FACE-1 T1，`src/update/service.py` `DISTRIBUTION_REPOSITORY`）
- [x] **官方发布仓改名（2026-10-02 用户拍板，WINIT-1 案1）**：`gualtier-xu/Fudan-CourseLens-Worker` → `gualtier-xu/Fudan-CourseLens-Worker-Release`；钉更新已同 commit 落齐（PKG-WINIT1-A，checker 四处恒等点），桥接发布归 PKG-WINIT1-B，GitHub 侧 Rename+reclaim 归 PKG-WINIT1-C（外写硬门）
- [ ] 其余两件仓名（私有主仓 / Worker 源仓）随干净重建定稿
- [ ] `DISTRIBUTION_REPOSITORY` 改为参数化（配置或环境注入）而非字面量
- [ ] 上述 scripts 全量 grep 复查归零（历史文档除外）
- [ ] 受管安装脚本（`install_managed_client.ps1` / `start_managed_courselens.ps1`，当前 0 引用）保持零仓名耦合

## 2. 镜像链 / 信任三件 / co 同步（重走初始化）

干净重建后，镜像与信任链按既有配方重走一遍，顺序固定：

1. 私有主仓推送到新 `Fudan-CourseLens-Private`，确认 `main` HEAD。
2. 运行镜像导出+发布（`release_worker_mirror.py` 链路）：生成 manifest（payload 文件清单+`allowlist_sha256`+protocol 版本）、Ed25519 签名（`document_sha256`）与 trust 三件（`root`/`trust`/`trust.sig`）。
3. 公共镜像仓 `Fudan-CourseLens-Worker` 接收 CI 签名产物并合并 PR；**七项核验全绿才算上镜**（先例=REPUBLISH-6：载荷逐文件对账 ADDED/REMOVED/CHANGED、OUTSIDE_MIRROR_DOMAIN 空、PAYLOAD_INTEGRITY 空、allowlist/protocol 恒等、`document_sha256` 在签名文档/本地导出/公共仓三方逐字节恒等、PIN-READBACK 全对）。
4. 客户端侧「一键修复 Worker」→ co 仓 sync 到镜像 HEAD，实跑一次真任务验证链路。
5. 更新渠道自检：新 `DISTRIBUTION_REPOSITORY` 下发一个空更新清单试运行，确认客户端 update 链 fail-closed 行为正常（验签不过必须拒绝）。

## 3. 发布日 Runbook 骨架

- [ ] **发布外写次序门（winit1b 先例，2026-10-02 定条款）**：发布外写（GitHub release / 更新链发布）必须是流程**最后一步**；执行前逐项核对门控前提——信任基座仓改名、镜像钉恒等、清单恒等、更新链 rehearse 冒烟全绿——**任一未核对即不得发布**（教训：client-v0.1.1 发布先于门控前提核对，发布面已指改名后仓名而 GitHub 侧改名未执行，信任门 404 窗口）。
- [ ] **门禁**：正典全量（客户端 pytest）0 失败 + `node --test tests/*.mjs` 全绿 + workbench 全绿 + worker 套件 0 失败（worker 全量用 `.venv-worker` 正典环境跑，口径、命令与基线见 worker 技术 README「本地验证」的测试环境家规；客户端 venv 跑 worker 全量会产生环境假失败，2026-10-02 实测 23 例）；`final_acceptance_audit`（PKG1）通过。
- [ ] **红线自证**：`git status --porcelain` 只含预期增量；13 件 PKG1 闭集与信任边界文件与已验收版本恒等。
- [ ] **真实账号走查**：一键启动→首跑向导→复旦登录→选课→播放/字幕→AI 处理→复习，用「第一次使用的复旦学生」视角过一遍（既有方法论：两轮活体终验）。
- [ ] **第 1 节渠道清单**全部勾完。
- [ ] **第 2 节镜像链**七项全绿+co sync 实跑通过。
- [ ] **安装包**：Inno 构建出包、沙箱装机 exit 0、布局与信任校验通过（先例基线：09-30 17:04 重建件 setup.exe ≈23.1MB，SHA256 入档；**以在盘复算件为准**，更早的 ≈29.8MB 基线已过期；构建细则=`docs/packaging-chain.md`）。
- [ ] **安装包边界话术**：发布页/公告/文档写明「setup.exe 仅用于**首次安装**；版本升级一律走产品内更新通道，不要重复跑 setup.exe」（实证与复演=§3.1）。
- [ ] **文档**：README 三步开始/安全与隐私架构/遇到问题与当期行为一致；`docs/getting-started.md` 与页面文案抽查一致；本清单勾选记录归档。
- [ ] **发布后**：观察首个真实任务的完整闭环（派发→runner→回传→导入→复习可见）；把「真实云端链路在线验收」从待办转为已完成并记录证据。

### 3.1 setup.exe 仅首次安装——升级一律走产品内更新通道（N9-A 实证 + N9-A2 复演）

**为什么**：在已有安装的机器上静默重跑 setup.exe，Inno 外壳会报 exit 0「成功」，但装后步骤
`install_managed_client.ps1` 实际 **exit 1**（受管根已存在且非空，触达 `InstallRoot must not
already exist` 守卫）——versions 槽与信任清单**不会刷新**。同版本重装看似无害；若把新版本的
setup.exe 当升级包用，就是**静默假成功**：壳换了、受管更新链还停在旧版本。

**三断言（N9-A 2026-09-25 02:18 沙箱实证）**：①Inno 静默重装 exit 0；②安装日志
`Process exit code: 1`（install_managed_client.ps1 被 `InstallRoot must not already exist`
守卫拒绝）；③受管根 `versions/` 仍只有 0.1.0、`state/current.json` 未动。

**复演**（N9-A2 2026-09-25 沙箱升级演练，0.1.0 装机后盖装 0.2.0 构建）：结果见
`archive/.../top-model-results/product-night9-lane-a2-pkg-finalize-20260925.md` U6 节——
三断言在新版本盖装场景逐条复现。

**正确姿势**：日常升级=客户端内更新通道（manifest→验签→versions 槽→pending/apply/confirm/
rollback，fail-closed）；setup.exe 只在「机器上还没有 CourseLens」时使用。若未来需要
「setup.exe 升级」语义，须给 `install_managed_client.ps1`（PKG1）增补 upgrade 分支=独立授权包。

## 4. 回滚

任何一步失败：停止发布、保留现场、用第 0 节快照恢复；客户端更新链本身 fail-closed，验签不过的清单不会被拉起，所以「渠道指向错误」的最坏结果是用户收不到更新，而不是装到坏包。

## 5. 2026-09-25 夜批后状态（REPUBLISH-9 收官时点，N9-C 记录）

### 5.1 R9 终态（P65 上镜，2026-09-25 02:16）

- 源=private/main `67fe98a`（P65 重登无 lck 变体 + R8 钉前移载体，恰 5 路径）；**本地预演三级验签 PASS**（72 文件、CHANGED 恰 4、元数据五件 root/trust/trust.sig BYTE-IDENTICAL、allowlist 零漂移、cat-file 72/72）。
- 公共仓 **PR#15 squash MERGED**（run 36037078988 success；checks 5/5：boundary/gitleaks/mirror-policy/protocol/unit；diff 恰 6 文件）。新公共三值：commit **7700057** / tree **93cdb364** / document_sha256 **02ee6234**（三方恒等复算过）。
- 钉前移完成：active=7700057/93cdb364/02ee6234，previous=c99e2625/0d2e5e1e/aee7051e（verbatim），r3/epoch1/["2"] 零轮换；PIN-READBACK 全对、重建门 2 passed、`release/` 与 root_keys 未动。镜像冒烟 432 passed/4 skipped/0F（anaconda 正典口径）。
- 信任三件自 R8 起零轮换；`previous` 即当前回滚锚。

### 5.2 第 1 节 78 处引用计数复核（2026-09-25 实测）

- 口径闭合：src 5 + scripts 16 + docs 52 + config 2 + frontend 1 + runtime-assets.json 2 = **78**（`git grep -I gualtier-xu` 全仓 146，另有 tests 53 与 .github 8 属测试/CI 面，不计入 78 口径）。
- 头号裁决项不变：`src/update/service.py:62 DISTRIBUTION_REPOSITORY`（运行时渠道 URL 之根）。
- runtime-assets.json 的 2 处在 R9 pin 前移中保持 repository=官方 Worker 仓未变（仓名随 §1 迁移历程走；2026-09-30 定稿、2026-10-02 改名（WINIT-1 案1）后以 `gualtier-xu/Fudan-CourseLens-Worker-Release` 为准，最终值由 RR-REBUILD-1 重建时落定）。

### 5.3 国庆干净重建前必办排序（建议，供拍板）

1. 新仓名三件定稿（用户拍板）→ §1 全清单参数化迁移，`DISTRIBUTION_REPOSITORY` 优先。
2. §0 灾备快照六步（删库前不可跳）。
3. 镜像链重走初始化（§2 步骤 2-3，配方=本文件 §5.1 记录的 R9 实绩路径）。
4. 信任三件重建+首个 release key 签发（r3 → 新纪元）。
5. co 仓 sync+客户端一键修复实跑（§2 步骤 4-5）。
6. 源 ref 治理：私仓现存 15 个 `agent/release-src-*` 与 backup 分支在重建时自然消亡，无需迁移。

## 6. 文档面发布就绪 D1（RR-DOC-1，2026-09-30）

本节记录发布材料 D1 稿的完成面（作者=RR-DOC-1 执行会话，详单见其结果文件）；D2 优化轮与发布日实填项另行勾选。

已完成（D1）：

- [x] `README.md` 全量重写为发布版：三步开始（下载→SHA-256 校验→安装→复旦统一身份登录）/功能总览（录播字幕、AI 总结与笔记、学习洞察、提问、解释、课程记忆）/Windows 安全提示（如实说明暂无代码签名+校验单+更新清单签名校验）/安全与隐私架构主章（新增「学生机零模型零本地推理」）/卸载与你的数据/官方仓 Issues 反馈入口；README 断言测试约束保持（含「三步开始」与技术 README 分离）。
- [x] `docs/release-notes-0100-draft.md`：仓名定稿落笔；「保持自动更新即可」旧句改为「应用内自动检查+手动确认安装（默认不静默）」（与 2026-09-30 拍板一致）；发布页附件清单=setup.exe+校验单+SmartScreen 指引（D2 定稿：发布资产恒为三件=`CourseLens-0.1.0-setup.exe`+《校验单》+`windows-security-prompt.md`；对外材料一律用固定 tag `releases/download/client-v0.1.0/` 资产直链，不写仓库相对路径）。
- [x] `docs/release-announcement-draft.md`：发布页/Issues 链接按定稿仓书写；安装小贴士补签名如实说明与指引页引用；待发布日实填项收窄为资产直链/二维码/渠道清单。
- [x] `docs/getting-started.md`：首跑流程安装版化（开始菜单打开客户端，登录在 GitHub 授权之前，与五步引导同序）；F4 两处修复（控制条 1.5s 自隐含暂停、悬停控制条不隐藏——9a799ac；旗标悬停菜单「删除」钮补记）。
- [x] `docs/privacy-notice.md` v1.1：新增零模型声明；外联主机表述与 README 闭集表同口径；补卸载保留数据句与官方仓 Issues 入口。
- [x] `docs/project-overview.md`：发布仓定稿名与 0.1.0 更新模式现状句（auto-check + manual confirm）。
- [x] `docs/changelog.md`：0.1.0 标注「首个公开发布版本，日期以官方发布页为准」。
- [x] 新增 `docs/release/windows-security-prompt.md`：SmartScreen/SAC 图文指引单页（certutil 校验步骤在内；截图位标注发布日实拍）。
- [x] 签名事实层口径（按批板契约）：全部对外材料只写「无代码签名——SHA-256 校验单+更新清单签名校验+SmartScreen 引导」，不引用内部 gate 名。

发布日实填/勾选（不属 D1）：

- [x] release notes 日期实填（2026-10-01，RR-PUB-1）；release 资产三件已上传（二维码未做，待定）。
- [x] 发布资产名 ASCII 定稿（RR-PUB-1，2026-10-01）：《校验单》发布名=`CourseLens-0.1.0-checksum.txt`——GitHub 资产名服务端剥除非 ASCII 字符（gh CLI 与 REST percent-encoded 两式上传均落地为 `CourseLens-0.1.0-.txt`，RR-PUB-1 实证）；本地生成文件名与生成器不变，仅发布资产与对外直链用 ASCII 名（README/notes/worker README 三处直链已同笔换名）。
- [x] 三件直链实测与哈希亲核（PUB1-RESUME-1，2026-10-01）：`CourseLens-0.1.0-setup.exe` HTTP 200（24,315,172B，实算 SHA-256=`e1ac6f1d…e33ec`，与《校验单》/release body 恒等，正典件未重传）+《校验单》HTTP 200（432B，SHA-256 行恒等）+`windows-security-prompt.md` HTTP 200（3,620B），三件均经 release-assets.githubusercontent.com 浏览器语义重定向；release body=定稿 notes 逐字节核验（ASCII 直链+发布日 2026-10-01+实哈希）。
- [x] 更新链冒烟口径落档（PUB1-RESUME-1，2026-10-01）：本地端到端演练 PASS（HEAD=a5ed0ba 干净构建，正链×2〔默认+override 通道〕check→download→SHA256→install→健康确认全过+回滚支+负分支×3 闭集码全符，76.5s）；真链现状=信任观察态（本机受管安装 trust enabled:false、root_keys 空）+stable manifest URL 404→客户端 fail-closed 无更新活动，零异常；真链 E2E 冒烟留待「生产 GO 翻转」独立授权包（RELPREP-1 任务4 P0/P1 分层）。
- [ ] §17 末三项（发布页附《隐私与数据说明》链接、校验单随发布页、「先校验再安装」一句）——②校验单随发布页与③「先校验再安装」句已随 0.1.0 body 落地（PUB1-RESUME-1 核验）；①《隐私与数据说明》链接上发布页待裁决，故整项保持未勾。
- [ ] 指引页三处截图位实拍回填。
- [ ] 若把 README 全文或渲染版贴上发布页/仓库首页，须连同 `docs/assets/readme/course-workspace.png` 一并上传并把图片引用改为直链（D2 已清除 README 的仓库相对文档链接，仅剩这张图随发布方式落定）。

每次发版站立项（N14-W2 追加，2026-10-02）：

- [x] `docs/release/windows-security-prompt.md` 内的版本号与 `certutil`/《校验单》文件名随新版本同步更新后再上传资产（~~0.1.1 发布随附的该资产内容口径是否已随版更新待核验~~ → 已核验：v0.1.1 资产 3,726B 确为 0.1.1 版口径；0.1.0 换装将随单重传仓内现值件 e2dfb52b/3,725B，比线上 0.1.0 旧资产多 ASCII 校验单名+改名后 Issues 链两处修正，见 §7）；README「三步开始」的 setup/校验单/安全指引三处直链与「当前版本」随 tag 同步——漏改会被 `tests/test_security_doc_contract.py` 的版本钉拦下。同步点自动化方案（一键脚本提案，待裁决）见[《README 版本同步方案稿》](release-version-sync-proposal.md)。

## 7. RELEASEPREP-010：0.1.0 定稿换装执行单（2026-10-04 备妥，止步只差外写）

> 背景：公开发布定版 0.1.0（2026-10-03 用户拍板）——首发（10-01）之后的全部学生
> 可感知增量收编进同版号重发布，`client-v0.1.0` 发布资产整体换装为 2026-10-04
> 构建。本节由 N15-W2 执行会话备妥；**一切 GitHub 外写=明晨用户裁决后才执行**。
> 构建基线=冻结提交 `7ce66a7f50e791be83ebc5cd7660930609989598`（tree
> `e98919c3…`；含 82de7d8 enqueue 闭集修复与 7ce66a7 llm.yml 队列观测）。

### 7.0 门控前提核对（§3 次序门，五项全过）

1. **信任基座仓改名已落地** ✓ WINIT1-C 2026-10-02 18:38 五点验证；本包回环取证 client-v0.1.0 资产直链全部经改名后仓名应答。
2. **镜像钉恒等** ✓ `public/worker-mirror` 已 fetch+detach 至钉代 `1296e926`（tree `dd58d841`），`test_public_template_reconstruction` 2 passed；本次构建 zip 内 `runtime-assets.json` 钉三点=1296e926/dd58d841/34778133 与 `fe3b6a0` 钉块恒等（previous=fafbbf06 环）。
3. **清单恒等** ✓ 信任策略 `config/client-update-trust.json` 本包零触碰；沙箱装机 trust 面在位（安装段 PASS）。
4. **更新链 rehearse 冒烟全绿** ✓ REHEARSAL-OK（正链×2〔默认+override〕+回滚支+负支×3 闭集码全符，12 requests）；`check_client_release_gates.py` 双模式 release_allowed=true；`check_distribution_references.py` 138 文件零漂移。
5. **notes 与构建件版本一致** ✓ `courselens-version.json`=iss AppVersion=setup 文件名=manifest version=notes 正文=0.1.0；test_security_doc_contract 10P+docs 四件套 48P+链接检查 624 文件 PASS。

### 7.1 换装资产清单（五件已构建，零上传）

| # | 发布资产名 | 旧值（现 online，2026-10-04 回环取证） | 新值（rebuild7-20261004） |
| --- | --- | --- | --- |
| 1 | `CourseLens-0.1.0-setup.exe` | `a4ef6037…` 24,352,092B（rebuild6 代） | **`640fb3a7…` 24,363,498B**（00:48:26+0800） |
| 2 | `CourseLens-0.1.0-checksum.txt` | `62de9e2a…` 432B | **`c95b0185…` 432B**（本地名 `CourseLens-0.1.0-校验单.txt`，上传前改 ASCII 名——RR-PUB-1 先例：GitHub 剥非 ASCII） |
| 3 | `courselens-0.1.0-windows-x86_64.zip` | `4f83e7db…` 1,274,779B | **`2b6f0384…` 1,288,198B**（135 件） |
| 4 | `courselens-windows-manifest.json` | `04dd3513…` 1,887B（rebuild6-20261002） | **`21151d79…` 4,800B**（rebuild7-20261004；source=7ce66a7/e98919c3；窗口至 2026-10-17T16:49Z；签名 release-2026-10-go1/epoch1 零新生成） |
| 5 | `windows-security-prompt.md` | `2bfa7dd9…` 3,620B（含旧仓名 Issues 链+中文校验单名两处旧口径） | **`e2dfb52b…` 3,725B**（仓内现值=ASCII 校验单名+改名后 Issues 链） |

不触碰：`courselens-windows-trust.json`（2,079B `ebc24d0c…`，信任根资产零变化）；
release tag `client-v0.1.0` 本身。构建工件留在 `private/main/.tmp-releaseprep010/out/`
（`freeze/output/installer/` 有 setup+校验单原件），**上传前按本表哈希逐件复核再传**。

### 7.2 操作次序（gh CLI，用户授权窗内执行）

1. `gh auth status` 确认 gualtier-xu 身份；在干净目录按 §7.1 哈希复核五件工件。
2. 校验单改名：`cp "CourseLens-0.1.0-校验单.txt" CourseLens-0.1.0-checksum.txt`（发布资产名恒 ASCII）。
3. 五件一次 clobber 上传（避免半态窗口）：`gh release upload client-v0.1.0 <五件> --repo gualtier-xu/Fudan-CourseLens --clobber`（clobber 换 asset id 属预期）。
4. **latest 指向翻转（本单关键步骤，漏做=更新链仍指 0.1.1）**：现 `releases/latest` 解析到 client-v0.1.1（2026-10-02 发布为 Latest），换装 client-v0.1.0 不会自动改指——`gh release edit client-v0.1.1 --repo gualtier-xu/Fudan-CourseLens --prerelease=true`（可逆、资产保留；删除 release 归用户另议，本单不做）。翻转后 `releases/latest/download/courselens-windows-manifest.json` 即落 rebuild7 manifest。

   > **R6 起（2026-10-09）本步不可对 mac release 复用（RELEASEREADY-AUDIT 关键发现，断链级护栏）**：本步的「latest 翻转」是 0.1.1→0.1.0 时代的单向操作（把旧 client-v0.1.1 打成 prerelease，使 latest 落回 client-v0.1.0）。R6 加入 mac prerelease 后方向相反：mac release 必须 `prerelease=true` 且**不得触碰 latest**，`releases/latest` 必须继续解析 `client-v0.1.0` 的 Windows manifest。若对 mac release 复用本步（或让 mac release 抢到 latest），`releases/latest/download/courselens-windows-manifest.json` 即 404→更新链断。mac tag/资产纪律与全链语义=update-chain-values.md §2.16（R6 预稿）。
5. 回环验收：api→CDN 逐件 sha256 与 §7.1 新值恒等（先例配方=PUB1-RESUME-1/REBUILD-6 roundtrip；CDN 首拉偶发 0 字节/404，5s 退避重试即中）。
6. 发布 body 更新：粘贴 [0.1.0 定稿正文](release-notes-0100-draft.md)（实填日期+新 setup SHA-256 行；已知问题段按发布管理员裁量取舍，含同版换装的既有装机说明）。

### 7.3 回滚路径

任一回环不恒即停：旧五件按 §7.1 旧值列重传 clobber 覆盖回原样；latest 翻转回
`--prerelease=false`。更新链全程 fail-closed（验签不过拒装），最坏结果=用户收不到
更新而非装到坏包；rebuild6 manifest 即便短暂在线也在 10-17 自然过期。零 force-push、
零 tag 改动、零密钥经手（manifest 用既有生产键签名件）。

### 7.4 晨间收尾项（本包受阻项）

1. **沙箱冒烟启动段**：安装段已 PASS（setup exit 0、五目录齐、`current.json`
   version=0.1.0/awaiting_health=false、[Run] exit 0、日志零真 error）；启动段待
   **真实实例关停后**执行——本机 6268 在服实例持有单实例互斥锁，沙箱 launcher 会走
   repull 聚焦既有页面（2026-10-04 01:0x 实证，fail-safe 非缺陷；N9-A/REBUILD-6
   冒烟当时的同等前提=零 CourseLens 进程）。指令：实例退出后
   `bash scripts/smoke_installer.sh 0.1.0 8808` 期望 SMOKE PASS。
2. **启动计时×3**：沙箱安装态双击三次掐「双击→主窗可交互」，逐次+中位数记档
   （0.1.0 端到端冷启基线；对照锚：splash 显示 583-795ms 暖/1,698ms 冷、信任走查
   6,434ms、UI 链 243ms——LAUNCH-FIX-1 实测值）。
3. 定稿构建未含 7ce66a7 之后并行车道的在途提交；晨间若 main 再进产品面修复，
   由总控裁决是否按同配方重冻结重构建（约 5 分钟）后换装。
