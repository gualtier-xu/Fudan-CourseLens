"""serve 测试实例自动注册（TESTBENCH-DESIGN-1 件③M2 src 侧接线）。

serve() 在测试模式下经本模块向工作区级实例注册表登记自身进程：claim 文件
与 tests/testbench/instances.py 的 ClaimRecord 同格式互认——车道「先 claim
再起进程」与 serve 全自动注册两条路汇入同一注册表，``instances --list`` 的
机读答案因此覆盖全部测试后端实例（含 serve 进程本体）。

语义（与设计件③协议六条对齐）：
- **采纳优先**：实例名已有 claim（车道先认领）→ 保留 owner_lane/事件史，
  只把 pid 改写为 serve 进程本体（stale 判定以真实持有进程为准）、端口改写
  为实际绑定端口，并追加 ``serve_adopt`` 事件留痕。
- **自动注册**：无 claim → 原子创建（O_EXCL，与实例管理器同形态），
  ``serve_register`` 事件留痕。
- **心跳**：守护线程周期触碰 claim mtime（默认 30s，与实例管理器同周期）。
- **释放**：finally 块删除自己 pid 名下的 claim（被接管/他人持有者不删——
  release 语义与实例管理器一致，防误删）。

发布版惰性：本模块只被 serve() 的测试模式分支 import；未设实例名 env 时
``registrar_from_env`` 返回 None，整条路径零成本。实例名/注册表目录的 env
读数一律经 src/runtime/test_mode.py（env 字面量唯一权威源，惰性钉
tests/test_test_mode_contract.py 强制，本文件连注释都不出现 env 字面量）。

安全边界：只读写注册表目录内的 claim JSON（工作区级 .testbench/instances/
或显式 override），零网络、零学生数据接触、不触碰产品默认数据目录。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from src.runtime import test_mode as tm

#: 心跳续约周期（秒）——与 tests/testbench/instances.py DEFAULT_HEARTBEAT_INTERVAL 同值。
HEARTBEAT_INTERVAL_SECONDS = 30.0

_CLAIM_SUFFIX = ".json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_registry_dir() -> Path:
    """默认注册表：工作区级 .testbench/instances/（与实例管理器同一目录）。

    产品仓根的上一级 = 多仓工作区根（tests 侧 instances.py workspace_root()
    同一物理事实的 src 侧换算，禁 cwd 依赖）。
    """
    from path_utils import PROJECT_ROOT

    return Path(PROJECT_ROOT).resolve().parent / ".testbench" / "instances"


class ServeInstanceRegistrar:
    """serve 进程的实例注册器：采纳/创建 claim → 心跳 → 释放。"""

    def __init__(
        self,
        *,
        name: str,
        registry_dir: str | os.PathLike[str],
        owner_lane: str,
        pid: int | None = None,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self.name = tm.validate_instance_name(name)
        self.registry_dir = Path(registry_dir)
        self.owner_lane = str(owner_lane).strip() or "serve-auto"
        self.pid = int(pid if pid is not None else os.getpid())
        self.heartbeat_interval = float(heartbeat_interval)
        self.port = 0
        self.data_dir = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._registered = False

    # -- 路径与读写 ----------------------------------------------------------

    @property
    def claim_path(self) -> Path:
        return self.registry_dir / f"{self.name}{_CLAIM_SUFFIX}"

    @staticmethod
    def _read_record(path: Path) -> dict | None:
        try:
            raw = path.read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            return None
        try:
            record = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None  # 并发写一半的中间态：按不存在处理（创建路径由 O_EXCL 兜底）
        return record if isinstance(record, dict) else None

    @staticmethod
    def _exclusive_create(path: Path, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    @staticmethod
    def _replace_atomic(path: Path, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        temp = path.with_name(f"{path.name}.serve-{os.getpid()}.tmp")
        fd = os.open(str(temp), os.O_CREAT | os.O_TRUNC | os.O_WRONLY)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.replace(str(temp), str(path))

    def _fresh_payload(self, *, port: int, data_dir: str) -> dict:
        now = _utc_now_iso()
        return {
            "name": self.name,
            "kind": "backend",
            "pid": self.pid,
            "port": int(port),
            "data_dir": str(data_dir),
            "owner_lane": self.owner_lane,
            "started_at": now,
            "heartbeat_at": now,
            "events": [{"type": "serve_register", "at": now, "by": self.owner_lane}],
        }

    # -- 注册 / 心跳 / 释放 ---------------------------------------------------

    def register(self, *, port: int, data_dir: str | os.PathLike[str]) -> None:
        """采纳既有 claim 或原子创建新 claim，并启动心跳守护线程。

        并发竞态处理：创建撞 FileExistsError = 他人抢先创建 → 转入采纳路径；
        采纳读到的中间态 JSON = 短重试。绑定端口已由 serve 的排他 bind 保证
        唯一，注册表只如实记录。
        """
        self.port = int(port)
        self.data_dir = str(data_dir)
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        for attempt in range(4):
            record = self._read_record(self.claim_path)
            if record is None:
                try:
                    self._exclusive_create(
                        self.claim_path, self._fresh_payload(port=self.port, data_dir=self.data_dir)
                    )
                    self._registered = True
                    break
                except FileExistsError:
                    continue  # 并发创建：下轮转采纳
            else:
                record["pid"] = self.pid
                record["port"] = self.port
                record["data_dir"] = self.data_dir
                events = record.get("events")
                record["events"] = list(events) if isinstance(events, list) else []
                record["events"].append(
                    {"type": "serve_adopt", "at": _utc_now_iso(), "by": f"pid:{self.pid}"}
                )
                self._replace_atomic(self.claim_path, record)
                self._registered = True
                break
        else:
            raise OSError(
                f"测试实例 {self.name!r} 的 claim 并发争用未收敛（重试 4 次仍无结果）。"
                "请用实例管理器 list 查看现状后换名重试。"
            )
        if self.heartbeat_interval > 0:
            self._thread = threading.Thread(
                target=self._heartbeat_loop, name=f"tb-serve-heartbeat-{self.name}", daemon=True
            )
            self._thread.start()

    def heartbeat(self) -> None:
        """心跳续约本体：触碰 claim mtime（守护线程与钉测共用）。"""
        try:
            os.utime(self.claim_path, None)
        except FileNotFoundError:
            pass  # claim 已被释放/接管：心跳静默退场，线程在下一轮停止

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval):
            if not self._registered:
                return
            self.heartbeat()

    def close(self) -> None:
        """停心跳并释放自己 pid 名下的 claim（幂等；他人持有的 claim 不删）。"""
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
            self._thread = None
        if not self._registered:
            return
        self._registered = False
        record = self._read_record(self.claim_path)
        if record is None:
            return
        if int(record.get("pid", 0)) != self.pid:
            print(
                f"[FudanCourseLens] 测试实例 {self.name!r} 的 claim 已易主"
                f"（现持有 PID {record.get('pid')}），本次退出不释放他人 claim。",
                file=os.sys.stderr,
                flush=True,
            )
            return
        for _attempt in range(10):
            try:
                self.claim_path.unlink()
                return
            except FileNotFoundError:
                return
            except OSError:
                time.sleep(0.02 * (_attempt + 1))  # Windows 读者握句柄瞬间：短退避重试


def registrar_from_env(environ=None) -> "ServeInstanceRegistrar | None":
    """从 env 构造注册器；未设实例名 env = None（发布版零成本）。

    实例名非法在启动契约（test_mode.validate_instance_name）已 fail-closed；
    本入口对直接调用者保持同口径防御。
    """
    name = tm.test_instance_name(environ)
    if not name:
        return None
    registry = tm.test_registry_override(environ)
    registry_dir = Path(registry) if registry else default_registry_dir()
    return ServeInstanceRegistrar(
        name=name,
        registry_dir=registry_dir,
        owner_lane=tm.test_instance_owner(environ),
    )


__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "ServeInstanceRegistrar",
    "default_registry_dir",
    "registrar_from_env",
]
