"""CourseLens 测试实例管理器（测试台架设计件③M1；TESTBENCH-DESIGN-1 落地）。

终结多车道抢真装/端口碰撞（8791 撞 8775 案）的结构性设施：

- **claim**：车道按互斥命名空间认领实例槽（数据目录+端口+claim 文件），
  原子创建（O_EXCL），名字冲突时失败并给出人话指引。
- **心跳**：持有者周期触碰 claim 文件 mtime 续约（守护线程，默认 30s）。
- **stale 判定与接管**：心跳落后超过阈值（默认 120s）**或** pid 不存在
  （进程实存为准，不盲信文件）即视为 stale；接管前强制持有进程存活核验，
  pid 仍存活者拒绝接管（防活持有者被抢=互踩灾难本身）。接管以原子重创建
  claim 文件为串行化点，并写入 takeover 事件留痕。
- **release**：用毕删除 claim 文件（handle.release() / finally 块）。
- **端口段 17700-17999**：registry 已占端口 + bind 实测双查；分配与 claim
  创建在同一跨进程互斥分配锁内，终结并发 allocate 撞号窗口。避开产品默认
  端口 6268、用户实例端口 7482、Hyper-V/WSL 保留段 8574-8673。
- **真装只读边界守卫**：拒绝认领用户默认数据目录（%LOCALAPPDATA%/CourseLens
  等）、产品仓内目录、测试段之外的任何端口。真装 pid 出现在驱动写操作=违规
  （车道 prompt 模板纪律，非本模块职责）。

注册表 = 工作区级 ``<workspace>/.testbench/instances/``（工作区非 git 仓，
天然不入版本库；路径一律工作区级绝对路径）。本模块是纯测试台架设施，
零产品改动、零产品行为影响。

车道接入三行（详见同目录 README.md）::

    from tests.testbench.instances import TestInstanceManager
    inst = TestInstanceManager().claim("mylane-backend", owner_lane="MY-LANE")
    ...  # inst.port / inst.data_dir / inst.chrome_profile_dir / inst.scratch_dir
    inst.release()
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# 常量与错误类型
# ---------------------------------------------------------------------------

#: 测试专用端口段（设计件③：避开 6268 产品默认 / 7482 类用户实例 /
#: 8574-8673 Hyper-V/WSL 保留坑，且避开 Windows 动态端口范围 49152+）。
TEST_PORT_MIN = 17700
TEST_PORT_MAX = 17999

#: 具名禁用端口（即使在测试段内也永不分配；语义=真装/用户面保护）。
FORBIDDEN_PORTS: dict[int, str] = {
    6268: "产品默认端口（用户真装实例）",
    7482: "用户实例端口（真装）",
}

#: 具名禁用端口段。
FORBIDDEN_PORT_RANGES: tuple[tuple[int, int, str], ...] = (
    (8574, 8673, "Hyper-V/WSL 保留段（名义空闲实为保留）"),
)

#: 心跳落后该秒数即 stale（真实使用默认；测试可注入小值）。
DEFAULT_STALE_SECONDS = 120.0

#: 心跳续约周期（秒）。
DEFAULT_HEARTBEAT_INTERVAL = 30.0

#: claim 文件名合法性（防路径逃逸；名字=车道 ID+角色）。
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

CLAIM_SUFFIX = ".json"
ALLOC_LOCK_NAME = ".port-alloc.lock"
ALLOC_LOCK_STEAL_SECONDS = 30.0


class InstanceManagerError(Exception):
    """测试实例管理器基础错误（人话消息，面向车道同学）。"""


class InvalidInstanceName(InstanceManagerError):
    """实例名非法（含路径逃逸尝试）。"""


class InstanceAlreadyClaimed(InstanceManagerError):
    """名字已被认领。"""


class InstanceNotClaimed(InstanceManagerError):
    """名字未被认领（已释放或从未存在）。"""


class TakeoverRefused(InstanceManagerError):
    """接管被拒（持有进程仍存活等）。"""


class RealInstallProtectionError(InstanceManagerError):
    """真装只读边界守卫触发（用户默认目录/真装端口等）。"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 路径与真装守卫
# ---------------------------------------------------------------------------


def workspace_root() -> Path:
    """工作区根（多仓工作区；模块位于 <workspace>/private/main/tests/testbench/，零 cwd 依赖）。"""
    return Path(__file__).resolve().parents[4]


