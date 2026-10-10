# Update chain current values: pinned sheet and change history

This sheet fixes the update chain's scattered effective values in one place,
with a `file:line` source anchor on every row and a change history for the
values that have already moved.  It is the companion to
[update-chain.md](update-chain.md) (structural map) and
`client-update-operations.md` (operations
runbook); the build-side inventory lives in
[packaging-chain.md](packaging-chain.md) and the go/no-go gates in
`release-checklist.md`.

**Verification basis:** every value below was read from the working tree at
HEAD `b6c9d7e3` (2026-10-06, after the REPUBLISH-24 mirror pin, the
CO-NEUTRAL source-identifier neutralization, and the 2026-10-06 ring-history
backfill; the previous full read was `c16608d3`, 2026-10-02).  On
2026-10-07 (DOCS-CHAIN-2, HEAD `92b46d5`) §1.5 was re-synced to the
REPUBLISH-28 pin and §2.13 was added for rings 25-28; every other row
remains on the `b6c9d7e3` read basis.  Release-face
values in 1.7 were additionally confirmed against a live fetch of the
published manifest on 2026-10-02.  On 2026-10-09 (UPDCHAIN-R6-PREP-1, HEAD
`b0e6dc5`) the two §1.6 `src/app.py` anchors were re-grepped and corrected
(`:94`→`:96`, `:101-102`→`:103-104`) and draft blocks for the R6 rebuild
were added to §1.7 and §2.16 (placeholders only — no value moved); the full
re-read of every anchor at the R6 freeze commit rides the morning release
chain.  Line anchors rot as files evolve — if a
row and its source disagree, re-grep the source file and treat the source as
truth, then update the row in the same commit that moved the value.

## 1. Current values

### 1.1 Channel identities

| Value | Current | Source |
| --- | --- | --- |
| Distribution repository (default channel) | `gualtier-xu/Fudan-CourseLens`（总仓门户，2026-10-04 REBUILD-9 重指；旧发布门户已退役，旧名不载公开版） | `src/update/service.py:62`; `config/distribution.json:3`; `config/client-update-trust.json:42` |
| Source repository (provenance pin) | `Fudan-CourseLens-Source`（中性标识；真实私仓名只存 checkout-only 侧车 `config/ops-private.json` 的 `source_repository` 键，不进安装器与发布包） | `src/update/service.py:61`; `config/distribution.json:4-5` |
| Channel override env | `COURSELENS_UPDATE_CHANNEL` (blank/unset keeps the default) | `src/update/service.py:63`, effective at `:104-105` |
| Channel label | `stable` | `config/client-update-trust.json:4` |
| Distribution registry | `courselens.distribution-registry.v1` at `config/distribution.json` (single source of truth for repository identities) | `src/distribution.py:18-20` |

> Resolution (2026-10-02): the stale *Source repository* row in the
> channel-inventory table of [update-chain.md](update-chain.md) (introduced
> by the CONFIG-1 doc sync) was fixed back to the true private source
> identity (registered only in the checkout-only sidecar; the literal name
> is elided in this public sheet), matching the anchors above.
> No value moved — the code and registry were always correct; this closes
> the follow-up this sheet was tracking (DOCSPOLISH-1).

> Resolution (2026-10-05, CO-NEUTRAL-1/2): the provenance pin itself became
> the neutral identifier `Fudan-CourseLens-Source` (`MANIFEST_SOURCE_ID`,
> `src/update/service.py:61`; `source_repository` + `manifest_source_id`,
> `config/distribution.json:4-5`) so the signed manifest and the release
> assets carry no private account name.  The true private source repository
> name now lives only in the checkout-only sidecar `config/ops-private.json`
> (`source_repository` key); the mirror exporter reads it from there with a
> neutral fallback and fails closed on a malformed sidecar, and the
> installer staging excludes the sidecar (CO-NEUTRAL-2).  The 2026-10-02
> note above records the pre-neutralization state; this public sheet elides
> the literal pre-neutralization identity (it lives only in the
> checkout-only sidecar).

### 1.2 Manifest and package URLs

| Value | Current | Source |
| --- | --- | --- |
| Manifest URL form | `https://github.com/<channel>/releases/latest/download/courselens-windows-manifest.json` | `src/update/service.py:108-112` (asset constant `MANIFEST_ASSET` at `:65`) |
| Effective pinned `manifest_url` (shipped policy) | `https://github.com/gualtier-xu/Fudan-CourseLens/releases/latest/download/courselens-windows-manifest.json` | `config/client-update-trust.json:40`; must equal `_stable_manifest_url()` or the policy fail-closes (`src/update/service.py:544`) |
| Package start-URL prefix | `https://github.com/<channel>/releases/download/<tag>/<asset>` with tag `client-v<semver>` | `src/update/service.py:229-236` |
| Release tag namespace | `client-v` | `src/update/service.py:64`; `config/client-update-trust.json:45` |
| Manifest asset name | `courselens-windows-manifest.json` | `src/update/service.py:65`; `config/client-update-trust.json:46`; `config/distribution.json:6` |
| Allowed hosts | `github.com`, `release-assets.githubusercontent.com`, `objects.githubusercontent.com` | `config/client-update-trust.json:35-39`; `config/distribution.json:7-11` |

### 1.3 Production trust gates (nine, machine-enforced, fail-closed)

Code authority is the `REQUIRED_PRODUCTION_GATES` set
(`src/update/service.py:80-84`); any missing boolean-true gate fails closed
with `production_gates_incomplete` (`src/update/service.py:530-534`).  The
shipped policy mirrors the same names (`config/client-update-trust.json:49-59`).

| Gate | Policy value | Source |
| --- | --- | --- |
| `public_release_repository` | `true` | `config/client-update-trust.json:50` |
| `protected_…_ci`¹ | `true` | `config/client-update-trust.json:51` |
| `offline_update_root` | `true` | `config/client-update-trust.json:52` |
| `protected_update_release_key` | `true` | `config/client-update-trust.json:53` |
| `public_download_safety_implemented` | `true` | `config/client-update-trust.json:54` |
| `release_artifact_sha256_published` | `true` | `config/client-update-trust.json:55` |
| `clean_machine_acceptance` | `true` | `config/client-update-trust.json:56` |
| `real_restart_acceptance` | `true` | `config/client-update-trust.json:57` |
| `power_loss_recovery_acceptance` | `true` | `config/client-update-trust.json:58` |

¹ One gate name is elided in this public sheet: the verbatim key embeds an
internal topology term.  It ships verbatim in the policy's `production_gates`
block and in the code set — see the anchors in the paragraph above.

All nine gates are `true` since the production GO flip (GO-FLIP-1,
`40e2d96`, 2026-10-02 — §2.7).  Renaming or adding a gate must still move
the code set, the policy, and the release workflow together; flipping gate
*values* is a separately reviewed production decision that moves the policy,
its coupled tests, and the audit script in the same commit (see section 3).

### 1.4 Protected environment and secret names

| Value | Current | Source |
| --- | --- | --- |
| Protected environment | `client-release-production` | `config/client-update-trust.json:60` |
| `required_secret_names` | `COURSELENS_UPDATE_RELEASE_SIGNING_KEY`, `COURSELENS_RELEASE_PUBLISHER_TOKEN` | `config/client-update-trust.json:61-64` |

The Authenticode pair (`WINDOWS_AUTHENTICODE_CERTIFICATE_PFX`,
`WINDOWS_AUTHENTICODE_CERTIFICATE_PASSWORD`) is retired (CONFIG-1, section
2.1); per the 2026-09-30 signing decision, tamper evidence is the published
`SHA256SUMS.txt` sheet plus the Ed25519-signed manifest.

### 1.5 Bundled worker-mirror pin (`runtime-assets.json`, `e698d2ae` round, D12-FIX-1)

The "three values" that move on every approved mirror republish are
`commit`, `tree`, and `manifest_sha256`, rolled `active` → `previous`.
Only the last two rings stay in the file; older rings live in section 2.

| Field | `active` | `previous` | Source |
| --- | --- | --- | --- |
| `repository` | `gualtier-xu/Fudan-CourseLens-Worker` | `gualtier-xu/Fudan-CourseLens-Worker` | `runtime-assets.json:24`, `:35` |
| `commit` | `e698d2ae3d25277604de0cfb3ec34864b796b036` | `022e84f94fdf6ffe6daf18b3ed09ab739b2c7ddb` | `runtime-assets.json:25`, `:36` |
| `tree` | `853eeecd0a03155b97df420cc10b62357227f5c4` | `390f22da95502ca4f782036be0f6f9aff335e68a` | `runtime-assets.json:26`, `:37` |
| `manifest_sha256` | `5ea0843dc2b40c9081ad5600195d0a88c7380e839640fc8524b77000f064179e` | `c53ba235081baeeea8e3716075c135e4e2272585eeb44b7eeaebe172fcee11b5` | `runtime-assets.json:27`, `:38` |

> 2026-10-09 D12-FIX-1 常规镜像环（PR 流回归先例）：源=91a79f7
> fix(worker-llm)（D-20261009-12 根修——llm.yml 精简装机补
> pycryptodome/curl-cffi 双钉）；镜像 PR #1 五门 CI 全绿（gitleaks/
> protocol/boundary/unit/punct-inference；mirror-policy 结构红=R21-R26
> 先例评论 #issuecomment-6078881710），squash 合并官方 main=
> `e698d2ae`（单父=`022e84f9` 线性，树=生成树恒等）；pin solo=`3866f59f`
>（13/13 断言）。R6 环（`022e84f9`）转 previous；`f2266c4` 环及更早见
> §2.15/§2.16。真跑复验：repair 同步实例仓至新环后回声 channel_test_valid
> → ready_for_dispatch=true，40690/663596 真实总结任务 llm.yml run
> `37918657141` conclusion=success（16297 字 markdown/63 章/6 带锚要点），
> D-20261009-12 结案（SUMMARY REPLAYED-PASS）。
>
> R6 删仓重建史注（2026-10-09）：公共镜像仓历史自 `022e84f9` 孤儿单根重新
> 起始（R5 前史见 §2.15/§2.16）；`f60f9b05` 环及更早（`d13a832b` 等）的公共
> 提交对象已随旧仓删除而不复存在，仅存本账与本地对象库作历史记录。既有装机
> （0.1.0 早期换装构建、0.1.1 桥接装机与 R2 临时发布代）内嵌 pin 指向已灭
> 历史，其镜像核验迁移与 §2.9 同属用户决策面（更新链对 ≤ 当前版本一律回
> 「已是最新」，不会自动推送修复）。

