"""Chrome 实例生命周期封装（测试台架件③M2 tests 侧；TB-W2）。

``chrome up`` / ``chrome down`` 的安全性是结构性的，三条铁律：

1. **独立 user-data-dir 铁律**：每个实例用认领的 ``chrome-profile/`` 目录启动，
   从不触碰用户默认浏览器配置（R3 教训：禁占默认目录单例）。
2. **指纹精确清理**：``chrome down`` 只杀「命令行里含本实例 profile 目录路径」的
   chrome.exe（PowerShell ``Get-CimInstance Win32_Process`` 逐进程核对 cmdline），
   逐 PID ``taskkill /PID <pid> /T /F``——**禁止** ``taskkill /IM chrome.exe``
   （R4 教训：用户此刻可能在用浏览器）。
3. **CDP 000 复核**：down 之后探测 ``http://127.0.0.1:<port>/json/version``
   必须回到连接拒绝（记 ``000``，curl 语义），加 bind 实测双确认端口已释放。

启动=PowerShell ``Start-Process -PassThru`` 分离启动（R3/R4 已验证形态），
就绪判定=CDP ``/json/version`` 轮询到 200；超时则按同一指纹清理现场并释放
claim（失败也不留残留进程/僵尸 claim）。

实例本体=件③M1 的 :class:`~tests.testbench.instances.TestInstanceManager` 认领
（kind=``chrome``，端口=测试段自动分配的 CDP 端口，心跳/接管/释放语义照旧）。
本模块是纯测试台架设施，零产品改动；src serve 的 test mode 挂接明确不在本
车道范围（移批 4）。

用法::

    from tests.testbench.instances import TestInstanceManager
    from tests.testbench import chrome_lifecycle as cl

    mgr = TestInstanceManager()
    up = cl.chrome_up(mgr, "mylane-chrome", owner_lane="MY-LANE")
    ...  # 用 up.cdp_port 连调试端口；up.chrome_pid 为启动器 PID
    down = cl.chrome_down(mgr, "mylane-chrome", owner_lane="MY-LANE")
    down.cdp_status == "000"  # 端口已回收

CLI（车道手工调试）：``python tests/testbench/chrome_lifecycle.py up|down <name> --lane <车道ID>``
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from tests.testbench.instances import (
    InstanceHandle,
    InstanceManagerError,
    TestInstanceManager,
)

__all__ = [
    "ChromeLifecycleError",
    "ChromeUnavailable",
    "ChromeStartupError",
    "ChromeDownResult",
    "ChromeUpResult",
    "cdp_probe",
    "chrome_down",
    "chrome_up",
    "find_chrome_executable",
    "list_fingerprint_processes",
    "match_fingerprint",
]

_CDP_READY_TIMEOUT_S = 30.0
_CDP_POLL_INTERVAL_S = 0.25
_KILL_SETTLE_ROUNDS = 4
_KILL_SETTLE_SLEEP_S = 1.0
_PS_TIMEOUT_S = 30.0


class ChromeLifecycleError(InstanceManagerError):
    """Chrome 生命周期封装基础错误（人话消息）。"""


class ChromeUnavailable(ChromeLifecycleError):
    """本机找不到 chrome.exe（车道应诚实 SKIP，禁改用用户 Edge/默认浏览器凑数）。"""


class ChromeStartupError(ChromeLifecycleError):
    """chrome up 失败（现场已按指纹清理、claim 已释放；消息不含路径之外的敏感信息）。"""


# ---------------------------------------------------------------------------
# chrome.exe 定位
# ---------------------------------------------------------------------------


def find_chrome_executable() -> Path:
    """定位 chrome.exe（env 覆盖 → 常见安装位）；找不到=ChromeUnavailable 诚实失败。"""
    env_hint = os.environ.get("COURSELENS_TESTBENCH_CHROME")
    if env_hint:
        candidate = Path(env_hint)
        if candidate.is_file():
            return candidate
        raise ChromeUnavailable(
            f"COURSELENS_TESTBENCH_CHROME={env_hint} 不是存在的文件——请指向 chrome.exe 本体。"
        )
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        Path(program_files) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(program_files_x86) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    if local_appdata:
        candidates.append(
            Path(local_appdata) / "Google" / "Chrome" / "Application" / "chrome.exe"
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ChromeUnavailable(
        "本机常见安装位找不到 chrome.exe（找过 Program Files / Program Files (x86) / "
        "LOCALAPPDATA）；设 COURSELENS_TESTBENCH_CHROME 指向 chrome.exe 后重试，"
        "或本车道按 ChromeUnavailable 诚实 SKIP——禁用用户的 Edge/默认浏览器凑数。"
    )


# ---------------------------------------------------------------------------
# CDP 探测（urllib 标准库，零新依赖；"000"=连接失败，curl 语义）
# ---------------------------------------------------------------------------


def cdp_probe(port: int, *, timeout_s: float = 2.0) -> str:
    """探测 CDP 端口：200=就绪；000=连接失败/超时（down 后必须回到这个值）。"""
    url = f"http://127.0.0.1:{int(port)}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return str(resp.status)
    except urllib.error.HTTPError as exc:
        return str(exc.code)
    except (urllib.error.URLError, OSError, ValueError):
        return "000"


def _port_bindable(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# cmdline 指纹枚举与匹配（纯函数与进程枚举分离，前者可离线钉死）
# ---------------------------------------------------------------------------


def match_fingerprint(command_line: str | None, profile_dir: str | Path) -> bool:
    """命令行是否含本实例 profile 目录指纹（大小写不敏感；两种路径分隔符都认）。

    这是「只杀自己人」的唯一判据：用户 Chrome 的 user-data-dir 是完全不同的
    目录树，结构上不可能含本实例 ``.testbench/instances/<名>/chrome-profile``。
    """
    if not command_line:
        return False
    profile = str(profile_dir)
    variants = {profile.lower()}
    variants.add(profile.replace("\\", "/").lower())
    variants.add(profile.replace("/", "\\").lower())
    cmdline = command_line.lower()
    return any(v and v in cmdline for v in variants)


def _ps_escape(value: str) -> str:
    """PowerShell 单引号字面量转义（' → ''）。"""
    return value.replace("'", "''")


