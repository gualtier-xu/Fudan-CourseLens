#!/usr/bin/env bash
# CourseLens 安装包一键冒烟（构建→门禁→沙箱装机→健康探测→按 PID 收尾）。
#
# 出处：N9-A/A2 夜批实弹验证（2026-09-25，SMOKE PASS 全链）。完整手册见
# docs/packaging-chain.md；本脚本是其 §3-§4 的可执行版。
#
# 用法（Git Bash / MSYS2）：
#   bash scripts/smoke_installer.sh [版本] [端口]
# 环境变量：
#   ISCC   Inno Setup 命令行编译器路径（默认找 %TEMP%\pkgpv1-artifacts 便携版，
#          其次 Program Files 的标准安装）
#   REPO   仓库根（默认按本脚本位置自动推导）
# 前置：dev 树已备好 tools/python312（捆绑运行时，不在 git 里）。
# 红线：全程 %TEMP% 隔离，不触碰真实受管根；只读引用 installer/courselens.iss。
set -uo pipefail
VER="${1:-0.1.0}"; PORT="${2:-8808}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="${REPO:-$(cd "$SCRIPT_DIR/.." && pwd)}"
REPOW=$(cygpath -w "$REPO")
if [ -n "${ISCC:-}" ]; then
    :
elif [ -f "$TMP/pkgpv1-artifacts/innosetup/ISCC.exe" ] || [ -f "/tmp/pkgpv1-artifacts/innosetup/ISCC.exe" ]; then
    ISCC="/tmp/pkgpv1-artifacts/innosetup/ISCC.exe"
else
    ISCC="/c/Program Files (x86)/Inno Setup 6/ISCC.exe"
fi
[ -f "$ISCC" ] || { echo "ISCC not found: $ISCC (set ISCC=...)"; exit 1; }

T=/tmp/cl-smoke; rm -rf "$T"; mkdir -p "$T/staging" "$T/payload/docs" "$T/out" "$T/profile/AppData/Local"
EPOCH=$(git -C "$REPO" log -1 --format=%ct)   # mtime 钉提交时刻=构建可复现（手册 §8）

git -C "$REPO" archive --format=tar HEAD > "$T/head.tar"
tar -xf "$T/head.tar" -C "$T/staging"
for d in frontend src shared scripts config docs/repository-readmes; do cp -r "$T/staging/$d" "$T/payload/$d"; done
for f in courselens-version.json credentials.py path_utils.py runtime-assets.json \
         requirements-client-py310.lock.txt requirements-client-py312.lock.txt \
         OpenFudanCourseLens.cmd SetupFudanCourseLensRuntime.cmd start_fudan_courselens.ps1; do
  cp "$T/staging/$f" "$T/payload/$f"
done
# 捆绑运行时是唯一「非 HEAD 树」输入（官方构建同样取自工作树）
MSYS2_ARG_CONV_EXCL='*' robocopy "$REPOW\tools\python312" "$(cygpath -w "$T/payload/tools/python312")" /E /MT:8 /NFL /NDL /NJH /NP /R:1 /W:1
rc=$?; [ "$rc" -le 7 ] || { echo "robocopy rc=$rc"; exit 1; }
find "$T/payload" -exec touch -d "@$EPOCH" {} +

# 暂存门禁（同 build_installer.ps1 口径）：必备件 + 泄漏扫描
PYTHONDONTWRITEBYTECODE=1 "$REPO/tools/python312/python.exe" - "$REPO" "$(cygpath -w "$T/payload")" <<'PYEOF'
import sys
from pathlib import Path
repo, payload = Path(sys.argv[1]), Path(sys.argv[2])
required = ["courselens-version.json", r"tools\python312\python.exe",
    r"scripts\install_managed_client.ps1", r"scripts\start_managed_courselens.ps1",
    r"scripts\client_update_helper.py", r"config\client-update-trust.json",
    "requirements-client-py312.lock.txt", "start_fudan_courselens.ps1",
    "OpenFudanCourseLens.cmd", "SetupFudanCourseLensRuntime.cmd",
    "credentials.py", "path_utils.py", "runtime-assets.json", "frontend", "src", "shared"]
