"""FAULT-MATRIX-1 维度四：多实例并发注入（真实进程级，非单元桩）。

在沙箱数据目录上真实启动多个 ``python -m src serve`` 进程，验证：
1. 同数据目录二次启动 → 命名单实例互斥（LIFECYCLE_E_INSTANCE_ACTIVE），
   快速诚实退出，先到实例零惊扰；
2. 端口冲突（不同数据目录抢同一端口）→ LIFECYCLE_E_PORT_BUSY + 人话提示，
   先到实例零惊扰；
3. 不同数据目录双开（绕过互斥的合法形态）→ 按设计隔离并行，证据文件/
   锁文件互不串扰；
4. 持锁进程被杀 → OS 锁随进程释放，下一位启动者干净接管（恢复验证）。

只触碰本 harness 自己启动的沙箱进程与目录；绝不杀既有 CourseLens。
用法：python tools/faultmatrix_multi_instance.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 托管路径校验要求数据目录在项目内（path_utils.ensure_inside_project）；
# 沙箱目录沿用既有 runtime/cache scratch 模式，收尾逐目录清理。
SCRATCH = PROJECT_ROOT / "runtime" / "cache" / "faultmatrix-multi-instance"
PYTHON = sys.executable


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _launch(data_dir: Path, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [
            PYTHON, "-m", "src", "serve",
            "--no-open", "--keep-server", "--no-window",
            "--data-dir", str(data_dir), "--port", str(port),
        ],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _wait_http_ready(port: int, *, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/", timeout=2
            ) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


def _wait_instance_published(data_dir: Path, *, timeout: float = 90.0) -> bool:
    """等真实 handler 就绪的证据（server-instance.json 在 ready 迁移时落盘）。"""
    deadline = time.monotonic() + timeout
    evidence = data_dir / "server-instance.json"
    while time.monotonic() < deadline:
        if evidence.is_file():
            return True
        time.sleep(0.3)
    return False


def _stop(proc: subprocess.Popen, *, natural_grace: float = 2.0) -> tuple[str, str]:
    """按身份收掉自己启动的沙箱进程。

    先给 ``natural_grace`` 秒自然退出窗（快速退出的实例必须自己走完打印
    与退出码，绝不硬杀取证）；仅长驻进程超时后才 terminate→强杀兜底。
    """
    try:
        proc.wait(timeout=natural_grace)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    try:
        out, err = proc.communicate(timeout=5)
    except (ValueError, OSError):
        out, err = "", ""
    return out or "", err or ""


class _Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def record(self, case: str, verdict: str, detail: str) -> None:
        self.rows.append((case, verdict, detail))
        print(f"[{verdict}] {case}: {detail}", flush=True)

    def finish(self) -> int:
        failed = [row for row in self.rows if row[1] != "PASS"]
        print("\n=== FAULT-MATRIX-1 多实例并发注入汇总 ===")
        for case, verdict, detail in self.rows:
            print(f"[{verdict}] {case}: {detail}")
        print(f"合计 {len(self.rows)} 例，FAIL {len(failed)} 例")
        return 1 if failed else 0


def main() -> int:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    report = _Report()
    dir_a = Path(tempfile.mkdtemp(dir=SCRATCH, prefix="fm-a-"))
    dir_b = Path(tempfile.mkdtemp(dir=SCRATCH, prefix="fm-b-"))
    port_a, port_b, port_c = _free_port(), _free_port(), _free_port()
    procs: list[subprocess.Popen] = []
    try:        # --- 用例 1：同数据目录二次启动=单实例互斥 -------------------------
        proc_a = _launch(dir_a, port_a)
        procs.append(proc_a)
        if not _wait_http_ready(port_a) or not _wait_instance_published(dir_a):
            report.record("A 启动", "FAIL", "首实例未在超时内就绪（HTTP/证据文件）")
            return report.finish()
        evidence_a = json.loads(
            (dir_a / "server-instance.json").read_text(encoding="utf-8")
        )
        report.record(
            "A 启动", "PASS",
            f"port={port_a} pid={evidence_a['pid']}",
        )

        proc_b = _launch(dir_a, port_b)
        procs.append(proc_b)
        started = time.monotonic()
        try:
            _, err_b = _stop(proc_b, natural_grace=120.0)
        finally:
            elapsed = time.monotonic() - started
        code_b = proc_b.returncode
        if code_b == 1 and "instance_active" in (err_b or "").lower() and elapsed < 60:
            report.record(
                "B 同数据目录双开被拦", "PASS",
                f"exit=1 code=instance_active 快速退出 {elapsed:.1f}s",
            )
        else:
            report.record(
                "B 同数据目录双开被拦", "FAIL",
                f"exit={code_b} stderr={err_b!r} elapsed={elapsed:.1f}s",
            )

        if _wait_http_ready(port_a, timeout=10):
            report.record("A 未被 B 惊扰", "PASS", "A 仍正常服务")
        else:
            report.record("A 未被 B 惊扰", "FAIL", "互斥拦截波及先到实例")
        evidence_after = json.loads(
            (dir_a / "server-instance.json").read_text(encoding="utf-8")
        )
        if evidence_after == evidence_a:
            report.record("A 证据文件未被 B 改写", "PASS", "owner_token/pid 不变")
        else:
            report.record("A 证据文件未被 B 改写", "FAIL", "证据文件被二次启动覆盖")

        # --- 用例 2：端口冲突（不同数据目录抢同一端口）---------------------
        proc_c = _launch(dir_b, port_a)
        procs.append(proc_c)
        _, err_c = _stop(proc_c, natural_grace=120.0)
        code_c = proc_c.returncode
        if code_c == 1 and "port_busy" in (err_c or "").lower():
            report.record(
                "C 端口冲突诚实退出", "PASS",
                "exit=1 code=port_busy（含占用人指认）",
            )
        else:
            report.record(
                "C 端口冲突诚实退出", "FAIL", f"exit={code_c} stderr={err_c!r}"
            )
        if _wait_http_ready(port_a, timeout=10):
            report.record("A 未被 C 惊扰", "PASS", "端口冲突不波及在位监听者")
        else:
            report.record("A 未被 C 惊扰", "FAIL", "端口冲突波及先到实例")

        # --- 用例 3：不同数据目录双开=按设计隔离 ---------------------------
        proc_d = _launch(dir_b, port_c)
        procs.append(proc_d)
        if _wait_http_ready(port_c) and _wait_instance_published(dir_b):
            report.record("D 不同数据目录并行", "PASS", f"port={port_c} 独立服务")
        else:
            report.record("D 不同数据目录并行", "FAIL", "第二档案未能独立启动")
        evidence_d = (
            json.loads(
                (dir_b / "server-instance.json").read_text(encoding="utf-8")
            )
            if (dir_b / "server-instance.json").is_file() else {}
        )
        isolated = (
            evidence_d.get("pid") not in (None, evidence_a.get("pid"))
            and evidence_d.get("owner_token") != evidence_a.get("owner_token")
        )
        report.record(
            "双实例证据文件互不串扰", "PASS" if isolated else "FAIL",
            f"A.pid={evidence_a.get('pid')} D.pid={evidence_d.get('pid')}",
        )

        # --- 用例 4：持锁进程死亡→OS 锁释放→下一位干净接管 -----------------
        out_a, _ = _stop(proc_a)
        procs.remove(proc_a)
        time.sleep(1.0)
        proc_e = _launch(dir_a, port_a)
        procs.append(proc_e)
        if _wait_http_ready(port_a) and _wait_instance_published(dir_a):
            report.record(
                "锁进程死亡后接管", "PASS",
                "OS 锁随进程释放，新实例干净启动（无残留锁僵死）",
            )
        else:
            report.record("锁进程死亡后接管", "FAIL", "旧锁残留导致无法重启")
    finally:
        for proc in procs:
            if proc.poll() is None:
                _stop(proc)
        # 沙箱数据目录按身份逐个清理（只动本 harness 创建的 fm-* 目录）。
        import shutil

        for directory in (dir_a, dir_b):
            shutil.rmtree(directory, ignore_errors=True)
    return report.finish()


if __name__ == "__main__":
    raise SystemExit(main())
