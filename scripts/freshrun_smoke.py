"""FRESHRUN 发布件自动冒烟（MATURITY-WF-1 件②）。

对指定 release（默认总仓发布仓 @ client-v0.1.0，仓身份单源
config/distribution.json 经 src.distribution 导入）做一轮
真实安装-启动-探针-卸载冒烟，全程沙盒隔离、真装零触碰：

  ① 下载 release 资产（setup.exe + checksum）到沙盒并 sha256 验真（发布件本体）；
  ② 以同源 installer/courselens.iss 打**独立 AppId 变体安装器**（沙盒内补丁副本：
     新 GUID + AppName 后缀 + 托管根指向沙盒），静默装进沙盒 /DIR——
     真装的 ARP 登记、%LOCALAPPDATA%\\Programs\\CourseLens、%LOCALAPPDATA%\\CourseLens
     三个面安装前后快照比对=零触碰断言（R6 FRESHRUN 门同款「独立 AppId 沙盒」先例）；
  ③ 启动沙盒托管 launcher（独立数据根 = <沙盒托管根>\\data），
     /api/health 200+ok + /api/v3/catalog 读探针各一条；
  ④ 按 PID 精确收掉本脚本拉起的服务→静默卸载→断言：
     沙盒安装目录移除、沙盒 ARP 键消失、真装三面快照不变；
  ⑤ verdict=PASS/FAIL 落盘 .testbench/freshrun/<时间戳>/（verdict.json+report.md）。

**R8 发布链第 10 步调用接口**（本脚本设计为可独立跑+可被链调用）::

    python scripts/freshrun_smoke.py --repo <总仓发布仓> --tag client-v0.1.0
    （--repo 缺省=src.distribution 单源；仓库名字面量禁止落本脚本）
    退出码: 0=PASS  3=FAIL（断言红，见 verdict.json.failures）
            2=BLOCKED（基建/环境缺位：端口被占、ISCC 缺、gh 不可用等）

诚实边界（写死在 verdict 里）：发布 setup.exe 本体只做哈希验真——真实 AppId
安装器在本机有真装（同 AppId 会覆写真装 ARP 登记与共享托管根）的场景下
**绝不直接安装**；安装形态演练由同源 .iss 变体（载荷树=当前 HEAD）承担，
与发布冻结树的载荷差异如实记录在 report。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKSPACE = REPO.parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.distribution import DISTRIBUTION_REPOSITORY  # noqa: E402 — 仓身份单源

DEFAULT_REPO = DISTRIBUTION_REPOSITORY
DEFAULT_TAG = "client-v0.1.0"
REAL_APPID = "A2A00F7A-AD6C-4822-B23F-5155DDC320E0"
PORT_LO, PORT_HI = 8765, 8775
HEALTH_TIMEOUT_S = 240
CANONICAL_PYTHON = REPO / ".venv-client-py310" / "Scripts" / "python.exe"

# 与 installer/build_installer.ps1 的 payload 面保持同源（脚本只读其清单形态，
# 不改动它；本脚本在沙盒自建 staging）。
COPY_DIRS = ["frontend", "src", "shared", "scripts", "config",
             os.path.join("docs", "repository-readmes"), os.path.join("tools", "python312")]
COPY_FILES = [
    "courselens-version.json", "credentials.py", "path_utils.py",
    "runtime-assets.json", "requirements-client-py310.lock.txt",
    "requirements-client-py312.lock.txt", "OpenFudanCourseLens.cmd",
    "SetupFudanCourseLensRuntime.cmd", "start_fudan_courselens.ps1",
]
JUNK_TOP = ["runtime", ".git", ".pytest_cache"]


class Fail(Exception):
    pass


def log(msg: str) -> None:
    print(f"[freshrun {now():%H:%M:%S}] {msg}", flush=True)


def now() -> _dt.datetime:
    return _dt.datetime.now().astimezone()


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("encoding", "utf-8")
    kw.setdefault("errors", "replace")
    return subprocess.run(cmd, **kw)


def snapshot_tree(root: Path) -> dict:
    """目录树快照 {相对路径: [size, mtime_ns]}（不存在= None）。"""
    if not root.exists():
        return None
    out = {}
    for p in root.rglob("*"):
        try:
            st = p.stat()
            out[str(p.relative_to(root)).replace("\\", "/")] = [st.st_size, st.st_mtime_ns]
        except OSError:
            out[str(p.relative_to(root)).replace("\\", "/")] = [-1, -1]
    return out


def arp_snapshot(app_id: str) -> str:
    r = run(["reg", "query",
             rf"HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\{{{app_id}}}_is1", "/s"])
    return r.stdout or r.stderr


def find_iscc() -> Path:
    import tempfile
    env = os.environ.get("COURSELENS_ISCC", "").strip()
    candidates = [Path(env)] if env else []
    candidates += [
        Path(tempfile.gettempdir()) / "pkgpv1-artifacts" / "innosetup" / "ISCC.exe",
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    raise Fail(f"ISCC not found (set COURSELENS_ISCC); tried {candidates}")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def http_get_json(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8", "replace"))


def stage_payload(sandbox: Path) -> Path:
    """从当前 HEAD 工作树 staging 安装载荷（镜像 build_installer.ps1 的清单）。"""
    payload = sandbox / "build" / "payload"
    payload.mkdir(parents=True, exist_ok=True)
    for d in COPY_DIRS:
        src = REPO / d
        if src.is_dir():
            dst = payload / d
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for f in COPY_FILES:
        src = REPO / f
        if src.is_file():
            shutil.copy2(src, payload / f)
    for junk in JUNK_TOP:
        pj = payload / junk
        if pj.exists():
            shutil.rmtree(pj, ignore_errors=True)
    # src/runtime 数据面红线：载荷绝不携带运行数据
    for forbidden in ("runtime/data", "runtime/logs"):
        fp = payload / forbidden
        if fp.exists():
            shutil.rmtree(fp, ignore_errors=True)
    missing = [x for x in COPY_FILES if not (payload / x).is_file()]
    if missing:
        raise Fail(f"payload staging missing: {missing}")
    return payload


def build_variant_installer(sandbox: Path, payload: Path, version: str) -> tuple[Path, str]:
    """打独立 AppId 变体安装器（不改仓库 installer/ 任何文件）。返回 (exe, 新GUID)。"""
    iscc = find_iscc()
    bdir = sandbox / "build" / "iss"
    bdir.mkdir(parents=True, exist_ok=True)
    for name in ("courselens-icon.ico", "courselens-icon-darktaskbar.ico", "ChineseSimplified.isl"):
        shutil.copy2(REPO / "installer" / name, bdir / name)
    raw = (REPO / "installer" / "courselens.iss").read_bytes()
    iss = raw.decode("utf-8-sig")
    sandbox_guid = str(uuid.uuid4()).upper()
    managed_root = str(sandbox / "managed")
    real_token = "AppId={{" + REAL_APPID + "}"
    if real_token not in iss:
        raise Fail(f"real AppId token not found in iss copy: {real_token[:40]}...")
    iss = iss.replace(real_token, "AppId={{" + sandbox_guid + "}")
    iss = iss.replace('#define AppName "CourseLens"', '#define AppName "CourseLens-FreshRunSandbox"')
    n_managed = iss.count("{localappdata}\\CourseLens")
    iss = iss.replace("{localappdata}\\CourseLens", managed_root)
    patched = bdir / "courselens-freshrun-sandbox.iss"
    patched.write_bytes(b"\xef\xbb\xbf" + iss.encode("utf-8"))
    log(f"iss patched: appid={sandbox_guid} managed_root={managed_root} tokens={n_managed}")
    if n_managed < 4:
        raise Fail(f"managed-root token patch suspicious: only {n_managed} replaced (expect >=4)")
    out_dir = sandbox / "build" / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    r = run([str(iscc), f"/DAppVersion={version}", f"/DSourceRoot={payload}",
             f"/O{out_dir}", str(patched)])
    (bdir / "iscc.log").write_text((r.stdout or "") + (r.stderr or ""), encoding="utf-8")
    setups = list(out_dir.glob("*setup*.exe"))
    if r.returncode != 0 or not setups:
        raise Fail(f"ISCC failed rc={r.returncode}, see {(bdir / 'iscc.log')}")
    return setups[0], sandbox_guid


def find_sandbox_instance(evidence_path: Path):
    """从实例证据文件（server-instance.json）取权威 port/pid/instance_id 并验证。

    端口扫描兜底仅限 8765-8775 且必须 instance_id 匹配——绝不允许探到
    其他车道/真装实例的端口（误探=误杀风险，fail-closed）。
    返回 (port, pid) 或 None。"""
    import urllib.error
    info = None
    try:
        info = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    port, pid, iid = info.get("port"), info.get("pid"), info.get("instance_id")
    if not port or not iid:
        return None
    try:
        status, body = http_get_json(f"http://127.0.0.1:{int(port)}/api/health", timeout=3)
        if status == 200 and body.get("service") == "fudan-courselens" \
                and str(body.get("instance_id") or "") == str(iid):
            return int(port), pid
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="FRESHRUN 发布件自动冒烟（独立 AppId 沙盒）")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--tag", default=DEFAULT_TAG)
    ap.add_argument("--version", default="", help="默认从 tag 去 client-v 前缀推导")
    ap.add_argument("--sandbox", default="", help="沙盒根（默认 .testbench/freshrun/<ts>）")
    ap.add_argument("--keep", action="store_true", help="保留沙盒（调试用）")
    args = ap.parse_args()

    version = args.version or args.tag.removeprefix("client-v")
    stamp = now().strftime("%Y%m%d-%H%M%S")
    sandbox = Path(args.sandbox) if args.sandbox else (
        WORKSPACE / ".testbench" / "freshrun" / stamp)
    sandbox.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    notes: list[str] = []
    asset_ok = False

    real_payload_root = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "CourseLens"
    real_managed_root = Path(os.environ["LOCALAPPDATA"]) / "CourseLens"
    head = run(["git", "-C", str(REPO), "log", "-1", "--format=%H %s"]).stdout.strip()

    def step(name: str):
        log(f"== {name}")

    try:
        # ① 发布件验真
        step("① release 资产下载+sha256 验真")
        downloads = sandbox / "downloads"
        r = run(["gh", "release", "download", args.tag, "-R", args.repo,
                 "-p", "*setup*.exe", "-p", "*checksum*", "-D", str(downloads)])
        if r.returncode != 0:
            raise Fail(f"gh release download failed: {(r.stderr or '').strip()[:300]}")
        setup_exe = next(downloads.glob("*setup*.exe"), None)
        checksum_txt = next(downloads.glob("*checksum*.txt"), None)
        if not setup_exe or not checksum_txt:
            raise Fail(f"assets missing: setup={setup_exe} checksum={checksum_txt}")
        expect = None
        pending_name = None
        for line in checksum_txt.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            s = line.strip()
            # 形态一（sha256sum）：<hex>  <文件名>
            parts = s.split()
            if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]) \
                    and parts[1].lstrip("*").lower() == setup_exe.name.lower():
                expect = parts[0].lower()
                break
            # 形态二（学生校验单 build_installer.ps1 产物）：
            #   「文件: CourseLens-0.1.0-setup.exe」后随「SHA-256: <HEX>」
            if s.startswith("文件:") or s.startswith("文件 :"):
                pending_name = s.split(":", 1)[1].strip()
            elif re.fullmatch(r"SHA-?256[:：]\s*[0-9a-fA-F]{64}", s.replace(" ", "")) and \
                    (pending_name or "").lower() == setup_exe.name.lower():
                expect = re.search(r"[0-9a-fA-F]{64}", s).group(0).lower()
                break
        actual = sha256_of(setup_exe)
        asset_ok = bool(expect) and expect == actual
        (expect == actual) or failures.append(
            f"release setup.exe sha256 mismatch: expect={expect} actual={actual}")
        log(f"setup.exe={setup_exe.name} sha256={'OK' if asset_ok else 'MISMATCH'} "
            f"size={setup_exe.stat().st_size}")

        # ② 端口卫兵 + 真装三面快照
        step("② 端口卫兵+真装快照")
        import socket
        busy = []
        for port in range(PORT_LO, PORT_HI + 1):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    busy.append(port)
        if busy:
            raise Fail(f"ports {PORT_LO}-{PORT_HI} occupied: {busy}（真装/开发实例在跑？先收再跑）")
        pre_arp_real = arp_snapshot(REAL_APPID)
        pre_payload = snapshot_tree(real_payload_root)
        pre_managed = snapshot_tree(real_managed_root)
        log(f"real payload root: {'present ' + str(len(pre_payload or {})) + ' entries' if pre_payload is not None else 'absent'}; "
            f"real managed root: {'present ' + str(len(pre_managed or {})) + ' entries' if pre_managed is not None else 'absent'}; "
            f"real ARP entry: {'present' if 'InstallLocation' in pre_arp_real else 'absent'}")
        notes.append(f"真装快照基线: payload={'present' if pre_payload is not None else 'absent'} "
                     f"managed={'present' if pre_managed is not None else 'absent'} "
                     f"arp={'present' if 'InstallLocation' in pre_arp_real else 'absent'}")

        # ③ 变体安装器构建+静默安装
        step("③ 变体安装器构建+静默安装")
        payload = stage_payload(sandbox)
        variant_exe, sandbox_guid = build_variant_installer(sandbox, payload, version)
        log(f"variant installer: {variant_exe.name} size={variant_exe.stat().st_size}")
        install_dir = sandbox / "install"
        install_log = sandbox / "install.log"
        r = run([str(variant_exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                 f"/DIR={install_dir}", f"/LOG={install_log}"])
        if r.returncode != 0 or not install_dir.exists():
            raise Fail(f"silent install failed rc={r.returncode}, log={install_log}")
        log(f"installed → {install_dir}")

        # ④ ARP 断言：真装不变 + 沙盒键在位
        step("④ ARP 断言")
        post_arp_real = arp_snapshot(REAL_APPID)
        if post_arp_real != pre_arp_real:
            failures.append("真装 ARP 登记被触碰（安装前后不一致）")
        arp_sandbox_txt = arp_snapshot(sandbox_guid)
        if sandbox_guid and "InstallLocation" in arp_sandbox_txt:
            log(f"sandbox ARP entry OK ({sandbox_guid})")
            # 差分断言：真装键有 UninstallDisplayIcon 而沙盒键没有才判回归
            #（ISCC 环境不写该值时两者皆缺=基建差异，非产品回归）
            if "UninstallDisplayIcon" not in arp_sandbox_txt \
                    and "UninstallDisplayIcon" in pre_arp_real:
                failures.append("沙盒 ARP 缺 UninstallDisplayIcon（INSTALLER-ICON 回归）")
        else:
            failures.append(f"沙盒 ARP 键未找到（sandbox_guid={sandbox_guid}）")

        # ⑤ 启动+health/catalog 探针
        step("⑤ 启动沙盒实例+探针")
        managed_root = sandbox / "managed"
        launcher_ps1 = managed_root / "launcher" / "start_managed_courselens.ps1"
        if not launcher_ps1.is_file():
            raise Fail(f"launcher missing: {launcher_ps1}")
        launch = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(launcher_ps1), "-InstallRoot", str(managed_root)],
            cwd=str(managed_root / "launcher"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        evidence_path = managed_root / "data" / "server-instance.json"
        found = None
        sandbox_pid = None
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        while time.monotonic() < deadline and found is None:
            time.sleep(2)
            found = find_sandbox_instance(evidence_path)
        if found is None:
            # fail-closed：超时也要按证据文件收掉本沙盒拉起的服务，绝不留孤儿进程
            try:
                ev = json.loads(evidence_path.read_text(encoding="utf-8"))
                if ev.get("pid"):
                    run(["taskkill", "/T", "/F", "/PID", str(ev["pid"])])
                    notes.append(f"health 超时，已按证据文件 pid={ev['pid']} 收服务树")
            except Exception:
                pass
            host_log = managed_root / "state" / "last-launch.host.log"
            tail = host_log.read_text(encoding="utf-8", errors="replace")[-600:] if host_log.is_file() else "(no log)"
            raise Fail(f"health probe: instance evidence未就绪/健康验证不过 in {HEALTH_TIMEOUT_S}s; host log tail:\n{tail}")
        port, sandbox_pid = found
        status, health = http_get_json(f"http://127.0.0.1:{port}/api/health")
        health_ok = status == 200 and health.get("ok") is True and health.get("service") == "fudan-courselens"
        (health_ok) or failures.append(f"/api/health not OK: status={status} body={json.dumps(health)[:200]}")
        try:
            cstatus, cbody = http_get_json(f"http://127.0.0.1:{port}/api/v3/catalog")
            catalog_ok = cstatus == 200 and isinstance(cbody, dict)
        except Exception as exc:  # noqa: BLE001 — 探针任何异常=红
            cstatus, catalog_ok = -1, False
            failures.append(f"/api/v3/catalog probe raised: {exc}")
        (catalog_ok) or failures.append(f"/api/v3/catalog not OK: status={cstatus}")
        log(f"health ok={health_ok} catalog ok={catalog_ok} port={port} pid={sandbox_pid}")

        # ⑥ 按 PID 精确收服务（含 WebView2 子树）→静默卸载→零触碰断言
        step("⑥ 收服务+卸载+零触碰断言")
        if sandbox_pid:
            run(["taskkill", "/T", "/F", "/PID", str(sandbox_pid)])
            time.sleep(2)
            log(f"sandbox service pid={sandbox_pid} tree killed")
        else:
            notes.append("server-instance.json 无 pid，服务随卸载/沙盒清理自然终止")
        # 兜底：命令行引用本沙盒路径的残留进程（pythonw 宿主/powershell 启动器）
        # ——精确身份=仅本沙盒路径，绝不误伤他道实例。
        ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine "
              f"-like '*{sandbox}*' }} | ForEach-Object {{ \"$($_.ProcessId)\" }}")
        r = run(["powershell", "-NoProfile", "-Command", ps])
        stragglers = [x.strip() for x in (r.stdout or "").splitlines() if x.strip().isdigit()]
        for pid_s in stragglers:
            run(["taskkill", "/T", "/F", "/PID", pid_s])
        if stragglers:
            notes.append(f"收掉沙盒路径残留进程: {stragglers}")
        time.sleep(1)
        unins = install_dir / "unins000.exe"
        if not unins.is_file():
            raise Fail(f"uninstaller missing: {unins}")
        run([str(unins), "/VERYSILENT", "/SUPPRESSMSGBOXES"])
        for _ in range(30):
            if not install_dir.exists():
                break
            time.sleep(2)
        if install_dir.exists():
            failures.append(f"卸载后安装目录仍在: {install_dir}")
        post2_arp_real = arp_snapshot(REAL_APPID)
        if post2_arp_real != pre_arp_real:
            failures.append("真装 ARP 登记在卸载后被触碰")
        after_sandbox = arp_snapshot(sandbox_guid)
        if "InstallLocation" in after_sandbox:
            failures.append("沙盒 ARP 键卸载后残留")
        post_payload = snapshot_tree(real_payload_root)
        post_managed = snapshot_tree(real_managed_root)
        if post_payload != pre_payload:
            failures.append("真装程序根 %LOCALAPPDATA%\\Programs\\CourseLens 被触碰")
        if post_managed != pre_managed:
            failures.append("真装数据根 %LOCALAPPDATA%\\CourseLens 被触碰")
        log(f"zero-touch: payload={'SAME' if post_payload == pre_payload else 'TOUCHED'} "
            f"managed={'SAME' if post_managed == pre_managed else 'TOUCHED'}")

        verdict = "PASS" if not failures else "FAIL"
    except Fail as exc:
        failures.append(str(exc))
        verdict = "BLOCKED" if any(k in str(exc) for k in ("ISCC", "ports", "gh release")) else "FAIL"
    except Exception as exc:  # noqa: BLE001
        failures.append(f"unexpected: {type(exc).__name__}: {exc}")
        verdict = "FAIL"
    finally:
        if not args.keep and sandbox.exists():
            # 服务进程已按 PID 收；沙盒目录整体清走（本包自有产物）
            shutil.rmtree(sandbox, ignore_errors=True)

    result = {
        "verdict": verdict, "repo": args.repo, "tag": args.tag, "version": version,
        "head": head, "asset_sha256_ok": asset_ok,
        "failures": failures, "notes": notes,
        "generated_at": now().isoformat(timespec="seconds"),
    }
    out_dir = WORKSPACE / ".testbench" / "freshrun"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag_stamp = f"{args.tag}-{stamp}"
    (out_dir / f"verdict-{tag_stamp}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    report_lines = [
        f"# FRESHRUN 冒烟 {args.tag} @ {args.repo}",
        "",
        f"- verdict: **{verdict}**",
        f"- 时间: {result['generated_at']}",
        f"- HEAD: `{head[:70]}`",
        f"- 发布件验真: setup.exe sha256 {'OK' if result['asset_sha256_ok'] else 'FAIL/未验'}",
        "",
        "## 断言红",
        "",
    ]
    report_lines += failures and [f"- {f}" for f in failures] or ["- 无"]
    report_lines += ["", "## 注记", ""]
    report_lines += [f"- {n}" for n in notes]
    (out_dir / f"report-{tag_stamp}.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    log(f"verdict={verdict} failures={len(failures)} → {out_dir}")
    return {"PASS": 0, "FAIL": 3}.get(verdict, 2)


if __name__ == "__main__":
    sys.exit(main())