### 1.6 Signing identity

| Value | Current | Source |
| --- | --- | --- |
| Mirror `signing_key_id` | `release-2026-09-cleanroom-r3` | `runtime-assets.json:28` (active), `:39` (previous) |
| Mirror `trust_epoch` | `1` | `runtime-assets.json:29`, `:40` |
| Mirror `protocol_versions` | `["2"]` | `runtime-assets.json:30-32`, `:41-43` |
| Mirror root key id | `root-2026-09-cleanroom-r3` | `runtime-assets.json:15` |
| Client-update policy `enabled` | `true` (production GO, §2.7) | `config/client-update-trust.json:3` |
| Client-update root key | `root-2026-10` (offline, outside the repo) | `config/client-update-trust.json:10` |
| Client-update release key | `release-2026-10-go1` (epoch 1, root-signed, stable/windows) | `config/client-update-trust.json:16` (authorization block `:12-34`) |
| Release key validity window | 2026-10-01T19:22:58+00:00 → 2027-10-01T20:22:58+00:00 | `config/client-update-trust.json:19-20` |
| Client-update `minimum_key_epoch` | `1` | `config/client-update-trust.json:8` |
| Client-update `minimum_version` (security floor) | `0.1.0` | `config/client-update-trust.json:7` |
| Update envelope schema | `courselens.client-update.v1` | `src/update/service.py:51` |
| Trust policy schema | `courselens.client-update-trust.v2` | `config/client-update-trust.json:2` |
| Policy file a source checkout reads | `config/client-update-trust.json` itself (but the install action fail-closes with `managed_install_required` — no managed-install layout) | `src/app.py:103-104`; `src/update/service.py:1094` |
| Policy file a managed install reads | `<install_root>/trust/client-update-trust.json` (seeded from the committed policy by the installer) | `src/app.py:96` (`_client_update_trust_path`) |

### 1.7 Published update face (`client-v0.1.0`; portal re-pointed to the master repo `gualtier-xu/Fudan-CourseLens` on 2026-10-04, REBUILD-9; old portal retired)

> **镜像环先行注记（2026-10-09，D12-FIX-1）**：worker 镜像环已前移至
> `e698d2ae` 环（§1.5，D-20261009-12 pure-LLM 装机根修），而已发布
> `client-v0.1.0` 面（总仓 release/Pages）按「总仓仅版本号更新」纪律本环
> 零触碰——既有 0.1.0 安装基的内嵌 pin 仍=R6 代（`022e84f9` 环，其 llm.yml
> 含六钉缺陷）。修复上学生装树走下一次版本发布流（版本 bump→构建资产→
> README/docs→tag→release 一笔发布流）随新构建 bundle 生效；dev 树与
> 新绑定实例自本环起即携修复（真跑复验见 §1.5 注）。

**门户重指（2026-10-04，REBUILD-9）**：客户端更新链与发布门户自
旧发布门户（**退役**——GO 换装后
不再更新；门户旧名不载公开版；存量 0.1.1 桥接装机的迁移归用户决策，见 §2.9 语义注记）重指至
总仓 **`gualtier-xu/Fudan-CourseLens`**。GO 在总仓创建 `client-v0.1.0`
release 并上传 rebuild9 五件后，总仓的 `releases/latest/download/*`
即成为现役更新面。

rebuild9 换装构建（release_id `rebuild9-20261004`，本页 §1.1/§1.2 重指后的
第一代构建）已备妥五件（构建基=冻结 `6d57dc4c7f5cd6a9961e85c537e5a2ba30636e17`，
tree `2f43f0e4598b1622479b07b698ef452da06c917f`；source=私仓同值；签名
`release-2026-10-go1`/epoch 1 零新生成；窗口 2026-10-04T17:07Z→10-18）：

| # | 资产 | SHA256 | 大小 |
| --- | --- | --- | --- |
| 1 | `CourseLens-0.1.0-setup.exe` | `744b87d3a3372555243e68db43505c8fdf359ade6b1ab9efcc26c3e133400026` | 24,373,819 B |
| 2 | `CourseLens-0.1.0-checksum.txt` | `bedfb72a921cdf8a53f529c611f1bad9694eb6f388cc1dde0bdc374fd788c5fe` | 432 B |
| 3 | `courselens-0.1.0-windows-x86_64.zip` | `8056a903ab69603bfe6a115d1a01b98da3b04e6a732a6e26958c4440e4dacba0` | 1,304,341 B（134 件） |
| 4 | `courselens-windows-manifest.json` | `38e37f960632b8f6e2960dff99e7cc228d5b8ac9ac57dda0a8107f90d9387da1` | 4,785 B |
| 5 | `windows-security-prompt.md` | `198ce1740d267ea9470e93e4c2cdd35b3a4c7588a183531553dc5342e0d190c1` | 3,695 B |

manifest `package.url` = 总仓标签直链
`https://github.com/gualtier-xu/Fudan-CourseLens/releases/download/client-v0.1.0/courselens-0.1.0-windows-x86_64.zip`
（`_validate_package_url` 要求 `client-v<semver>` 标签形态；trust
`manifest_url` 的 `releases/latest/download` 形态不在此限）。zip 内
worker pin 三点 = `6a3c7179`/`df60eaf7`/`a6aa8817` 环恒等（REPUBLISH-21 交账），
previous=`1296e926` 环。上传=GO 外写另批（总仓 `client-v0.1.0` release 创建
后，`releases/latest/download/*` 即落本代）。

rebuild9b 换装构建（release_id `rebuild9b-20261005`，2026-10-05，REBUILD-9B；
载荷新增托盘深浅色自适应〔SYSTRAY-IMPL-3〕与 Pages 重做〔PAGES-REDESIGN-2〕
入库面，**取代 rebuild9 五件成为 GO 上传对象**；rebuild9 五件退役留存
`.tmp-rebuild9/out/` 存档）已备妥五件（构建基=冻结
`63f72f7341b344f312418cc0dcdde1992280b06a`，
tree `2a4573e40c7e6f95be0e4509b4cf0b2a4db79a71`；source=私仓同值
〔SEC-2：`source.repository` 现值=当时真实私仓标识（literal 名不载公开版）
随发布资产公开属 GO 前用户裁决项，换装包照常生成未自行处置；后经
CO-NEUTRAL-1 中性化收口〕；签名
`release-2026-10-go1`/epoch 1 零新生成；窗口自生成时刻起 336h）：

| # | 资产 | SHA256 | 大小 |
| --- | --- | --- | --- |
| 1 | `CourseLens-0.1.0-setup.exe` | `1a1f0ed7d4cada7cb883823ba4e0e62693614e0d6f966536b960325805809f7a` | 24,399,634 B |
| 2 | `CourseLens-0.1.0-checksum.txt` | `3697d6f6e21e66812ddd8cffc439c52e73eca2f91a669aaa5b94357b27e45d9f` | 432 B |
| 3 | `courselens-0.1.0-windows-x86_64.zip` | `58f08bb67d5e1572a22c8f5c49db71a1166704ee80b675a238cb72f2f4b3ed53` | 1,306,020 B（134 件） |
| 4 | `courselens-windows-manifest.json` | `82127ffd92d2c53027b220c3116091972461c68113d8a8ba4073ab6a26654870` | 4,786 B |
| 5 | `windows-security-prompt.md` | `198ce1740d267ea9470e93e4c2cdd35b3a4c7588a183531553dc5342e0d190c1` | 3,695 B |

rebuild9b 的 manifest `package.url` = 总仓标签直链（与上同值：
`https://github.com/gualtier-xu/Fudan-CourseLens/releases/download/client-v0.1.0/courselens-0.1.0-windows-x86_64.zip`，
`client-v<semver>` 标签形态）。zip 内 worker pin 三点 = 同环
`6a3c7179`/`df60eaf7`/`a6aa8817` 恒等（REPUBLISH-21 交账），previous=`1296e926`
环；`check_client_release_gates.py --require-release-ready` EXIT=0（冻结树
trust 件）。

rebuild9c 换装构建（release_id `rebuild9c-20261005`，2026-10-05，REBUILD-9C；
CO-NEUTRAL-1 中性化后第一代：manifest `source.repository`=`Fudan-CourseLens-Source`
逐位〔e38d7ba 新钉自动写入，签名 go1/epoch 1 零轮换〕，更新链资产零 co 终验
**zip 解包面+manifest 面标识符类全零**）已备妥五件（构建基=冻结
`e38d7badda2be162bfa65c3cf4b476816c8ffd2e`，
tree `546b562255c2853510c8e34f76f13898ad6956e6`；窗口自生成时刻起 336h）：

| # | 资产 | SHA256 | 大小 |
| --- | --- | --- | --- |
| 1 | `CourseLens-0.1.0-setup.exe` | `707467542fa686d4689bbe1458d3c6e53ed0b2620d59f58420dcd96a786c2440` | 24,402,112 B |
| 2 | `CourseLens-0.1.0-checksum.txt` | `a04e6a9c4fdf6792f5118fa9474d4d9ec133f170e3f2b85616d41b1726127220` | 432 B |
| 3 | `courselens-0.1.0-windows-x86_64.zip` | `44fd6e0ac0d5980c5977f50b39344899c835072a52176f09e69cfbeec5ca9afe` | 1,306,559 B（134 件） |
| 4 | `courselens-windows-manifest.json` | `5df34a7c266c2b4a8c94bb1ea49ed501d652768aaac0e016f8f61fb90b5193db` | 4,770 B |
| 5 | `windows-security-prompt.md` | `198ce1740d267ea9470e93e4c2cdd35b3a4c7588a183531553dc5342e0d190c1` | 3,695 B |

