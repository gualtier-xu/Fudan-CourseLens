"""持久测试身份设施（测试台架件①M1/M2；TB-IDT-M1）。

GitHub 专用测试身份（co 账号）的登录态获取/持久化/过期自愈/边界守卫。
设计出处：``archive/external-artifacts/top-model-results-20260930/product-testbench1-result-20261009.md``
件①；登录实测配方=TEST-LOGIN-1（headless=False 账密直登 + storageState 复用闭环）。

分级（与设计件①一一对应）：
- **Tier A**（凭据本体）：工作区 ``.local-secrets/co-github-test.json``（username+password）。
  只经本模块/驱动文件内读取进内存；零明文进 argv/env/日志/结果文件/prompt。
- **Tier B**（会话态）：工作区 ``.testbench/identity/<身份名>/storageState.json``（0600，
  cookie=等效凭据）+ ``meta.json``（owner/用途/时间，无秘密）。可整体删除重建=轮换。

载体决策（对设计的最小适配，已申报）：TEST-LOGIN-1 复用腿已证 storageState 新 context
复用闭环成立，本设施以 **storageState-first、免 profile 目录锁** 为载体——多车道可并发
各开各的 context 共享同一登录态，不被 persistent profile 的单进程锁互斥。

浏览器执行体：Node ``gh_login_driver.cjs``（require('playwright')）经 subprocess 编排；
playwright 模块解析序=env ``COURSELENS_TESTBENCH_NODE_MODULES`` → npx 缓存（stable 优先）
→ 工作区 ``.tmp-*/node_modules`` → 无则 :class:`PlaywrightUnavailable`（车道据此诚实 SKIP）。

边界守卫（设计件①边界表，fail-closed）：
- purpose 闭集 allow-list（:data:`ALLOWED_PURPOSES`）——学校登录/真实首跑走查等
  不在册用途一律 :class:`BoundaryViolationError`，拒绝持久化、拒绝本设施接管；
- 学校形态凭据文件（键名含 uis/school/student/campus/学号/校园）拒收；
- 身份名只允许字母/数字/点/下划线/连字符（与实例管理器同名法语义），禁路径逃逸；
- CAPTCHA/2FA/设备验证挑战=:class:`ChallengeEncountered` 即停转人工（红线不代批）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# 复用实例管理器的工作区根口径（模块位置推导、零 cwd 依赖）——跨模块最小复用。
from tests.testbench.instances import workspace_root

__all__ = [
    "ALLOWED_PURPOSES",
    "PURPOSE_REGISTRY",
    "BoundaryViolationError",
    "ChallengeEncountered",
    "CredentialsUnavailable",
    "DriverError",
    "EnsureResult",
    "GitHubTestSecrets",
    "IdentityError",
    "OutboundUnavailable",
    "PlaywrightUnavailable",
    "ProbeResult",
    "default_identity_root",
    "default_tier_a_path",
    "ensure_session",
    "hardened_access_ok",
    "harden_file",
    "identity_dir",
    "load_tier_a_secrets",
    "persist_storage_state",
    "redact",
    "simulate_expiry",
    "verify_session",
]


# ---------------------------------------------------------------------------
# 异常（全部人话，像耐心的同学在解释）
# ---------------------------------------------------------------------------


class IdentityError(Exception):
    """持久测试身份设施的基础异常。"""


class BoundaryViolationError(IdentityError):
    """边界守卫拒绝：不在册用途/学校凭据/真实首跑场景/非法身份名。"""


class CredentialsUnavailable(IdentityError):
    """Tier A 凭据文件缺失或形态不符（车道应诚实 SKIP，不得伪造登录态）。"""


class PlaywrightUnavailable(IdentityError):
    """找不到可用的 node+playwright 执行体（车道应诚实 SKIP）。"""


class DriverError(IdentityError):
    """登录驱动致命失败（配置/环境类；不含凭据明文）。"""


class ChallengeEncountered(IdentityError):
    """GitHub 反自动化挑战（2FA/设备验证/CAPTCHA）——即停转人工，禁暴力重试。"""


class OutboundUnavailable(IdentityError):
    """github.com 外联不可达（会话有效性与它无关；车道应诚实 SKIP 而非误重登）。"""


# ---------------------------------------------------------------------------
# purpose 闭集（fail-closed allow-list；申报表=测试身份用途清单）
# ---------------------------------------------------------------------------

ALLOWED_PURPOSES = frozenset(
    {"device-auth", "app-install", "remote-e2e", "api", "generic-test"}
)

PURPOSE_REGISTRY = {
    "device-auth": "GitHub 设备码授权页自动化（远程链绑定，设计件①M2）",
    "app-install": "GitHub App 安装/范围收紧页自动化（设计件①M2）",
    "remote-e2e": "远程链 E2E 的 GitHub 网页段（REMOTE-E2E 型车道）",
    "api": "api.github.com 只读探测/资源清单读取",
    "generic-test": "一般测试会话的网页登录态（走查矩阵/回归面）",
}

# 在册拒绝示例（文档性；allow-list 下一切不在册用途本就被拒）
BOUNDARY_REFUSED_EXAMPLES = {
    "real-first-run-walkthrough": "真实首跑走查必须真人真账号（构建者盲区门），测试身份不接管",
    "school-uis-login": "学校 UIS 凭据不做持久会话（安全等级更高），本设施结构性不接",
}

_IDENTITY_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SCHOOLISH_KEY_RE = re.compile(r"uis|school|student|campus|学号|校园", re.IGNORECASE)
_DEFAULT_IDENTITY = "github-co"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def redact(login: str | None) -> str:
    """登录名脱敏（前 2 字符+***）；任何日志/产物只允许出现脱敏形态。"""
    return login[:2] + "***" if login and len(login) > 2 else "***"


def _ensure_identity_name(name: str) -> str:
    if not isinstance(name, str) or not _IDENTITY_NAME_RE.match(name) or ".." in name:
        raise BoundaryViolationError(
            f"身份名 {name!r} 非法：只允许字母/数字/点/下划线/连字符（例 github-co），"
            "禁路径逃逸——Tier B 目录按身份名归档，名字即边界。"
        )
    return name


def ensure_purpose_allowed(purpose: str) -> str:
    if purpose not in ALLOWED_PURPOSES:
        known = "；".join(
            f"{k}（{v}）" for k, v in sorted(BOUNDARY_REFUSED_EXAMPLES.items())
        )
        raise BoundaryViolationError(
            f"用途 {purpose!r} 不在测试身份用途闭集内（{sorted(ALLOWED_PURPOSES)}），"
            f"拒绝持久化也拒绝登录：测试身份只服务测试会话。真实首跑走查要真人真账号、"
            f"学校凭据不做持久会话（例：{known}）。"
        )
    return purpose


# ---------------------------------------------------------------------------
# Tier A / Tier B 路径
# ---------------------------------------------------------------------------


def default_tier_a_path() -> Path:
    """Tier A 凭据本体（工作区 .local-secrets/co-github-test.json）。"""
    return workspace_root() / ".local-secrets" / "co-github-test.json"


def default_identity_root() -> Path:
    """Tier B 身份目录根（工作区级 .testbench/identity/，工作区非 git 仓不入库）。"""
    return workspace_root() / ".testbench" / "identity"


def identity_dir(identity_name: str = _DEFAULT_IDENTITY, root: Path | None = None) -> Path:
    """Tier B 单身份目录（按身份名归档；名字先过边界守卫）。"""
    _ensure_identity_name(identity_name)
    return (root or default_identity_root()) / identity_name


def _state_path(identity_name: str, root: Path | None) -> Path:
    return identity_dir(identity_name, root) / "storageState.json"


def _meta_path(identity_name: str, root: Path | None) -> Path:
    return identity_dir(identity_name, root) / "meta.json"


# ---------------------------------------------------------------------------
# Tier A 凭据装载（文件→内存；零明文出内存）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitHubTestSecrets:
    """GitHub 测试身份凭据（只存内存；repr 永脱敏）。"""

    username: str
    password: str

    def __repr__(self) -> str:  # 防误入日志
        return f"GitHubTestSecrets(username={redact(self.username)}, password=***)"


def load_tier_a_secrets(path: Path | None = None) -> GitHubTestSecrets:
    """读 Tier A 凭据（只回内存对象；缺失/形态不符=CredentialsUnavailable）。"""
    tier_a = Path(path) if path else default_tier_a_path()
    if not tier_a.exists():
        raise CredentialsUnavailable(
            f"Tier A 凭据文件不存在（{tier_a}）：持久测试身份需要它做账密直登；"
            "本车道请诚实 SKIP，禁止伪造登录态或向日志/prompt 索要凭据。"
        )
    try:
        raw = json.loads(tier_a.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CredentialsUnavailable(
            f"Tier A 凭据文件不可读或非 JSON（{tier_a}）：{type(exc).__name__}（内容未读取入任何日志）。"
        ) from exc
    schoolish = sorted(k for k in raw if isinstance(k, str) and _SCHOOLISH_KEY_RE.search(k))
    if schoolish:
        raise BoundaryViolationError(
            f"凭据文件含学校形态键名（{schoolish}）：学校凭据不做持久会话，"
            "本设施结构性不接（边界守卫；测试身份只认 GitHub 测试账号 username+password 形态）。"
        )
    username, password = raw.get("username"), raw.get("password")
    if not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password:
        raise CredentialsUnavailable(
            f"Tier A 凭据文件形态不符（{tier_a}）：需要非空 username+password 字符串字段。"
        )
    return GitHubTestSecrets(username=username, password=password)


# ---------------------------------------------------------------------------
# 0600 硬化（Windows icacls 等效 / POSIX chmod）
# ---------------------------------------------------------------------------


def harden_file(path: Path) -> Path:
    """把文件收权到仅当前用户（Windows=去继承+仅本用户 F；POSIX=0600）。"""
    path = Path(path)
    if sys.platform == "win32":
        user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        if not user:
            import getpass

            user = getpass.getuser()
        subprocess.run(
            ["icacls", str(path), "/inheritance:r"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["icacls", str(path), "/grant:r", f"{user}:F"],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        path.chmod(0o600)
    return path


def hardened_access_ok(path: Path) -> bool:
    """核验文件是否已收权到仅当前用户（storageState 即凭据，落盘必查）。"""
    path = Path(path)
    if not path.exists():
        return False
    if sys.platform != "win32":
        return (path.stat().st_mode & 0o777) == 0o600
    try:
        out = subprocess.run(
            ["icacls", str(path)], check=True, capture_output=True, text=True
        ).stdout
    except subprocess.CalledProcessError:
        return False
    user = (os.environ.get("USERNAME") or os.environ.get("USER") or "").lower()
    lowered = out.lower()
    if user and f"{user}:".lower() not in lowered:
        return False
    for leaked in ("everyone", "authenticated users", "builtin\\users", "miracl"):
        if leaked in lowered:
            return False
    return "(i)" not in lowered  # 继承 ACE 已全部移除


# ---------------------------------------------------------------------------
# 持久化（Tier B 落盘）
# ---------------------------------------------------------------------------


def persist_storage_state(
    incoming: Path,
    *,
    identity_name: str = _DEFAULT_IDENTITY,
    purpose: str,
    owner_lane: str,
    root: Path | None = None,
    extra_meta: dict | None = None,
) -> Path:
    """把驱动产出的 storageState 收进 Tier B：校验形态→硬化 0600→原子落位→写 meta。"""
    _ensure_identity_name(identity_name)
    ensure_purpose_allowed(purpose)
    incoming = Path(incoming)
    try:
        payload = json.loads(incoming.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DriverError(f"驱动产出的 storageState 不可读或非 JSON：{type(exc).__name__}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("cookies"), list):
        raise DriverError("驱动产出的 storageState 形态不符（缺 cookies 数组）——拒绝落盘。")

    target_dir = identity_dir(identity_name, root)
    target_dir.mkdir(parents=True, exist_ok=True)
    state_path = target_dir / "storageState.json"
    harden_file(incoming)
    os.replace(incoming, state_path)  # 同卷原子换位；ACL 随文件保留
    if not hardened_access_ok(state_path):
        raise DriverError(f"storageState 落盘后收权核验未通过（{state_path}）——宁可失败不可裸奔。")

    meta = {
        "identity": identity_name,
        "purpose": purpose,
        "owner_lane": owner_lane,
        "created_at": _utc_now_iso(),
        "updated_at": _utc_now_iso(),
        "storage_state": str(state_path),
        "probe_endpoint": "github.com/settings/profile（零导航探测；api.github.com 实测拒收 web 会话 cookie）",
        "note": "storageState=等效凭据（Tier B 管控，0600）；本文件无秘密；整体删除目录即轮换。",
        "allowed_purposes": sorted(ALLOWED_PURPOSES),
    }
    if extra_meta:
        meta.update(extra_meta)
    meta_path = _meta_path(identity_name, root)
    if meta_path.exists():
        try:
            old = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["created_at"] = old.get("created_at") or meta["created_at"]
        except ValueError:
            pass
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return state_path


def simulate_expiry(
    identity_name: str = _DEFAULT_IDENTITY, root: Path | None = None
) -> Path:
    """把 Tier B 会话态替换为空 cookie 形态（自愈钉测/演练钩子；硬化保持 0600）。"""
    _ensure_identity_name(identity_name)
    state_path = _state_path(identity_name, root)
    if not state_path.exists():
        raise IdentityError(f"没有可过期的会话态（{state_path} 不存在）——先 ensure_session 一次。")
    scratch = state_path.with_name(".storageState.incoming.json")
    scratch.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    harden_file(scratch)
    os.replace(scratch, state_path)
    return state_path


# ---------------------------------------------------------------------------
# Node+playwright 执行体解析与驱动编排
# ---------------------------------------------------------------------------

_DRIVER_SCRIPT = Path(__file__).resolve().parent / "gh_login_driver.cjs"
_STABLE_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def find_playwright_node_modules() -> Path:
    """定位含 playwright 的 node_modules（env 覆盖→npx 缓存 stable 优先→工作区 .tmp-*）。"""
    env_hint = os.environ.get("COURSELENS_TESTBENCH_NODE_MODULES")
    if env_hint:
        candidate = Path(env_hint)
        if (candidate / "playwright" / "package.json").exists():
            return candidate
        raise PlaywrightUnavailable(
            f"COURSELENS_TESTBENCH_NODE_MODULES={env_hint} 下没有 playwright/package.json——"
            "请指向含 playwright 的 node_modules 目录。"
        )
    candidates: list[tuple[tuple[int, ...], bool, Path]] = []
    local_appdata = os.environ.get("LOCALAPPDATA")
    # (glob 根, 相对模式) 对：pathlib 基路径不能带通配，通配必须整体走 glob()。
    glob_specs: list[tuple[Path, str]] = []
    if local_appdata:
        glob_specs.append(
            (Path(local_appdata) / "npm-cache" / "_npx", "*/node_modules")
        )
    workspace = workspace_root()
    glob_specs.extend(
        [
            (workspace, ".tmp-*/node_modules"),
            (workspace, ".tmp-*/scratch/node_modules"),
        ]
    )
    for glob_root, rel_pattern in glob_specs:
        for candidate in glob_root.glob(rel_pattern):
            pkg = candidate / "playwright" / "package.json"
            if not pkg.exists():
                continue
            try:
                version = json.loads(pkg.read_text(encoding="utf-8")).get("version", "")
            except (OSError, ValueError):
                continue
            stable = bool(_STABLE_VERSION_RE.match(version))
            parts = tuple(int(p) for p in re.findall(r"\d+", version)[:3] or [0])
            candidates.append((parts, stable, candidate))
    if candidates:
        candidates.sort(key=lambda item: (item[1], item[0]), reverse=True)
        return candidates[0][2]
    raise PlaywrightUnavailable(
        "找不到含 playwright 的 node_modules（找过 npx 缓存与工作区 .tmp-*）；"
        "设 COURSELENS_TESTBENCH_NODE_MODULES 后重试，或本车道按 PlaywrightUnavailable 诚实 SKIP。"
    )


def _driver_command(mode: str, *, secrets_path: Path | None = None,
                    state_out: Path | None = None, state_path: Path | None = None,
                    headless: bool = False) -> list[str]:
    """纯函数：驱动 argv（只含路径与旗标，凭据永不入 argv/env）。"""
    cmd = ["node", str(_DRIVER_SCRIPT), mode]
    if mode == "login":
        cmd += ["--secrets", str(secrets_path), "--state-out", str(state_out)]
        if headless:
            cmd.append("--headless")
    else:
        cmd += ["--state", str(state_path)]
    return cmd


def _driver_env(node_modules: Path) -> dict:
    env = dict(os.environ)
    env["NODE_PATH"] = str(node_modules)
    return env


def run_driver(args: list[str], *, timeout_s: float = 180.0) -> dict:
    """跑驱动并解析其最后一个 stdout JSON 行；退出码→异常映射（4=挑战即停）。"""
    if not _DRIVER_SCRIPT.exists():
        raise DriverError(f"驱动脚本缺失：{_DRIVER_SCRIPT}")
    node = shutil.which("node")
    if not node:
        raise PlaywrightUnavailable("PATH 上找不到 node——登录驱动无法执行。")
    env = _driver_env(find_playwright_node_modules())
    argv = [node] + args[1:]  # args[0] 是字面 "node"，替换为解析到的可执行文件
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        env=env,
    )
    payload: dict = {}
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue
    if not payload:
        raise DriverError(
            f"驱动无 JSON 输出（exit={proc.returncode}）；stderr 摘要="
            f"{(proc.stderr or '').strip()[:200]!r}（已截断，无凭据）"
        )
    if proc.returncode == 4:
        raise ChallengeEncountered(
            "GitHub 反自动化挑战（2FA/设备验证/CAPTCHA）——按红线即停转人工，"
            f"kind={payload.get('kind') or payload.get('error')}；禁止退避重试暴力闯关。"
        )
    if proc.returncode == 2:
        raise DriverError(f"驱动致命失败：{payload.get('error')}（{payload.get('detail', '')}）")
    return payload


# ---------------------------------------------------------------------------
# 探测与自愈主入口
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    """会话探活结果（status=HTTP 码，200=有效/302·401=无效；-1=外联不可达）。"""

    status: int
    login_redacted: str | None = None
    error_kind: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200


@dataclass(frozen=True)
class EnsureResult:
    """ensure_session 结果：reused=旧态直接可用；refreshed=本次重新登录刷新。"""

    identity: str
    purpose: str
    reused: bool
    refreshed: bool
    login_redacted: str | None
    state_path: Path
    meta_path: Path


def verify_session(
    identity_name: str = _DEFAULT_IDENTITY, root: Path | None = None
) -> ProbeResult:
    """零导航探活：新 context 装载 Tier B storageState → GET github.com/settings/profile。

    （api.github.com/user 实测 401——api 主机不收 web 会话 cookie，禁作探针；
    正典端点=设计件①R2 配方 settings/profile，登出态 302 到登录墙=无效信号。）
    """
    _ensure_identity_name(identity_name)
    state_path = _state_path(identity_name, root)
    if not state_path.exists():
        return ProbeResult(status=0, error_kind="state-missing")
    payload = run_driver(_driver_command("verify", state_path=state_path), timeout_s=120.0)
    if payload.get("errorKind") == "network" or payload.get("status") == -1:
        raise OutboundUnavailable(
            "github.com 外联不可达：会话是否有效未知；车道按诚实 SKIP 处理，勿在断网时误重登。"
        )
    return ProbeResult(
        status=int(payload.get("status") or 0),
        login_redacted=payload.get("loginRedacted"),
        error_kind=payload.get("errorKind"),
    )


def ensure_session(
    purpose: str,
    identity_name: str = _DEFAULT_IDENTITY,
    owner_lane: str = "UNKNOWN-LANE",
    root: Path | None = None,
    force_refresh: bool = False,
) -> EnsureResult:
    """车道主入口：守卫→旧态探活（可用即复用）→失效自动重登刷新 Tier B。

    边界守卫先于一切网络动作：purpose/身份名不合法=BoundaryViolationError，
    不创建目录、不外联。学校凭据/真实首跑走查永远走不到这里（allow-list 拒绝）。
    """
    _ensure_identity_name(identity_name)
    ensure_purpose_allowed(purpose)

    state_path = _state_path(identity_name, root)
    if not force_refresh and state_path.exists():
        probe = verify_session(identity_name, root)
        if probe.ok:
            return EnsureResult(
                identity=identity_name,
                purpose=purpose,
                reused=True,
                refreshed=False,
                login_redacted=probe.login_redacted,
                state_path=state_path,
                meta_path=_meta_path(identity_name, root),
            )

    # 旧态缺失/失效 → 账密直登刷新（Tier A 文件路径传给驱动，凭据零明文出内存）。
    tier_a = default_tier_a_path()
    load_tier_a_secrets(tier_a)  # 先验形态（缺凭据=CredentialsUnavailable，诚实失败）
    target_dir = identity_dir(identity_name, root)
    target_dir.mkdir(parents=True, exist_ok=True)
    incoming = target_dir / ".storageState.incoming.json"
    headless = os.environ.get("COURSELENS_TESTBENCH_GH_HEADLESS") == "1"
    payload = run_driver(
        _driver_command(
            "login", secrets_path=tier_a, state_out=incoming, headless=headless
        ),
        timeout_s=240.0,
    )
    if not payload.get("ok"):
        # 直登失败/登录后探活未过（驱动已如实分类）：不落盘、不假绿。
        raise DriverError(
            f"登录驱动未成功（phase={payload.get('phase')}，verdict={payload.get('verdict')}，"
            f"probe={payload.get('probe')}）——storageState 不落盘；请先人工核对账号侧状态。"
        )
    state = persist_storage_state(
        incoming,
        identity_name=identity_name,
        purpose=purpose,
        owner_lane=owner_lane,
        root=root,
        extra_meta={"login_final_url": payload.get("finalUrl", "")},
    )
    probe = verify_session(identity_name, root)
    if not probe.ok:
        raise DriverError(
            f"重登后探活未通过（status={probe.status}）——登录面与探活面不一致，"
            "请勿继续使用该身份；先人工核对 GitHub 侧会话状态。"
        )
    return EnsureResult(
        identity=identity_name,
        purpose=purpose,
        reused=False,
        refreshed=True,
        login_redacted=payload.get("loginRedacted"),
        state_path=state,
        meta_path=_meta_path(identity_name, root),
    )
