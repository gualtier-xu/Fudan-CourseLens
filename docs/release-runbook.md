# CourseLens 发布日 Runbook（终稿）

发布日按小时执行的完整剧本。所有步骤均引用 2026-09-25 夜批实弹验证过的资产（N9-A/A2 包装链、N9-B 更新链彩排、N9-C 发布链 R9），**没有发明任何未验证步骤**。配套文档：`docs/release-checklist.md`（发布就绪清单）、`docs/packaging-chain.md`（包装链手册）、`docs/update-chain.md`（更新链手册）、`docs/release-chain.md`（镜像发布链）。

**纪律**：每一步先只读核对、再动手；任何一步验证不过就停在那里，不要跳步。渠道上传、公告发布涉及 GitHub 外写，属于独立授权门，不在本 runbook 的普通授权内（见 §5/§6 的硬门标注）。

---

## 时间线总览

| 时刻 | 阶段 | 产物 | 预计耗时 |
| --- | --- | --- | --- |
| T+0h00 | §1 灾备快照 | 仓库 bundle + 校验单 | 实测 ≈2 分钟（N9-R 2026-09-25 04:54–04:56） |
| T+0h10 | §2 构建安装包 | setup.exe + SHA256 | 实测见 §B 计时基线（smoke 一条链） |
| T+0h40 | §3 校验单生成 | SHA256SUMS 文件 | ≈1 分钟 |
| T+0h50 | §4 更新链彩排 | rehearsal-report.json（6 pass） | 实测见 §B |
| T+1h30 | §5 渠道上传【硬门】 | GitHub Release（manifest+包+签名） | ≈15 分钟（未演练，授权包内实测） |
| T+2h00 | §6 公告发布【硬门】 | Release notes + 公告 | ≈30 分钟（未演练，授权包内实测） |
| T+2h30 | §7 客户端验证 | 走查记录 | ≈60 分钟 |
| T+4h00 | §8 收尾观察 | 首个真实任务闭环证据 | 持续 |

---

## §1 灾备快照（T+0h00，删库/发布前必办）

N9-R 实弹验证（2026-09-25 04:54）：三证齐全。

```bash
# 1) 全历史 bundle（工作区 backup/ 目录）
mkdir -p <workspace>/backup
git -C <repo> bundle create <workspace>/backup/courselens-full-YYYYMMDD.bundle --all
# 预期：exit 0；实测 5.9MB（5918836 字节）

# 2) 完整性核验
git -C <repo> bundle verify <workspace>/backup/courselens-full-YYYYMMDD.bundle
# 预期输出两行：The bundle records a complete history. / The bundle uses this hash algorithm: sha1

# 3) 试克隆自检（换目录恢复一次）
git clone -q <workspace>/backup/courselens-full-YYYYMMDD.bundle /tmp/restore-check
git -C /tmp/restore-check rev-parse HEAD   # 必须与源仓 HEAD 逐位一致
rm -rf /tmp/restore-check                  # 按身份清理

# 4) 校验单（bundle + 全部 setup.exe 产物）
cd <workspace>/backup && sha256sum courselens-full-YYYYMMDD.bundle > SHA256SUMS-YYYYMMDD.txt
```

**失败停点**：verify 报错、克隆 HEAD 不一致 → 停止发布，先修备份。
**数据面注意**（引用 release-checklist §0）：`runtime/data` 学生数据另行整包备份；DPAPI 凭据密文换机不可解密，只记录「哪些账号哪天需重登」，不把密文当可恢复备份。

## §2 构建安装包（T+0h10）

一条链完成 构建→暂存门禁→沙箱装机→健康探测→按 PID 收尾（N9-A/A2 实弹，SMOKE PASS 全链）：

```bash
bash scripts/smoke_installer.sh 0.1.0 8808
```

**预期输出序列**（每段都是失败停点）：

1. `payload files=<N> size=<M>MB` + `GATES: OK` —— 暂存门禁（必备件+泄漏扫描）。
   失败停点：`GATES: FAIL`（缺件/泄漏/禁入目录）→ 查构建输入，禁止带病出包。
2. ISCC `Successful compile`（日志在 `/tmp/cl-smoke/out/iscc.log`）。
   失败停点：`ISCC failed` → 用 `python scripts/parse_installer_log.py <log>` 定位。
3. `SHA256: <64hex>` —— **与上一版发布同 HEAD 重建时必须逐位一致**（构建可复现；
   配方=工单文件 mtime 钉提交时刻，见 packaging-chain.md §8；净树口径与在途脏 iss 归因法见 §C；
   先例基线 9b5558a4，N9-R 第五收敛点）。
4. `HEALTH: {"ok": true, ...}` —— 沙箱静默装机后健康探测通过（重定向
   USERPROFILE/LOCALAPPDATA，全程不触碰真实受管根）。
   失败停点：`setup exit=<非0>` / `health never ok` → 查 `/tmp/cl-smoke/install.log` 与 `launch.out`。