def product_repo_root() -> Path:
    """产品仓根（private/main）。"""
    return Path(__file__).resolve().parents[2]


def default_registry_dir() -> Path:
    """默认注册表：工作区级 .testbench/instances/（工作区非 git 仓，天然不入库）。"""
    return workspace_root() / ".testbench" / "instances"


def platform_default_data_dirs() -> list[Path]:
    """用户真装默认数据目录候选集（与 src/platform/paths.py 口径对齐的镜像判定）。

    特意不 import 产品模块：守卫需要的是「用户真装默认目录」这一物理事实，
    不应被 COURSELENS_DATA_DIR 等环境覆盖干扰。
    """
    candidates: list[Path] = []
    local = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if local:
        candidates.append(Path(local) / "CourseLens")
    home = Path.home()
    candidates.append(home / "AppData" / "Local" / "CourseLens")  # Windows 默认
    candidates.append(home / "Library" / "Application Support" / "CourseLens")  # macOS
    return candidates


def ensure_test_port(port: int) -> int:
    """真装只读边界守卫（端口侧）：测试段之外/具名禁用端口一律拒绝。

    拒绝即人话解释（北极星：像耐心的同学在说明，不是系统日志）。
    """
    port = int(port)
    for lo, hi, why in FORBIDDEN_PORT_RANGES:
        if lo <= port <= hi:
            raise RealInstallProtectionError(
                f"端口 {port} 不能用于测试实例：{why}。"
                f"请改用管理器分配的测试段端口（{TEST_PORT_MIN}-{TEST_PORT_MAX}）。"
            )
    if port in FORBIDDEN_PORTS:
        raise RealInstallProtectionError(
            f"端口 {port} 不能用于测试实例：{FORBIDDEN_PORTS[port]}。"
            f"请改用管理器分配的测试段端口（{TEST_PORT_MIN}-{TEST_PORT_MAX}）。"
        )
    if not (TEST_PORT_MIN <= port <= TEST_PORT_MAX):
        raise RealInstallProtectionError(
            f"端口 {port} 在测试段（{TEST_PORT_MIN}-{TEST_PORT_MAX}）之外："
            "测试段外的端口属于用户/真装面，测试车道一律不许占用（真装只读边界）。"
            "请省略 port 参数让管理器自动分配。"
        )
    return port


def ensure_test_data_dir(path: str | os.PathLike[str]) -> Path:
    """真装只读边界守卫（数据目录侧）：用户默认目录/产品仓内目录一律拒绝。"""
    resolved = Path(path).expanduser().resolve()
    for default_dir in platform_default_data_dirs():
        default_resolved = default_dir.resolve()
        if resolved == default_resolved or default_resolved in resolved.parents:
            raise RealInstallProtectionError(
                f"数据目录 {resolved} 位于用户真装默认数据目录（{default_resolved}）之内："
                "学生真实数据结构性不可达是测试台架的安全底线（真装只读边界）。"
                "请使用管理器认领的实例目录（registry/<实例名>/data）。"
            )
    repo_root = product_repo_root().resolve()
    if resolved == repo_root or repo_root in resolved.parents:
        raise RealInstallProtectionError(
            f"数据目录 {resolved} 位于产品仓（{repo_root}）之内："
            "实例数据必须落在工作区级 .testbench/instances/ 注册表内，"
            "不许写进产品仓（真装只读边界）。"
        )
    return resolved


def _ensure_instance_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise InvalidInstanceName(
            f"实例名 {name!r} 非法：只允许字母/数字/点/下划线/连字符"
            "（名字=车道 ID+角色，例如 remotee2e1r4-backend）。"
        )
    return name


# ---------------------------------------------------------------------------
# 进程存活核验（Windows 安全形态；禁用 os.kill(pid, 0)——Windows 上会真杀进程）
# ---------------------------------------------------------------------------


def pid_alive(pid: int | None) -> bool:
    """进程是否实存（不盲信文件）。Windows 用 OpenProcess 查询，绝不误杀。"""
    if pid is None or int(pid) <= 0:
        return False
    pid = int(pid)
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # ERROR_ACCESS_DENIED(5)=进程存在但受保护（视为存活）；
            # ERROR_INVALID_PARAMETER(87) 等=进程不存在。
            return ctypes.get_last_error() == 5
        try:
            exit_code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    # POSIX：信号 0 仅探测存在性。
    import errno

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError as exc:  # PermissionError ⊂ OSError
        return exc.errno == errno.EPERM
    return True


