# CourseLens 完整包装链总图（release-chain）

> 面向发布日执行者与接手会话的一页图：**一个本地提交如何变成学生机器上可验证的更新**。
> 每一环给出执行者、命令入口、校验门与当前状态（✓ 已走通 / ？待决）。
> 状态基准：2026-10-09 第三轮删仓重建 R6 genesis 后现值（§2 总表随本笔刷新。上游逐环定义沿用 2026-10-07 DOCS-CHAIN-2 基准=REPUBLISH-28 后现值，源 96be49f，彼时公共 main=d13a832b——该公共对象已随旧仓删除灭失，账面留档）。

## 0. 全链一张图

```
[实现车道+finalizer]                    私有主仓（正名=origin，身份经 checkout-only 侧车 `config/ops-private.json`；旧身份已冻结退役，不在公开版点名）
  本地 main 提交 ──▶ ①源 ref 推私仓 agent/release-src-<short>
                              │
                              ▼
                 ②公共仓导出工作流（workflow_dispatch: source_commit+branch_name；工作流文件名不载公开版）
                    App token 只读拉私仓 commit → 祖先门 → 导出+签名 → 推 generated/* → 开 PR
                              │
                              ▼
[公共镜像仓 gualtier-xu/Fudan-CourseLens-Worker]
  ③PR 七项检查全绿 → squash merge → 公共 main = 镜像上镜（commit/tree/doc 三值）
                              │
                              ▼
[客户端仓 private/main]
  ④pin 前移 runtime-assets.json（active←新三值；previous←旧 active）→ 随下轮 finalizer 提交
                              │
                              ▼
[安装包/更新包]  ⑤installer+PKG1 闭集 → ⑥build_client_update+sign_client_release → client-v* tag
                              │
                              ▼
[渠道]  ⑦DISTRIBUTION_REPOSITORY releases/download（总仓，REBUILD-9 已重指）
                              │
                              ▼
[学生机器]  ⑧客户端 update service 拉 manifest → 信任链验签（fail-closed）→ 应用/回滚
```

配套文档：安装包装机链细则见 `docs/packaging-chain.md`（源码→setup.exe→装机冒烟，N9-A）；客户端更新域细则见 `docs/update-chain.md`（签名更新链地图+本地复演，N9-B）；发布日 Runbook 见 `docs/release-checklist.md`（§5=2026-09-25 夜批后状态）。

## 1. 逐环定义

### ① 版本源 → 源 ref（✓ 每次发布走通）
- 执行者：finalizer（本地提交）+ 发布执行车道（推 ref）。
- 命令：`git push origin <source_sha>:refs/heads/agent/release-src-<short7>`（404 时双 `-c credential.helper=` + gh 凭据通道）。
- 校验：源=干净树上的本地 main HEAD；零本地新增提交、零主链提交；workflow 祖先门 `merge-base --is-ancestor origin/main HEAD`（源必须是私仓 origin/main 后代）。
- 残留治理：源 ref 只增不清（历史在案 agent/release-src-e124b900 等 14 个）；生命周期策略=发布列队项。

### ② 镜像导出+签名（✓ R5 全链实证 PASS；R9 为本地复验历史先例）
- 执行者：发布执行车道（本地预演）与公共 workflow（正式导出）走同一 exporter：`scripts/export_worker_mirror.py`。
- 配方：`--shared` 临时克隆 → detached 到源 commit → `PYTHONDONTWRITEBYTECODE=1`；`COURSELENS_DATA_DIR` 指回主仓 `runtime/data`，`--local-dpapi-key-id release-2026-09-cleanroom-r3`（DPAPI 读 key，值零打印）。
- 校验门（三级，R9 配方在案）：
  1. `verify_manifest_document`：root_keys→trust(epoch)→manifest 签名链+协议版本闭集；
  2. `verify_snapshot_files`：逐文件 sha256/size（下方 72 文件=R9 历史配方示例，现役 file_count=107，见 §2）；
  3. 对账 vs 上一公共 release：ADDED/REMOVED/CHANGED 逐文件列出（R9 历史先例：恰 4=P65 四件）；元数据五件中 root/trust/trust.sig 必须 BYTE-IDENTICAL；`exporter.allowlist_sha256` 与旧值恒等；逐文件 vs `git cat-file` 源 commit 全量字节比对。
