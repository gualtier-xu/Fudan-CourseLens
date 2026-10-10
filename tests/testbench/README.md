# tests/testbench — 测试台架（件③M1/M2：实例管理器 + Chrome 生命周期 + serve 挂接）

多车道调试不再抢真装、不再手工撞端口（8791 撞 8775 案终结于此）。所有需要
独立后端实例/浏览器实例/驱动工作目录的车道，**统一从这里拿测试实例**：
认领（claim）→ 用 → 释放（release），持有者心跳续约，持有者死亡后他人可
接管（接管留痕）。

设计出处：`archive/external-artifacts/top-model-results-20260930/product-testbench1-result-20261009.md` 件③。
本目录是纯测试设施：零产品源码改动、零产品行为影响。

## 车道接入（三行）

```python
from tests.testbench.instances import TestInstanceManager
inst = TestInstanceManager().claim("mylane-backend", owner_lane="MY-LANE-1")  # 心跳自动续约
...  # 用 inst.port / inst.data_dir / inst.chrome_profile_dir / inst.scratch_dir 起进程
inst.release()   # 用毕释放（或用 with 块自动释放；进程被杀也不怕——他人可接管）
```

**serve 自动注册形态（件③M2 src 侧，二选一均可）**：给 serve 进程加 env
`COURSELENS_TEST_INSTANCE=<实例名>`（可选 `COURSELENS_TEST_INSTANCE_OWNER=<车道ID>`、
`COURSELENS_TEST_REGISTRY=<注册表目录>`），绑定落地即把自身 pid/实际端口登记进
注册表、心跳续约、优雅退出时释放；实例名已有车道 claim 时 serve 采纳之
（owner/事件史保留，pid/port 换 serve 本体）。注册失败不拦服务（只留人话警告）。
钉测：`tests/test_test_instance_registration.py`（13 钉，含子进程真起冒烟）。

- `claim(...)` 原子认领：名字=车道 ID+角色（如 `remotee2e1r4-backend`），
  重名直接报错并给出人话指引（换名零成本 / 确认对方已死可 `takeover`）。
- `inst.port`：从测试段 **17700-17999** 分配（registry 在册 + bind 实测双查，
  跨进程分配锁内完成，杜绝并发撞号）。禁再手工选端口。
- `inst.data_dir`：`<registry>/<实例名>/data`——注入 `COURSELENS_DATA_DIR`；
  同实例还有 `chrome-profile/`（独立 user-data-dir 铁律）与 `scratch/`。
- `owner_lane` 必填（=车道 ID），接管与追责全靠它。

## GitHub 登录态获取（件①M1/M2，持久测试身份）

```python
from tests.testbench.instances import TestInstanceManager
from tests.testbench import github_identity as gi
inst = TestInstanceManager().claim("mylane-driver", owner_lane="MY-LANE-1")
session = gi.ensure_session(purpose="remote-e2e", owner_lane="MY-LANE-1")  # 会话有效即复用；失效自动账密重登
context = browser.new_context(storage_state=session.state_path)            # 把 GitHub 登录态注入任意新 context
```

- 凭据=Tier A `.local-secrets/co-github-test.json`（只进内存）；登录态=Tier B
  `.testbench/identity/github-co/storageState.json`（**即凭据，0600**，失效自愈自动刷新）。
- `purpose` 闭集 allow-list（fail-closed）：`device-auth`/`app-install`/`remote-e2e`/`api`/
  `generic-test`——学校登录与真实首跑走查**不在册、必被拒**（真人真账号红线）。
- 2FA/CAPTCHA/设备验证挑战=抛 `ChallengeEncountered` 即停转人工；外联不可达=
  `OutboundUnavailable`（车道诚实 SKIP，勿断网重登）；详见模块 docstring 与钉测。

## 身份轮换（件①M3）

```bash
.venv-client-py310/Scripts/python.exe tests/testbench/identity_cleanup.py github-co           # 干跑（默认）：只打印将删什么
.venv-client-py310/Scripts/python.exe tests/testbench/identity_cleanup.py github-co --apply   # 真删 Tier B 目录（Tier A 分毫不动）
.venv-client-py310/Scripts/python.exe tests/testbench/identity_cleanup.py --list [--json]     # 列出全部身份目录
```

