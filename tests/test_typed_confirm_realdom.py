"""D-20261009-07 typed 危险确认全族真实 DOM 回归钉（CONFIRM-DIALOG-FIX-1）。

单测假 DOM 无法表达「label.textContent 抹除子元素」的真实 DOM 行为
（registry 平铺无父子语义），P1 缺陷因此漏检。本钉以真实 chromium 驱动
合成壳前端，逐门禁走完 开窗→键入→确认 全链：

- 在册有名课程删除记录：门=课程名逐字精确匹配；
- 无名孤儿行删除记录：门=课程编号逐字精确匹配；
- 清除全部孤儿：门=非空确认语 + lead 点名清单。

「不应发生」断言：全程零 console 错误/页面异常；#data-confirm-input 在
每次 label 更新后仍在 DOM。

运行契约与 test_visual_baseline 同源：node/playwright 包/chromium 缺席时
SKIP 而非红（可选重依赖，缺席不拖垮正典全量）；安装方式同
docs/technical/visual-baseline.md。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# 两条孤儿种子（未知课程的终态任务→数据页孤儿行）；9000 留给清除全部孤儿点名。
SEED_TASKS = [
    {"kind": "subtitle", "course_id": "8888", "sub_id": "88881", "state": "completed"},
    {"kind": "subtitle", "course_id": "9000", "sub_id": "90001", "state": "completed"},
]


def _skip_reason() -> str | None:
    if shutil.which("node") is None:
        return "node 不在 PATH（typed 确认真实 DOM 钉需要 node + playwright）"
    if not (HERE / "visual-baseline" / "node_modules" / "playwright").is_dir():
        return "tests/visual-baseline/node_modules 缺失（cd tests/visual-baseline && npm install）"
    return None


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")
def test_typed_confirm_family_realdom() -> None:
    scratch = ROOT / "runtime" / "cache" / "typed-confirm-realdom" / f"pytest-{os.getpid()}-{int(time.time())}"
    scratch.mkdir(parents=True, exist_ok=True)
    seed_path = scratch / "seed-tasks.json"
    seed_path.write_text(json.dumps(SEED_TASKS), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1"}
    server = subprocess.Popen(
        [
            sys.executable, "-m", "tests.synthetic_shell_server",
            "--port", "0",
            "--onboarding-guide", "completed",
            "--cache-root", str(scratch),
            "--seed-tasks", str(seed_path),
        ],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line) for line in server.stdout], daemon=True).start()
    base_url = None
    deadline = time.monotonic() + 60
    try:
        while time.monotonic() < deadline:
            if server.poll() is not None:
                stderr = server.stderr.read() if server.stderr else ""
                pytest.fail(f"合成壳提前退出（code {server.returncode}）：\n{stderr[-2000:]}")
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                continue
            if "http://127.0.0.1:" in line:
                base_url = line.strip()
                break
        if base_url is None:
            pytest.fail("合成壳 60s 未就绪")
        run = subprocess.run(
            ["node", str(HERE / "typed_confirm_realdom.mjs"), "--base-url", base_url, "--out", str(scratch)],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=240,
        )
        if run.returncode == 3:
            pytest.skip("playwright chromium 未安装（cd tests/visual-baseline && npx playwright install chromium）")
        tail = "\n".join((run.stdout or "").splitlines()[-40:])
        assert run.returncode == 0, (
            "typed 确认族真实 DOM 钉未全绿：\n"
            f"{tail}\nstderr: {(run.stderr or '')[-1000:]}"
        )
        assert "fail=0" in (run.stdout or ""), "钉脚本 SUMMARY 缺 fail=0"
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(scratch, ignore_errors=True)