rebuild9c 的 manifest `package.url` = 总仓标签直链（同上值）；zip 内 worker pin
三点 = 同环 `6a3c7179`/`df60eaf7`/`a6aa8817` 恒等，previous=`1296e926` 环；
`check_client_release_gates.py --require-release-ready` EXIT=0（冻结树 trust 件）。
**SEC（REBUILD-9C 零 co 终验新增发现，GO 前必须处置）**：setup 安装器内嵌面
（iss `[Files]` 整树暂存）零 co 终验**未过**——`config/ops-private.json`（2）与
`scripts/export_worker_mirror.py:284`（1）随安装器分发私仓名（装机落盘
`<install>\config\ops-private.json` 实证）；**本代 setup 不得作为 GO 上传对象**，
待 CO-NEUTRAL 后继包（安装器暂存排除或侧车迁位+脚本默认值中性化）按同配方
换代重建；本代五件留存 `.tmp-rebuild9c/out/` 存档。

rebuild9d 换装构建（release_id `rebuild9d-20261005`，2026-10-05，
CO-NEUTRAL-2+9D；CO-NEUTRAL-2 后第一代：iss `[Files]` 暂存排除 checkout-only
侧车与私仓 exporter+`export_worker_mirror` 私仓名改 ops-private 侧车读入
〔缺省中性回退+stderr，坏侧车 fail closed〕，零 co 终验升级版
**setup 内嵌面标识符 6 模式双口径全零**〔装机落盘两排除件缺席实证〕，zip
唯一残留=coordinator.py 全大写环境变量名〔9C 同针如实记档〕；REPUBLISH-23
环演进随代：zip 内 worker pin active=`45dd6230` 环、previous=`6a3c7179` 环）
已备妥五件（构建基=冻结 `662e761853b2b7d7b3d175581775edf1b0479ad7`，
tree `6438d066d27fd6826121142209054ca76b6312e0`；签名 `release-2026-10-go1`/
epoch 1 零新生成零轮换；窗口自生成时刻起 336h）：

| # | 资产 | SHA256 | 大小 |
| --- | --- | --- | --- |
| 1 | `CourseLens-0.1.0-setup.exe` | `1f8a492d47b77daee5bf3bdca6f2a0731b55ee69036ae4822ea974396b3f305b` | 24,398,182 B |
| 2 | `CourseLens-0.1.0-checksum.txt` | `892e6376592d8c31560dddff5d88e80b4ea24aacc0acadd13d6e572dbccde30c` | 432 B |
| 3 | `courselens-0.1.0-windows-x86_64.zip` | `8b2e79c28973209bf5f6867f97c4227b6bda9efbf118bc92ce854c0d764ee392` | 1,306,561 B（134 件） |
| 4 | `courselens-windows-manifest.json` | `e4d5b821da28a56e3e3201ac962cabfb53735b10fa195e65eeeb5349dbf42864` | 4,770 B |
| 5 | `windows-security-prompt.md` | `198ce1740d267ea9470e93e4c2cdd35b3a4c7588a183531553dc5342e0d190c1` | 3,695 B |

rebuild9d 的 manifest `package.url` = 总仓标签直链（同上值）；
`check_client_release_gates.py --require-release-ready` EXIT=0（冻结树 trust 件）；
冒烟 PASS+计时×3（中位 6.428s，锚 9C 6.518s 同量级）+信任链正负支双 PASS。
**GO 上传对象换代：本代五件取代 rebuild9c 五件；rebuild9c 五件显式作废，
不得作为 GO 上传对象**（其 SEC 项已由 CO-NEUTRAL-2 修复并经 rebuild9d 终验
清零）；rebuild9c 五件退役留存 `.tmp-rebuild9c/out/` 存档。GO 上传已落地：
总仓 `client-v0.1.0` release 创建于 2026-10-05T12:57Z，2026-10-07
DOCS-CHAIN-2 live 复核 latest 实供 `rebuild9d-20261005`
（`source.repository`=`Fudan-CourseLens-Source`，窗口至 2026-10-19）。

**换代（2026-10-08，RELEASE-0.1.0-REBUILD-R2；当日晚间已由 R5 换代，见下块）**：总仓同名
删仓重建后 `client-v0.1.0` release（release id 406093889，tag 挂新 genesis
`c1e26aac`）由 **rebuild-r2b-20261008** 五件整体换装（clobber 重灌，notes=
定稿 2026-10-08 围栏段 1,832 字符）。构建基=冻结 `c06bf4a5cffa38993a54f410932c445871287e02`
（含 §2.14 新 pin，删仓重建后镜像核验可用性所系；首版 r2 资产构建于
`f5098d8b` 内嵌 R28 旧 pin=已灭历史，显式作废重构，未产生有效分发）。
签名 `release-2026-10-go1`/epoch 1 零轮换；`package.url`=总仓标签直链；
`--require-release-ready` EXIT=0；上传后远端 digest 逐位复核：

| # | 资产 | SHA256 | 大小 |
| --- | --- | --- | --- |
| 1 | `CourseLens-0.1.0-setup.exe` | `04ab4088a453b834353bec71f2bc3d2054bb4e2dfd34ece92699e2482414a8ee` | 24,445,055 B |
| 2 | `CourseLens-0.1.0-checksum.txt` | `8cbb9f4dc2872f91ef863a64540b144bf3f877251343574affb52e62f414ee79` | 432 B |
| 3 | `courselens-0.1.0-windows-x86_64.zip` | `b8ada55b9bb2e239accbe37a730a68f79774097065d02f61cfcb855ce57b7a6f` | 1,394,095 B（154 件） |
| 4 | `courselens-windows-manifest.json` | `548f31c4e246f83022536aaf6d290c14ff01835357865b2dffb736da7d023b71` | 6,091 B |
| 5 | `windows-security-prompt.md` | `198ce1740d267ea9470e93e4c2cdd35b3a4c7588a183531553dc5342e0d190c1` | 3,695 B |

FRESHRUN 双门（f5098d8b 构建实机+真实 UIS 登录全流程 / r2b 构建复验同法）
均 PASS，计时×3 中位 3.484s / 3.422s（对照锚 rebuild9b 7.418s 同法更快，
与空闲降载+预加载提示两笔性能合入一致）。rebuild9d 五件自此退役（其哈希表
保留于上段作历史对账）。

**换代（2026-10-08，RELEASE-0.1.0-REBUILD-R5，现行 live 面）**：第二轮同名
删仓重建（总仓新 id 1409759470）后 `client-v0.1.0` release（release id
406434415，tag 挂新 genesis `5ec83560`）由 **rebuild-r5-20261008** 五件首发
（notes=定稿 2026-10-08 围栏段 1,937 字符，含 VTT-PERF 大字幕提速+C4 窄窗
两线增量）。构建基=冻结 `b50560f15b3bdea2af37235efd5636d647fab9a9`
（tree `27801b4dc819ee1a8e6bbc4e25683dacf393bcca`；含 §2.15 新 pin 与
INSTALL-ICONFIX 安装器图标注册修复；R4 死前 12:07 旧构建内嵌同值 pin 但缺
图标修复，显式作废重构，未产生有效分发）。manifest
`source.repository`=`Fudan-CourseLens-Source` 中性；签名
`release-2026-10-go1`/epoch 1 零轮换；`package.url`=总仓标签直链；
`--require-release-ready` EXIT=0；上传后远端 digest 逐位复核：

| # | 资产 | SHA256 | 大小 |
| --- | --- | --- | --- |
| 1 | `CourseLens-0.1.0-setup.exe` | `e651916f85ace3ee27915d31af954a677e710ce4d8c24288ec25e30715bc640a` | 24,458,876 B |
| 2 | `CourseLens-0.1.0-checksum.txt` | `164defef17011eeed2b672a6144399dcb481568790f80f14efaaec61b5e3d878` | 432 B |
| 3 | `courselens-0.1.0-windows-x86_64.zip` | `ec486b03440a9617e70478a43e5387aae3f131532eea2cba2a0755d3eacb99ef` | 1,404,674 B（154 件） |
| 4 | `courselens-windows-manifest.json` | `4927734d7c97514b44cd6f2f688f9cc094e6bf6ecbc0cfab00929c1641aed4c0` | 6,395 B |
| 5 | `windows-security-prompt.md` | `198ce1740d267ea9470e93e4c2cdd35b3a4c7588a183531553dc5342e0d190c1` | 3,695 B |

FRESHRUN 单门 PASS 后装机保留（真机正式安装=用户装机注册恢复；五腿全过+
计时×3 中位 3.968s，量级同 R2）。R2 的 r2b 五件自此退役（其哈希表保留于
上段作历史对账；该代资产随旧仓 release id 406093889 消灭）。

**现行 live 面（2026-10-09，RELEASE-0.1.0-REBUILD-R6 发布环填实；取代上节 R5
表）**：第三轮同名删仓重建（总仓新 id `1411171710`、镜像仓新 id
`1411171779`，两仓同名 URL 不变）后 `client-v0.1.0` release（**id 407431898**，tag 挂本代 genesis 孤儿单根
`0fa0dc9…`；R5 旧 release id 406434415 随旧仓消灭）由 **rebuild-r6-20261009** 五件首发（notes=定稿 0.1.0 围栏段
2,902 字符，含 R5 构建基后学生可感增量清单）。构建基=冻结
`9650c9b44cc217224d95c7b362655b225b78c5f5`（=pin 前移 solo 同笔；tree
`ff63edd6631b04e132a3e0f02a7605aa06d373a8`；含 b96dc1f/50ef718/86dcadc/
86c6fd2 四笔学生可见修复（R6 存在理由，RELEASEREADY-AUDIT 实证）、R12
一行修（`ce32cca`）与其后全部已验收收口笔）。manifest
`source.repository`=`Fudan-CourseLens-Source` 中性沿用；签名
`release-2026-10-go1`/epoch 1 零轮换；zip 内 worker pin 三点=
`022e84f9…/390f22da…/c53ba235…`（新环随装生根）、previous=`f2266c4` 环；
`package.url`=总仓标签直链；`--require-release-ready` EXIT=0；上传后远端
digest 逐位复核：