- 轮换语义：整体删除 `.testbench/identity/<名>/`（storageState=等效凭据），
  下次 `ensure_session` 自动账密重登重建；密码本体轮换=改 `.local-secrets`
  文件一次即可（自愈链实测 PASS，见 TB-W2 结果文件）。
- 三层守卫：身份名过件①同一守卫（禁路径逃逸）；路径任一级含
  `.local-secrets`/`instances` 或落在产品仓内一律拒绝——清理脚本对 Tier A
  凭据本体结构性不可达。

## Chrome 生命周期（件③M2，`chrome_lifecycle.py`）

```python
from tests.testbench.instances import TestInstanceManager
from tests.testbench import chrome_lifecycle as cl
mgr = TestInstanceManager()
up = cl.chrome_up(mgr, "mylane-chrome", owner_lane="MY-LANE-1")   # 独立 profile + CDP 就绪才返回
down = cl.chrome_down(mgr, "mylane-chrome", owner_lane="MY-LANE-1")  # down.cdp_status == "000"
```

- **独立 user-data-dir 铁律**：用认领实例的 `chrome-profile/` 目录，禁触用户
  默认浏览器配置；`chrome.exe` 定位=env `COURSELENS_TESTBENCH_CHROME` → 常见
  安装位，找不到=诚实 SKIP（禁用用户 Edge 凑数）。
- **指纹精确清理**：down 只杀「cmdline 含本实例 profile 目录」的 chrome.exe
  （PowerShell CIM 逐进程核对），逐 PID `taskkill /PID <pid> /T /F`；
  `taskkill /IM` 全量杀被结构性排除（钉测扫源码）——用户此刻在用浏览器也不受扰
  （10 次起停循环实测：用户 Chrome 30 进程全程无恙）。
- **CDP 000 复核**：down 后 `/json/version` 探测必须回到 `000`（连接失败，
  curl 语义）+ bind 实测双确认，不过=不释放 claim（可重试）。
- 默认 `--headless=new`（最少打扰）；`chrome_up(..., headless=False)` 或 CLI
  `--headed` 可切换。CLI：`python tests/testbench/chrome_lifecycle.py up|down <名> --lane <车道ID> [--proxy URL]`。

## real 档测试身份（件②M3：真 UIS 登录自动化）

```bash
COURSELENS_TEST_MODE=real \
COURSELENS_TEST_EGRESS_ALLOW=school:readonly,github:api \
COURSELENS_TEST_CREDENTIALS_FILE=<工作区>/.local-secrets/<测试身份>.json \
.venv-client-py310/Scripts/python.exe private/main/tests/testbench/real_chain_selftest.py
```

- 凭据文件形态：`{"student_id":..., "password":...}`（或 username/account/
  fudan_account 别名；`{"credentials": {...}}` 包裹同收）——**只在内存走一次
  真 UIS 登录，结构性零落盘**（装载器 `src/runtime/test_credentials.py`，
  repr/日志全脱敏）；synthetic 档设凭据文件=契约拒绝。
- 联动 fail-closed：凭据文件必须配 `COURSELENS_TEST_MODE=real` +
  `COURSELENS_TEST_EGRESS_ALLOW` 含 `school:readonly`（缺档启动即拒，人话提示）。
- 真链自测脚本：school 腿=产品自身 `WebVPNSession.login()` 七步真链（每跳
  egress 审计可事后复核）；github 腿=既有 gh_login 链；外联不可达=诚实 SKIP
  勿重登，CAPTCHA/2FA=FAIL 即停不代批。钉测：`tests/test_real_credentials_contract.py`。

## 车道迁移模板（件③M3：新车道一律按此四条执行）

1. **先 claim 再起进程**：后端/浏览器/驱动进程开始前先 `TestInstanceManager().claim(...)`
   （或给 serve 进程设 `COURSELENS_TEST_INSTANCE` 让它自动注册）；禁手工选端口、
   禁抢真装。同一实例名全局互斥，等待不如换名。
2. **真装只读边界**：真装 pid 出现在任何驱动写操作=违规；需要真装态时只做
   只读观察（诊断 API/health 读数），行为验证一律在测试实例复现。
3. **实例名入结果文件**：车道结果文件必须写明本次用的实例名与 claim 证据
   （`instances --list` 输出或注册横幅行），收尾时 claim 已释放或如实记录残留。
