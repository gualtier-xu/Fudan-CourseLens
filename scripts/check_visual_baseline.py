"""视觉基线一键自检/重采集 CLI（VISUAL-BASELINE，0.1.1-A 的人类入口）。

流程与正典 pytest 包装器（tests/test_visual_baseline.py）完全同源：
  node tests/visual-baseline/capture.mjs   # 起合成壳 + playwright 截 30 组合
  python tests/visual-baseline/visual_compare.py  # 像素比对 / --update 提升

用法（仓库根执行）：
  python scripts/check_visual_baseline.py            # 自检：与基线比对，超差 EXIT 1
  python scripts/check_visual_baseline.py --update   # 有意视觉变更后重采集基线
  python scripts/check_visual_baseline.py --diff-out .tmp-visbase/diff   # 超差时出热力图

playwright 未安装时的引导信息见 docs/technical/visual-baseline.md。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VB_DIR = ROOT / "tests" / "visual-baseline"
BASELINE_DIR = VB_DIR / "baselines"


def _python_for_synthetic_shell() -> str:
    """合成壳需要 src/** 的完整客户端依赖（含 Pillow）：优先显式 env，其次
    正典客户端 venv，最后回退当前解释器。"""
    env_python = os.environ.get("COURSELENS_VISUAL_PYTHON")
    if env_python:
        return env_python
    candidates = [
        ROOT / ".venv-client-py310" / "Scripts" / "python.exe",
        ROOT / ".venv-client-py310" / "bin" / "python",
        ROOT / ".venv-client-py312" / "Scripts" / "python.exe",
        ROOT / ".venv-client-py312" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def main() -> int:
    parser = argparse.ArgumentParser(description="视觉基线自检/重采集")
    parser.add_argument("--update", action="store_true", help="把本次采集提升为新基线（有意视觉变更后使用）")
    parser.add_argument("--pixel-tolerance", type=int, default=None)
    parser.add_argument("--fail-ratio", type=float, default=None)
    parser.add_argument("--diff-out", type=Path, default=None, help="判红热力图输出目录")
    parser.add_argument("--keep", type=Path, default=None, help="保留本次采集结果到指定目录（默认用后即清）")
    args = parser.parse_args()

    if shutil.which("node") is None:
        print("[visual-baseline] node 不在 PATH：视觉采集需要 node >= 18 与 playwright。", file=sys.stderr)
        return 2
    if not (VB_DIR / "node_modules" / "playwright").is_dir():
        print("[visual-baseline] 依赖未安装：cd tests/visual-baseline && npm install", file=sys.stderr)
        return 2

    scratch = args.keep or (ROOT / "runtime" / "cache" / "visual-baseline" / f"check-{os.getpid()}-{int(time.time())}")
    capture = subprocess.run(
        ["node", str(VB_DIR / "capture.mjs"), "--out", str(scratch),
         "--python", _python_for_synthetic_shell()],
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
    )
    if capture.returncode == 3:
        print("[visual-baseline] chromium 未安装：cd tests/visual-baseline && npx playwright install chromium", file=sys.stderr)
        return 3
    if capture.returncode != 0:
        return capture.returncode

    compare_argv = ["--current", str(scratch)]
    if args.pixel_tolerance is not None:
        compare_argv += ["--pixel-tolerance", str(args.pixel_tolerance)]
    if args.fail_ratio is not None:
        compare_argv += ["--fail-ratio", str(args.fail_ratio)]
    if args.diff_out is not None:
        compare_argv += ["--diff-out", str(args.diff_out)]
    if args.update:
        compare_argv.append("--update")
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("course_lens_visual_compare", VB_DIR / "visual_compare.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    code = module.main(compare_argv)
    if args.keep is None:
        shutil.rmtree(scratch, ignore_errors=True)
    if args.update and code == 0:
        print(f"[visual-baseline] 新基线就位：{BASELINE_DIR}（{time.strftime('%Y-%m-%d %H:%M:%S')}）")
    return code


if __name__ == "__main__":
    sys.exit(main())