| # | 资产 | SHA256 | 大小 |
| --- | --- | --- | --- |
| 1 | `CourseLens-0.1.0-setup.exe` | `566fa7be5ef80ca59c647ba780915fe4060283f2d5ed8febf5f88752508b80b6` | 24,485,449 B |
| 2 | `CourseLens-0.1.0-checksum.txt` | `3a4dc2284f72ae9655a24700ca3f351ff7f6306c3d4217492e84b259237ad5a1` | 432 B |
| 3 | `courselens-0.1.0-windows-x86_64.zip` | `684101f8afdd50ce8d4118b3ba4a670be8210a13bb2628c30c49a9e130f798ca` | 1,444,845 B（155 件；R5 基线 154 件+1=新钉随运行时面入包） |
| 4 | `courselens-windows-manifest.json` | `264f2853eb5f689b2e5c7f6c303553beec9f637aa37601da60a3bb7cc86b9b4b` | 9,021 B |
| 5 | `windows-security-prompt.md` | `198ce1740d267ea9470e93e4c2cdd35b3a4c7588a183531553dc5342e0d190c1` | 3,695 B（四代恒等） |

回声判据（放行门）：api→CDN 五件 sha256 逐位复核 +
`releases/latest/download/courselens-windows-manifest.json` 落 r6 manifest
（`release_id=rebuild-r6-20261009`）；**mac prerelease 资产不得出现在
latest 解析面**（mac=独立 tag+`prerelease=true` 且 latest 不动，护栏与全链
语义见 §2.16）。

下表为**旧门户** 2026-10-04 时点的 live 面存档（退役值，供回滚与对账）。
These values live on the release face, not in the repo; re-derive them any
time by fetching the manifest asset from the old portal's
`releases/latest/download`.  The rows below were re-fetched live and
re-hashed on 2026-10-04 (N15-W2 roundtrip; this section had lagged one
generation behind on `rebuild5-20261002` after the same-day REBUILD-6 swap
— refreshed now).
**The old portal's `releases/latest` tag resolves to `client-v0.1.1` since
2026-10-02**; the client-v0.1.0 rows below describe the assets on that tag.

| Value | Current | Source |
| --- | --- | --- |
| Release tags | `client-v0.1.1` (= `latest`, the bridge release, manifest serves this) and `client-v0.1.0` (rows below) | release face; `src/update/service.py:108-112` |
| Signed manifest asset (client-v0.1.0) | `courselens-windows-manifest.json`, SHA256 `04dd3513b61cf716de0bee7a6c03090f75a1e58f33dbbf01e8e389964201aeb7` (1,887 B) | release asset (live-fetch verified 2026-10-04) |
| Manifest `release_id` | `rebuild6-20261002` | manifest field |
| Manifest signature | `release-2026-10-go1`, `key_epoch` 1 | manifest signature block |
| Manifest source pin | 当时真实私仓标识（pre-neutralization；literal 名不载公开版） @ `593a5c0e374beb0dbd82a72f4a53fa156db0ad1e` (tree `9dd73ac948376555ce7d267b5e14797da78b542a`) | manifest `source` block |
| `minimum_security_version` | `0.1.0` | manifest field |
| Update package | `courselens-0.1.0-windows-x86_64.zip`, SHA256 `4f83e7dbd9ccbe0d4e90a1a5fde2595e06e0de0e15c7098003301946af8ecfc7`, 1,274,779 B | release asset (REBUILD-6 byte-roundtrip; manifest `package` block) |
| Canonical installer | `CourseLens-0.1.0-setup.exe`, SHA256 `a4ef60378997bc309ee8200b4a58f8705345ada18e8f270f46201c22fafe179d`, 24,352,092 B | release asset (REBUILD-6 byte-roundtrip 2026-10-02) |
| Checksum sheet | `CourseLens-0.1.0-checksum.txt`, SHA256 `62de9e2a44137b4fdc0a825198224ee1400856065e3c8bbaa06b4111ecb9dfd7`, 432 B | release asset (live-fetch verified 2026-10-04) |
| Security prompt | `windows-security-prompt.md`, SHA256 `2bfa7dd9573cf5f70c822c4757d7514a1b68cc06a9c578072be029439877c527`, 3,620 B | release asset (live-fetch verified 2026-10-04) |
| Trust policy asset | `courselens-windows-trust.json`, SHA256 `ebc24d0c472a9f2e57e6f4f3d6e557100261caaa45cf8586c9050e1542dc1b7b` — byte-identical to the committed `config/client-update-trust.json` | release asset; sha256 of the repo file |

The `goflip1-20261002` lineage (§2.7) was superseded by
`rebuild5-20261002` (§2.8) and then by `rebuild6-20261002` the same day
(REBUILD-6; its §2.x record lives in the REBUILD-6 execution result file).
The canonical-installer lineage and the swap that supersedes this
generation are recorded in §2.9 and
`release-checklist.md` §7 (staged, upload = morning
external-write decision).

## 2. Change history

Recorded changes to these values, in landing order; the mirror-ring history
now runs unbroken from pin1 through REPUBLISH-28 (the REPUBLISH-16→21 rings
were backfilled on 2026-10-06, §2.11; REPUBLISH-25→28 recorded on
2026-10-07, §2.13).  Before/after values are read from
the respective commits — or, for release-face assets, from the signed
artifacts themselves — not from memory.

### 2.1 CONFIG-1 — retire Authenticode secret names (`19da9d9ea012c50331c22707c01662a10fa20269`, 2026-10-01)

- `config/client-update-trust.json` `required_secret_names`: 4 entries → 2.
  Removed `WINDOWS_AUTHENTICODE_CERTIFICATE_PFX`,
  `WINDOWS_AUTHENTICODE_CERTIFICATE_PASSWORD`; kept
  `COURSELENS_UPDATE_RELEASE_SIGNING_KEY`,
  `COURSELENS_RELEASE_PUBLISHER_TOKEN`.
- `docs/update-chain.md` channel-inventory table: distribution and source
  repository rows synced from the auxiliary-account namespace to the
  official `gualtier-xu` namespace (the literal pre-sync names are elided
  in this public sheet).
  The distribution row matches the code; the source-repository row does not
  (fixed 2026-10-02, DOCSPOLISH-1 — see the resolution note in 1.1).
- `scripts/bootstrap_asset_host.py:96+`: bootstrap instruction switched from
  uploading an Authenticode certificate to publishing the `SHA256SUMS.txt`
  checksum sheet.

### 2.2 FACE-1 — release face account + signing decision (2026-09-30, two landings)

- **T1 `8c0b1ae13dbc9f88560aa828442e0f0a035af77c`** — pointed the client
  release face at the official `gualtier-xu` account (release workflow,
  ADR 0006, operations manual, frontend help link).  The config flip landed
  in the companion commit **`c3f6e636cd99d9cfa52cfe44c9e77c653ee48470`**:
  `distribution_repository` moved from the auxiliary-account mirror to
  the official-account mirror (`gualtier-xu/Fudan-CourseLens-Worker`;
  the pre-move auxiliary-account name is elided in this public sheet)
  across
  `config/distribution.json`, `config/client-update-trust.json`
  (`distribution.repository` + `manifest_url`), `src/distribution.py`,
  `src/update/service.py:62`, both `runtime-assets.json` worker-mirror
  `repository` fields, and the release-domain tests.  The source repository
  stayed in the auxiliary-account namespace (the test auxiliary account
  keeps the private
  source).  Mirror pin commit/tree/sha were unchanged at this point
  (`2cc894c4` round).
- **T2 `02fe34bd7a81092d918eaf305fc53e55e18e8784`** — landed the 2026-09-30
  no-code-signing decision: production gate
  `authenticode_signed_launcher` → `release_artifact_sha256_published`
  (still `false`, fail-closed) in `src/update/service.py:80-84` and
  `config/client-update-trust.json`; `client-release.yml` dropped the
  certificate import/sign/verify steps and now publishes `SHA256SUMS.txt`
  with the draft assets.

### 2.3 pin1 — pin approved mirror release `a4fd9a6b` (`66187877d34f7708842235dd562fa435d9d90489`, 2026-10-01)

`runtime-assets.json` worker-mirror roll:

- `active`: `e86808cc625c1accc7e9f27490531ad514b68fdb` /
  `a19759659b2ef1686b72829f97159b9a3e1872f3` /
  `379262aee6d6bdb4a88a3f7c26389e2464a0339279fe4dcfb1556dc741cce621` →
  `a4fd9a6b57825fe0575474a3d0adfe5acfbb1da9` /
  `5dbd1ceefdd2dd58adc78bb8e7e011a514ca0d67` /
  `b2ce303beed072d8e1d54a64d1a5a84ca39054ce5b4c1602c3ef1e38bfd67be5`.
- `previous`: `20aa75928900a55304ffc0d0441bc909674513ed` /
  `736f54af29742fe39ab84c7993ae12d771f5da92` /
  `483ed41b9b6e6a100200a664e5897f0d435682acb6a4b07f9050f357142941d7` →
  the outgoing active triple (`e86808cc...`).
- `signing_key_id` / `trust_epoch` / `protocol_versions` unchanged.

### 2.4 pin2 — pin approved mirror release `8f991672` (`a5ed0ba083361627e0d736b9c20b60dec0b5a634`, 2026-10-01)

`runtime-assets.json` worker-mirror roll:

- `active`: `a4fd9a6b...` triple → `8f991672a8a8caaa98df16ad94adc22e0183ef99`
  / `18e1fb11a17b2a930f40d705361dfbd43d24126c` /
  `d252a777288a93f0aeffe9a8bd457d64a9b45750ff6bd4da2a03a92e6f109130`.
- `previous`: `e86808cc...` triple → the outgoing `a4fd9a6b...` triple.
- `signing_key_id` / `trust_epoch` / `protocol_versions` unchanged.