5. `SMOKE PASS (port 8808, pid <N> stopped; artifacts in /tmp/cl-smoke)`。

前置：dev 树已备 `tools/python312`（捆绑运行时，不在 git 里）；ISCC 便携版在 `%TEMP%\pkgpv1-artifacts` 或标准安装。

## §3 校验单生成（T+0h40）

```bash
cd /tmp/cl-smoke/out && sha256sum CourseLens-0.1.0-setup.exe >> <workspace>/backup/SHA256SUMS-YYYYMMDD.txt
```

格式实例（N9-R 2026-09-25 实产 `backup/SHA256SUMS-20260925.txt`）：正文=严格 `sha256sum -c`
兼容两空格行（`hash␣␣文件名`），注记一律写 `#` 注释行（实测：行内括号注记会让 `-c` 报
malformed）。
**教训**（N9-R U4①）：校验单只收**实际在盘且哈希已复算**的产物；引用了找不到文件的哈希（如 G 车道 6cf8bd8d）= 可追溯缺口。

## §4 更新链彩排（T+0h50）

```bash
# 正典 venv（或任意装了 pynacl 的环境）
python scripts/rehearse_client_update_local.py
```

内容（N9-B 实弹 6 pass）：一次性干净克隆 → `build_client_update.py` 原样出包 → 127.0.0.1 一次性渠道 → 真·更新状态机 check→download→verify→extract→install→pending→apply→confirm + 回滚 pass + 3 个负例（坏签名等，fail-closed 必须拒绝）。
**预期**：`REHEARSAL-OK report=<stage>/rehearsal-report.json passes=… negatives=…`。
**失败停点**：任一 pass / 负例失败 → 更新链有问题，禁止进入渠道上传。
**边界**：脚本用一次性本地 Ed25519 密钥，生产密钥只在受保护发布环境；全程零外写零真实渠道。

## §5 渠道上传【硬门：须 GitHub 写授权，单独授权包】（T+1h30）

前置核验（只读）：`config/distribution.json` 指向正确仓库；`src/update/service.py:62 DISTRIBUTION_REPOSITORY` 与目标渠道一致（国庆重建后的头号裁决项，见 release-checklist §1）。

步骤：draft release → 上传 `manifest.json` + 更新包 zip（§4 同配方产出）+ 签名 → **先 private 后 publish**。
上传后只读自检：`curl -sL <releases/download/…/manifest.json>` 可达且 SHA256 与本地上传件一致。

## §6 公告发布【硬门】（T+2h00）

素材：`docs/release-announcement-draft.md`（底稿）+ `docs/release-notes-template.md`（模板）。

必带边界话术：
- **setup.exe 仅用于首次安装；升级一律走产品内更新通道**，不要重复跑 setup.exe（实证=release-checklist §3.1 三断言：静默重装 exit 0 假成功，受管链停在旧版本）。
- 安全与隐私架构段（README 主章节口径）：本地优先、DPAPI、零遥测、闭集外联。
- 文案讲人话：错误提示像耐心的同学，不用系统日志腔。

## §7 客户端验证（T+2h30，真实学生视角）

1. **干净机首装**（沙箱或真机）：下载 setup.exe → 校验 SHA256（§3 校验单）→ 安装 → 首启向导 → 复旦登录 → 目录/课程页可达。
2. **已装机器升级**：产品内检查更新 → 发现新版 → 确认 → 下载 → 验签 → 应用 → 重启进新版（§4 已彩排同一状态机；**绝不重跑 setup.exe**）。
3. 观察首个真实任务完整闭环：派发→runner→回传→导入→复习可见。
4. 卸载路径：卸载器默认「保留学习数据」（iss 数据保护红线：`{localappdata}\CourseLens` 不进自动删除清单，删除只在学生亲选「否」后）；重装后数据仍在。
   已知边界：Windows SAC 可能拦截未签名卸载器（N9-A 实录）——如实记录，降维用目录布局断言，不硬闯。

验收流程的完整步骤与通过判据见 [runtime-acceptance](runtime-acceptance.md)。

## §8 收尾观察（T+4h00）

- 渠道观测：release 下载量、更新检查失败率（客户端闭集错误码）。
- 首个真实任务闭环证据入档；「真实云端链路在线验收」待办转已完成。
- 回滚预案（引用 release-checklist §4）：任何一步失败→停止发布、保留现场、用 §1 快照恢复；客户端更新链 fail-closed，验签不过的清单不会被拉起——渠道指向错误的最坏结果是用户收不到更新，而不是装到坏包。回滚锚=信任三件 `previous`（R9 时点=c99e2625/0d2e5e1e/aee7051e）。

---

## §A 资产索引

