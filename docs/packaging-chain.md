# 包装链手册：从源码到 setup.exe（CourseLens 桌面端）

> 本文是包装链的唯一操作手册：源码 → 安装包 → 装机冒烟 → 交给学生的校验单。
> 所有命令与数字均来自 2026-09-25 夜间 N9-A 车道的实机验证（HEAD=67fe98a4）。

## 0. 一分钟总览

```
源码树(git) ──git archive──▶ staging(=HEAD树) ──选择性拷贝──▶ payload
                                                            │
dev树 tools/python312(捆绑运行时) ──────────────────────────┘
                                                            ▼
                                              ISCC (installer/courselens.iss)
                                                            ▼
                                   CourseLens-<版本>-setup.exe + 校验单.txt
                                                            ▼
                              静默安装(沙箱) ──▶ 受管根五目录 ──▶ 启动器 ──▶ serve
```

两个根，一生不变（卡③）：程序负载根 `{localappdata}\Programs\CourseLens`；受管数据根
`{localappdata}\CourseLens`（`launcher/versions/state/trust/data` 五目录）。卸载默认保留
数据，`[UninstallDelete]` 必须恒空——这是数据保护红线，不是疏漏。

## 1. 前置工具

| 工具 | 位置 | 说明 |
| --- | --- | --- |
| Inno Setup ISCC 6.7.3（便携） | `%TEMP%\pkgpv1-artifacts\innosetup\ISCC.exe` | PKGPV-1 遗产，免安装；换版本须重跑冒烟+可复现性对账 |
| 捆绑运行时 | `<仓库>/tools/python312` | 便携 CPython 3.12 + 客户端依赖**就地安装**（`scripts/install_bundled_runtime.ps1`）；**不在 git 里**，新机需先在 dev 树备好 |
| Python（跑门禁/审计） | `tools/python312/python.exe` | 全程 `PYTHONDONTWRITEBYTECODE=1`，防 `__pycache__` 污染 payload |

版本号正典 = 仓库根 `courselens-version.json`（schema `courselens.client-version.v1`）；
`courselens.iss` 的 `#define AppVersion "0.1.0"` 只是缺省，构建时一律用
`/DAppVersion` 覆盖。

## 2. 标准构建路径（官方，日常用）

```powershell
# 干净工作树；-ValidateOnly 只做静态契约+暂存演练不编译
powershell -NoProfile -ExecutionPolicy Bypass -File installer\build_installer.ps1
```

脚本代劳的事：`.iss` 静态契约 10 token 校验（AppId、per-user 根、卸载红线等）→
从**工作树**暂存 payload → StagingSource 16 项存在性门 → NoLeakage 扫描
（`runtime\data`/`runtime\logs`/`.git`/`.venv*` 禁入；site-packages 之外禁
`.pfx/.pem/.key/.sig/.nupkg` 与私钥块）→ ISCC → 体积 ≤300MB 门 → 产出
`output\installer\CourseLens-<版本>-setup.exe` + 学生可读 `校验单.txt`
（SHA-256 + 「像核对快递单号一样核对」话术）。

脏树演练传 `-AllowDirty`；发布构建必须干净树。

## 3. 归档构建路径（HEAD 恒等，夜间车道/并行期用）

并行写者在途时工作树不干净，官方路径会拒绝。改从 HEAD 归档构建，**内容与
HEAD 逐字对应**（下述 N9-A 实测全程零 product 树写入）：