### 2.5 REPUBLISH-14 — pin approved mirror release `751755fe` (`fb9249646ec3ab818d2401c317ae19a260795c84`, 2026-10-02)

`runtime-assets.json` worker-mirror roll:

- `active`: `8f991672...` triple → `751755feb74205d80e63a27a2edfba5656f52684`
  / `224b055e6a23fd49118857b7209b835e8d3ec7c1` /
  `c36fa2aca733dd702f5ae86ec2f206d0b594e6504f88eb413a7e71f9629d23d0`.
- `previous`: `a4fd9a6b...` triple → the outgoing `8f991672...` triple.
- `signing_key_id` / `trust_epoch` / `protocol_versions` unchanged.

### 2.6 REPUBLISH-15 — pin approved mirror release `0c401f1c` (`3306260fd2f2beac4c9b19dd71905c190c156e16`, 2026-10-02)

`runtime-assets.json` worker-mirror roll (superseded as §1.5 current by REPUBLISH-23, §2.10):

- `active`: `751755fe...` triple → `0c401f1c4012cde432f40a3c27878464767c5c91`
  / `da6be0f59c751e4ebe0535d1b66813563081f0ce` /
  `7c9fabd5a240424a67e270e9e5747fbe01aef0de69dfe0b081193ffcfea88f26`.
- `previous`: `8f991672...` triple → the outgoing `751755fe...` triple.
- `signing_key_id` / `trust_epoch` / `protocol_versions` unchanged.

### 2.7 GO-FLIP-1 — production GO trust flip (`40e2d96bc8459b4649e233dbcca350072a7e28cb`, 2026-10-02)

The user-authorized GO decision turned `config/client-update-trust.json`
from the disabled template into the reviewed production policy, in one
commit with its coupled tests and the audit script (4 files: the policy +
`scripts/check_client_release_gates.py` + `tests/test_client_release_gates.py`
+ `tests/test_bootstrap_asset_host.py`):

- `enabled`: `false` → `true`.
- `root_keys`: `{}` → `root-2026-10` (generated by the offline key
  ceremony outside the repo; root-seed escrow is a standing user action).
- `release_key_authorizations`: `[]` → one root-signed authorization for
  `release-2026-10-go1` (epoch 1, stable/windows, window in §1.6).
- Production gates: the seven `false` gates → `true` (all nine now `true`,
  §1.3).  Gate names and the code set unchanged.
- `scripts/check_client_release_gates.py`: `policy_valid` now certifies the
  closed-set policy shape only; `release_allowed` = shape ∧ enabled ∧ all
  gates, and `--require-release-ready` keeps failing closed on any disabled
  policy.
- First signed manifest published to `client-v0.1.0`:
  `release_id=goflip1-20261002`, manifest SHA256
  `31eff1d0527843b4cc91632e449addf0e0b02d9ab8eddba6ba7e58e381dbd626`, zip
  `f59e4a368724a17b6bcfd8b181c1607f338565e729c6f26816d9f16c4044de48`,
  source `40e2d96` (tree `22ce21ba8d19edd0238657ec938b6ab0ab9154e6`),
  window 2026-10-01T20:26:22Z → 2026-10-15T20:26:22Z — superseded the same
  day by REBUILD-5 (§2.8).

### 2.8 REBUILD-5 — canonical installer + update-package re-sign (2026-10-02, build pin `70cae5e`)

No repo commit — tracked tree untouched; this is the release-face successor
to §2.7's first publication:

- Canonical setup: `d7b22968acb250dd281ea11a5a03b48dc5ebb1e88b04a3312f376d74eb4725fe`
  (REBUILD-4) → `cc5c8bf812d1c543bd26072c4412801ec5de02605f853ff1a28e100a08d3d789`
  (24,343,138 B), built at pin `70cae5e`; checksum sheet regenerated
  (SHA256 `877c56230c08784c9795e29b79696c2b8413e5601d3562ef9ae027abf79c6ff3`).
- Signed manifest re-signed with the *same* `release-2026-10-go1` key (no
  key generation, no rotation): `goflip1-20261002` → `rebuild5-20261002`,
  manifest SHA256
  `e8a54ed852fa418d2272f75e6d9d4e1c2f30a2c93d6facf1124bff27c0c59797`,
  source pin `70cae5e16c1185db9fee670e51e950397a965e16` (tree
  `5de5792b3f87fa74c3175e372ab58ca908d34787`), window
  2026-10-01T22:13:28Z → 2026-10-15T22:13:28Z.
- Update package zip: `f59e4a36...` →
  `af4fb8e2fe33693a3f562516a22fd19ac403d4cc503e324af59bb0657ddfb298`
  (1,263,818 B); the zip ships the `0c401f1c` mirror pin (§1.5).
- Trust policy asset (`ebc24d0c...`) and `windows-security-prompt.md`
  untouched on the release face.
- No value in sections 1.1-1.6 moved: the mirror pin stays on the
  `0c401f1c` ring and the signing identity is unchanged.

### 2.3 WINIT-1 case 1 — release repository rename to the temporary `-Release` portal (2026-10-02)

- `distribution_repository` `gualtier-xu/Fudan-CourseLens-Worker` →
  the temporary `-Release` portal (the post-rename literal name is elided
  in this public sheet) across `config/distribution.json`,
  `config/client-update-trust.json` (`distribution.repository` +
  `manifest_url`), `src/distribution.py`, `src/update/service.py:62`,
  both `runtime-assets.json` worker-mirror `repository` fields, the
  release workflow, and the coupled tests/docs (PKG-WINIT1-A,
  single-writer batch). Mirror pins (`dc5cce70` ring), the signing
  identity, and `source_repository` (the auxiliary-account private source)
  are
  unchanged; no mirror release is triggered.
- The GitHub-side rename (Settings → Rename) is a later gated step
  (PKG-WINIT1-C); until it lands, the pins name the post-rename
  identity. Old-name links keep working via GitHub's redirect until
  the owner reclaims the old name for the personal executor.
- Section 2.2 history above preserves its original values verbatim.

### 2.9 REBUILD-7 — 0.1.0 定稿换装构建备妥（2026-10-04，build pin `7ce66a7`；上传=晨间外写裁决）

No upload yet — the five rebuilt assets are staged and hashed; the swap
itself is the morning execution sheet (`release-checklist.md` §7).  What
was built, from the frozen commit `7ce66a7f50e791be83ebc5cd7660930609989598`
(tree `e98919c3…`, first 0.1.0 build that also carries the 10-03/04
client fixes):

- Canonical setup: `a4ef6037…`/24,352,092 B (REBUILD-6) →
  `640fb3a7…` (24,363,498 B), built 2026-10-04 00:48:26+0800; checksum
  sheet regenerated (SHA256 `c95b0185…`).
- Signed manifest re-signed with the *same* `release-2026-10-go1` key
  (no key generation, no rotation): `rebuild6-20261002` →
  `rebuild7-20261004`, manifest SHA256 `21151d79…` (4,800 B), source pin
  `7ce66a7…` (tree `e98919c3…`), window 2026-10-03T16:49:41Z →
  2026-10-17T16:49:41Z, `minimum_security_version` 0.1.0, release notes
  = the 0.1.0 final body (3,683 B).
- Update package zip: `4f83e7db…` → `2b6f0384…` (1,288,198 B, 135 files);
  the zip ships the `1296e926` mirror pin ring (the §1.5 current ring at
  the time; superseded by REPUBLISH-23, §2.10).
- `windows-security-prompt.md` refresh prepared: repo current
  (`e2dfb52b…`, 3,725 B) carries the ASCII checksum filename and the
  renamed-repo Issues link that the online `2bfa7dd9…` (3,620 B) predates.
- Trust policy asset (`ebc24d0c…`) untouched; tag `client-v0.1.0`
  unchanged; **`releases/latest` still resolves to `client-v0.1.1` until
  the §7.2 prerelease flip runs** — that flip rides the same morning
  authorization, without it the update chain keeps serving 0.1.1.
- Update-chain semantics note (src/update/service.py:772, strictly
  greater): a 0.1.0 manifest is `up_to_date` for every existing 0.1.0/0.1.1
  install — the swap pushes nothing to anyone and downgrades no one;
  existing-install migration stays a separate decision.

### 2.10 REPUBLISH-23 — pin approved mirror release `45dd6230` (`b30ffa678806b4ba0e8af688daf7054c905cbd20`, 2026-10-05)

`runtime-assets.json` worker-mirror roll (current state, section 1.5):

- `active`: `6a3c7179...` triple → `45dd6230a7aaf721c8f14d8ca3451dc996f0408f`
  / `2c64f5c76fd0fd9343088fc8a89c82367dd054e1` /
  `50017855c9a5929cf51c6a3c6c1f8379b4450888da915402fde8e1b19f13f442`;
  `repository` moved from the temporary `-Release` portal back to the
  canonical `gualtier-xu/Fudan-CourseLens-Worker`.
- `previous`: `1296e926...` triple → the outgoing `6a3c7179...` triple
  (repository then the temporary `-Release` portal).
- `signing_key_id` / `trust_epoch` / `protocol_versions` unchanged.

Sheet-sync note (README-SYNC, 2026-10-06): section 1.5 had rotted at the
`0c401f1c` round (REPUBLISH-15) while the pin file had already rolled
through REPUBLISH-18/19/20/21; this sync moves §1.5 straight to the
REPUBLISH-23 state per the README-SYNC mandate.  The intermediate rings
(`25ac5807` / `fafbbf06` / `1296e926`) are recorded in their pin-commit
messages only; backfilling §2 for them is left to the docs-sync lane.
Backfilled 2026-10-06 by DOCS-SYNC — see §2.11 (and §2.12 for
REPUBLISH-24, which pinned after this note was written).

### 2.11 REPUBLISH-16→21 ring backfill (rings landed 2026-10-02 → 2026-10-04; recorded 2026-10-06)

Six rings that moved `runtime-assets.json` while this sheet's history had
stopped at REPUBLISH-15 (found by README-SYNC; backfilled by DOCS-SYNC).
Each ring's triple was reconciled bit-for-bit against its pin-commit
message in this repo and its execution result file.  Each ring's
`previous` is the outgoing triple of the ring above; `signing_key_id` /
`trust_epoch` / `protocol_versions` unchanged throughout.

