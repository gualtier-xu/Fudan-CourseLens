"""发布链健康每日探针（MATURITY-WF-1 件③）。

四点只读检查（gh CLI 只读查询+HTTPS 探测，零凭据明文、零远端写）：

  ① 镜像 HEAD tree ≡ 开发树 runtime-assets.json 的 worker_mirror.active.tree
     （gh api 镜像仓 commits/main，只读；镜像仓身份=同 pin 的 active.repository）；
  ② 最近回声（echo.yml）run 状态——release-chain ⑧ 的「回声=ready_for_dispatch」
     可观测代理面（实例 echo run 在辅助账号名下实例仓，gh 现有令牌不可见时
     如实标注不可见；持续 24h+ 零可见回声=降级信号上报，不代判）；
  ③ cloud-daily 工作流近 24h schedule 投递命中——SMART-SCHED 网格
     （工作日 11 点位 + 每日 22:00 北京兜底）中落在窗口内的点位数 vs 实际 run 数
     （R6/R7 期 6 连丢槽先例=本探针的直接动机）；
  ④ 总仓 release 资产数（latest release）与 Pages HTTP 200。

结果写 <workspace>/.testbench/health/<日期>.md，结论行 verdict=HEALTHY/DEGRADED/BLOCKED。
被 schtasks 每日 09:00 任务 CourseLens-Release-Health 调用：
  <venv python> scripts/release_health_probe.py
独立跑形态相同；退出码 0=HEALTHY 3=DEGRADED 2=BLOCKED。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKSPACE = REPO.parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.distribution import DISTRIBUTION_REPOSITORY  # noqa: E402 — 总仓身份单源

RUNTIME_ASSETS = REPO / "runtime-assets.json"
# 镜像仓身份单源=runtime-assets.json pin（与 ① 的 tree 对账同源同值）
_PIN_ACTIVE = json.loads(RUNTIME_ASSETS.read_text(encoding="utf-8"))["worker_mirror"]["active"]
WORKER_REPO = str(_PIN_ACTIVE["repository"])
# Pages 项目页 URL 由发布仓名派生（<owner>.github.io/<name>，GitHub Pages 结构性等值）
_OWNER, _NAME = DISTRIBUTION_REPOSITORY.split("/", 1)
PAGES_URL = f"https://{_OWNER}.github.io/{_NAME}/"
MASTER_REPO = DISTRIBUTION_REPOSITORY
WINDOW_H = 24

# SMART-SCHED 网格（cloud-daily.yml schedule 的 UTC cron，工作日）+每日 22:00 北京兜底
GRID_CRONS_UTC_WEEKDAY = [(1, 15), (2, 10), (3, 10), (4, 5), (5, 0),
                          (6, 45), (7, 40), (8, 40), (9, 35), (10, 30)]
NIGHTLY_FALLBACK_UTC = (14, 0)


def run(cmd: list[str]) -> tuple[int, str, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout or "", r.stderr or ""


def gh_api(path: str):
    rc, out, err = run(["gh", "api", path])
    if rc != 0:
        return None, err.strip()[:300]
    try:
        return json.loads(out), ""
    except json.JSONDecodeError as exc:
        return None, f"json: {exc}"


def now() -> _dt.datetime:
    return _dt.datetime.now().astimezone()


def cron_points_in_window(window_start_utc: _dt.datetime, window_end_utc: _dt.datetime) -> list[_dt.datetime]:
    """窗口内的理论 schedule 点（UTC）。GitHub schedule 按 UTC cron 触发。"""
    points = []
    day = (window_start_utc - _dt.timedelta(days=1)).date()
    while day <= window_end_utc.date():
        for hh, mm in GRID_CRONS_UTC_WEEKDAY:
            t = _dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=_dt.timezone.utc)
            if t.isoweekday() <= 5 and window_start_utc <= t <= window_end_utc:
                points.append(t)
        hh, mm = NIGHTLY_FALLBACK_UTC
        t = _dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=_dt.timezone.utc)
        if window_start_utc <= t <= window_end_utc:
            points.append(t)
        day += _dt.timedelta(days=1)
    return points


def main() -> int:
    ap = argparse.ArgumentParser(description="发布链健康每日探针（只读）")
    ap.add_argument("--date", default=now().strftime("%Y-%m-%d"))
    args = ap.parse_args()
    # schtasks 用 pythonw 无窗调度：stdout 落 run.log（console 缺位也安全）
    health_dir = WORKSPACE / ".testbench" / "health"
    health_dir.mkdir(parents=True, exist_ok=True)
    run_log = open(health_dir / f"{args.date}.run.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = run_log
    sys.stderr = run_log
    checks: list[dict] = []

    # ① 镜像 tree ≡ pin
    pin = json.loads(RUNTIME_ASSETS.read_text(encoding="utf-8"))
    active = pin.get("worker_mirror", {}).get("active", {})
    expect_tree = str(active.get("tree") or "")
    commit, err = gh_api(f"repos/{WORKER_REPO}/commits/main")
    actual_tree = str((commit or {}).get("commit", {}).get("tree", {}).get("sha") or "")
    mirror_ok = bool(expect_tree) and expect_tree == actual_tree
    checks.append({"id": "mirror_tree_pin", "ok": mirror_ok,
                   "detail": f"pin={expect_tree[:12]} mirror={actual_tree[:12]} err={err}"})

    # ② 回声 run（可观测代理面）
    echo_visible = None
    runs, err = gh_api(f"repos/{WORKER_REPO}/actions/workflows/echo.yml/runs?per_page=5")
    if runs is None:
        echo_detail = f"gh api err: {err}"
        echo_ok = False
    else:
        items = runs.get("workflow_runs") or []
        if items:
            latest = items[0]
            echo_visible = latest.get("created_at")
            echo_ok = latest.get("conclusion") == "success"
            echo_detail = (f"latest run {latest.get('databaseId')} {latest.get('conclusion')} "
                           f"at {latest.get('created_at')}")
        else:
            echo_ok = False
            echo_detail = "镜像仓 echo.yml 无可见 run（实例回声落在辅助账号实例仓，现有令牌不可见；24h+ 持续空窗=降级信号，人工复核）"
    checks.append({"id": "echo_run", "ok": echo_ok, "detail": echo_detail})

    # ③ cloud-daily 近 24h schedule 命中
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    win_start = now_utc - _dt.timedelta(hours=WINDOW_H)
    window_note = ""
    meta, meta_err = gh_api(f"repos/{WORKER_REPO}")
    if meta is not None and meta.get("created_at"):
        created = _dt.datetime.fromisoformat(str(meta["created_at"]).replace("Z", "+00:00"))
        if created > win_start:
            win_start = created
            window_note = f"（窗口起点按镜像仓建仓时刻裁剪 {created:%m-%d %H:%M} UTC；建仓前点位不可能投递）"
    expected = cron_points_in_window(win_start, now_utc)
    runs, err = gh_api(f"repos/{WORKER_REPO}/actions/workflows/cloud-daily.yml/runs?per_page=30")
    if runs is None:
        daily_ok, daily_detail = False, f"gh api err: {err}"
    else:
        items = runs.get("workflow_runs") or []
        sched = [r for r in items if r.get("event") == "schedule"
                 and win_start <= _dt.datetime.fromisoformat(
                     str(r.get("created_at")).replace("Z", "+00:00")) <= now_utc]
        ok_runs = [r for r in sched if r.get("conclusion") == "success"]
        # expected=0（窗口内无理论点位，如建仓后未到首个 cron）=空判断真，不降级
        delivery_ok = (len(sched) >= len(expected) - 1) if expected else True
        success_ok = len(ok_runs) == len(sched)
        daily_ok = delivery_ok and success_ok
        daily_detail = (f"expected={len(expected)} schedule_runs={len(sched)} success={len(ok_runs)} "
                        f"(窗口 {win_start:%m-%d %H:%M}–{now_utc:%m-%d %H:%M} UTC){window_note}；"
                        f"丢槽=expected-actual>1 即降级（R6/R7 六连丢槽先例）")
        if not items:
            daily_detail += "；镜像仓当前零 run（删仓重建后 schedule 尚未投递=丢槽信号）"
    checks.append({"id": "cloud_daily_schedule", "ok": daily_ok, "detail": daily_detail})

    # ④ 总仓 release 资产数 + Pages 200
    rel, err = gh_api(f"repos/{MASTER_REPO}/releases/latest")
    if rel is None:
        rel_ok, rel_detail = False, f"gh api err: {err}"
    else:
        n_assets = len(rel.get("assets") or [])
        rel_ok = n_assets >= 3
        rel_detail = f"latest={rel.get('tag_name')} assets={n_assets} published={rel.get('published_at')}"
    checks.append({"id": "release_assets", "ok": rel_ok, "detail": rel_detail})
    try:
        with urllib.request.urlopen(PAGES_URL, timeout=15) as resp:
            pages_ok = resp.status == 200
            pages_detail = f"HTTP {resp.status}"
    except urllib.error.URLError as exc:
        pages_ok, pages_detail = False, f"HTTP err: {exc}"
    checks.append({"id": "pages_200", "ok": pages_ok, "detail": pages_detail})

    verdict = "HEALTHY" if all(c["ok"] for c in checks) else "DEGRADED"
    if "gh api err" in checks[0]["detail"]:
        verdict = "BLOCKED"  # 连镜像 pin 对账面都不可达=探针自身无法履职
    head = run(["git", "-C", str(REPO), "log", "-1", "--format=%H"])[1].strip()
    health_dir = WORKSPACE / ".testbench" / "health"
    health_dir.mkdir(parents=True, exist_ok=True)
    out = health_dir / f"{args.date}.md"
    lines = [
        "# CourseLens 发布链健康每日探针",
        "",
        f"- 日期: {args.date}",
        f"- 生成: {now().isoformat(timespec='seconds')}",
        f"- verdict: **{verdict}**",
        f"- dev HEAD: `{head[:12]}`",
        "",
        "| 检查点 | 结果 | 详情 |",
        "|---|---|---|",
    ]
    lines += [f"| {c['id']} | {'OK' if c['ok'] else 'RED'} | {c['detail'].replace('|', '/')} |"
              for c in checks]
    lines += ["", f"## 结论", "", f"- {'四点全绿' if verdict == 'HEALTHY' else '存在红点，见上表；红点处置=总控按 release-chain 对账'}"]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[release-health] verdict={verdict} → {out}")
    for c in checks:
        print(f"[release-health] {c['id']}: {'OK' if c['ok'] else 'RED'} — {c['detail']}")
    return {"HEALTHY": 0, "DEGRADED": 3}.get(verdict, 2)


if __name__ == "__main__":
    sys.exit(main())