```bash
EPOCH=$(git log -1 --format=%ct)                    # 提交时刻，可复现性关键
git archive --format=tar HEAD > /tmp/n9a-head.tar   # 498 文件，见第 6 节行尾口径
mkdir -p /tmp/n9a-staging && tar -xf /tmp/n9a-head.tar -C /tmp/n9a-staging

# payload = 官方暂存清单的同款选择性拷贝
P=/tmp/n9a-payload; mkdir -p "$P/docs"
for d in frontend src shared scripts config docs/repository-readmes; do cp -r /tmp/n9a-staging/$d "$P/$d"; done
for f in courselens-version.json credentials.py path_utils.py runtime-assets.json \
         requirements-client-py310.lock.txt requirements-client-py312.lock.txt \
         OpenFudanCourseLens.cmd SetupFudanCourseLensRuntime.cmd start_fudan_courselens.ps1; do
  cp /tmp/n9a-staging/$f "$P/$f"
done
# ★捆绑运行时是唯一「非 HEAD 树」输入：官方脚本同样取自工作树（见第 7 节）
MSYS2_ARG_CONV_EXCL='*' robocopy '<仓库>\tools\python312' 'C:\...\n9a-payload\tools\python312' /E /MT:8 /NFL /NDL /NJH /NP /R:1 /W:1   # rc≤7 即成功

find "$P" -exec touch -d "@$EPOCH" {} +             # ★可复现处方：mtime 钉到提交时刻

MSYS2_ARG_CONV_EXCL='*' /tmp/pkgpv1-artifacts/innosetup/ISCC.exe \
  /DAppVersion=0.1.0 /DSourceRoot='C:\...\n9a-payload' /O'C:\...\n9a-out' \
  '<仓库>\installer\courselens.iss'                 # 仓内 .iss 只读引用，中文 .isl 脚本相对解析
```

门禁在归档路径须自己复刻（`.py` 门禁脚本示例见 N9-A 结果文件）：StagingSource
16 项 + NoLeakage 全绿再进 ISCC。ISCC 日志认 `Successful compile`；日志里 grep
"error" 的命中多是 `error.py` 这类文件名，逐条看过再下结论。

实测（N9-A，110.1MB payload）：编译 ~40-45s，产物 29,853,083–29,854,151 字节。

## 4. 隔离装机冒烟清单（每包发布前过一遍）

沙箱原则：`USERPROFILE`/`LOCALAPPDATA` 重定向到 `%TEMP%` 沙箱 profile，让
`{localappdata}` 系常量全部落沙箱；真实受管根
`C:\Users\<你>\AppData\Local\{CourseLens,Programs\CourseLens}` 装机前后做
tripwire（本机验证时两根 ABSENT，装后必须仍 ABSENT）。

```bash
MSYS2_ARG_CONV_EXCL='*' USERPROFILE='C:\...\n9a-profile' LOCALAPPDATA='C:\...\n9a-profile\AppData\Local' \
  CourseLens-0.1.0-setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART \
  /DIR='C:\...\n9a-install' /LOG='C:\...\n9a-install.log'   # 期望 exit 0
```

核对点（N9-A 实测口径）：

1. 安装日志零真实 error（注意区分 `error.py` 文件名命中）；`Need to restart Windows? No`。
2. `/DIR` 负载根 11 关键件在位；文件数 = payload+2（`unins000.exe/.dat`）。
3. 沙箱受管根五目录齐：`launcher`（5966 文件=5963 运行时+helper+启动 ps1+信任清单）、
   `versions/<版本>`（版本槽）、`state/current.json`（`awaiting_health=false`）、
   `trust/client-update-trust.json`、`data/`（空）。
4. 信任清单 `courselens.launcher-trust.v2`：全量 SHA256 映射，launcher 启动时逐件校验。
5. 启动（选避开排除段的端口，见第 8 节）：
   `launcher\start_managed_courselens.ps1 -InstallRoot <受管根> -Port 8808 -NoOpen`
   → `/api/health` ~10s 内 `ok:true, service=fudan-courselens, version=<版本>,
   state=ready/serving` 且 pid 与进程一致；`GET /` 200 SPA 壳。
6. 数据隔离：`data/` 下长出全新 `instance.lock/learning.db/state.db`；真实数据零触碰。
7. 收尾按 PID 精确杀进程（health 里的 pid），确认端口释放、无 python 残留。

安装期自带运行实例探测：`InitializeSetup` 扫 `127.0.0.1:8765-8775` 与 `6268` 的
`/api/health`，发现 CourseLens 在跑会先请学生退出（WinHttp COM 实现）。