- 冒烟：导出快照上以 anaconda python（含 numpy）`PYTHONPATH=<snapshot> pytest -q tests`。

### ③ 公共 PR → squash merge（✓ R5 空仓首 pub 先例=孤儿直落 main 无 PR 面；下一常规环恢复 PR 流）
- 执行者：workflow 自动开 PR；发布车道等检查后 merge（参考实现 `scripts/release_worker_mirror.py` 的等待/合并段）。
- 命令：`gh workflow run <导出工作流> --repo gualtier-xu/Fudan-CourseLens-Worker --ref main -f source_commit=<sha> -f branch_name=generated/worker-mirror-<short7>`（工作流文件名不载公开版，见公共仓 `.github/workflows/`）。
- 校验门：workflow conclusion=success；PR diff 恰 6 文件（manifest+sig+trust 相关+载荷变更，**trust 三件/allowlist/workflow yml 不得出现在 diff**）；PR checks 全绿（R8 历史实绩 7 项；空仓首 pub 无 PR 面=五门 push 触发，见 §2）；merge 后删 generated 分支；新克隆复算三方恒等（公共 main commit/tree/manifest document_sha256）。
- 瞬态处方：API 抖动按退避重试；代理故障直连（在案先例）。

### ④ pin 前移（✓ 语义固定）
- 执行者：发布车道（改后留脏）→ 下轮 finalizer 随批提交。
- 语义（`update_pin`，release_worker_mirror.py:89）：`previous←active 逐字段`；`active←{repository, commit, tree, manifest_sha256, signing_key_id, trust_epoch, protocol_versions}`。
- 校验门：diff 恰 6+/6-；PIN-READBACK（回读文件与预期三值逐位比对）；signing_key_id/trust_epoch/protocol_versions 零轮换；`release/` 零触碰；重建门测试 2 passed；porcelain 恰 1 件（runtime-assets.json）。
- 回滚通道：`previous` 三值 + `worker_mirror.root_keys` 撤销链（revoked_manifests）——回滚演练在发布列队。

### ⑤ 安装包（✓ 既有闭集；操作手册=docs/packaging-chain.md）
- 载体：`installer/` + `scripts/install_managed_client.ps1` + `scripts/start_managed_courselens.ps1` + `runtime-assets.json`（PKG1 13 件闭集，改动须走闭集门）。
- 校验：PKG1 final_acceptance_audit（豁免清单在案）；编码门（UTF-8 BOM 纪律，P61 后闭环）。

### ⑥ 更新包构建+签名（✓ 既有；操作手册=docs/update-chain.md）
- 命令：`scripts/build_client_update.py` → `scripts/sign_client_release.py` → 推 `client-v*` tag + `courselens-windows-manifest.json` 资产。
- 校验：`scripts/check_client_release_gates.py`（production_gates 全真布尔 + distribution 闭集字段）。

### ⑦ 渠道（✓ 2026-10-04 REBUILD-9 已重指总仓）
- 根：`src/update/service.py:62 DISTRIBUTION_REPOSITORY = "gualtier-xu/Fudan-CourseLens"`（2026-10-04 REBUILD-9 重指总仓；旧发布门户已退役，旧名不载公开版）——releases/download 前缀之源。
- 状态：发布门户已重指总仓；GO 换装已落地（r5=现行 live 面，§2 ⑦；R6 预排见 §2 表后注）。

### ⑧ 客户端检查/应用/回滚（✓ fail-closed 在案）
- 客户端 update service 拉 `courselens-windows-manifest.json` → 信任链验签（同 ② 的验证函数族）→ 通过才应用；任何一环不过=fail-closed 停在当前版本。
- 断链保护：更新器 fail-closed 是审计实绩（敏感值静态扫零发现+外联闭集表佐证）。