forbidden_dirs = [r"runtime\data", r"runtime\logs", ".git", ".venv-client-py310", ".venv-client-py312"]
failures = [f"missing {i}" for i in required if not (payload / i).exists()]
failures += [f"forbidden dir {i}" for i in forbidden_dirs if (payload / i).exists()]
for p in payload.rglob("*"):
    if not p.is_file():
        continue
    rel = p.relative_to(payload)
    if "site-packages" in rel.parts:
        continue
    if p.suffix.lower() in {".pfx", ".pem", ".key", ".sig", ".nupkg"}:
        failures.append(f"secret artifact {rel}")
    if p.suffix.lower() in {".py", ".ps1", ".json", ".txt", ".js"} and p.stat().st_size < 4_000_000:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("-----BEGIN") and "PRIVATE KEY-----" in line:
                failures.append(f"key material {rel}")
                break
files = sum(1 for p in payload.rglob("*") if p.is_file())
mb = sum(p.stat().st_size for p in payload.rglob("*") if p.is_file()) / 1048576
print(f"payload files={files} size={mb:.1f}MB")
if failures:
    print("GATES: FAIL"); [print(" -", f) for f in failures[:10]]; sys.exit(1)
print("GATES: OK")
PYEOF
[ $? -eq 0 ] || exit 1

MSYS2_ARG_CONV_EXCL='*' "$ISCC" "/DAppVersion=$VER" "/DSourceRoot=$(cygpath -w "$T/payload")" "/O$(cygpath -w "$T/out")" "$REPOW\installer\courselens.iss" > "$T/out/iscc.log" 2>&1
grep -q "Successful compile" "$T/out/iscc.log" || { echo "ISCC failed (see $T/out/iscc.log)"; exit 1; }
SETUP="$T/out/CourseLens-$VER-setup.exe"
echo "SHA256: $(sha256sum "$SETUP" | cut -d' ' -f1)"

# 沙箱静默安装：重定向 USERPROFILE/LOCALAPPDATA，让 {localappdata} 全落沙箱
MSYS2_ARG_CONV_EXCL='*' USERPROFILE="$(cygpath -w "$T/profile")" LOCALAPPDATA="$(cygpath -w "$T/profile/AppData/Local")" \
  "$SETUP" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART "/DIR=$(cygpath -w "$T/install")" "/LOG=$(cygpath -w "$T/install.log")"
rc=$?; [ "$rc" -eq 0 ] || { echo "setup exit=$rc"; exit 1; }
MGR="$T/profile/AppData/Local/CourseLens"
MSYS2_ARG_CONV_EXCL='*' USERPROFILE="$(cygpath -w "$T/profile")" LOCALAPPDATA="$(cygpath -w "$T/profile/AppData/Local")" \
  powershell -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$MGR/launcher/start_managed_courselens.ps1")" \
  -InstallRoot "$(cygpath -w "$MGR")" -Port "$PORT" -NoOpen > "$T/launch.out" 2>&1 &
LP=$!
ok=""
for i in $(seq 1 40); do sleep 2; curl -s --noproxy '*' -m 2 "http://127.0.0.1:$PORT/api/health" > "$T/health.json" 2>/dev/null && grep -q '"ok": *true' "$T/health.json" && { ok=1; break; }; done
grep -q '"ok": *true' "$T/health.json" 2>/dev/null || { echo "health never ok (see $T/launch.out)"; kill "$LP" 2>/dev/null; exit 1; }
echo "HEALTH: $(cat "$T/health.json")"
PID=$(sed -n 's/.*"pid": \([0-9]*\).*/\1/p' "$T/health.json" | head -1)
taskkill //PID "$PID" //F > /dev/null 2>&1
wait "$LP" 2>/dev/null
echo "SMOKE PASS (port $PORT, pid $PID stopped; artifacts in $T)"