## 5. 开发版 vs 安装版差异

| 维度 | 开发版 | 安装版 |
| --- | --- | --- |
| 入口 | 仓根 `start_fudan_courselens.ps1` | 受管根 `launcher\start_managed_courselens.ps1 -InstallRoot …` |
| 端口 | 8765-8775 自适应扫描 | 默认 6268（`-Port` 可覆盖） |
| 数据根 | 仓 `runtime/data`（或 `COURSELENS_DATA_DIR`） | `<受管根>\data`（launcher 注入同名环境变量） |
| 运行时 | dev venv 或仓内 tools | `launcher\python312`（信任清单校验后使用） |
| 更新 | 直接改源码 | versions 槽 + pending/apply/rollback（client_update_helper） |
| 开窗 | `serve(open_browser=…)` | launcher `-NoOpen` 透传 `--no-open` |

## 6. 行尾口径（树恒等判断必读）

`.gitattributes`：`*.ps1/*.cmd` 出口 CRLF，`*.py/*.js/*.json/...` 出口 LF，
其余 `text=auto`。`git archive` 按**出口属性**落盘，所以归档树的 `installer/courselens.iss`
与 `start_fudan_courselens.ps1` 是 CRLF，与 blob（LF）哈希不同——**这不是偏差**，
官方构建的工作树源本就是同一形态。判断「导出树=HEAD 树」：eol=lf 域逐字节恒等 +
crlf 域仅行尾差异 + 文件数恒等（N9-A：498/498，9 抽样 7 恒等 2 行尾）。

## 7. 捆绑运行时

- `tools/python312` = 便携 Python + 就地安装的依赖（`install_bundled_runtime.ps1`）。
  没有 venv、没有机器重定位步骤 → 中文用户名机与 ASCII 机安装路径完全一致（R-OP7）。
- 它**不在 git 里**；官方构建与归档构建都从 dev 树现拷。换依赖 = 改
  `requirements-client-py312.lock.txt` 后重跑 install_bundled_runtime，再全链冒烟。
- 装机后它复制两处：负载根 `<程序根>\tools\python312`（版本槽内容）与
  `<受管根>\launcher\python312`（稳定 launcher，信任清单钉死哈希）。
- 已知死重：运行时树含 `__pycache__/*.pyc`（N9-A 实测随包上镜），体积优化候选
  （见 docs 同代积压）。

## 8. 坑清单（每条都真实咬过人）

1. **Windows 排除端口段**：`netsh interface ipv4 show excludedportrange protocol=tcp`
   的动态保留段（本机 2026-09-25 实测含 8700-8799）会让绑定失败，客户端把它报成
   `LIFECYCLE_E_PORT_BUSY`——其实端口没被占用，是被系统收走了。dev 默认带
   8765-8775 可能整段中招。选冒烟端口前先看排除表。
2. **launcher 健康探测失败时看不到真因**：受管 launcher `Start-Process` 不重定向
   子进程输出，30s 探测超时只抛「failed its startup health check」。归因要手动用
   版本槽运行时直跑 `python -m src serve --port X --no-open --keep-server` 抓 stderr。
3. **可复现性**：Inno 把文件 LastWriteTime 编进安装包。同一 payload 重建逐字节恒等；
   全新解包重建 mtime 变→SHA256 变（实测 25 字节之差）。处方=第 3 节的
   `touch -d @EPOCH`（实测两棵独立 payload 钉同一提交时刻 → SHA256 逐字节恒等
   `9b5558a4…`）。前提：ISCC 版本与 tools/python312 树不变。
4. **git-bash 调 Windows 工具**：一律 `MSYS2_ARG_CONV_EXCL='*'` 防路径改写；
   robocopy 退出码 ≤7 都是成功。
5. **6268 是受管默认端口**，与常见本地代理端口相撞（本机代理就在 6268 接线）。
   受管启动器支持 `-Port`，冒烟与文档示例应显式给端口。