def list_fingerprint_processes(
    profile_dir: str | Path,
    *,
    process_name: str = "chrome.exe",
) -> list[tuple[int, str]]:
    """枚举命令行含 profile 目录指纹的进程（PowerShell CIM 逐进程核对 cmdline）。

    只读操作：不杀任何东西，供 down 与测试取证共用。
    """
    script = (
        "$d = @(Get-CimInstance Win32_Process -Filter \"Name='"
        + _ps_escape(process_name)
        + "'\" | Select-Object ProcessId,CommandLine); "
        "if ($d.Count -gt 0) { $d | ConvertTo-Json -Compress } else { '[]' }"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_PS_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise ChromeLifecycleError(
            f"进程枚举失败（powershell exit={proc.returncode}）："
            f"{(proc.stderr or '').strip()[:200]!r}——宁可失败不可盲杀。"
        )
    payload = json.loads((proc.stdout or "[]").strip() or "[]")
    rows = payload if isinstance(payload, list) else [payload]
    matched: list[tuple[int, str]] = []
    for row in rows:
        cmdline = row.get("CommandLine")
        if match_fingerprint(cmdline, profile_dir):
            matched.append((int(row["ProcessId"]), str(cmdline)))
    return matched


# ---------------------------------------------------------------------------
# 启动（PowerShell Start-Process 分离启动）
# ---------------------------------------------------------------------------


def build_chrome_argv(
    *,
    profile_dir: str | Path,
    cdp_port: int,
    proxy_server: str | None = None,
    headless: bool = True,
    extra_args: tuple[str, ...] = (),
) -> list[str]:
    """chrome argv（纯函数，钉测面）：独立 profile + CDP 端口 + 显式可选项。"""
    argv = [
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={cdp_port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
    ]
    if headless:
        argv.append("--headless=new")
    if proxy_server:
        argv.append(f"--proxy-server={proxy_server}")
    argv.extend(extra_args)
    return argv


def _start_detached(executable: Path, argv: list[str]) -> int:
    """PowerShell Start-Process 分离启动（R3/R4 已验证形态），返回启动器 PID。"""
    quoted = ", ".join(f"'{_ps_escape(a)}'" for a in argv)
    script = (
        "$p = Start-Process -FilePath '"
        + _ps_escape(str(executable))
        + f"' -ArgumentList @({quoted}) -PassThru; $p.Id"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_PS_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise ChromeStartupError(
            f"Start-Process 启动失败（exit={proc.returncode}）："
            f"{(proc.stderr or '').strip()[:200]!r}"
        )
    try:
        return int((proc.stdout or "").strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise ChromeStartupError(
            f"Start-Process 未返回 PID（stdout={ (proc.stdout or '').strip()[:80]!r}）"
        ) from exc


@dataclass(frozen=True)
class ChromeUpResult:
    """chrome up 结果：句柄 + CDP 就绪证据。"""

    handle: InstanceHandle
    chrome_pid: int
    cdp_port: int
    profile_dir: Path
    ready_seconds: float


def chrome_up(
    manager: TestInstanceManager,
    name: str,
    *,
    owner_lane: str,
    proxy_server: str | None = None,
    headless: bool = True,
    extra_args: tuple[str, ...] = (),
    ready_timeout_s: float = _CDP_READY_TIMEOUT_S,
) -> ChromeUpResult:
    """认领实例并以独立 profile 分离启动 Chrome，等 CDP 就绪。

    失败语义：CDP 超时=按指纹清理已起进程+释放 claim+抛 ChromeStartupError
    （失败也不留残留进程/僵尸 claim）。
    """
    executable = find_chrome_executable()
    handle = manager.claim(name, kind="chrome", owner_lane=owner_lane)
    profile_dir = handle.chrome_profile_dir
    if profile_dir is None:  # 结构上必非 None（claim 保证）；守卫只防未来漂移。
        manager.release(name, owner_lane=owner_lane)
        raise ChromeLifecycleError(f"实例 {name!r} 的 claim 缺 chrome-profile 目录——拒绝启动。")
    argv = build_chrome_argv(
        profile_dir=profile_dir,
        cdp_port=handle.port,
        proxy_server=proxy_server,
        headless=headless,
        extra_args=extra_args,
    )
    started = time.monotonic()
    try:
        chrome_pid = _start_detached(executable, argv)
        # 启动器 pid 顺手落 scratch（取证用；清理以 cmdline 指纹为准，不依赖 pid）。
        try:
            if handle.scratch_dir is not None:
                (handle.scratch_dir / "chrome-launcher-pid.txt").write_text(
                    str(chrome_pid), encoding="utf-8"
                )
        except OSError:
            pass  # 取证文件写失败不影响主流程。
        deadline = started + ready_timeout_s
        while time.monotonic() < deadline:
            if cdp_probe(handle.port) == "200":
                return ChromeUpResult(
                    handle=handle,
                    chrome_pid=chrome_pid,
                    cdp_port=handle.port,
                    profile_dir=profile_dir,
                    ready_seconds=round(time.monotonic() - started, 2),
                )
            if not _pid_alive_quick(chrome_pid):
                break  # 启动器已死且 CDP 未起：直接走清理。
            time.sleep(_CDP_POLL_INTERVAL_S)
        raise ChromeStartupError(
            f"chrome up 超时：CDP 端口 {handle.port} 在 {ready_timeout_s:.0f}s 内未就绪"
            "（现场已按指纹清理、claim 已释放；请检查 chrome.exe 可用性后重试）。"
        )
    except Exception:
        _cleanup_fingerprint_now(profile_dir)
        with _quiet_release(manager, name, owner_lane):
            pass
        raise


def _pid_alive_quick(pid: int) -> bool:
    from tests.testbench.instances import pid_alive

    return pid_alive(pid)


@dataclass
class _QuietRelease:
    """释放失败不掩盖主异常（收尾双保险的「不二次伤害」语义）。"""

    manager: TestInstanceManager
    name: str
    owner_lane: str

    def __enter__(self) -> "_QuietRelease":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self.manager.release(self.name, owner_lane=self.owner_lane)
        except InstanceManagerError:
            pass
        return False


def _quiet_release(manager: TestInstanceManager, name: str, owner_lane: str) -> _QuietRelease:
    return _QuietRelease(manager, name, owner_lane)


# ---------------------------------------------------------------------------
# 关停（指纹精确清理 + CDP 000 复核）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChromeDownResult:
    """chrome down 结果：杀掉的 PID 清单 + 双重释放证据。"""

    killed_pids: tuple[int, ...]
    cdp_status: str
    port_free: bool
    profile_dir: Path


def _taskkill_tree(pid: int) -> subprocess.CompletedProcess:
    """逐 PID 树杀（唯一允许的杀形态；/IM 全量杀被结构性排除——见钉测）。"""
    return subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15.0,
    )


def _cleanup_fingerprint_now(profile_dir: str | Path) -> tuple[int, ...]:
    """按指纹清现场（up 失败收尾与 down 共用本体）。返回实际下发过 taskkill 的 PID。"""
    killed: list[int] = []
    for _ in range(_KILL_SETTLE_ROUNDS):
        matched = list_fingerprint_processes(profile_dir)
        if not matched:
            break
        for pid, _cmdline in matched:
            proc = _taskkill_tree(pid)
            # 128=进程已不存在（竞态自然退出）：算清干净，不算失败。
            if proc.returncode == 0 or proc.returncode == 128:
                killed.append(pid)
        time.sleep(_KILL_SETTLE_SLEEP_S)
    return tuple(killed)


def chrome_down(
    manager: TestInstanceManager,
    name: str,
    *,
    owner_lane: str | None = None,
) -> ChromeDownResult:
    """按 cmdline 指纹精确关停实例 Chrome，CDP 000 + bind 双复核后释放 claim。

    只杀「命令行含本实例 profile 目录」的 chrome.exe；用户 Chrome 与他道实例
    结构性不在清理面内（指纹不同目录树）。
    """
    record = manager._read_record(name)  # noqa: SLF001 — 同包设施内部协作面。
    if record.kind != "chrome":
        raise ChromeLifecycleError(
            f"实例 {name!r} 的 kind={record.kind!r} 不是 chrome——"
            "chrome_down 只操作 kind=chrome 的实例（防误杀他类实例进程）。"
        )
    if record.chrome_profile_dir is None:
        raise ChromeLifecycleError(f"实例 {name!r} 的 claim 缺 chrome-profile 目录——拒绝盲杀。")
    profile_dir = record.chrome_profile_dir
    killed = _cleanup_fingerprint_now(profile_dir)

    residual = list_fingerprint_processes(profile_dir)
    if residual:
        raise ChromeLifecycleError(
            f"chrome down 后仍有 {len(residual)} 个指纹匹配进程存活（PID "
            f"{[p for p, _ in residual]}）——不释放 claim（实例仍占着，可重试 down）。"
        )
    cdp_status = cdp_probe(record.port)
    port_free = _port_bindable(record.port)
    if cdp_status != "000" or not port_free:
        raise ChromeLifecycleError(
            f"端口 {record.port} 释放复核未通过（CDP={cdp_status}，bind_free={port_free}）"
            "——不释放 claim；请稍后重试或人工核对该端口占用。"
        )
    manager.release(name, owner_lane=owner_lane)
    return ChromeDownResult(
        killed_pids=killed,
        cdp_status=cdp_status,
        port_free=port_free,
        profile_dir=profile_dir,
    )


# ---------------------------------------------------------------------------
# CLI（车道手工调试）
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chrome 实例生命周期（测试台架件③M2）")
    parser.add_argument("--registry", default=None, help="注册表目录（默认工作区 .testbench/instances）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_up = sub.add_parser("up", help="认领并以独立 profile 启动 Chrome，等 CDP 就绪")
    p_up.add_argument("name")
    p_up.add_argument("--lane", required=True)
    p_up.add_argument("--proxy", default=None, help="显式 --proxy-server（缺省不设）")
    p_up.add_argument("--headed", action="store_true", help="有头启动（缺省 headless=new）")
    p_down = sub.add_parser("down", help="按 cmdline 指纹精确清理并释放")
    p_down.add_argument("name")
    p_down.add_argument("--lane", default=None)
    args = parser.parse_args(argv)

    manager = TestInstanceManager(args.registry)
    if args.cmd == "up":
        result = chrome_up(
            manager,
            args.name,
            owner_lane=args.lane,
            proxy_server=args.proxy,
            headless=not args.headed,
        )
        print(
            f"up ok: {result.handle.name} cdp_port={result.cdp_port} "
            f"chrome_pid={result.chrome_pid} ready={result.ready_seconds}s"
        )
        return 0
    if args.cmd == "down":
        result = chrome_down(manager, args.name, owner_lane=args.lane)
        print(
            f"down ok: killed={list(result.killed_pids)} cdp={result.cdp_status} "
            f"port_free={result.port_free}"
        )
        return 0
    parser.error(f"未知命令 {args.cmd}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