# ---------------------------------------------------------------------------
# bind 实测与 claim 记录
# ---------------------------------------------------------------------------


def _bind_ok(port: int) -> bool:
    """bind 实测：0.0.0.0 绑定成功才算可用（最严口径，TIME_WAIT 也让路）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


@dataclass
class ClaimRecord:
    """一条实例认领记录（<name>.json 的机器可读形态）。"""

    name: str
    kind: str  # backend | chrome | driver
    pid: int
    port: int
    data_dir: Path
    owner_lane: str
    started_at: str
    heartbeat_at: str
    chrome_profile_dir: Path | None = None
    scratch_dir: Path | None = None
    events: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        payload = {
            "name": self.name,
            "kind": self.kind,
            "pid": self.pid,
            "port": self.port,
            "data_dir": str(self.data_dir),
            "owner_lane": self.owner_lane,
            "started_at": self.started_at,
            "heartbeat_at": self.heartbeat_at,
            "events": self.events,
        }
        if self.chrome_profile_dir is not None:
            payload["chrome_profile_dir"] = str(self.chrome_profile_dir)
        if self.scratch_dir is not None:
            payload["scratch_dir"] = str(self.scratch_dir)
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> "ClaimRecord":
        return cls(
            name=str(payload["name"]),
            kind=str(payload.get("kind", "backend")),
            pid=int(payload["pid"]),
            port=int(payload["port"]),
            data_dir=Path(payload["data_dir"]),
            owner_lane=str(payload.get("owner_lane", "unknown-lane")),
            started_at=str(payload.get("started_at", "")),
            heartbeat_at=str(payload.get("heartbeat_at", "")),
            chrome_profile_dir=(
                Path(payload["chrome_profile_dir"])
                if payload.get("chrome_profile_dir")
                else None
            ),
            scratch_dir=Path(payload["scratch_dir"]) if payload.get("scratch_dir") else None,
            events=list(payload.get("events", [])),
        )


def _exclusive_create(path: Path, payload: dict) -> None:
    """原子独占创建（O_EXCL）：并发下恰好一个胜者；单次 write 缩小中间态窗口。"""
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _unlink_with_retry(path: Path, attempts: int = 10, delay: float = 0.02) -> None:
    """删除（Windows 形态）：读者握着句柄的瞬间删除会撞 WinError 32——短退避重试。"""
    for attempt in range(attempts):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


# ---------------------------------------------------------------------------
# 管理器
# ---------------------------------------------------------------------------


@dataclass
class InstanceHandle:
    """认领成功的实例句柄（心跳线程随 claim 启动，随 release 停止）。"""

    manager: "TestInstanceManager"
    record: ClaimRecord
    _heartbeat_thread: "HeartbeatThread | None" = None
    _released: bool = False

    @property
    def name(self) -> str:
        return self.record.name

    @property
    def port(self) -> int:
        return self.record.port

    @property
    def data_dir(self) -> Path:
        return self.record.data_dir

    @property
    def chrome_profile_dir(self) -> Path | None:
        return self.record.chrome_profile_dir

    @property
    def scratch_dir(self) -> Path | None:
        return self.record.scratch_dir

    def heartbeat(self) -> None:
        self.manager.heartbeat(self.record.name)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.stop()
        self.manager.release(self.record.name, owner_lane=self.record.owner_lane)

    def __enter__(self) -> "InstanceHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


class HeartbeatThread:
    """心跳守护线程：周期触碰 claim 文件 mtime 续约（设计件③协议第 2 条）。"""

    def __init__(self, manager: "TestInstanceManager", name: str, interval: float) -> None:
        self._manager = manager
        self._name = name
        self._interval = interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "HeartbeatThread":
        self._thread = threading.Thread(
            target=self._run, name=f"tb-heartbeat-{self._name}", daemon=True
        )
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval):
            try:
                self._manager.heartbeat(self._name)
            except InstanceNotClaimed:
                return  # 已被释放（或被接管换主）：线程自然退出。

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)


class TestInstanceManager:
    """测试实例管理器：claim / 心跳 / stale 接管 / release / 端口段分配。"""

    # 名字以 Test 开头会被 pytest 误当测试类收集；显式退出收集。
    __test__ = False

    def __init__(
        self,
        registry_dir: str | os.PathLike[str] | None = None,
        *,
        stale_seconds: float = DEFAULT_STALE_SECONDS,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    ) -> None:
        self.registry_dir = (
            Path(registry_dir) if registry_dir is not None else default_registry_dir()
        ).resolve()
        self.stale_seconds = float(stale_seconds)
        self.heartbeat_interval = float(heartbeat_interval)
        self.registry_dir.mkdir(parents=True, exist_ok=True)

    # -- 基础 ---------------------------------------------------------------

    def _claim_path(self, name: str) -> Path:
        _ensure_instance_name(name)
        return self.registry_dir / f"{name}{CLAIM_SUFFIX}"

    def _read_record(self, name: str, *, tolerant: bool = False) -> ClaimRecord | None:
        """读 claim；tolerant=False 时缺失抛 InstanceNotClaimed、半写重试后仍坏则抛错。"""
        path = self._claim_path(name)
        for attempt in range(4):
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                if tolerant:
                    return None
                raise InstanceNotClaimed(
                    f"注册表里没有实例 {name!r} 的 claim（已释放或从未认领）。"
                    f"可用 claim({name!r}, owner_lane=...) 新认领。"
                ) from None
            try:
                return ClaimRecord.from_dict(json.loads(raw.decode("utf-8")))
            except (json.JSONDecodeError, KeyError, ValueError):
                # claim 原子创建与单次写之间可能被并发读者看到中间态：短暂重试。
                time.sleep(0.05 * (attempt + 1))
        if tolerant:
            return None
        raise InstanceManagerError(
            f"实例 {name!r} 的 claim 文件存在但持续不可读（可能正被其他进程写一半）。"
            "请稍后重试；若确认是残留文件可手工删除该 .json 后重试。"
        )

    # -- 端口分配（跨进程互斥分配锁内：扫描 + bind 实测 + claim 创建） --------

    @contextlib.contextmanager
    def _alloc_lock(self, timeout: float = 15.0) -> Iterator[None]:
        lock_path = self.registry_dir / ALLOC_LOCK_NAME
        deadline = time.monotonic() + timeout
        fd = None
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    age = time.time() - lock_path.stat().st_mtime
                except FileNotFoundError:
                    age = 0.0
                if age > ALLOC_LOCK_STEAL_SECONDS:
                    # 僵尸锁（持锁进程崩溃）：窃取。最坏=两次分配短暂重叠，
                    # 仍有 claim 原子创建 + bind 实测双层兜底。
                    with contextlib.suppress(OSError):
                        lock_path.unlink()
                    continue
                if time.monotonic() > deadline:
                    raise InstanceManagerError(
                        "端口分配锁等待超时（15s）：并发认领太密集或锁被残留。"
                        f"若确认无车道在认领，可删除 {lock_path} 后重试。"
                    )
                time.sleep(0.05)
        try:
            yield
        finally:
            if fd is not None:
                os.close(fd)
            with contextlib.suppress(OSError):
                _unlink_with_retry(lock_path)

    def _used_ports_locked(self) -> set[int]:
        used: set[int] = set()
        for record in self._scan_records():
            if record.port:
                used.add(record.port)
        return used

    def _allocate_port_locked(self) -> int:
        used = self._used_ports_locked()
        for port in range(TEST_PORT_MIN, TEST_PORT_MAX + 1):
            if port in used:
                continue
            if _bind_ok(port):
                return port
        raise InstanceManagerError(
            f"测试端口段 {TEST_PORT_MIN}-{TEST_PORT_MAX} 已耗尽："
            "请先 release 用毕实例，或清理注册表内残留 claim。"
        )

    def allocate_port(self) -> int:
        """从测试段实测分配一个当前可用端口（尽力而为、无预约语义）。

        车道起实例请直接 ``claim(...)``（分配+登记原子完成）；本入口供
        非实例场景（如临时合成壳）取号。
        """
        with self._alloc_lock():
            return self._allocate_port_locked()

    # -- claim / heartbeat / release -----------------------------------------

    def claim(
        self,
        name: str,
        *,
        kind: str = "backend",
        owner_lane: str,
        port: int | None = None,
        pid: int | None = None,
        auto_heartbeat: bool = True,
    ) -> InstanceHandle:
        """认领实例槽（互斥命名空间 + 数据目录 + 测试段端口），原子创建。"""
        _ensure_instance_name(name)
        if not owner_lane or not str(owner_lane).strip():
            raise InstanceManagerError("claim 必须写明 owner_lane（=车道 ID），便于接管与追责。")
        if kind not in ("backend", "chrome", "driver"):
            raise InstanceManagerError(
                f"kind={kind!r} 非法：只允许 backend / chrome / driver（设计件③闭集）。"
            )
        with self._alloc_lock():
            path = self._claim_path(name)
            existing = self._read_record(name, tolerant=True)
            if existing is not None or path.exists():
                raise InstanceAlreadyClaimed(self._already_claimed_message(name))
            if port is not None:
                ensure_test_port(port)
                used = self._used_ports_locked()
                if port in used:
                    holder = next(
                        (r for r in self._scan_records() if r.port == port), None
                    )
                    raise InstanceManagerError(
                        f"端口 {port} 已被实例 {holder.name if holder else '?'}"
                        f"（车道 {holder.owner_lane if holder else '?'}）认领。"
                        "请省略 port 参数让管理器自动分配，避免撞号（8791 撞 8775 案）。"
                    )
                if not _bind_ok(port):
                    raise InstanceManagerError(
                        f"端口 {port} 实测已被非台架服务占用（bind 失败）。"
                        "请省略 port 参数让管理器自动分配。"
                    )
            else:
                port = self._allocate_port_locked()

            instance_root = self.registry_dir / name
            data_dir = instance_root / "data"
            chrome_profile_dir = instance_root / "chrome-profile"
            scratch_dir = instance_root / "scratch"
            # 真装只读边界守卫：结构上必然在注册表内，仍统一过守卫（防未来重构漂移）。
            ensure_test_data_dir(data_dir)
            for directory in (data_dir, chrome_profile_dir, scratch_dir):
                directory.mkdir(parents=True, exist_ok=True)

            record = ClaimRecord(
                name=name,
                kind=kind,
                pid=int(pid) if pid is not None else os.getpid(),
                port=port,
                data_dir=data_dir,
                chrome_profile_dir=chrome_profile_dir,
                scratch_dir=scratch_dir,
                owner_lane=str(owner_lane),
                started_at=_utc_now_iso(),
                heartbeat_at=_utc_now_iso(),
                events=[{"type": "claim", "at": _utc_now_iso(), "by": str(owner_lane)}],
            )
            try:
                _exclusive_create(path, record.to_dict())
            except FileExistsError:
                raise InstanceAlreadyClaimed(self._already_claimed_message(name)) from None
        handle = InstanceHandle(manager=self, record=record)
        if auto_heartbeat:
            handle._heartbeat_thread = HeartbeatThread(
                self, name, self.heartbeat_interval
            ).start()
        return handle

    def _already_claimed_message(self, name: str) -> str:
        record = self._read_record(name, tolerant=True)
        if record is None:
            return (
                f"实例 {name!r} 已被认领（claim 文件刚创建）。"
                "若是残留实例，可先查看 list_instances() 后用 takeover() 接管；"
                "或换个名字零成本。"
            )
        age = self._record_age_seconds(record)
        return (
            f"实例 {name!r} 已被车道 {record.owner_lane!r} 认领"
            f"（PID {record.pid}，端口 {record.port}，心跳 {age:.0f}s 前）。"
            "等人不如换名：换一个名字立即开工；确认对方已死可用 takeover() 接管。"
        )

    def heartbeat(self, name: str) -> None:
        """心跳续约：触碰 claim 文件 mtime（持有者调用）。"""
        path = self._claim_path(name)
        if not path.exists():
            raise InstanceNotClaimed(
                f"实例 {name!r} 没有 claim 可续约（已释放或从未认领）。"
            )
        os.utime(path, None)  # ← 心跳本体：抽掉此行即"无心跳"突变体。

    # -- stale 判定与接管 -----------------------------------------------------

    def _record_age_seconds(self, record: ClaimRecord) -> float:
        path = self._claim_path(record.name)
        try:
            return max(0.0, time.time() - path.stat().st_mtime)
        except FileNotFoundError:
            return 0.0

    def is_stale(self, name: str) -> bool:
        """stale 判定（设计件③协议第 3 条）：心跳落后超阈值 **或** pid 不存在。"""
        record = self._read_record(name)
        if self._record_age_seconds(record) > self.stale_seconds:
            return True
        return not pid_alive(record.pid)

    def takeover(
        self,
        name: str,
        *,
        new_owner_lane: str,
        kind: str | None = None,
        pid: int | None = None,
        auto_heartbeat: bool = True,
    ) -> InstanceHandle:
        """接管 stale 实例：先核验，后原子换主，takeover 事件留痕。

        接管前持有进程存活核验：pid 仍存活一律拒绝（活持有者的数据目录/端口
        被抢=互踩灾难本身；等它死透或换名）。
        """
        record = self._read_record(name)
        if not self.is_stale(name):
            raise TakeoverRefused(
                f"实例 {name!r} 未过期：心跳 {self._record_age_seconds(record):.0f}s 前、"
                f"持有进程存活。接管只面向 stale 实例；活着的老实例谁也不能抢。"
            )
        if pid_alive(record.pid):
            raise TakeoverRefused(
                f"实例 {name!r} 的持有进程（PID {record.pid}）仍存活，拒绝接管"
                "（接管前进程存活核验）。若它只是心跳线程卡死，请等进程退出或换名开工。"
            )
        path = self._claim_path(name)
        # 接管互斥标记（固定名 O_EXCL）：并发接管者只有一个能进入换主段；
        # 其余等标记消失后进入双重核验并如实报"已被他人接管"。
        # （终结 unlink TOCTOU 双胜者案：败者永不触碰 claim 文件本身。）
        marker = self.registry_dir / f".takeover.{name}.lock"
        marker_deadline = time.monotonic() + 10.0
        while True:
            try:
                fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                if time.monotonic() > marker_deadline:
                    raise InstanceManagerError(
                        f"实例 {name!r} 的接管锁等待超时（10s）：可能他车道正在接管，稍后重试。"
                    )
                time.sleep(0.02)
        try:
            # 双重核验（拿到标记后重读）：确认仍是当初判定的那条 claim，防 TOCTOU。
            current = self._read_record(name, tolerant=True)
            if (
                current is None
                or current.owner_lane != record.owner_lane
                or current.pid != record.pid
                or current.started_at != record.started_at
            ):
                raise InstanceAlreadyClaimed(
                    f"接管竞态失败：{name!r} 在核验与换主之间被他人接管/改动/释放。"
                    "请重读 list_instances() 后再决定。"
                )
            new_record = ClaimRecord(
                name=record.name,
                kind=kind or record.kind,
                pid=int(pid) if pid is not None else os.getpid(),
                port=record.port,
                data_dir=record.data_dir,
                chrome_profile_dir=record.chrome_profile_dir,
                scratch_dir=record.scratch_dir,
                owner_lane=str(new_owner_lane),
                started_at=record.started_at,
                heartbeat_at=_utc_now_iso(),
                events=record.events
                + [
                    {
                        "type": "takeover",
                        "at": _utc_now_iso(),
                        "by": str(new_owner_lane),
                        "from_owner": record.owner_lane,
                        "from_pid": record.pid,
                    }
                ],
            )
            _unlink_with_retry(path)
            try:
                _exclusive_create(path, new_record.to_dict())
            except FileExistsError:
                raise InstanceAlreadyClaimed(
                    f"接管竞态失败：{name!r} 刚被他人接管/重新认领。请重读 list_instances()。"
                ) from None
        finally:
            with contextlib.suppress(OSError):
                _unlink_with_retry(marker)
        handle = InstanceHandle(manager=self, record=new_record)
        if auto_heartbeat:
            handle._heartbeat_thread = HeartbeatThread(
                self, name, self.heartbeat_interval
            ).start()
        return handle

    def release(self, name: str, *, owner_lane: str | None = None) -> None:
        """用毕释放（删 claim 文件；数据目录保留供事后取证，可整体删除重建）。"""
        record = self._read_record(name)
        if owner_lane is not None and record.owner_lane != owner_lane:
            raise InstanceManagerError(
                f"实例 {name!r} 属于车道 {record.owner_lane!r}，"
                f"车道 {owner_lane!r} 无权释放（接管后老持有者的 release 不生效）。"
            )
        _unlink_with_retry(self._claim_path(name))

    # -- 查询 -----------------------------------------------------------------

    def _scan_records(self) -> list[ClaimRecord]:
        records: list[ClaimRecord] = []
        for path in sorted(self.registry_dir.glob(f"*{CLAIM_SUFFIX}")):
            try:
                records.append(ClaimRecord.from_dict(json.loads(path.read_bytes().decode("utf-8"))))
            except (json.JSONDecodeError, KeyError, ValueError, OSError):
                continue  # 半写中间态：跳过（claim 原子创建兜底在下一次扫描生效）。
        return records

    def list_instances(self) -> list[dict]:
        """全部在册实例（机读；含 stale 标记与心跳年龄）——终结「谁在用谁」靠翻结果文件。"""
        out: list[dict] = []
        for record in self._scan_records():
            path = self._claim_path(record.name)
            exists = path.exists()
            age = self._record_age_seconds(record) if exists else None
            stale = self._is_stale_record(record, age) if exists else None
            out.append(
                {
                    **record.to_dict(),
                    "heartbeat_age_seconds": round(age, 1) if age is not None else None,
                    "pid_alive": pid_alive(record.pid),
                    "stale": stale,
                }
            )
        return out

    def _is_stale_record(self, record: ClaimRecord, age: float | None) -> bool:
        if age is None:
            return True
        if age > self.stale_seconds:
            return True
        return not pid_alive(record.pid)

    def wait_released(self, name: str, timeout: float = 30.0, poll: float = 0.2) -> bool:
        """轮询等待目标实例释放（协议第 6 条可选项；默认建议换名而非等待）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._claim_path(name).exists():
                return True
            time.sleep(poll)
        return not self._claim_path(name).exists()