6. **PYTHONDONTWRITEBYTECODE=1**：任何对着 payload 跑 Python 的动作都要带，
   否则 `__pycache__` 现场长进安装包。
7. ISCC 手术刀：`/DAppVersion`、`/DSourceRoot`、`/O` 三个开关足够，**不改
   `installer/`**（PKG1 禁编辑清单；AppId 一经公开发布永不再变）。

## 9. 红线（包装链不做什么）

- **不签名**：Authenticode 属发布域 runbook（`scripts/sign_client_release.py`，须单独授权批次）。
- **不自动删数据**：`[UninstallDelete]` 恒空；删除只存在于卸载询问页学生亲口选「否」之后。
- **不外联**：构建与安装全程零下载（运行实例探测是本机 loopback）；install_managed_client
  是「受控试点安装器」，不下载任何可执行代码。

## 10. 术语表

| 术语 | 含义 |
| --- | --- |
| 负载根 / `{app}` | `{localappdata}\Programs\CourseLens`，setup.exe 的安装目标（`/DIR` 可改，仅冒烟用） |
| 受管根 | `{localappdata}\CourseLens`，五目录：`launcher`（稳定运行时+信任清单）、`versions\<版本>`（版本槽）、`state`（current/pending 状态机）、`trust`（client-update-trust）、`data`（学习数据） |
| 版本槽 | `versions\<版本>`，一次发布的不可变内容集（schema `courselens.client-package.v1`） |
| 信任清单 | `launcher\launcher-trust.json`（schema `courselens.launcher-trust.v2`），launcher 全量文件的 SHA-256 映射，启动时逐件校验，一票拒启 |
| 捆绑运行时 | `tools/python312`：便携 CPython 3.12 + 就地安装的客户端依赖；无 venv、无机器重定位（R-OP7） |
| 启动态 handler | AS9 两阶段绑定的占位 handler：`/api/health` 答 200/`ok:false`、其余 GET 答启动页、POST 一律 503；热替换后退役为兜底面（P64） |
| 热替换 | `server.RequestHandlerClass = make_handler(...)`：监听不换、请求级切换到真 handler |
| P64 开窗时序 | 浏览器从「绑定即开」迁到「热替换+ready 后恰一次」；失败兜底补开窗；`--no-open` 全程零开窗 |
| 校验单 | `output\installer\CourseLens-<版本>-校验单.txt`：学生可读的 SHA-256 核对指引 |
| 排除端口段 | Windows（Hyper-V/WSL）动态保留的端口区间，绑定被拒但**不是**被占用——`netsh interface ipv4 show excludedportrange protocol=tcp` 查看 |

## 11. 账号角色与官方发布面（2026-09-30 迁移拍板）

- **gualtier-xu = 官方发布账号**：官方镜像仓 `gualtier-xu/Fudan-CourseLens-Worker`（签名 Worker 镜像；2026-10-05 REPUBLISH-23 自临时 `-Release` 门户回迁正名，旧名重定向）+ 客户端 Release 门户=总仓 `gualtier-xu/Fudan-CourseLens`（2026-10-04 REBUILD-9 起，旧门户退役）。发布面常量唯一事实源 = `config/distribution.json`，`scripts/check_distribution_references.py` 以 fail-closed 钉住 registry/`src/distribution.py`/`src/update/service.py`/trust 模板四处字节恒等。
- **辅助账号 = 测试辅助身份**（账号与仓名不载公开版）：其名下历史镜像仓保留为只读历史（不删不改），不再接收发布。
- 结构性保证（B1 消失证明）：客户端守卫「绑定仓 == 模板仓 → 拒绝执行」（`src/remote/github_app.py` `check_worker_integrity`）在迁移后对遗留辅助账号绑定恒为不等；钉在 `tests/test_distribution_namespace_migration.py`。
- 镜像代际：pin 三值（commit/tree/manifest_sha256）随签名代前移，密钥轮换为零（`signing_key_id`/`trust_epoch` 不变）；发布操作链见 `docs/release-chain.md`，账号职能以本章为准。