4. **验证口径**：改 `src/runtime/http_api.py`（合同/路由面）的定向集必含
   `tests/test_http_service_completeness.py` + `tests/test_http_container_reconciliation.py`
   （新 service.* 引用两边同时受钉）；实例/凭据相关改动另跑
   `tests/test_test_instance_registration.py` / `tests/test_real_credentials_contract.py`。

## 注册表在哪

工作区级 `D:\...\Fudan_CourseLens\.testbench\instances\`（工作区非 git 仓，
天然不入库；路径一律工作区级绝对路径）。每实例一个 `<名字>.json`，含
pid/端口/数据目录/owner/事件史（claim/takeover 留痕）。

## 协议六条（与设计件③一一对应）

| 条目 | 语义 |
|---|---|
| claim | 原子创建（O_EXCL），互斥命名空间 |
| 心跳 | 持有者周期触碰 claim mtime（守护线程默认 30s，claim 即自动启动） |
| stale 判定 | 心跳落后 >120s **或** pid 不存在（进程实存为准，不盲信文件） |
| 接管 | `takeover(name, new_owner_lane=...)`：接管前强制持有进程存活核验——**pid 仍存活一律拒绝**；接管以固定名互斥标记+双重核验完成，takeover 事件留痕 |
| release | 删 claim 文件；接管后老持有者的 release 不生效（防误删） |
| 查询 | `list_instances()` / CLI `list --json`：谁在用、心跳几秒、是否 stale，机读 |

## 真装只读边界守卫（安全底线）

- 端口：测试段之外一律拒绝（具名禁用：6268 产品默认 / 7482 用户实例 /
  8574-8673 Hyper-V/WSL 保留段）。
- 数据目录：用户默认目录（`%LOCALAPPDATA%\CourseLens` 等）之内、产品仓之内
  一律拒绝——学生真实数据结构性不可达。
- 实例名：禁路径逃逸（只允许字母/数字/点/下划线/连字符）。
- 车道纪律（写进 prompt 模板，非本模块职责）：真装 pid 出现在任何驱动写
  操作=违规；需要真装态时只做只读观察（诊断 API/health 读数）。

## CLI（车道手工调试用）

```
.venv-client-py310/Scripts/python.exe tests/testbench/instances.py --registry <目录> list [--json]
... claim <名字> --lane <车道ID> [--kind backend|chrome|driver] [--port <端口>]
... release <名字> [--lane <车道ID>]
... heartbeat <名字> | stale <名字> | allocate-port
```

省略 `--registry` 时用默认工作区注册表。

## 钉测与变异验证

`test_instance_manager.py`（21 钉，正典 venv，~3s）：并发 claim 互斥（线程级
+ 跨进程级）、10 车道并发零端口碰撞、心跳过期 stale/接管、接管前存活核验、
接管竞态恰好一胜者、端口双查避让、真装守卫四类拒绝、release 语义、机读
清单。变异验证已做：抽掉心跳本体 `os.utime(path, None)` → 心跳两钉必红
（红绿证据见 TB-INST-M1 结果文件）。

`test_identity_cleanup.py`（9 钉）：默认干跑零删除、apply 只删身份目录而
Tier A 分毫不动、路径逃逸/受保护目录级/产品仓根拒绝、--list 机读。

`test_chrome_lifecycle.py`（11 钉）：argv 纯函数、指纹匹配纯函数（用户/
他道实例/空 cmdline/前缀相似实例一律不认）、taskkill 唯一形态（/IM 结构性
排除）、CDP 000 语义、kind 守卫、up 失败收尾释放 claim、缺 chrome.exe 诚实
失败，以及真实起停钉=**10 次 up/down 循环零残留/零端口冲突+用户 Chrome
逐 PID cmdline 复核不受扰**（chrome.exe 缺位自动 SKIP）。

## 边界注记

- src serve 的 test mode 挂接已于 TB-W4 落地（件③M2 src 侧：`COURSELENS_TEST_INSTANCE`
  自动注册/心跳/释放，见「车道接入」节）；本目录仍是纯测试设施 + 契约模块
  （src/runtime/test_mode.py、test_instance.py、test_credentials.py）。
- 注册表残留 claim（车道被 kill -9）的最坏后果=多起一个实例（端口 bind 实测
  兜底），无安全后果；接管前请先 `list` 看 pid_alive。
