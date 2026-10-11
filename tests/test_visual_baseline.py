"""视觉回归基线包装器（VISUAL-BASELINE，0.1.1-A；入正典 pytest 套件）。

两条用例：
- ``test_visual_baseline_inventory``：零浏览器依赖的基线卫生检查（体积上限
  ≤300KB/张、命名闭集、组合非空）——任何环境（含 CI 无 playwright）恒跑。
- ``test_visual_baseline_matches``：真采集+比对——起合成壳服务器，playwright
  截 5 页 × 2 主题 × 3 视口全页图，与 ``tests/visual-baseline/baselines/``
  逐张像素比对（容差与失败阈值经 visual_compare.py 默认值）。playwright 未
  安装（node 包或 chromium 缺）时 SKIP 而非红：视觉 harness 是可选重依赖，
  缺席不拖垮正典全量；安装方式见 docs/technical/visual-baseline.md。

有意视觉变更后重采集：设 ``COURSELENS_VISUAL_UPDATE=1`` 跑本文件，或
``python scripts/check_visual_baseline.py --update``（推荐，流程同源）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VB_DIR = HERE / "visual-baseline"
BASELINE_DIR = VB_DIR / "baselines"
CAPTURE_SCRIPT = VB_DIR / "capture.mjs"
COMPARE_SCRIPT = VB_DIR / "visual_compare.py"

MAX_BASELINE_BYTES = 307200
# harness 预算按 82 组合实测算（30 页面 ≈28s + 52 状态组合，其中 8 个直播
# 进入态含 HLS 会话建立 ≈17s/个）；VISUAL-BASELINE-2 由 180s 上调。
HARNESS_BUDGET_MS = 480_000
NAME_PATTERN = re.compile(r"^[a-z-]+--(light|dark)--\d+x\d+\.jpg$")


def _node_missing_reason() -> str | None:
    if shutil.which("node") is None:
        return "node 不在 PATH（视觉采集需要 node + playwright）"
    if not (VB_DIR / "node_modules" / "playwright").is_dir():
        return "tests/visual-baseline/node_modules 缺失（cd tests/visual-baseline && npm install）"
    return None


def test_visual_baseline_inventory() -> None:
    assert BASELINE_DIR.is_dir(), f"基线目录缺失：{BASELINE_DIR}"
    shots = sorted(BASELINE_DIR.glob("*.jpg"))
    assert shots, "基线为空：先跑一次重采集（COURSELENS_VISUAL_UPDATE=1 或 scripts/check_visual_baseline.py --update）"
    seen: set[str] = set()
    for shot in shots:
        assert NAME_PATTERN.match(shot.name), f"基线命名漂移（应为 <page>--<theme>--<WxH>.jpg）：{shot.name}"
        assert shot.name not in seen, f"重复组合：{shot.name}"
        seen.add(shot.name)
        size = shot.stat().st_size
        assert size <= MAX_BASELINE_BYTES, f"基线超体积上限（{MAX_BASELINE_BYTES}B）：{shot.name} = {size}B"
    themes = {name.split("--")[1] for name in seen}
    assert themes == {"light", "dark"}, f"双主题不齐：{themes}"
    pages = sorted({name.split("--")[0] for name in seen})
    assert pages, "无任何页面组合"


@pytest.mark.skipif(_node_missing_reason() is not None, reason=_node_missing_reason() or "")
def test_visual_baseline_matches() -> None:
    scratch = ROOT / "runtime" / "cache" / "visual-baseline" / f"pytest-{os.getpid()}-{int(time.time())}"
    update_mode = os.environ.get("COURSELENS_VISUAL_UPDATE") == "1"
    try:
        capture = subprocess.run(
            ["node", str(CAPTURE_SCRIPT), "--out", str(scratch), "--python", sys.executable],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=540,
        )
        if capture.returncode == 3:
            pytest.skip("playwright chromium 未安装（cd tests/visual-baseline && npx playwright install chromium）")
        assert capture.returncode == 0, (
            "视觉采集失败：\n"
            f"stdout: {capture.stdout[-2000:]}\nstderr: {capture.stderr[-2000:]}"
        )
        manifest = scratch / "capture-manifest.json"
        assert manifest.is_file(), "采集 manifest 缺失"
        elapsed_ms = int(manifest.read_text(encoding="utf-8").split('"elapsedMs": ')[1].split(",")[0])
        assert elapsed_ms <= HARNESS_BUDGET_MS, f"harness 运行 {elapsed_ms}ms 超出 {HARNESS_BUDGET_MS}ms 预算"

        argv = ["--current", str(scratch)]
        if update_mode:
            argv.append("--update")
        compare = subprocess.run(
            [sys.executable, str(COMPARE_SCRIPT), *argv],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=240,
        )
        if update_mode:
            assert compare.returncode == 0, f"基线更新失败：\n{compare.stdout[-2000:]}\n{compare.stderr[-2000:]}"
            assert any(BASELINE_DIR.glob("*.jpg")), "更新后基线仍为空"
            return
        if compare.returncode == 1:
            pytest.fail(f"视觉回归报警（与 0.1.0 基线超差）：\n{compare.stdout[-3000:]}")
        assert compare.returncode == 0, f"比对器异常：\n{compare.stdout[-2000:]}\n{compare.stderr[-2000:]}"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
