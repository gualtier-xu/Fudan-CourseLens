#!/usr/bin/env bash
# 学生第一天彩排（首装→首启→健康→按 PID 收尾）——N9-R 2026-09-25 实弹资产化。
#
# 出处：docs/release-runbook.md §7 与 docs/release-day-ops-pack.md §10；
# 计时基线见 runbook §B（N9-R 首测：装机 84s / 冷启 READY 13s）。
#
# 用法（Git Bash / MSYS2）：
#   bash scripts/rehearse_first_day.sh <setup.exe 绝对路径> [端口]
# 环境变量：
#   STAGE 彩排根（默认 /tmp/cl-firstday，脚本独占，会先清空）
# 红线：全程沙箱重定向（USERPROFILE/LOCALAPPDATA），不触碰真实受管根；
#       只按 health 返回的 PID 收尾自己启动的客户端；不做登录、不填凭据。
set -uo pipefail
SETUP="${1:-}"; PORT="${2:-8821}"
[ -f "$SETUP" ] || { echo "usage: $0 <setup.exe> [port]"; exit 1; }
STAGE="${STAGE:-/tmp/cl-firstday}"
rm -rf "$STAGE"; mkdir -p "$STAGE/profile/AppData/Local" "$STAGE/install"

SANDBOX_ENV=(MSYS2_ARG_CONV_EXCL='*'
  USERPROFILE="$(cygpath -w "$STAGE/profile")"
  LOCALAPPDATA="$(cygpath -w "$STAGE/profile/AppData/Local")")

t0=$(date +%s)
env "${SANDBOX_ENV[@]}" "$SETUP" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART \
  "/DIR=$(cygpath -w "$STAGE/install")" "/LOG=$(cygpath -w "$STAGE/install.log")"
rc=$?; t1=$(date +%s)
[ "$rc" -eq 0 ] || { echo "INSTALL FAIL exit=$rc (see $STAGE/install.log)"; exit 1; }
echo "INSTALL OK dur=$((t1-t0))s exit=0"
MGR="$STAGE/profile/AppData/Local/CourseLens"
for d in data launcher state trust versions; do
  [ -d "$MGR/$d" ] || { echo "LAYOUT FAIL: missing $MGR/$d"; exit 1; }
done
echo "LAYOUT OK (data/launcher/state/trust/versions)"

env "${SANDBOX_ENV[@]}" powershell -NoProfile -ExecutionPolicy Bypass -File \
  "$(cygpath -w "$MGR/launcher/start_managed_courselens.ps1")" \
  -InstallRoot "$(cygpath -w "$MGR")" -Port "$PORT" -NoOpen > "$STAGE/launch.out" 2>&1 &
LP=$!
if grep -q "Another managed CourseLens launcher" "$STAGE/launch.out" 2>/dev/null; then
  echo "MUTEX BUSY: 本会话已有别的 CourseLens 启动器在跑（常见=其他车道的 pytest 集成腿）。"
  echo "启动器互斥 Local\\FudanCourseLensManagedUpdate 会串行化所有装机彩排——等几分钟重跑本脚本即可。"
  wait "$LP" 2>/dev/null; exit 2
fi
t2=$(date +%s); ok=""
for i in $(seq 1 40); do
  sleep 2
  curl -s --noproxy '*' -m 2 "http://127.0.0.1:$PORT/api/health" > "$STAGE/health.json" 2>/dev/null \
    && grep -q '"ok": *true' "$STAGE/health.json" && { ok=1; break; }
done
t3=$(date +%s)
if [ -z "$ok" ]; then
  echo "HEALTH FAIL (see $STAGE/launch.out)"
  PID=$(sed -n 's/.*"pid": \([0-9]*\).*/\1/p' "$STAGE/health.json" 2>/dev/null | head -1)
  [ -n "$PID" ] && taskkill //PID "$PID" //F >/dev/null 2>&1
  wait "$LP" 2>/dev/null; exit 1
fi
PID=$(sed -n 's/.*"pid": \([0-9]*\).*/\1/p' "$STAGE/health.json" | head -1)
VER=$(sed -n 's/.*"version": "\([^"]*\)".*/\1/p' "$STAGE/health.json" | head -1)
echo "HEALTH OK ready=$((t3-t2))s version=$VER pid=$PID"
echo "手工走查（可选）：浏览器开 http://127.0.0.1:$PORT 走首启向导→目录→设置（N9-R 口径 7 截图）"
taskkill //PID "$PID" //F >/dev/null 2>&1
wait "$LP" 2>/dev/null
echo "FIRSTDAY PASS (port $PORT, install+ready+layout 全绿; artifacts in $STAGE)"