## 2. 当前状态总表（2026-10-09 第三轮删仓重建 genesis 后现值；RELEASE-0.1.0-REBUILD-R6 刷新）

> 2026-10-08 第二轮同名删仓重建（用户明令授权，R2 临时发布后当日并入全部
> 修复重新干净发布）：总仓新 id 1409759470、镜像仓新 id 1409759635；公共
> 镜像历史自孤儿单根 `f2266c4` 重新起始，`f60f9b05` 环及更早公共对象已随
> 旧仓消灭（账面留档=update-chain-values §2.14/§2.15）。公共 PR 环节在重建
> 后首发布走「空仓首 pub」形态（孤儿提交直落 main，无 PR 面），下一常规环
> 恢复 PR 流（镜像结构 policy 门的结构红先例评论照旧）。

| 环 | 状态 | 最近实证 |
| --- | --- | --- |
| ① 源 ref | ✓ | R5 genesis 源=d998e81（Pages v6 终态；pin solo=7a5c422，INSTALL-ICONFIX=b50560f） |
| ② 导出+签名 | ✓ | file_count=107，manifest_sha256=1f38db36，深比较 11/11+checker --go passed（evidence 43cb30d5） |
| ③ 公共首发布 | ✓ | 空仓直落 main=f2266c4/87517ffa（CI run 37730950799 五门全绿；无 PR 面） |
| ④ pin 前移 | ✓ | active=f2266c4/87517ffa/1f38db36（previous=f60f9b05 环原串留档） |
| ⑤ 安装包 | ✓ | rebuild-r5-20261008 五件（setup e651916f；三门绿+release-ready 门 EXIT=0） |
| ⑥ 更新包 | ✓ | zip ec486b03/154 件+manifest 4927734d（go1/epoch1 零轮换） |
| ⑦ 渠道 | ✓ | 总仓（新 id 1411171710）client-v0.1.0 release id 407431898（rebuild-r6-20261009 五件，远端 digest 逐位吻合=update-chain-values §1.7 现行表）+mac prerelease id 407443867（client-v0.1.0-macos-test，prerelease=true，latest 不动）；Pages=legacy/main /docs，live 200（v8 终态，R6 genesis 直载） |
| ⑧ 客户端 | ✓ | FRESHRUN 单门 PASS 且装机保留（真实 UIS 登录全流程+计时中位 3.968s）；回声 run 37731537229 success+test-channel echo → ready_for_dispatch=true |

> **R6 现值（2026-10-09 发布环；全链语义与回声判据见 update-chain-values §2.16）**：第三轮同名删仓重建（总仓 1409759470→1411171710、镜像仓 1409759635→1411171779）后全链重走：①源 ref=冻结 9650c9b4（=pin 前移 solo 同笔）；②导出 file_count=107（payload 树与前两代恒等）；③空仓首 pub 孤儿=022e84f9（树 390f22da）+五门 run 37873885366；④pin 前移 solo=9650c9b4（先于删仓落树，13/13 断言）；⑤⑥ rebuild-r6-20261009 五件（§1.7 现行表）；⑦ client-v0.1.0 release+**mac prerelease 独立 tag（prerelease=true，latest 不动=硬护栏，R6 新增步）**；⑧ FRESHRUN 沙盒门 PASS+回声=ready_for_dispatch（镜像核验+test-channel echo）。差异注记（vs R5）：mac prerelease 新增步；Pages 树内 v8 genesis 直载（live 一步到位）；构建基含 b96dc1f 起 R5 未含的四笔学生可见修复+R12 一行修（已落树）+MAC-FONT-1。

## 3. 发布日干系（指向 release-checklist）

干净仓重建时本链重走初始化：镜像链（②③）、信任三件（root_keys 重建）、辅助账号仓同步、渠道参数化（⑦ 解冻）。逐步骤 Runbook 见 `docs/release-checklist.md`。