- **REPUBLISH-16 — `dc5cce70` ring** (pin `593a5c0e374beb0dbd82a72f4a53fa156db0ad1e`, 2026-10-02):
  active `dc5cce7088b1d092ce40048628c4e30f4877e23f` /
  `c8ae0eafd9f7665404329375b7e168ab0774303e` /
  `0ae496aaa089d748b98fac227401ce04ec5b0078fc738ec3fc95de61eaf66651`;
  previous ← the `0c401f1c` ring; PR #7; media-prefetch integrity gate
  (`media_prefetch_incomplete` fail-closed + exactly-once re-signed
  re-fetch; source `225487a` = `6eb428d` + one allowlist row, worker
  payload unchanged by the row); `wire.py` unchanged; payload files 90.
- **REPUBLISH-17 — `083521e3` ring** (pin `7f20d4c45e9022295ed5db51396d9e0e1ea47049`, 2026-10-02):
  active `083521e36d5aecb112491afdb2f8641f16b093b8` /
  `330a5738d2082855cb30a7b1c744c9f441e6a99f` /
  `739f1c41d609f662dfece83d0dbc328219c7a24f97c4f75d01fc85754451964e`
  (repository field already the post-rename temporary
  `-Release` portal); previous ← the
  `dc5cce70` ring; PR #8; N10 first landing — `funasr-onnx==0.4.3` ct-punc
  engine restore + R3-06/07/08 llm plumbing convergence (source
  `5c6898d6`); payload files 90.
- **REPUBLISH-18 — `25ac5807` ring** (pin `2fca8be60678d672618c1c09514bed55cc6a45b4`, 2026-10-02):
  active `25ac58072811f7eb66a55fd523c23e02709373ff` /
  `d164970fbf9622b276b6833337dcb0c23ca546ec` /
  `8bdd048c26f51610934836c45896d69c010f782d28005fc97e9de92cde64fd57`;
  previous ← the `083521e3` ring; PR #9; THINK-LADDER-1 Phase 2 judge
  thinking tier wiring (the process workflow's env line, `vars||low`
  fallback,
  source `66d21548`); payload files 90.
- **REPUBLISH-19 — `fafbbf06` ring** (pin `b53b4662e48ef2a7a1edd37ae517c6357e79493b`, 2026-10-03):
  active `fafbbf06c216441d718248ecabba4c34001b2018` /
  `c121a78cb8e7040b08ca564916a2127c93be4616` /
  `4aa2fd53af7ba3b437b2a5fe6238d86eaf9f3d2389254240c1ad4b06ff28eb3d`;
  previous ← the `25ac5807` ring; PR #10; N17 defuse — the `funasr-onnx`
  pin removed to clear the ResolutionImpossible P0 (source `733a0e8f`);
  ct-punc back on the honest lazy-import fallback; payload files 90.
- **REPUBLISH-20 — `1296e926` ring** (pin `fe3b6a022de4eba2de4cae786e79ad84d73eca6e`, 2026-10-03):
  active `1296e9267b4b340a669d0ff37003f57009611d98` /
  `dd58d841f69b40f4506a1820d1f05090ab10420e` /
  `34778133dcfe67777374cde3bb5c4a55b8841d646c9b70d2a273a10cbd86322e`;
  previous ← the `fafbbf06` ring; PR #11; N10 re-land (split `--no-deps`
  install with six explicit transitive pins + a ci punct-inference
  real-inference gate, source `c1110c6`) + N1 media-free `llm.yml` fast
  path for pure-LLM jobs (source `f08b6c0`).
- **REPUBLISH-21 — `6a3c7179` ring** (pin `82a0ba5b058ed265600ff7f0c0b14cae007832d6`, 2026-10-04):
  active `6a3c71799120be4d35f05e5ec85a5be098fcc1e5` /
  `df60eaf7f6732bd59cf1df19d66eac4d711ae13b` /
  `a6aa8817038a6d6979313ccfe4395835c849d8bab4d3e6fdd8574552839f62b1`
  (repository still the temporary `-Release` portal);
  previous ← the `1296e926` ring; PR #12; H1/N21 same-root-cause fix
  (runner count block moved below the metrics construction) +
  N18/N13/N15/N8/N16/N20/N5+N19 batch, eleven linear commits `94aa9cb →
  4a49903`; payload files 96.

REPUBLISH-22 never became a ring: it was declared a candidate batch in the
REPUBLISH-21 result (N2-2a, the TL-Phase2 worker legs, N10 sherpa, N12) and
its feature set shipped in the REPUBLISH-24 ring instead (§2.12); no pin
commit exists between `82a0ba5b` and `b30ffa67`.

### 2.12 REPUBLISH-24 — pin approved mirror release `4fd3efc4` (`b6c9d7e36fb2de6f345a02299628e9c367625cff`, 2026-10-06)

`runtime-assets.json` worker-mirror roll (current state, section 1.5):

- `active`: `45dd6230...` triple → `4fd3efc49ed8567cd0fcbc629fdea72887c1727a`
  / `267180ef09f92ab249a0e0c486a855f6b792b0de` /
  `68a5da10eb77dec127a496ede11fa754a88d68c30c7baf2a9608aa969e481b86`;
  `repository` stays `gualtier-xu/Fudan-CourseLens-Worker`.
- `previous`: `6a3c7179...` triple → the outgoing `45dd6230...` triple.
- `signing_key_id` / `trust_epoch` / `protocol_versions` unchanged.
- Ring payload (TL-PHASE2-1 batch, source `23f8acf`): two-stage quality
  judge (flash screen + adjudicated pass, `stages` report key), the
  term-boundary ruling face, N2-2a judge co-run on summary jobs, and
  login-probe sub-leg telemetry rows — worker commits `c39d5ae` /
  `7577506` / `9fba847` / `23f8acf`; PR #14 with all five substantive
  gates green (including the punct-inference real-inference gate);
  payload files 96 (tree `9543e2dc`).

### 2.13 REPUBLISH-25 → 28 ring sync (rings landed 2026-10-07; recorded 2026-10-07, DOCS-CHAIN-2)

Section 1.5 had rotted at the REPUBLISH-24 round while the pin file rolled
through REPUBLISH-25/27/28; this sync moves §1.5 to the REPUBLISH-28 state
and records the intermediate rings from their pin-commit messages in this
repo.  REPUBLISH-26 never became a ring: its `ce43fb50` triple was voided by
the public gitleaks CI red, PR #16 was superseded-closed, and the source fix
landed as `77d54d9` (GITLEAKS-FIX) before the re-land.  `signing_key_id` /
`trust_epoch` / `protocol_versions` unchanged throughout; `repository` stays
`gualtier-xu/Fudan-CourseLens-Worker` (renamed back from the temporary
`-Release` name; the old name redirects).

- **REPUBLISH-25 — `4b04d90f` ring** (pin
  `2d61a166fb10060fbf37f111bc82c81547179178`, 2026-10-07):
  active `4b04d90fda98b8221dcf6b28dfc25afb0edcc46a` /
  `05a2fe460e02c7f8b000b898436616853054a9d9` /
  `88ecf9c4ecdbc2f8d2899affad6d8426ed25b43ef64d587727dd353fec104ca3`;
  previous ← the `4fd3efc4` ring; PR #15 (five gates green);
  source `f9a160c` SMART-SCHED-RESUME + `d5690af` E2E2-FIX defect B.
- **REPUBLISH-26 — no ring** (voided): the `ce43fb50` / `c5a3972b` /
  `59816942` triple was voided (gitleaks CI red on the mirror patch face,
  job 112731740677); PR #16 superseded-closed, generated branch deleted.
- **REPUBLISH-27 — `1b78d0bb` ring** (pin
  `cfd15e24293839c22bdc8d774b7b9134663f7841`, 2026-10-07):
  active `1b78d0bb3b0a28e6c6b8c431d189d3d229f00e66` /
  `41f8fc2ab1a42080777a03defdf97ad8aee46818` /
  `a78d8c3c717498daf7f2063f88cdba6075cac66333ddc39e2f897386273d44de`;
  previous ← the `4b04d90f` ring; PR #17 (five gates green incl. gitleaks);
  source `77d54d92e5c1c04fb2ca490420613ce2ac3d5058` GITLEAKS-FIX.
- **REPUBLISH-28 — `d13a832b` ring** (pin
  `2a81f48152806709588c7020cfc4a1e21532e212`, 2026-10-07; superseded as
  `active` by §2.14, retained as the last pre-rebuild `previous`):
  active `d13a832b60739f925bb535e62beb6164a96f57fb` /
  `181c2e96c5b58c5416d35e03df1e53a6a7014fd1` (= the public commit's real
  tree incl. 5 metadata; the manifest's `payload_git_tree` is `f99bde80`) /
  `84d88f084788a62df342c7b0ab730799a9d797b669e33ff56865a413146e94d7`;
  previous ← the `1b78d0bb` ring; PR #18 (five gates green; the mirror
  structural-policy gate's expected structural red explained per the
  R21-R27 precedent); source `96be49f`
  WF-SHELL-CLEAN (public Actions empty-shell workflows root-removed).

### 2.14 RELEASE-0.1.0-REBUILD-R2 — 同名删仓重建 genesis（2026-10-08；pin `c06bf4a5`）

用户 2026-10-07 两度明令「发布前可以进行删仓库重建以保证完全干净」，夜间
全自动窗内一次收口；删前双仓全值归档见结果文件
`product-release010rebuild-result-20261008.md`。

- **两仓同名删除重建（URL 不变）**：总仓 id 1362691068 → **1409276109**；
  镜像仓 id 1396799900 → **1409276347**（均 public/main/空仓起步）。旧公共
  历史（含 `d13a832b` 及全部先代 ring 提交、旧 `client-v0.1.0` release 资产
  setup `1f8a492d` 代）随删除消灭；删除前资产 SHA256/分支面/Pages 状态已
  归档入结果文件。
