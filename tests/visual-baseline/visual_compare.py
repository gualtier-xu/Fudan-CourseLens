"""视觉基线像素比对（VISUAL-BASELINE，0.1.1-A）。

Pillow 实现（客户端锁定依赖 pillow==12.3.0，零新增 Python 依赖）。逐文件
配对 baseline/ 与 current/ 目录下的 ``*.jpg`` 截图：

- 尺寸不一致 = 直接不合格（布局漂移不是像素噪声）；
- 逐像素比对：任一通道差值 > ``--pixel-tolerance`` 记为变化像素；
- 变化像素占比 > ``--fail-ratio`` 判红（可配，默认 0.2%）；
- 基线缺失/多余组合 = 不合格（防「新增表面忘采基线」与「删面留死基线」）；
- 判红时输出 ``--diff-out`` 热力图（红=超容差像素，暗底=原亮度），供
  人眼定位漂移区域。

有意视觉变更后的基线重采集流程见 docs/technical/visual-baseline.md；
快捷路径：``python scripts/check_visual_baseline.py --update``。

退出码：0=全部在容差内；1=存在超差/缺失/多余；2=用法或环境错误。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops

EXIT_OK = 0
EXIT_DIFF = 1
EXIT_ERROR = 2

DEFAULT_PIXEL_TOLERANCE = 6
DEFAULT_FAIL_RATIO = 0.002


def compare_pair(baseline_path: Path, current_path: Path, pixel_tolerance: int) -> dict[str, Any]:
    """比对单组合，返回闭集结果字典（不抛异常路径；读图失败记 error）。"""
    result: dict[str, Any] = {
        "file": baseline_path.name,
        "baseline_bytes": baseline_path.stat().st_size,
        "current_bytes": current_path.stat().st_size,
    }
    try:
        with Image.open(baseline_path) as base_img, Image.open(current_path) as cur_img:
            base = base_img.convert("RGB")
            current = cur_img.convert("RGB")
    except Exception as exc:  # noqa: BLE001 - 比对器对损坏文件诚实报错而非崩溃
        result.update(status="error", detail=f"decode failed: {exc}")
        return result
    result["baseline_size"] = list(base.size)
    result["current_size"] = list(current.size)
    if base.size != current.size:
        result.update(status="size-mismatch")
        return result
    diff = ImageChops.difference(base, current)
    masks = [band.point(lambda v, tol=pixel_tolerance: 255 if v > tol else 0) for band in diff.split()]
    combined = masks[0]
    for mask in masks[1:]:
        combined = ImageChops.lighter(combined, mask)
    hist = combined.histogram()
    changed = sum(hist[1:])
    total = base.size[0] * base.size[1]
    result["changed_pixels"] = changed
    result["total_pixels"] = total
    result["changed_ratio"] = changed / total if total else 0.0
    result["status"] = "changed" if changed else "identical"
    result["mask"] = combined
    return result


def render_heatmap(mask: Image.Image, baseline_path: Path, diff_out: Path) -> str:
    """把变化掩码渲染成红字热力图（暗底取自基线亮度），返回落盘路径。"""
    width, height = mask.size
    heatmap = Image.merge("RGB", (mask, Image.new("L", (width, height), 0), Image.new("L", (width, height), 0)))
    try:
        with Image.open(baseline_path) as base_img:
            dim = base_img.convert("L").point(lambda v: v // 5)
            backdrop = Image.merge("RGB", (dim, dim, dim))
            heatmap = ImageChops.lighter(backdrop, heatmap)
    except Exception:  # noqa: BLE001 - 底图失败时退回纯红掩码
        pass
    diff_out.mkdir(parents=True, exist_ok=True)
    target = diff_out / f"{baseline_path.stem}-diff.png"
    heatmap.save(target, format="PNG", optimize=True)
    return str(target)


def compare_dirs(
    baseline_dir: Path,
    current_dir: Path,
    *,
    pixel_tolerance: int = DEFAULT_PIXEL_TOLERANCE,
    fail_ratio: float = DEFAULT_FAIL_RATIO,
    diff_out: Path | None = None,
) -> tuple[int, dict[str, Any]]:
    """全量配对比对，返回 (退出码, 摘要)。"""
    if not baseline_dir.is_dir():
        print(f"[visual-compare] baseline 目录不存在：{baseline_dir}", file=sys.stderr)
        return EXIT_ERROR, {}
    if not current_dir.is_dir():
        print(f"[visual-compare] current 目录不存在：{current_dir}", file=sys.stderr)
        return EXIT_ERROR, {}
    baselines = sorted(path.name for path in baseline_dir.glob("*.jpg"))
    currents = sorted(path.name for path in current_dir.glob("*.jpg"))
    missing = sorted(set(currents) - set(baselines))
    extra = sorted(set(baselines) - set(currents))
    report: dict[str, Any] = {
        "pixel_tolerance": pixel_tolerance,
        "fail_ratio": fail_ratio,
        "compared": [],
        "missing_in_baseline": missing,
        "missing_in_current": extra,
        "failures": [],
    }
    failed = bool(missing or extra)
    for name in sorted(set(baselines) & set(currents)):
        result = compare_pair(baseline_dir / name, current_dir / name, pixel_tolerance)
        mask = result.pop("mask", None)
        over_ratio = result.get("changed_ratio", 0.0) > fail_ratio
        bad = result["status"] in {"error", "size-mismatch"} or (result["status"] == "changed" and over_ratio)
        if bad and mask is not None and diff_out is not None:
            result["heatmap"] = render_heatmap(mask, baseline_dir / name, diff_out)
        result["verdict"] = "fail" if bad else "pass"
        report["compared"].append(result)
        if bad:
            failed = True
            report["failures"].append(result["file"])
    if failed:
        return EXIT_DIFF, report
    return EXIT_OK, report


def print_report(report: dict[str, Any], *, baseline_dir: Path, current_dir: Path) -> None:
    print(f"[visual-compare] baseline={baseline_dir}")
    print(f"[visual-compare] current={current_dir}")
    worst = sorted(
        (item for item in report["compared"] if item["status"] == "changed"),
        key=lambda item: item["changed_ratio"],
        reverse=True,
    )[:5]
    for item in report["compared"]:
        if item["verdict"] == "fail":
            if item["status"] == "size-mismatch":
                print(f"  FAIL {item['file']}: 尺寸漂移 {item['baseline_size']} -> {item['current_size']}")
            elif item["status"] == "error":
                print(f"  FAIL {item['file']}: {item['detail']}")
            else:
                print(
                    f"  FAIL {item['file']}: 变化像素 {item['changed_pixels']}/{item['total_pixels']}"
                    f" ({item['changed_ratio']:.4%} > {report['fail_ratio']:.4%})"
                    + (f" 热力图={item['heatmap']}" if item.get("heatmap") else "")
                )
    for item in worst:
        if item["verdict"] == "pass":
            print(f"  note {item['file']}: 容差内变化 {item['changed_ratio']:.4%}")
    if report["missing_in_baseline"]:
        print(f"  FAIL 基线缺失组合: {report['missing_in_baseline']}（有意新增表面请走重采集流程）")
    if report["missing_in_current"]:
        print(f"  FAIL 基线多余组合: {report['missing_in_current']}（表面已删除请走重采集流程）")
    passed = len(report["compared"]) - len(report["failures"])
    print(f"[visual-compare] {passed}/{len(report['compared'])} 组合在容差内"
          f"（tolerance={report['pixel_tolerance']}, fail_ratio={report['fail_ratio']:.4%}）")


def update_baselines(baseline_dir: Path, current_dir: Path) -> None:
    """把 current 采集结果提升为新基线（jpg 逐文件覆盖）。"""
    baseline_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for path in sorted(current_dir.glob("*.jpg")):
        shutil.copyfile(path, baseline_dir / path.name)
        copied += 1
    print(f"[visual-compare] 基线已更新：{copied} 个组合 -> {baseline_dir}")


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    default_baseline = here / "baselines"
    parser = argparse.ArgumentParser(description="视觉基线像素比对")
    parser.add_argument("--baseline", type=Path, default=default_baseline)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--pixel-tolerance", type=int, default=DEFAULT_PIXEL_TOLERANCE)
    parser.add_argument("--fail-ratio", type=float, default=DEFAULT_FAIL_RATIO)
    parser.add_argument("--diff-out", type=Path, default=None, help="判红时输出热力图的目录")
    parser.add_argument("--update", action="store_true", help="先比对留痕，再把 current 提升为新基线")
    parser.add_argument("--json", action="store_true", help="打印机器可读摘要")
    args = parser.parse_args(argv)

    code, report = compare_dirs(
        args.baseline, args.current,
        pixel_tolerance=args.pixel_tolerance, fail_ratio=args.fail_ratio,
        diff_out=args.diff_out,
    )
    if args.json and report:
        slim = {key: value for key, value in report.items()}
        slim["compared"] = [
            {key: value for key, value in item.items() if key != "mask"}
            for item in report["compared"]
        ]
        print(json.dumps(slim, ensure_ascii=False))
    else:
        print_report(report, baseline_dir=args.baseline, current_dir=args.current)
    if args.update:
        if code not in {EXIT_OK, EXIT_DIFF}:
            return code
        update_baselines(args.baseline, args.current)
        return EXIT_OK
    return code


if __name__ == "__main__":
    sys.exit(main())