| 资产 | 用途 | 出处 |
| --- | --- | --- |
| `scripts/smoke_installer.sh` | §2 一条链 | N9-A/A2 SMOKE PASS |
| `scripts/parse_installer_log.py` | ISCC 日志定位 | N9-A |
| `scripts/rehearse_client_update_local.py` | §4 更新链彩排 | N9-B 6 pass |
| `scripts/rehearse_first_day.sh` | §7 首日彩排（装→布局→健康→收尾） | N9-R FIRSTDAY PASS |
| `scripts/cleanup_release_rehearsal_sandbox.sh` | 沙箱按身份清理 | N9-R 自证 |
| `docs/release-day-ops-pack.md` | 发布日运维包（分工/话术/FAQ/判据卡等 18 节） | N9-R |
| `docs/release-notes-0100-draft.md` | 首版 notes 草稿（事实已溯源） | N9-R |
| `scripts/build_client_update.py` | 更新包生产（PKG1 闭集） | 闭集门 |
| `docs/release-checklist.md` | 就绪清单+仓名裁决+§3.1 | N9-C 收官 |
| `docs/packaging-chain.md` / `update-chain.md` / `release-chain.md` | 三链手册 | N9-A/B/C |
| `backup/SHA256SUMS-20260925.txt` | 校验单格式实例 | N9-R |

## §B 发布日计时基线（彩排实测回填，N9-R 2026-09-25）

| 步骤 | 实测 | 记录 |
| --- | --- | --- |
| §1 灾备 bundle 四证 | 2 分钟（5.9MB bundle） | N9-R 04:54–04:56 |
| §2 smoke 全链（构建→门禁→装机→健康→收尾） | **149 秒**（payload 110.1MB/6142 文件，GATES OK→SMOKE PASS） | N9-R 04:57–05:00 |
| §2 净树复现核验（HEAD iss 重编译） | ≈70 秒，SHA256 逐位=钉定值 | N9-R 05:03 |
| §4 更新链彩排（2 happy+1 回滚+3 负例） | **14 秒**（载荷构建 1.8s，requests=12） | N9-R 05:09 |
| §7 沙箱静默装机 | 84 秒 exit 0 | N9-R 05:05 |
| §7 冷启→ready（捆绑运行时首启） | 13 秒 | N9-R 05:07 |
| §7 首启向导 5 步+目录/设置可达 | ≈2 分钟（7 截图） | N9-R 05:07–05:10 |
| §7「卸载保留数据」→重装→数据恒等→再启 | 重装 54 秒；再启 19 秒 READY，instance_id 恒等 | N9-R 05:11–05:13 |

## §C 已知边界速查

- setup.exe 仅首装；重装/升级跑 setup.exe = 静默假成功（三断言见 release-checklist §3.1）。
  N9-R 补充实证：「卸载保留数据」后同版本重装——Inno exit 0、装后步 exit 1（受管根含数据被
  `InstallRoot must not already exist` 守卫拒），但 versions 旧槽仍在→客户端可用；**跨版本场景
  依旧必须走产品内更新通道**（槽不会刷新）。
- 构建可复现性条件（N9-R 第五收敛点实证）：同 HEAD+同捆绑运行时+**HEAD 版 installer/*.iss 与
  icon**→SHA256 逐位复现（9b5558a4）；smoke 脚本从工作树读 iss，若有在途 iss 脏增量，重建哈希
  会变（归因法：`git show HEAD:installer/courselens.iss` 提取净版重编译即收敛）。
- 端口段 8700–8799 被 Windows 排除段罩住，冒烟勿用（PORT_BUSY 误报）；默认 8808 安全。
- SAC（Smart App Control，本机 State=On）拦截未签名卸载器：`unins000.exe` 报「An Application
  Control policy has blocked this file」；且 Git Bash 直启报 Permission denied——卸载器一律走
  `powershell Start-Process -PassThru -Wait` 取真实退出码。SAC 机器上的降维验收=布局断言
  （程序目录移除+受管根原地保留+数据指纹比对，N9-R 实测指纹逐位恒等）。
- 受管启动器持有**会话级命名互斥** `Local\FudanCourseLensManagedUpdate`（launcher :122）：同
  Windows 会话内同时只允许一个受管客户端/装机彩排。并行车道同跑 smoke/首日彩排会互相
  MUTEX BUSY（rehearse_first_day.sh 退出码 2=互斥占用，等几分钟重跑）；集成测试（pytest 拉
  启动器）同样参与该互斥。发布日彩排按串行排程即可。
- 静默卸载（/SILENT、/VERYSILENT）不再弹任何框：UNINST-FIX-1（f157fd2，2026-10-01）改为
  `UninstallSilent()` 直接跳过数据询问、写卸载日志并默认保留学习数据；交互卸载仍走询问页
  （默认「保留」，选「否」需二次确认）。已实测的是数据保留路径本身（iss 不把受管根列入
  自动删除 + N9-R 布局/指纹断言）；真机静默卸载实测仍列入二次排期。
