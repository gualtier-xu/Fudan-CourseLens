"""CourseLens 夜间定时回归 runner（MATURITY-WF-1 件①）。

依次执行三个阶段并把日志+结构化结果落盘到工作区级目录：
  ① 全量 pytest（分块执行，每块目标 ≤10 分钟，正典 venv .venv-client-py310）
  ② 视觉基线套件（scripts/check_visual_baseline.py）
  ③ 路由探针矩阵套件（pytest tests/test_http_route_probe_matrix.py）

抗清理者设计（宿主 ~55 分钟周期清理者已六杀实证）：
  - pytest 分块：每块独立子进程+独立日志，单块挂掉损失 ≤ 一块；
  - 每块结束由本 runner 追加终局标记行 ``=== CHUNK_END ... ===``；
    日志无终局行=子进程或 runner 被杀→重跑时从该块续跑（每块 ≤3 次尝试）；
  - 全部进度落 state.json/results.json，重跑幂等续跑，不重做已绿块。

结果目录（默认）：<workspace>/.testbench/nightly/<日期>/
  chunk_<NN>.log / chunk_<NN>.json   每块日志与 per-test 结果
  visual_baseline.log                视觉基线日志
  route_probe.log                    路由探针日志
  state.json                         分块状态（断点续跑依据）
  results.json                       合并 per-test 结果（delta 依据）
  report.md                          晨间报告（含与上一轮的 delta 报告）

用法：
  python scripts/nightly_regression.py               # 今天（或续跑今天）
  python scripts/nightly_regression.py --date 2026-10-10
  python scripts/nightly_regression.py --smoke       # 冒烟：1 块×4 文件+两套件跳过

被 R8 发布链调度形态：schtasks 每日 03:30 任务 CourseLens-Nightly-Regression
调用 ``<venv python> scripts/nightly_regression.py``。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKSPACE = REPO.parents[1]
CANONICAL_PYTHON = REPO / ".venv-client-py310" / "Scripts" / "python.exe"
TESTS_DIR = REPO / "tests"
CHUNK_TARGET = 12          # 期望分块数（按文件均衡）
CHUNK_TIMEOUT_S = 900      # 单块硬超时（清理者窗口内的安全上界）
MAX_ATTEMPTS = 3           # 单块最大尝试次数
CHUNK_END_MARK = "=== CHUNK_END"

# 已知在途面（DEFECT-2-FIX 车道独占，本 runner 只记录不判不修）
INFLIGHT_FILES = (
    "src/application.py",
    "src/runtime/http_api.py",
    "src/runtime/student_features.py",
)

V_LINE = re.compile(r"^(tests/\S+?::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b")
SUMMARY_LINE = re.compile(r"^\s*=.*\bin [\d.]+s.*=\s*$")
SUMMARY_TOKEN = re.compile(r"(\d+) (passed|failed|skipped|xfailed|errors?)\b")


def parse_summary(text: str) -> tuple[str, dict]:
    """从 pytest 输出尾部找汇总行并解析计数（形态多样，token 级提取）。"""
    for line in reversed(text.splitlines()):
        if SUMMARY_LINE.search(line):
            counts = {"failed": 0, "passed": 0, "skipped": 0, "xfailed": 0, "errors": 0}
            for n, word in SUMMARY_TOKEN.findall(line):
                key = "errors" if word.startswith("error") else word
                counts[key] = counts.get(key, 0) + int(n)
            return line.strip(), counts
    return "", {}


def merge_summary(parts: list[str]) -> dict:
    agg = {"failed": 0, "passed": 0, "skipped": 0, "xfailed": 0, "errors": 0, "seconds": 0.0}
    for s in parts:
        _, counts = parse_summary(s)
        for k, v in counts.items():
            agg[k] = agg.get(k, 0) + v
    return agg


def now_local() -> _dt.datetime:
    return _dt.datetime.now().astimezone()


def default_outdir(date_str: str) -> Path:
    env = os.environ.get("COURSELENS_NIGHTLY_OUTDIR", "").strip()
    if env:
        return Path(env)
    return WORKSPACE / ".testbench" / "nightly" / date_str


def git_snapshot() -> dict:
    def _git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(REPO), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout.strip()

    return {
        "head": _git("log", "-1", "--format=%H %s")[:200],
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": _git("status", "--porcelain").splitlines(),
    }


def list_test_files() -> list[Path]:
    return sorted(TESTS_DIR.glob("test_*.py"))


def make_chunks(files: list[Path], size: int) -> list[list[str]]:
    return [[str(p.relative_to(REPO)).replace("\\", "/") for p in files[i:i + size]]
            for i in range(0, len(files), size)]


def run_pytest_chunk(paths: list[str], log_path: Path, timeout_s: int) -> dict:
    """跑一块 pytest，返回 {rc, killed, seconds, per_test, summary}。"""
    cmd = [str(CANONICAL_PYTHON), "-m", "pytest", *paths,
           "-v", "--tb=short", "-rf", "--no-header", "-p", "no:cacheprovider"]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    start = time.monotonic()
    killed = False
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n=== CHUNK_START {[_ for _ in paths]} {now_local().isoformat()} ===\n")
        log.flush()
        try:
            proc = subprocess.run(cmd, cwd=str(REPO), stdout=log, stderr=subprocess.STDOUT,
                                  timeout=timeout_s, env=env)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc, killed = -9, True
    seconds = round(time.monotonic() - start, 1)
    text = log_path.read_text(encoding="utf-8", errors="replace")
    per_test = {}
    for line in text.splitlines():
        m = V_LINE.match(line.strip())
        if m:
            per_test[m.group(1)] = m.group(2)
    summary, _ = parse_summary(text)
    # 终局标记（父进程写）：无此行=被杀
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"{CHUNK_END_MARK} rc={rc} killed={killed} seconds={seconds} "
                  f"tests={len(per_test)} {now_local().isoformat()} ===\n")
    return {"rc": rc, "killed": killed, "seconds": seconds,
            "per_test": per_test, "summary": summary}


def parse_visual_output(text: str) -> dict:
    """从 check_visual_baseline.py 输出提取通过/超差摘要。"""
    ok = re.search(r"(\d+)\s*/\s*(\d+)\s*(?:组合|frames|OK)", text)
    failed = bool(re.search(r"EXIT 1|超差|FAIL", text))
    return {"ok": not failed, "detail": (ok.group(0) if ok else ""), "tail": text[-800:]}


def delta_vs_previous(outdir: Path, results: dict) -> dict:
    """与最近一个更早日期的结果目录 diff 出新增红/消失红/通过数变化。"""
    nightly_root = outdir.parent
    prev_dir = None
    for cand in sorted(nightly_root.glob("*/results.json"), reverse=True):
        if cand.parent != outdir:
            prev_dir = cand.parent
            break
    if prev_dir is None:
        return {"previous": None, "note": "首跑无前轮结果，delta 记为本轮基线"}
    prev = json.loads(prev_dir.joinpath("results.json").read_text(encoding="utf-8"))
    prev_tests = prev.get("per_test", {})
    cur_tests = results["per_test"]
    bad = {"FAILED", "ERROR"}
    new_red = sorted(t for t, s in cur_tests.items() if s in bad and prev_tests.get(t) not in bad)
    gone_red = sorted(t for t, s in prev_tests.items() if s in bad and cur_tests.get(t) not in bad)
    new_tests = sorted(set(cur_tests) - set(prev_tests))
    removed_tests = sorted(set(prev_tests) - set(cur_tests))
    return {
        "previous": str(prev_dir),
        "new_red": new_red,
        "disappeared_red": gone_red,
        "new_tests": len(new_tests),
        "removed_tests": len(removed_tests),
        "passed_prev": sum(1 for s in prev_tests.values() if s == "PASSED"),
        "passed_cur": sum(1 for s in cur_tests.values() if s == "PASSED"),
    }


def write_report(outdir: Path, snap: dict, stage_rows: list[str],
                 results: dict, delta: dict) -> Path:
    totals = results["totals"]
    lines = [
        "# CourseLens 夜间回归报告",
        "",
        f"- 日期: {outdir.name}",
        f"- 生成: {now_local().isoformat(timespec='seconds')}",
        f"- HEAD: `{snap['head']}`（branch {snap['branch']}）",
        f"- 脏面: {len(snap['dirty'])} 文件",
        *(f"  - `{l}`" for l in snap["dirty"][:20]),
        f"- 在途面注记: {', '.join(INFLIGHT_FILES)}（DEFECT-2-FIX 车道独占；"
        f"命中这些行为面的红=在途面嫌疑，归因不判不修）",
        "",
        "## 阶段结果",
        "",
        "| 阶段 | 结果 | 耗时 | 备注 |",
        "|---|---|---|---|",
        *stage_rows,
        "",
        "## 汇总",
        "",
        f"- passed={totals.get('passed', 0)} failed={totals.get('failed', 0)} "
        f"errors={totals.get('errors', 0)} skipped={totals.get('skipped', 0)} "
        f"xfailed={totals.get('xfailed', 0)}（pytest 部分合计）",
        "",
    ]
    reds = sorted(t for t, s in results["per_test"].items() if s in {"FAILED", "ERROR"})
    lines.append(f"## 红清单（{len(reds)}）")
    lines.append("")
    lines.extend(f"- `{t}`" for t in reds) if reds else lines.append("- 无")
    lines += ["", "## Delta vs 上一轮", ""]
    if delta.get("previous") is None:
        lines.append(f"- {delta.get('note', '无前轮')}")
    else:
        lines += [
            f"- 前轮: `{delta['previous']}`",
            f"- 新增红（{len(delta['new_red'])}）: " + (", ".join(f"`{t}`" for t in delta["new_red"][:40]) or "无"),
            f"- 消失红（{len(delta['disappeared_red'])}）: " + (", ".join(f"`{t}`" for t in delta["disappeared_red"][:40]) or "无"),
            f"- 通过数变化: {delta['passed_prev']} → {delta['passed_cur']}（Δ={delta['passed_cur'] - delta['passed_prev']:+d}）",
            f"- 新增/移除测试: {delta['new_tests']}/{delta['removed_tests']}",
        ]
    report = outdir / "report.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="CourseLens 夜间定时回归 runner")
    ap.add_argument("--date", default=now_local().strftime("%Y-%m-%d"))
    ap.add_argument("--smoke", action="store_true", help="冒烟模式：只跑第一块前 4 个文件")
    args = ap.parse_args()

    outdir = default_outdir(args.date)
    outdir.mkdir(parents=True, exist_ok=True)
    # schtasks 用 pythonw 无窗调度：stdout/stderr 落 run.log（console 缺位也安全）
    run_log = open(outdir / "run.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = run_log
    sys.stderr = run_log
    state_path = outdir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

    snap = git_snapshot()
    print(f"[nightly] HEAD={snap['head'][:60]} dirty={len(snap['dirty'])} out={outdir}")

    files = list_test_files()
    if args.smoke:
        files = files[:4]
    chunk_size = max(1, -(-len(files) // CHUNK_TARGET)) if not args.smoke else 4
    chunks = make_chunks(files, chunk_size)
    print(f"[nightly] {len(files)} test files → {len(chunks)} chunks (size~{chunk_size})")

    state.setdefault("chunks", {})
    summaries: list[str] = list(state.get("summaries", []))
    per_test: dict[str, str] = dict(state.get("per_test", {}))

    # 阶段①：分块 pytest（续跑：跳过已完成块，被杀块 ≤MAX_ATTEMPTS 次重试）
    for idx, chunk in enumerate(chunks):
        rec = state["chunks"].get(str(idx), {"attempts": 0, "done": False})
        if rec.get("done"):
            continue
        if rec["attempts"] >= MAX_ATTEMPTS:
            print(f"[nightly] chunk {idx} 达最大尝试次数，跳过（BLOCKED）")
            continue
        rec["attempts"] += 1
        print(f"[nightly] chunk {idx}/{len(chunks) - 1} attempt {rec['attempts']} "
              f"({len(chunk)} files) start")
        res = run_pytest_chunk(chunk, outdir / f"chunk_{idx:02d}.log", CHUNK_TIMEOUT_S)
        (outdir / f"chunk_{idx:02d}.json").write_text(
            json.dumps(res, ensure_ascii=False), encoding="utf-8")
        rec["done"] = not res["killed"]
        rec["killed"] = res["killed"]
        rec["summary"] = res["summary"]
        rec["seconds"] = res["seconds"]
        state["chunks"][str(idx)] = rec
        if res["killed"]:
            # 续跑语义：本块记为被杀，继续下一块（不空等）；
            # 下轮重跑时若 attempts<3 会先补跑本块。
            print(f"[nightly] chunk {idx} 被杀（timeout），继续下一块")
        else:
            summaries.append(res["summary"])
            per_test.update(res["per_test"])
        state["summaries"] = summaries
        state["per_test"] = per_test
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    blocked = [i for i, r in state["chunks"].items()
               if not r.get("done") and r["attempts"] >= MAX_ATTEMPTS]
    killed_open = [i for i, r in state["chunks"].items()
                   if not r.get("done") and r["attempts"] < MAX_ATTEMPTS]

    # 阶段②：视觉基线
    vb_log = outdir / "visual_baseline.log"
    vb = {"ok": None}
    if not args.smoke:
        print("[nightly] stage B visual baseline start")
        with open(vb_log, "a", encoding="utf-8") as log:
            log.write(f"=== VB_START {now_local().isoformat()} ===\n")
            log.flush()
            try:
                p = subprocess.run([str(CANONICAL_PYTHON), "scripts/check_visual_baseline.py"],
                                   cwd=str(REPO), stdout=log, stderr=subprocess.STDOUT,
                                   timeout=CHUNK_TIMEOUT_S,
                                   env=dict(os.environ, PYTHONIOENCODING="utf-8"))
                vb = {"ok": p.returncode == 0, "rc": p.returncode}
            except subprocess.TimeoutExpired:
                vb = {"ok": False, "rc": -9}
            log.write(f"=== VB_END {vb} {now_local().isoformat()} ===\n")
        vb.update(parse_visual_output(vb_log.read_text(encoding="utf-8", errors="replace")))
        print(f"[nightly] stage B visual baseline ok={vb['ok']} {vb.get('detail', '')}")

    # 阶段③：路由探针矩阵
    rp_log = outdir / "route_probe.log"
    rp = {"ok": None}
    if not args.smoke:
        print("[nightly] stage C route probe start")
        res = run_pytest_chunk(["tests/test_http_route_probe_matrix.py"], rp_log, CHUNK_TIMEOUT_S)
        rp = {"ok": res["rc"] == 0 and not res["killed"], "rc": res["rc"]}
        for t, s in res["per_test"].items():
            per_test[f"route_probe::{t}"] = s
        summaries.append(res["summary"])
        print(f"[nightly] stage C route probe ok={rp['ok']} {res['summary']}")

    totals = merge_summary(summaries)
    totals["seconds"] = round(sum(r.get("seconds", 0) for r in state["chunks"].values()), 1)
    results = {"date": args.date, "head": snap["head"], "dirty_count": len(snap["dirty"]),
               "totals": totals, "per_test": per_test,
               "visual_baseline": vb, "route_probe": rp,
               "blocked_chunks": blocked, "killed_open_chunks": killed_open,
               "generated_at": now_local().isoformat(timespec="seconds")}
    (outdir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                         encoding="utf-8")

    stage_rows = [
        f"| ① pytest 分块 | {'DONE' if not killed_open and not blocked else 'PARTIAL'}"
        f"（killed_open={len(killed_open)} blocked={len(blocked)}） | {totals.get('seconds', 0):.0f}s |"
        f" passed={totals.get('passed', 0)} failed={totals.get('failed', 0)}"
        f" errors={totals.get('errors', 0)} |",
        f"| ② 视觉基线 | {vb.get('ok')} | - | {vb.get('detail', '')} |",
        f"| ③ 路由探针矩阵 | {rp.get('ok')} | - | {summaries[-1] if rp.get('ok') is not None else ''} |",
    ]
    delta = delta_vs_previous(outdir, results)
    report = write_report(outdir, snap, stage_rows, results, delta)
    print(f"[nightly] report → {report}")
    print(f"[nightly] totals={totals} new_red={len(delta.get('new_red', []))} "
          f"disappeared_red={len(delta.get('disappeared_red', []))}")
    return 2 if (killed_open or blocked) else 0


if __name__ == "__main__":
    sys.exit(main())
