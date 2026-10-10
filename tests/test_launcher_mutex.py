"""第二十一案：launcher 单实例互斥+真重启语义（A2/N6W-ALL）。

钉四件事：已活实例时新启动=带前台+明确退出；-Restart 只在实例文件 pid +
健康身份 + 进程创建时刻三方一致时才停旧后端（否则 fail-closed）；退出时按
精确身份（本启动器直系 python 子进程）清点残留；实例证据改读数据根的
server-instance.json（lifecycle InstanceLock.publish 的真身路径）。

本文件与 tests/test_launcher_scripts.py 互不重叠：后者带 PKG1 未提交增量，
第二十一案的钉全部落在这一份新文件里。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "start_fudan_courselens.ps1"


def _text() -> str:
    return LAUNCHER.read_text(encoding="utf-8")


def _function(name: str) -> str:
    match = re.search(
        rf"function {re.escape(name)} \{{(?P<body>.*?)\n\}}\n",
        _text(),
        re.DOTALL,
    )
    assert match, f"function {name} missing from launcher"
    return match.group(0)


def test_restart_switch_and_foreground_attach_are_wired():
    content = _text()
    assert "[switch]$Restart" in content
    # 已活实例分支：先带前台，再明确退出；修复守卫保留
    branch = content[content.index("if ($runningUrl) {") : content.index("$mutex = [System.Threading.Mutex]")]
    assert 'Find-LauncherWindow -Title "FudanCourseLens $ProjectToken"' in branch
    assert "Stop the running service before repairing the runtime" in branch
    assert "The service is already running: $runningUrl" in branch
    assert "Open-ServiceUi -Url $runningUrl" in branch
    assert "exit 0" in branch
    assert "-Restart for a clean restart" in branch


def test_restart_stop_is_fail_closed_behind_triple_identity():
    body = _function("Stop-LiveInstance")
    # 身份链：实例文件 → instance_id 一致 → 健康端点同身份 → 进程创建时刻一致
    assert "Get-InstanceEvidence -DataRoot $DataRoot" in body
    assert "$evidence.instance_id -ne $ExpectedInstanceId" in body
    assert "Get-ServiceHealth -Port $port -ExpectedInstanceId $ExpectedInstanceId" in body
    identity = body.index("Test-InstanceProcessIdentity")
    kill = body.index("Stop-Process -Id $ownerPid -Force")
    assert identity < kill
    # 任一环失败：绝不动进程，只返回失败（调用侧给出人话退出）
    assert "return $false" in body


def test_identity_check_requires_python_and_creation_time_window():
    body = _function("Test-InstanceProcessIdentity")
    assert "$process.Name -notmatch '(?i)^python'" in body
    assert "ManagementDateTimeConverter]::ToDateTime" in body
    assert "ToUnixTimeMilliseconds() * 1000" in body
    assert "-le 2000000" in body


def test_instance_evidence_reads_the_data_root_file():
    body = _function("Get-InstanceEvidence")
    assert 'Join-Path $DataRoot "server-instance.json"' in body
    runner = re.search(
        r"Find-RunningService -StartPort \$PreferredPort.*",
        _text(),
    )
    assert runner, "Find-RunningService call site missing"
    assert "-DataRoot $DataRoot" in runner.group(0)
    # 旧的项目根死路径不得再出现
    assert 'runtime\\server-instance.json' not in _text()


def test_exit_sweep_only_touches_own_python_children():
    content = _text()
    body = _function("Stop-OwnBackendChildren")
    assert '"ParentProcessId=$PID"' in body
    assert "$_.Name -match '(?i)^python'" in body
    # 优雅排空宽限先于强杀
    grace = body.index("AddSeconds(15)")
    kill = body.index("Stop-Process -Id ([int]$child.ProcessId) -Force")
    assert grace < kill
    assert "Stop-OwnBackendChildren" in content[content.rindex("} finally {") :]


def test_window_title_carries_the_project_token():
    content = _text()
    assert '$Host.UI.RawUI.WindowTitle = "FudanCourseLens $ProjectToken"' in content
    # 已活实例分支用它做 FindWindow 的精确标题
    assert 'Find-LauncherWindow -Title "FudanCourseLens $ProjectToken"' in content


def _run_extracted_functions(names: list[str], calls: str) -> str:
    """Extract named launcher functions and exercise them in a real PowerShell.

    The launcher script cannot be dot-sourced (top-level side effects), so the
    harness copies just the requested function bodies into a scratch script.
    """
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("powershell.exe is not available")
    parts = "".join(_function(name) + "\n" for name in names)
    scratch_dir = Path(tempfile.mkdtemp(prefix="n6wu-mutex-"))
    script_path = scratch_dir / "n6wu-harness.ps1"
    script_path.write_text(parts + calls, encoding="utf-8-sig")
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(ROOT),
        )
    finally:
        script_path.unlink(missing_ok=True)
        scratch_dir.rmdir()
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_project_token_canonicalizes_junction_paths():
    """A2 邻接加固：junction/subst 路径启动时 token 与真实路径一致——
    Python 侧 PROJECT_INSTANCE_ID 用 Path.resolve() 规范化，两侧必须同源。"""
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("powershell.exe is not available")
    scratch = Path(tempfile.mkdtemp(prefix="n6wu-junction-"))
    junction = scratch / "n6wu-link"
    link = subprocess.run(
        ["cmd", "/c", "mklink /J {} {}".format(junction, ROOT)],
        capture_output=True, text=True,
    )
    if not junction.exists():
        pytest.skip("junction creation unavailable: " + link.stderr.strip())
    try:
        body = _function("Get-ProjectToken")
        calls = (
            "$token = Get-ProjectToken -Root '{}'\n"
            "$direct = Get-ProjectToken -Root '{}'\n"
            "Write-Output ('junction=' + $token)\n"
            "Write-Output ('direct=' + $direct)\n"
        ).format(junction, ROOT)
        script_path = scratch / "n6wu-token.ps1"
        script_path.write_text(body + calls, encoding="utf-8-sig")
        completed = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(ROOT),
        )
        assert completed.returncode == 0, completed.stderr
        out = completed.stdout
        junction_token = out.split("junction=")[1].split()[0]
        direct_token = out.split("direct=")[1].split()[0]
        assert junction_token == direct_token, out
    finally:
        subprocess.run(["cmd", "/c", "rmdir {}".format(junction)], capture_output=True)
        (scratch / "n6wu-token.ps1").unlink(missing_ok=True)
        scratch.rmdir()


def test_find_launcher_window_miss_is_quiet_and_false():
    out = _run_extracted_functions(
        ["Find-LauncherWindow"],
        'Write-Output ("miss=" + (Find-LauncherWindow -Title "n6wu-no-such-window"))\n',
    )
    assert "miss=False" in out


def test_identity_check_fails_closed_on_foreign_and_bogus_pids():
    out = _run_extracted_functions(
        ["Test-InstanceProcessIdentity"],
        textwrap.dedent(
            """
            $self = [int]$PID
            Write-Output ("self-zero=" + (Test-InstanceProcessIdentity -OwnerPid $self -StartedAtMicroseconds 0))
            Write-Output ("system=" + (Test-InstanceProcessIdentity -OwnerPid 4 -StartedAtMicroseconds 1))
            Write-Output ("bogus=" + (Test-InstanceProcessIdentity -OwnerPid 4000000 -StartedAtMicroseconds 1))
            """
        ),
    )
    assert "self-zero=False" in out
    assert "system=False" in out
    assert "bogus=False" in out
