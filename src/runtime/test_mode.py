"""CourseLens 测试模式契约（TESTBENCH-DESIGN-1 件②M1）。

正式契约 = 环境变量 ``COURSELENS_TEST_MODE``，值闭集 ``synthetic``/``real``；
``--e2e`` 是 serve CLI 的 ``real`` 档别名。本模块是全部测试模式读数的唯一
权威源：env 字面量只允许出现在本文件（发布版惰性钉
tests/test_test_mode_contract.py 强制）。

发布版惰性保证（S5，硬性验收）：
- 不设 env：本模块全部入口在「env 为空 → 立即返回」一条常量判断后原路返回，
  egress 门恒开、数据目录/端口契约整体跳过——学生可见行为与契约落地前逐位一致。
- 测试模式进程结构性读不到学生默认目录的凭据信封：数据目录强制独立
  （平台默认目录 = 拒绝启动），DPAPI 信封随默认目录留在测试进程之外。

契约六条（与 TESTBENCH-DESIGN-1 §2.1 一一对应，逐条可验收）：
1. 沙盒数据目录强制（fail-closed）：``test_data_dir_contract()``——必须显式
   指向沙盒（``COURSELENS_DATA_DIR`` 单一语义，serve 会回写 env 让子进程继承，
   终结「--data-dir 须同时设 env」双旗坑）；指向平台默认目录或产品默认
   数据根 = 拒绝启动 + 人话提示。
2. 端口段：测试模式禁用 6268 产品默认端口（``ensure_test_port_allowed()``）；
   serve CLI 在测试模式下缺省 port=0（OS 分配）或由件③实例管理器从
   17700-17999 测试段取号（tests/testbench/instances.py）。
3. 出站 fail-closed：``ensure_egress_allowed()``——测试模式默认白名单 =
   本机 loopback 闭集（合成后端/本机测试桩），其余全部拒绝；拒绝 =
   闭集码 ``egress_blocked`` + stderr 审计行（逐 host 可事后复核）。
   ``COURSELENS_TEST_EGRESS_ALLOW`` 显式开子集，枚举闭集 =
   ``school:readonly``（真 UIS/iCourse 只读验证）/ ``github:api``
   （远程链真跑）；白名单档只在 real 档生效（synthetic 档设 ALLOW =
   契约违例，启动即拒）。
4. 确定性时钟：产品运行时钟不引入任何测试分支；确定性由种子层
   （合成后端 ``--seed-clock real|frozen``）+ 浏览器层（capture.mjs 冻结钟）
   承担。本模块仅成文该边界（``SEED_CLOCK_MODES``），零时钟代码。
5. 可脚本化引导跳过：合成后端 ``--onboarding-guide
   new|dismissed|completed|corrupt|legacy|auto``（``ONBOARDING_GUIDE_MODES``）
   升格为正式旗标；skip 语义与前端既有 maybeAutoOpen 协同（disposition
   记录驱动，前端零改动）。E2E-2v6 教训吸收：``COURSELENS_TEST_FRESH_OPERATIONS=1``
   （仅测试模式生效）跳过 operation 幂等回放历史——复用沙盒数据目录时
   stored_id 会把上一次运行的旧失败记录当既有结果回放；置 1 后操作一律
   重新执行（``fresh_operations_requested()``）。
6. 合成后端对接点：``tests/synthetic_shell_server.py`` 收编为 synthetic 档
   正式标准后端（``SYNTHETIC_BACKEND_MODULE`` + ``synthetic_backend_recipe``）；
   车道经该入口起合成环境，不再自拷自改 serve 变体。

安全边界（S1/S3）：本模块只读 env、只打印审计行，零网络、零文件写入、
零学生数据接触；测试模式永不携带真实凭据出站（默认白名单缺省空）。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from src.platform.paths import APP_DIR_NAME, DATA_DIR_ENV

# ---------------------------------------------------------------------------
# 契约常量（闭集）
# ---------------------------------------------------------------------------

TEST_MODE_ENV = "COURSELENS_TEST_MODE"
TEST_MODE_SYNTHETIC = "synthetic"
TEST_MODE_REAL = "real"
TEST_MODE_VALUES = frozenset({TEST_MODE_SYNTHETIC, TEST_MODE_REAL})

EGRESS_ALLOW_ENV = "COURSELENS_TEST_EGRESS_ALLOW"

#: 件③M2（src 侧）：serve 自动注册的实例名（可选）。未设 = 不注册，
#: 发布版零成本；设了 = serve 进程把自身 pid/实际端口登记进工作区实例
#: 注册表（与 tests/testbench/instances.py 同一注册表、同格式互认），
#: 心跳续约、退出释放——`instances --list` 的机读答案因此覆盖全部测试
#: 后端实例（含 serve 进程本体）。
TEST_INSTANCE_ENV = "COURSELENS_TEST_INSTANCE"
#: serve 注册写进 claim 的 owner_lane（未设 = "serve-auto"）；车道「先 claim
#: 再起进程」时 serve 采纳既有 claim、保留原 owner_lane（追责链不断）。
TEST_INSTANCE_OWNER_ENV = "COURSELENS_TEST_INSTANCE_OWNER"
#: 注册表目录覆盖（未设 = 工作区级 .testbench/instances/，与实例管理器
#: 同一注册表；测试钉用临时目录注入，禁触真实注册表）。
TEST_REGISTRY_ENV = "COURSELENS_TEST_REGISTRY"

#: 件②M3（real 档）：测试身份凭据文件（Tier A 形态：student_id/username/account
#: + password 的 JSON）。设了 = 真链自动化用凭据只在内存里走一次真 UIS 登录，
#: 结构性零落盘（沙盒数据目录与产品加密信封互不相通）；仅 real 档生效，
#: 且 school:readonly 白名单档必须同时在册（联动 fail-closed，见
#: ``resolve_test_credentials_linkage``）。
TEST_CREDENTIALS_FILE_ENV = "COURSELENS_TEST_CREDENTIALS_FILE"

#: E2E-2v6 教训旗标（件⑤同族）：跳过 operation 幂等回放历史。
#: 仅测试模式生效；值闭集 = {"1"}，任何其他值（含未设）= 不启用。
FRESH_OPERATIONS_ENV = "COURSELENS_TEST_FRESH_OPERATIONS"
FRESH_OPERATIONS_VALUES = frozenset({"1"})

#: 出站白名单档闭集（COURSELENS_TEST_EGRESS_ALLOW，逗号分隔；仅 real 档生效）。
EGRESS_TIER_SCHOOL_READONLY = "school:readonly"
EGRESS_TIER_GITHUB_API = "github:api"
EGRESS_TIER_VALUES = frozenset({EGRESS_TIER_SCHOOL_READONLY, EGRESS_TIER_GITHUB_API})

#: 档 → 主机闭集。school = src/runtime/config.py 三基址 + WebVPN 教务目标闭集
#: （webvpn.ALLOWED_TARGET_HOSTS）；github = 远程链两客户端根主机 + 发布资产
#: 下载闭集（src/distribution.py，更新链真跑所需）。
TIER_HOSTS: dict[str, frozenset[str]] = {
    EGRESS_TIER_SCHOOL_READONLY: frozenset({
        "webvpn.fudan.edu.cn",
        "id.fudan.edu.cn",
        "icourse.fudan.edu.cn",
        "fdjwgl.fudan.edu.cn",
        "yjsxktest.fudan.sh.cn",
    }),
    EGRESS_TIER_GITHUB_API: frozenset({
        "api.github.com",
        "github.com",
        "release-assets.githubusercontent.com",
        "objects.githubusercontent.com",
    }),
}

#: 本机环回闭集：synthetic 档缺省白名单（合成后端/本机测试桩的宿主）。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: 测试模式禁用端口（产品默认端口 = 用户真装实例的语义保护）。
PRODUCT_DEFAULT_PORT = 6268

#: 契约错误码闭集（serve/main 逐个翻译成人话；除此集合外不新增）。
TEST_MODE_INVALID = "test_mode_invalid"
TEST_DATA_DIR_REQUIRED = "test_data_dir_required"
TEST_DATA_DIR_CONFLICT = "test_data_dir_conflict"
TEST_DATA_DIR_DEFAULT_FORBIDDEN = "test_data_dir_default_forbidden"
TEST_PORT_RESERVED = "test_port_reserved"
TEST_EGRESS_ALLOW_INVALID = "test_egress_allow_invalid"
TEST_EGRESS_ALLOW_SYNTHETIC = "test_egress_allow_synthetic"
TEST_MODE_CONFLICT = "test_mode_conflict"
TEST_INSTANCE_NAME_INVALID = "test_instance_name_invalid"
TEST_CREDENTIALS_MODE_CONFLICT = "test_credentials_mode_conflict"
TEST_CREDENTIALS_TIER_REQUIRED = "test_credentials_tier_required"
TEST_CREDENTIALS_FILE_MISSING = "test_credentials_file_missing"
TEST_CREDENTIALS_FILE_INVALID = "test_credentials_file_invalid"
TEST_MODE_ERROR_CODES = frozenset({
    TEST_MODE_INVALID,
    TEST_DATA_DIR_REQUIRED,
    TEST_DATA_DIR_CONFLICT,
    TEST_DATA_DIR_DEFAULT_FORBIDDEN,
    TEST_PORT_RESERVED,
    TEST_EGRESS_ALLOW_INVALID,
    TEST_EGRESS_ALLOW_SYNTHETIC,
    TEST_MODE_CONFLICT,
    TEST_INSTANCE_NAME_INVALID,
    TEST_CREDENTIALS_MODE_CONFLICT,
    TEST_CREDENTIALS_TIER_REQUIRED,
    TEST_CREDENTIALS_FILE_MISSING,
    TEST_CREDENTIALS_FILE_INVALID,
})

EGRESS_BLOCKED_CODE = "egress_blocked"

#: 件⑥：合成档正式标准后端（收编自 tests/ 私搭；入口/旗标归契约文档面）。
SYNTHETIC_BACKEND_MODULE = "tests.synthetic_shell_server"

#: 件⑤：可脚本化引导跳过闭集（合成后端 --onboarding-guide 的取值域，
#: skip 语义与前端 maybeAutoOpen 协同——记录驱动，前端零改动）。
ONBOARDING_GUIDE_MODES = ("new", "dismissed", "completed", "corrupt", "legacy", "auto")

#: 件④：确定性时钟唯一接入点 = 种子层（合成后端 --seed-clock）；
#: 产品运行时钟零测试分支（本文件即全部成文，别处禁再立时钟钩子）。
SEED_CLOCK_MODES = ("real", "frozen")

_HUMAN_MESSAGES: dict[str, str] = {
    TEST_MODE_INVALID: (
        "COURSELENS_TEST_MODE 的值不认识（只接受 synthetic 或 real）。"
        "请检查环境变量拼写；不需要测试模式时直接不设它即可。"
    ),
    TEST_DATA_DIR_REQUIRED: (
        "测试模式需要显式沙盒数据目录：请设置 COURSELENS_DATA_DIR 指向测试专用目录"
        "（推荐测试实例管理器分配的目录）， CourseLens 绝不会把测试数据写进你的正式数据目录。"
    ),
    TEST_DATA_DIR_CONFLICT: (
        "测试模式检测到 --data-dir 与 COURSELENS_DATA_DIR 指向不同目录。"
        "测试模式统一以 COURSELENS_DATA_DIR 为准：请二选一，保持两边一致。"
    ),
    TEST_DATA_DIR_DEFAULT_FORBIDDEN: (
        "测试模式拒绝使用默认数据目录（那里是你的真实课程数据和凭据信封）。"
        "请把 COURSELENS_DATA_DIR 指向独立的测试沙盒目录。"
    ),
    TEST_PORT_RESERVED: (
        "测试模式禁用产品默认端口 6268（那是你平时使用的 CourseLens 实例的端口）。"
        "请用 --port 0 由系统分配，或使用测试实例管理器分配的测试段端口。"
    ),
    TEST_EGRESS_ALLOW_INVALID: (
        "COURSELENS_TEST_EGRESS_ALLOW 里有不认识的白名单档"
        "（只接受 school:readonly、github:api，逗号分隔）。"
    ),
    TEST_EGRESS_ALLOW_SYNTHETIC: (
        "synthetic（合成）档不出站：COURSELENS_TEST_EGRESS_ALLOW 只在 real 档生效。"
        "需要真实学校/GitHub 只读验证时请改用 COURSELENS_TEST_MODE=real。"
    ),
    TEST_MODE_CONFLICT: (
        "--e2e 与 COURSELENS_TEST_MODE 冲突：--e2e 等价于 COURSELENS_TEST_MODE=real，"
        "不能同时再把它设成 synthetic。"
    ),
    TEST_INSTANCE_NAME_INVALID: (
        "COURSELENS_TEST_INSTANCE 的实例名不合法：只允许字母/数字/点/下划线/连字符"
        "（名字=车道 ID+角色，例如 remotee2e1r4-backend）。"
        "不需要实例注册时直接不设这个变量即可。"
    ),
    TEST_CREDENTIALS_MODE_CONFLICT: (
        "COURSELENS_TEST_CREDENTIALS_FILE 只在测试模式 real 档生效"
        "（synthetic 合成档不出本机，永远用不到真凭据）。"
        "请改用 COURSELENS_TEST_MODE=real，或去掉这个凭据文件变量。"
    ),
    TEST_CREDENTIALS_TIER_REQUIRED: (
        "设置 COURSELENS_TEST_CREDENTIALS_FILE（真 UIS 登录）时必须同时开"
        " school:readonly 出站白名单档：真登录链一定会访问学校主机，"
        "请把 COURSELENS_TEST_EGRESS_ALLOW 设为含 school:readonly 的子集。"
    ),
    TEST_CREDENTIALS_FILE_MISSING: (
        "COURSELENS_TEST_CREDENTIALS_FILE 指向的文件不存在。"
        "请核对路径（测试身份凭据只放工作区级 .local-secrets/ 一类受控位置，"
        "且永远不会进入日志或结果文件）。"
    ),
    TEST_CREDENTIALS_FILE_INVALID: (
        "测试身份凭据文件形态不符：需要非空 student_id（或 username/account）"
        "+ password 字符串字段的 JSON。文件内容永远不会被打印或写入任何产物。"
    ),
}


class TestModeContractError(ValueError):
    """测试模式契约违例（闭集码 + 人话消息；零上游/用户数据）。"""

    def __init__(self, code: str):
        self.code = str(code)
        message = _HUMAN_MESSAGES.get(self.code, self.code)
        self.human_message = message
        super().__init__(f"{self.code}: {message}")


class EgressBlockedError(RuntimeError):
    """测试模式出站被拒（闭集码 egress_blocked；审计行已先行落 stderr）。"""

    def __init__(self, host: str, *, mode: str, purpose: str = "", reason: str = EGRESS_BLOCKED_CODE):
        self.host = str(host)
        self.mode = str(mode)
        self.purpose = str(purpose)
        self.code = EGRESS_BLOCKED_CODE
        self.reason = str(reason)
        super().__init__(f"egress_blocked mode={self.mode} host={self.host} purpose={self.purpose}")


# ---------------------------------------------------------------------------
# 模式解析（发布版惰性的唯一分支点）
# ---------------------------------------------------------------------------

def _env_value(environ, key: str) -> str:
    try:
        return str(environ.get(key, "") or "").strip()
    except AttributeError:
        return str(os.environ.get(key, "") or "").strip()


def raw_test_mode(environ=None) -> str:
    """返回原始 env 值（strip 后；未设 = 空串）。发布版路径零成本。"""
    if environ is None:
        environ = os.environ
    return _env_value(environ, TEST_MODE_ENV)


def is_test_mode(environ=None) -> bool:
    """是否处于测试模式（未设 env = False，发布版唯一需要的第一判断）。"""
    return bool(raw_test_mode(environ))


def resolve_test_mode(environ=None) -> str:
    """解析测试模式："" （未设）/ "synthetic" / "real"；非法值抛契约错。

    serve() 启动前置用它做一次性校验；egress 门运行时用
    ``egress_denial_reason()``（非法值 = fail-closed 拒绝，不抛）。
    """
    value = raw_test_mode(environ)
    if not value:
        return ""
    if value not in TEST_MODE_VALUES:
        raise TestModeContractError(TEST_MODE_INVALID)
    return value


def mode_for_gate(environ=None) -> str:
    """egress 门视角的模式：未设 = ""；非法值按 fail-closed 归为 "invalid"（拒绝）。"""
    value = raw_test_mode(environ)
    if not value:
        return ""
    return value if value in TEST_MODE_VALUES else "invalid"


def fresh_operations_requested(environ=None) -> bool:
    """是否要求跳过 operation 幂等回放历史（COURSELENS_TEST_FRESH_OPERATIONS=1）。

    仅测试模式生效：未设 COURSELENS_TEST_MODE（发布版）恒 False——发布路径
    与本旗标零接触。测试模式下值闭集 = {"1"}；E2E-2v6 教训：复用沙盒数据
    目录时 stored_id 会回放上一次运行的旧失败记录，置 1 后各 operation
    入口把既有记录当不存在、一律重新执行。
    """
    if not is_test_mode(environ):
        return False
    return _env_value(environ, FRESH_OPERATIONS_ENV) in FRESH_OPERATIONS_VALUES


# ---------------------------------------------------------------------------
# 件③：出站 fail-closed 门
# ---------------------------------------------------------------------------

def allowed_egress_tiers(environ=None) -> frozenset[str]:
    """解析 COURSELENS_TEST_EGRESS_ALLOW 子集；非法 token 抛契约错（启动面用）。

    synthetic 档设 ALLOW = 契约违例（合成档结构上不出本机）。
    未设 env 时返回空集且永不抛（发布版路径零行为）。
    """
    mode = raw_test_mode(environ)
    raw = _env_value(environ, EGRESS_ALLOW_ENV)
    if not mode or not raw:
        return frozenset()
    if mode == TEST_MODE_SYNTHETIC:
        raise TestModeContractError(TEST_EGRESS_ALLOW_SYNTHETIC)
    if mode != TEST_MODE_REAL:
        raise TestModeContractError(TEST_EGRESS_ALLOW_INVALID)
    tiers: set[str] = set()
    for token in raw.split(","):
        value = token.strip().casefold()
        if not value:
            continue
        if value not in EGRESS_TIER_VALUES:
            raise TestModeContractError(TEST_EGRESS_ALLOW_INVALID)
        tiers.add(value)
    return frozenset(tiers)


def egress_denial_reason(host: str, *, mode: str | None = None, tiers: frozenset[str] | None = None) -> str:
    """返回拒绝原因闭集串；空串 = 放行。

    发布版（mode=""）恒放行——整个测试模式契约对正常用户唯一的存在方式
    就是这条「env 为空 → 空串」常量判断。非法模式值 fail-closed 拒绝。
    """
    if mode is None:
        mode = mode_for_gate()
    if not mode:
        return ""
    if mode == "invalid":
        return TEST_MODE_INVALID
    hostname = str(host or "").strip().casefold().rstrip(".")
    if not hostname:
        return EGRESS_BLOCKED_CODE
    if hostname in LOOPBACK_HOSTS:
        # 环回 = 合成后端/本机测试桩宿主：两档测试模式都放行（不出本机）。
        return ""
    if mode == TEST_MODE_SYNTHETIC:
        return EGRESS_BLOCKED_CODE
    # real 档：非环回主机必须命中显式白名单档。
    if tiers is None:
        tiers = allowed_egress_tiers()
    for tier in tiers:
        if hostname in TIER_HOSTS.get(tier, frozenset()):
            return ""
    return EGRESS_BLOCKED_CODE


def _audit(event: str, *, mode: str, host: str, purpose: str, detail: str = "") -> None:
    suffix = f" detail={detail}" if detail else ""
    print(
        f"[egress] {event} code={EGRESS_BLOCKED_CODE if event == 'blocked' else 'egress_allowed'} "
        f"mode={mode} host={host} purpose={purpose}{suffix}",
        file=sys.stderr,
        flush=True,
    )


def _host_of(url_or_host: str) -> str:
    value = str(url_or_host or "").strip()
    if "://" in value or value.startswith("//"):
        try:
            return str(urlsplit(value).hostname or "").casefold()
        except ValueError:
            return ""
    return value.casefold()


def ensure_egress_allowed(url_or_host: str, *, purpose: str = "", environ=None) -> str:
    """出站前置门：放行返回主机名；拒绝先落审计行再抛 EgressBlockedError。

    发布版（未设 env）= 恒放行 + 零打印（行为与契约落地前逐位一致）。
    各客户端接线点把 EgressBlockedError 翻译成本族既有闭集失败
    （requests.ConnectionError / DirectICourseError / GitHubRemoteError /
    GitHubAppError / LiveRoomError / UpdateError），产品对外错误码集合零扩张；
    闭集码 ``egress_blocked`` 由审计行与异常属性承载，供钉测与事后复核。
    """
    mode = mode_for_gate(environ)
    if not mode:
        # 发布版热路径：一次 env 读取 + 常量判断即返回。
        return _host_of(url_or_host)
    host = _host_of(url_or_host)
    tiers: frozenset[str] | None = None
    try:
        tiers = allowed_egress_tiers(environ)
    except TestModeContractError as exc:
        _audit("blocked", mode=mode, host=host, purpose=purpose, detail=exc.code)
        raise EgressBlockedError(host, mode=mode, purpose=purpose, reason=exc.code) from exc
    reason = egress_denial_reason(host, mode=mode, tiers=tiers)
    if reason:
        _audit("blocked", mode=mode, host=host, purpose=purpose, detail=reason)
        raise EgressBlockedError(host, mode=mode, purpose=purpose, reason=reason)
    if host and host not in LOOPBACK_HOSTS:
        tier_hit = next(
            (tier for tier in sorted(tiers) if host in TIER_HOSTS.get(tier, frozenset())), ""
        )
        _audit("allowed", mode=mode, host=host, purpose=purpose, detail=tier_hit)
    return host


# ---------------------------------------------------------------------------
# 件①：沙盒数据目录强制 + 件②：端口段
# ---------------------------------------------------------------------------

def forbidden_default_data_dirs(environ=None) -> frozenset[Path]:
    """测试模式禁指的默认数据根闭集：平台默认目录（凭据信封所在地）
    + 产品仓内默认数据根（开发树真实数据）。路径全部 resolve 后比较。"""
    from src.platform.paths import platform_paths

    base = dict(os.environ if environ is None else environ)
    base.pop(DATA_DIR_ENV, None)
    forbidden: set[Path] = set()
    for platform in ("win32", "darwin"):
        try:
            forbidden.add(platform_paths(platform, environ=base).data_dir)
        except Exception:  # noqa: BLE001 - 平台默认目录缺环境变量时跳过该平台
            continue
    from path_utils import PROJECT_ROOT

    forbidden.add((Path(PROJECT_ROOT) / "runtime" / "data").resolve())
    return frozenset(forbidden)


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def test_data_dir_contract(data_dir_param: str | Path | None, *, environ=None) -> Path:
    """件①沙盒数据目录契约（纯解析，不写 env）。

    单一语义：COURSELENS_DATA_DIR 优先，--data-dir 参数其次；两者必须一致；
    缺一即拒；命中默认数据根闭集即拒。返回 resolve 后的沙盒根。
    """
    environ = os.environ if environ is None else environ
    env_value = _env_value(environ, DATA_DIR_ENV)
    param_value = str(data_dir_param).strip() if data_dir_param is not None else ""
    if not env_value and not param_value:
        raise TestModeContractError(TEST_DATA_DIR_REQUIRED)
    if env_value and param_value:
        if Path(env_value).expanduser().resolve() != Path(param_value).expanduser().resolve():
            raise TestModeContractError(TEST_DATA_DIR_CONFLICT)
    effective = Path(env_value or param_value).expanduser().resolve()
    for forbidden in forbidden_default_data_dirs(environ):
        if effective == forbidden or _is_within(effective, forbidden):
            raise TestModeContractError(TEST_DATA_DIR_DEFAULT_FORBIDDEN)
    return effective


def publish_data_dir_env(resolved: Path) -> None:
    """把契约解析出的沙盒根回写进程 env（子进程同源同值——终结双旗坑）。"""
    os.environ[DATA_DIR_ENV] = str(resolved)


def ensure_test_port_allowed(port: int) -> int:
    """件②端口契约：测试模式禁用 6268 产品默认端口；其余端口不设限
    （0=OS 分配，或件③实例管理器从 17700-17999 测试段取号）。"""
    if int(port) == PRODUCT_DEFAULT_PORT:
        raise TestModeContractError(TEST_PORT_RESERVED)
    return int(port)


def resolve_egress_allow_or_raise(environ=None) -> frozenset[str]:
    """启动面专用：合法时返回白名单档集合；违例抛契约错（serve 启动即拒）。"""
    return allowed_egress_tiers(environ)


# ---------------------------------------------------------------------------
# 件③M2（src 侧）：serve 自动注册的实例名契约
# ---------------------------------------------------------------------------

#: 与 tests/testbench/instances.py _NAME_RE 同口径（防路径逃逸；名字=车道
#: ID+角色）。src 侧独立持有同值正则：产品进程不 import 测试设施。
INSTANCE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def test_instance_name(environ=None) -> str:
    """serve 自动注册的实例名（未设 = 空串，发布版零成本）。"""
    return _env_value(environ, TEST_INSTANCE_ENV)


def test_instance_owner(environ=None) -> str:
    """serve 注册写进 claim 的 owner_lane（未设 = 空串 → 注册器用 "serve-auto"）。"""
    return _env_value(environ, TEST_INSTANCE_OWNER_ENV)


def test_registry_override(environ=None) -> str:
    """注册表目录覆盖（未设 = 空串 → 注册器用工作区级默认注册表）。"""
    return _env_value(environ, TEST_REGISTRY_ENV)


def validate_instance_name(name: str) -> str:
    """实例名契约校验：空串 = 未启用注册（原样通过）；非法名抛契约错。

    serve 启动契约在绑定端口前调用——拼写错误的实例名 fail-closed 拒启，
    不留「注册静默失败」的烂摊子。
    """
    value = str(name or "").strip()
    if value and not INSTANCE_NAME_RE.match(value):
        raise TestModeContractError(TEST_INSTANCE_NAME_INVALID)
    return value


# ---------------------------------------------------------------------------
# 件②M3：real 档测试身份凭据文件联动（内存注入真 UIS 登录）
# ---------------------------------------------------------------------------

def test_credentials_file(environ=None) -> str:
    """测试身份凭据文件 env 原始值（未设 = 空串，发布版零成本）。"""
    return _env_value(environ, TEST_CREDENTIALS_FILE_ENV)


def resolve_test_credentials_linkage(environ=None) -> str:
    """凭据文件三重联动校验（启动契约调用，绑定端口前 fail-closed）。

    1. 只在 real 档生效：未设模式/synthetic 档设了文件 = 契约违例；
    2. school:readonly 白名单档必须同时在册（真 UIS 登录链必出站学校主机，
       缺档 = 把必然的 egress_blocked 提前到启动期说清楚，不留给登录半程）；
    3. 文件必须实存（存在性在此核验；形态/schema 由读取方
       src/runtime/test_credentials.py 在使用时校验——serve 启动路径自身
       不读凭据内容，凭据只进真正执行登录的进程内存）。

    返回 resolve 后的文件路径；未设 env = 空串（发布版零成本）。
    """
    raw = test_credentials_file(environ)
    if not raw:
        return ""
    mode = raw_test_mode(environ)
    if mode != TEST_MODE_REAL:
        raise TestModeContractError(TEST_CREDENTIALS_MODE_CONFLICT)
    tiers = allowed_egress_tiers(environ)
    if EGRESS_TIER_SCHOOL_READONLY not in tiers:
        raise TestModeContractError(TEST_CREDENTIALS_TIER_REQUIRED)
    resolved = Path(raw).expanduser().resolve()
    if not resolved.is_file():
        raise TestModeContractError(TEST_CREDENTIALS_FILE_MISSING)
    return str(resolved)


# ---------------------------------------------------------------------------
# 件⑥：合成后端收编 + 件④/⑤契约成文
# ---------------------------------------------------------------------------

def resolve_synthetic_backend_mode(environ=None) -> str:
    """合成后端（tests.synthetic_shell_server）的模式归一：

    未设 → 结构性自设 synthetic（既有车道命令行原样可用=兼容期语义）；
    synthetic → 原样；real / 非法值 → 契约违例（合成壳绝不充当 real 后端）。
    """
    value = raw_test_mode(environ)
    if not value:
        return TEST_MODE_SYNTHETIC
    if value == TEST_MODE_SYNTHETIC:
        return value
    raise TestModeContractError(TEST_MODE_CONFLICT if value == TEST_MODE_REAL else TEST_MODE_INVALID)


def synthetic_backend_recipe() -> str:
    """合成档标准后端启动配方（车道文档一处可查；入口/旗标归契约面）。

    车道标准姿势（配合件③实例管理器）::

        from tests.testbench.instances import TestInstanceManager
        inst = TestInstanceManager().claim("<lane>-shell", owner_lane="<LANE>")
        env["COURSELENS_TEST_MODE"] = "synthetic"      # 壳内缺省会自设，显式更佳
        env["COURSELENS_DATA_DIR"] = str(inst.data_dir)
        python -m tests.synthetic_shell_server --port <inst.port 或 0> [旗标]

    旗标闭集：--onboarding-guide（ONBOARDING_GUIDE_MODES）/ --onboarding-fault
    none|actions / --onboarding-profile ready|partial / --seed（SEED_PRESETS，
    可重复）/ --seed-tasks <json> / --seed-clock real|frozen（确定性时钟唯一
    接入点=种子层）/ --state-dir <持久根，项目内> / --cache-root <项目内>。

    env 旗标闭集（同为此处一处可查）：COURSELENS_TEST_MODE（synthetic|real）/
    COURSELENS_DATA_DIR（沙盒数据目录，serve 回写子进程继承）/
    COURSELENS_TEST_EGRESS_ALLOW（仅 real 档；school:readonly|github:api）/
    COURSELENS_TEST_FRESH_OPERATIONS=1（仅测试模式；跳过 operation 幂等
    回放历史，E2E-2v6 教训）。
    """
    return (
        f"python -m {SYNTHETIC_BACKEND_MODULE} --port <port|0> "
        f"[--onboarding-guide {'|'.join(ONBOARDING_GUIDE_MODES)}] "
        "[--onboarding-fault none|actions] [--onboarding-profile ready|partial] "
        "[--seed <preset> ...] [--seed-tasks <json>] "
        f"[--seed-clock {'|'.join(SEED_CLOCK_MODES)}] "
        "[--state-dir <in-project>] [--cache-root <in-project>]"
    )


__all__ = [
    "APP_DIR_NAME",
    "DATA_DIR_ENV",
    "EGRESS_ALLOW_ENV",
    "EGRESS_BLOCKED_CODE",
    "EGRESS_TIER_GITHUB_API",
    "EGRESS_TIER_SCHOOL_READONLY",
    "EGRESS_TIER_VALUES",
    "EgressBlockedError",
    "FRESH_OPERATIONS_ENV",
    "FRESH_OPERATIONS_VALUES",
    "INSTANCE_NAME_RE",
    "LOOPBACK_HOSTS",
    "ONBOARDING_GUIDE_MODES",
    "PRODUCT_DEFAULT_PORT",
    "SEED_CLOCK_MODES",
    "SYNTHETIC_BACKEND_MODULE",
    "TEST_DATA_DIR_CONFLICT",
    "TEST_DATA_DIR_DEFAULT_FORBIDDEN",
    "TEST_DATA_DIR_REQUIRED",
    "TEST_EGRESS_ALLOW_INVALID",
    "TEST_EGRESS_ALLOW_SYNTHETIC",
    "TEST_CREDENTIALS_FILE_ENV",
    "TEST_CREDENTIALS_FILE_INVALID",
    "TEST_CREDENTIALS_FILE_MISSING",
    "TEST_CREDENTIALS_MODE_CONFLICT",
    "TEST_CREDENTIALS_TIER_REQUIRED",
    "TEST_INSTANCE_ENV",
    "TEST_INSTANCE_NAME_INVALID",
    "TEST_INSTANCE_OWNER_ENV",
    "TEST_MODE_CONFLICT",
    "TEST_MODE_ENV",
    "TEST_MODE_ERROR_CODES",
    "TEST_MODE_INVALID",
    "TEST_MODE_REAL",
    "TEST_MODE_SYNTHETIC",
    "TEST_MODE_VALUES",
    "TEST_PORT_RESERVED",
    "TEST_REGISTRY_ENV",
    "TestModeContractError",
    "TIER_HOSTS",
    "allowed_egress_tiers",
    "egress_denial_reason",
    "ensure_egress_allowed",
    "ensure_test_port_allowed",
    "forbidden_default_data_dirs",
    "fresh_operations_requested",
    "is_test_mode",
    "mode_for_gate",
    "publish_data_dir_env",
    "raw_test_mode",
    "resolve_synthetic_backend_mode",
    "resolve_test_mode",
    "synthetic_backend_recipe",
    "test_data_dir_contract",
    "test_credentials_file",
    "resolve_test_credentials_linkage",
    "test_instance_name",
    "test_instance_owner",
    "test_registry_override",
    "validate_instance_name",
]