- **总仓净树 genesis**：`c1e26aacec3b54080cc33b0bc570a1ee2bff54c4`
  （tree `7dc71798801927fece77135415d527f80276960a`，孤儿单根，署名
  Gualtier Xu；15 文件=README+Pages v5+DEPLOYMENT+technical 双政策+
  macOS CI 工作流；12 闭集模式零命中）。Pages（legacy/main /docs）重建后
  live 200（v5 终态标题抽验过）。
- **镜像新仓首发布（空仓首 pub 模式）**：导出 file_count=107
  （manifest_sha256 `8c66dab5d9ed23b0db347d024370a82e5f63c94c32a544e74477f839e4c94844`，
  payload_git_tree `3077c843`），孤儿单根提交直落 main=
  **`f60f9b058c5aeaee08be9423eec644a72da7c8d8`**
  （tree `68550700b393b6991d504f94b86beeb61fda7434`）；五门 CI
  run 37675863303 全绿（gitleaks 绿；无 PR 面=镜像结构 policy 门未触发，先例
  评论待下一常规环）。
- **pin 前移**（`c06bf4a5cffa38993a54f410932c445871287e02`，恰 1 路径）：
  §1.5 表即现值；深比较 11/11+checker
  `--go WORKER-RECONSTRUCTION-GO` passed；回声=echo run 37681268984
  success → `ready_for_dispatch=true`（回声主体=携新 pin 的 dev 树实例；
  途中经正典 repair-worker 同步辅助账号派发仓至新模板）。
- **发布资产换代（r2b）**：`client-v0.1.0` release 五资产以含新 pin 的
  冻结树 `c06bf4a5` 重开构建并 clobber 重灌（release_id
  `rebuild-r2b-20261008`；setup SHA256 `04ab4088…`；manifest
  source=c06bf4a5；§1.7 表随之刷新）。首版 r2 资产（f5098d8 冻结、内嵌
  R28 旧 pin）因删仓致 pin 指向已灭历史而作废重构，未产生有效分发。
- **信任根零变化**：`release-2026-09-cleanroom-r3`/epoch 1 与
  `release-2026-10-go1`/epoch 1 零轮换零新生成；辅助账号名下实例仓
  （worker/mailbox）零触碰，其派发仓模板由正典 repair-worker 流同步。

### 2.15 RELEASE-0.1.0-REBUILD-R5 — 第二轮同名删仓重建 genesis（2026-10-08；pin `7a5c422`）

R2 临时发布后当日并入全部修复（大字幕提速、窄窗适配、Pages v6、四链文档
公开版、INSTALL-ICONFIX 安装器图标注册修复），R4 冻结A 导出/孤儿构建/pin
前移完成后死于 GitHub 1302 限速，R5 自冻结B 续完；删前现值归档见结果文件
`product-release010rebuild-result-20261008.md`。

- **两仓同名删除重建（URL 不变）**：总仓 id 1409276109 → **1409759470**；
  镜像仓 id 1409276347 → **1409759635**（均 public/main/空仓起步）。旧公共
  历史（含 `f60f9b05` 环、R2 genesis `c1e26aac`、旧 release id 406093889）
  随删除消灭；删前现值已归档入结果文件。
- **总仓净树 genesis**：`5ec8356081972e9fc9cb901873f983b8c2092f8e`
  （tree `d29775252a1ee4349e4daa833e9d7f695421e063`，孤儿单根，署名
  Gualtier Xu；**19 文件**=README+Pages v6+DEPLOYMENT+technical 双政策+
  **四链文档公开版**（release-chain/update-chain/update-chain-values/
  packaging-chain）+macOS CI 工作流；12 闭集模式零命中）。Pages
  （legacy/main /docs）重建后 live 200（v6 终态标题/资产/链文档抽验过；
  2026-10-09 注：live 停留 v6=R5 genesis 态，树内已前行至 v8
  （`21c68de`/`50652e4`/`c7065b6` 三笔），待 R6 genesis 一步上线终态——
  §2.16）。
- **镜像新仓首发布（空仓首 pub 模式）**：R4 冻结A 导出复用
  （file_count=107，manifest_sha256 `1f38db36…`，payload_git_tree `a1a6b980`），
  孤儿单根提交直落 main=
  **`f2266c416da6febd0cd4f01277668c68aee0761c`**
  （tree `87517ffa1af6b9e22cf7be91b02689b200bc290d`）；五门 CI
  run 37730950799 全绿（gitleaks 绿；无 PR 面=镜像结构 policy 门未触发，
  先例评论待下一常规环）。
- **pin 前移**（`7a5c422f97a079f251cdca37b5cf25cc5ee5c74b`，恰 1 路径，
  R4 死前已落树）：§1.5 表即现值；深比较 11/11+checker
  `--go WORKER-RECONSTRUCTION-GO` passed（evidence_sha256 `43cb30d5…`）；
  回声=echo run 37731537229 success + 实例 test-channel echo
  （task `43b259f4…`）→ `channel_test_valid` →
  `ready_for_dispatch=true`（回声主体=携新 pin 的 dev 树实例；辅助账号派发
  仓由派发前自动 repair 同步至新模板，tree `87517ffa` 恒等）。
- **发布资产换代（r5）**：`client-v0.1.0` release 五资产以含新 pin+图标修复
  的冻结树 `b50560f` 重开构建并首发（release_id `rebuild-r5-20261008`；
  setup SHA256 `e651916f…`；manifest source=b50560f；§1.7 表随之刷新）。
  R4 死前旧构建（同 pin、缺图标修复）作废未分发。
- **FRESHRUN 门（真机保留安装）**：新 setup.exe 实机正式安装=用户装机注册
  恢复（HKCU 卸载键+桌面/开始菜单快捷方式；`[Run]` exit 0=INSTALL-ICONFIX
  实证）；五腿全过（真实 UIS 登录/目录 21 门/课表 7 学期/播放 206）+计时×3
  中位 3.968s。机上 0.1.1 测试残留受管根整体改名保留
  （`CourseLens-pre-r5-20261008`），处置归用户。
- **信任根零变化**：`release-2026-09-cleanroom-r3`/epoch 1 与
  `release-2026-10-go1`/epoch 1 零轮换零新生成；trust 三件跨三代逐字同
  （装树与冻结 config SHA256 前 16=4538758367307f06 恒等）；辅助账号名下
  实例仓零触碰，其派发仓模板由自动 repair 流同步。

### 2.16 RELEASE-0.1.0-REBUILD-R6 — 第三轮同名删仓重建 genesis + mac prerelease 首发（2026-10-09 发布环现值）

R6 定义（用户令）：干净仓真首次发布——删仓重建两仓 → 总仓 genesis →
`client-v0.1.0` 真首次 + **mac prerelease 首发** → pin 前移 → 回声。R5 临时
发布代之后并入的全部修复（REALRUN-FIX 等四笔学生可见修复、MAC-WIRE-2
含 R12 一行修、FAULT-MATRIX、AVATAR-POLISH 走查批、PAGES-V8 三笔、
MAC-FONT-1 等）随本代入发布面。删前现值归档：总仓旧 id 1409759470、镜像
旧 id 1409759635、旧 release client-v0.1.0（id 406434415，R5 五件 digest
见 §1.7 R5 表）、Pages legacy/main /docs=v6 live、macos-dev=53effd2
（MAC-WIRE-2 出厂快照，删前本地保全）。

- **两仓同名删除重建（URL 不变）**：总仓 id 1409759470 → `1411171710`；
  镜像仓 id 1409759635 → `1411171779`（均 public/main/空仓起步）。旧公共
  历史（含 `f2266c4` 环、R5 genesis `5ec83560`、旧 release id 406434415）
  随删除消灭；删前现值归档入 R6 发布结果文件。
- **总仓净树 genesis**：19 文件基=README+Pages v8（docs/index.html，
  树内 `21c68de`/`50652e4`/`c7065b6` 三笔现值）+DEPLOYMENT+technical 双
  政策+四链文档公开版（本笔填实版）+macOS CI 工作流；12 闭集模式零命中。
  Pages（legacy/main /docs）重建后 live 200（v8 终态标题/资产/mac 小字串
  抽验随发布环终验）。
- **镜像新仓首发布（空仓首 pub 模式）**：删仓前本地预持新环对象（R4/R5
  时序教训）：导出 file_count=`107`（manifest_sha256
  `c53ba235081baeeea8e3716075c135e4e2272585eeb44b7eeaebe172fcee11b5`，
  payload_git_tree `a1a6b980d07cea1e2008968f0ff913f18f048781`，与前两代
  载荷树恒等=worker 面未变），孤儿单根提交直落 main=**`022e84f94fdf6ffe6daf18b3ed09ab739b2c7ddb`**
  （tree `390f22da95502ca4f782036be0f6f9aff335e68a`，author=CourseLens
  Worker Mirror 同构）；五门 CI run `37873885366`（gitleaks 绿；无 PR 面=
  镜像结构 policy 门未触发，先例评论待下一常规环）。
- **pin 前移**（`9650c9b44cc217224d95c7b362655b225b78c5f5`，恰 1 路径
  runtime-assets.json，先于删仓落树）：§1.5 表即现值（active=新三值、
  previous=`f2266c4`/`87517ffa`/`1f38db36` 原串）；深比较 13/13 断言过+
  checker `--go WORKER-RECONSTRUCTION-GO` passed（evidence
  `eeefc3ab…`）；回声=镜像核验+实例 test-channel echo →
  `channel_test_valid` → `ready_for_dispatch=true`（echo run
  **37875780163** success，回声主体=携新 pin 的 dev 树实例，R2/R5 先例
  语义）。发布环实况：辅助账号名下实例仓（worker/mailbox）开工时已被删
  （非本环所为，本环外写=恰两公共仓同名重建），经产品 bootstrap 链重建
  （镜像仓补 `is_template=true`——旧仓为模板仓、generate 开仓依赖该标记，
  同名重建时遗失致首跑 bootstrap 404 `resource_missing`，补标后一次过）。