def detect_port_conflicts(manager: TestInstanceManager) -> list[dict]:
    """在册实例间的端口冲突检测（撞号哨兵；正常应恒空）。"""
    seen: dict[int, str] = {}
    conflicts: list[dict] = []
    for entry in manager.list_instances():
        port = entry["port"]
        if port in seen:
            conflicts.append({"port": port, "instances": [seen[port], entry["name"]]})
        else:
            seen[port] = entry["name"]
    return conflicts


# ---------------------------------------------------------------------------
# CLI：python tests/testbench/instances.py <list|claim|release|heartbeat|stale|allocate-port>
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CourseLens 测试实例管理器（测试台架件③M1）")
    parser.add_argument("--registry", default=None, help="注册表目录（默认工作区 .testbench/instances）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="列出全部在册实例（--json 机读）")
    p_list.add_argument("--json", action="store_true")
    p_claim = sub.add_parser("claim", help="认领实例")
    p_claim.add_argument("name")
    p_claim.add_argument("--lane", required=True)
    p_claim.add_argument("--kind", default="backend", choices=["backend", "chrome", "driver"])
    p_claim.add_argument("--port", type=int, default=None)
    p_release = sub.add_parser("release", help="释放实例")
    p_release.add_argument("name")
    p_release.add_argument("--lane", default=None)
    sub.add_parser("heartbeat").add_argument("name")
    p_stale = sub.add_parser("stale", help="查看 stale 判定")
    p_stale.add_argument("name")
    sub.add_parser("allocate-port", help="从测试段实测分配端口")
    args = parser.parse_args(argv)

    manager = TestInstanceManager(args.registry)
    if args.cmd == "list":
        entries = manager.list_instances()
        conflicts = detect_port_conflicts(manager)
        if args.json:
            print(json.dumps({"instances": entries, "port_conflicts": conflicts},
                             ensure_ascii=False, indent=2))
        else:
            for entry in entries:
                print(
                    f"{entry['name']:<32} lane={entry['owner_lane']:<20} kind={entry['kind']:<8}"
                    f" pid={entry['pid']:<8} port={entry['port']:<6}"
                    f" heartbeat={entry['heartbeat_age_seconds']}s"
                    f" pid_alive={entry['pid_alive']} stale={entry['stale']}"
                )
            if conflicts:
                print(f"!! 端口冲突（撞号哨兵）：{conflicts}")
        return 0
    if args.cmd == "claim":
        handle = manager.claim(args.name, kind=args.kind, owner_lane=args.lane, port=args.port)
        print(f"claimed: {handle.name} port={handle.port} data_dir={handle.data_dir}")
        return 0
    if args.cmd == "release":
        manager.release(args.name, owner_lane=args.lane)
        print(f"released: {args.name}")
        return 0
    if args.cmd == "heartbeat":
        manager.heartbeat(args.name)
        print(f"heartbeat: {args.name}")
        return 0
    if args.cmd == "stale":
        print(f"stale={manager.is_stale(args.name)}")
        return 0
    if args.cmd == "allocate-port":
        print(manager.allocate_port())
        return 0
    parser.error(f"未知命令 {args.cmd}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