- **发布资产首发（r6）**：`client-v0.1.0` release（id 407431898，tag 挂
  本 genesis）五资产以含新 pin 的冻结树
  `9650c9b44cc217224d95c7b362655b225b78c5f5` 构建首发（release_id
  `rebuild-r6-20261009`；五件哈希表与回声判据=§1.7 现行表；FRESHRUN 门=
  独立 AppId 沙盒全旅程 PASS：装→ARP 注册断言〔含 UninstallDisplayIcon〕
  →启动探针×3→首跑引导 new→completed→卸载→数据保全 322 件+真装零触碰）。
- **mac prerelease（R6 新增步，R5 无此步）**：macos-dev 分支=`55a2034`
  （本代 private/main 冻结树全树的孤儿快照单提交，同 MAC-WIRE-2 出厂配
  方，含 R12 一行修与 MAC-FONT-1）；CI run **37874667751** 全绿
  （绿门+py2app build）→ artifact=courselens-macos-test-build → 以独立
  tag **client-v0.1.0-macos-test**（release id 407443867，target=macos-dev）
  发布：`prerelease=true`+资产 `CourseLens-0.1.0-macos-arm64.zip`
  （SHA256 `07e0c95664d16eae72096c5481b99827ee3d80abf52d1df5fe70063ead5e26fa`，
  55,636,891 B）+`SHA256SUMS.txt`（`477e64ff…`）；**latest 不动=硬护栏已
  断言**——发布后 `releases/latest` 实测仍解析 `client-v0.1.0`
  （prerelease=false）的 Windows manifest（`rebuild-r6-20261009`）；未照抄
  release-checklist §7.2 步骤 4 的 latest 翻转语义（方向相反，复用即
  manifest 404=更新链断）。
- **信任根零变化**：`release-2026-09-cleanroom-r3`/epoch 1 与
  `release-2026-10-go1`/epoch 1 零轮换零新生成；trust 三件跨代逐字同
  （root/trust/trust.sig 对前代 f2266c4 逐字节对比过）；辅助账号名下实例
  仓=上文 bootstrap 链重建（非本环删除），其模板由 bootstrap/repair 流同
  步至新代（同步 push 触发实例仓 Public worker CI run 37875688741 绿）。
- **R6 与 R5 差异注记**：①**mac prerelease 新增步**（上条，含 latest 不动
  硬护栏）；②**Pages 树内已 v8**——R5 genesis 时点树内=live=v6 无落差，
  R5 发布后树内先行走到 v8（live 停 v6，§2.15 注记在案），R6 genesis 直载
  v8+mac 小字终态、live 一步到位追平；③**构建基含 R5 后学生可感增量**——
  b96dc1f/50ef718/86dcadc/86c6fd2 四笔（R5 构建基 b50560f 不含，
  RELEASEREADY 实证=R6 存在理由）+R12 一行修（`ce32cca`）+Pages v8 走查
  批+MAC-FONT-1；④**时序同 R5**：pin 前移先于删仓（新环孤儿对象本地预
  持），避免 R2 首版资产内嵌已灭 pin 的作废重构教训。

### 2.17 D12-FIX-1 — 常规镜像环：D-20261009-12 pure-LLM 装机双钉根修上镜（2026-10-09）

REMOTE-E2E-1-R4 真跑定谳 P1 级缺陷 D-20261009-12（`worker/llm.yml:54`
六钉装机清单漏 pycryptodome，platform_session.py 硬导入族
Crypto+curl_cffi 双缺——AST 全闭包审计定谳只补其一不能结案）→ 91a79f7
fix(worker-llm) 恰 2 文件（llm.yml 装机行六钉→八钉
`pycryptodome==3.23.0`+`curl-cffi==0.15.0`，与 requirements.txt 权威钉逐字
对齐；守卫测试同改+两防漂移钉：装机行↔requirements 逐字核对、
platform_session 导入名↔发行名闭集核对；变异验证=回退六钉 4 处红）。
镜像环（本车道 D12-FIX-1 执行）：

- **A1 导出**：file_count=`107`｜manifest_sha256
  `5ea0843dc2b40c9081ad5600195d0a88c7380e839640fc8524b77000f064179e`｜
  payload_git_tree `941c0e79300020e5edc0716637d220dd344b01b8`（源
  dd465e1，91a79f7 祖先+导出面零增量）；112 件对账齐。
- **A2 发布（PR 流回归）**：孤儿树=生成树=`853eeecd0a03155b97df420cc10b62357227f5c4`，
  generated=`902d6ea6`，镜像 **PR #1**（批语义 body+policy 结构红说明评论
  `#issuecomment-6078881710`，R21-R26 先例同构）；**五门 CI 全绿**
  （boundary 7s/gitleaks 8s/protocol 8s/punct-inference 1m31s/unit 1m12s
  ——unit 门=八钉守卫+新钉测试在镜像 CI 实跑过）。
- **A3 squash 合并**：官方 main=**`e698d2ae3d25277604de0cfb3ec34864b796b036`**
  （单父=`022e84f9` 线性，树恒等生成树）；generated 分支已删。
- **A4 pin 前移**：solo=**`3866f59f79367e236e40fba77222f5985f217401`**
  （恰 1 路径 runtime-assets.json 12+/12-，零 push；13/13 断言形态，含
  verify_manifest_document 过门/信任三件 vs `022e84f9` 代零轮换/私有源
  提交公共仓不可达/previous 字典恒等——§1.5 表即现值）。
- **A5 checker**：`verify_public_template_reconstruction --go
  WORKER-RECONSTRUCTION-GO` passed（public_commit=`e698d2ae`/tree=
  `853eeecd`/source=dd465e1/files=107，evidence_sha256=`eddf64fe…`）+
  邻接 drill 套件 2P。
- **A6 回声+真跑复验（R4 落库讲次 40690/663596）**：dev 实例携新 pin →
  worker_tree_drifted 现形 → 产品 repair-worker 动作同步实例仓至
  `e158c9c3`（树=`853eeecd` 新环）→ test-channel echo（run `37918215278`
  success）→ **channel_test_valid → ready_for_dispatch=true**；真实登录
  （既有客户端流，4s ready）后重发总结任务 `eaf9c269`：llm.yml run
  `37918657141` **conclusion=success**，e2e 425.8s，timestamp_summary
  16297 字/63 章/6 带锚要点 + lecture_ir/quality_report/review_views/
  lecture_chapters 五产物全落——**D-20261009-12 结案（SUMMARY
  REPLAYED-PASS）**。问答复验（证据命中案「光刻胶」重放）：失败定因=
  `job kind learning_pack requires workflow profile process-v1 (got
  llm-v1)`=**恰 D-20261009-13 原形**（未修独立缺陷，非本车道域；闭集
  profile 拒绝非模块错误=装机修复在问答路径同样成立）。

## 3. Sync checklist: if you change one of these values

Rule of thumb: a value change is only complete when every equality point,
its mirror in the docs, and this sheet move in the **same commit**.

| If you change... | You must sync | Verify with | Reference sections |
| --- | --- | --- | --- |
| Repository identities (distribution/source) | `config/distribution.json`, `src/update/service.py:61-62`, `config/client-update-trust.json` (`manifest_url` + `distribution.repository`), `runtime-assets.json` worker-mirror `repository`, release workflow, frontend help links, all docs mentioning the repo | `scripts/check_distribution_references.py` | `release-checklist.md` §1 (repository/URL reference census); [packaging-chain.md](packaging-chain.md) §11 (account roles) |
| Trust policy enablement, root/release keys (key ceremony, GO-class flip) | `config/client-update-trust.json` (`enabled` / `root_keys` / `release_key_authorizations`), offline key ceremony (`scripts/bootstrap_asset_host.py generate-root-key`; keys stay outside the repo), coupled tests + `scripts/check_client_release_gates.py`, the release-face `courselens-windows-trust.json` asset (kept byte-identical to the committed policy), operations manual; root-seed escrow | `TrustPolicy.load` self-check; `scripts/check_client_release_gates.py`; gate/bootstrap tests | `client-update-operations.md` (key + secret operations); [update-chain.md](update-chain.md) "Trust anchors" |
| Production gate set or names | `src/update/service.py:80-84` (`REQUIRED_PRODUCTION_GATES`), `config/client-update-trust.json` `production_gates`, gate tests, release workflow gate steps, operations manual + ADR note.  Flipping gate *values* is the GO-class decision above, not a rename | gate tests (`tests/test_client_release_gates.py`) | `release-checklist.md` §3 (release-day runbook) |
| Secret names | `config/client-update-trust.json` `required_secret_names`, release workflow consumption points, the actual GitHub environment secrets under `client-release-production`, operations manual, `scripts/bootstrap_asset_host.py` | workflow dry-run / bootstrap plan output | `client-update-operations.md` (key + secret operations) |
| Mirror pin (approved republish) | `runtime-assets.json` worker-mirror `active`/`previous` roll (commit/tree/`manifest_sha256`), mirror-release checklist | mirror manifest sha re-derivation | `release-checklist.md` §2 (mirror chain / trust trio / auxiliary-account sync) |
| Published manifest / update package (re-sign or new version) | sign from a clean clone at the pinned commit (`scripts/build_client_update.py`), upload manifest + zip (`gh release upload --clobber`), refresh the checksum sheet + release-body SHA line when the canonical setup moved, CDN byte roundtrip | roundtrip sha256 equality (api.github.com → CDN) | `release-checklist.md` §2-3; `client-update-operations.md` publish steps; [packaging-chain.md](packaging-chain.md) (canonical installer) |
| Manifest asset, tag namespace, URL form | `src/update/service.py:64-65`, `config/client-update-trust.json:40,45-46`, `config/distribution.json:5-6`, **and already-installed trust policies** (an installed policy whose `manifest_url` no longer equals `_stable_manifest_url()` fail-closes) | focused update-service tests | [update-chain.md](update-chain.md) "Channel pointing" + "Trust anchors" |
| Any value on this sheet | this sheet, same commit (refresh the anchor and the verification-basis HEAD) | this sheet vs `git grep` | — |
