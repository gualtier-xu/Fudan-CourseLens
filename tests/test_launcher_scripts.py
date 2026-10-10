import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

LAUNCHER = ROOT / "start_fudan_courselens.ps1"
SETUP = ROOT / "scripts" / "setup_fudan_courselens_runtime.ps1"
ENTRY_CMD = ROOT / "OpenFudanCourseLens.cmd"
INSTALLER = ROOT / "scripts" / "install_managed_client.ps1"
MANAGED_LAUNCHER = ROOT / "scripts" / "start_managed_courselens.ps1"

LAUNCHER_PROBE_RE = re.compile(r'& \$PythonPath -c "([^"]+)"')
SETUP_PROBE_RE = re.compile(r'\$code = "([^"]+)"')


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_dependency_import_probes_stay_in_sync():
    launcher = LAUNCHER_PROBE_RE.search(_text(LAUNCHER)).group(1)
    setup = SETUP_PROBE_RE.search(_text(SETUP)).group(1)
    assert launcher.strip() == setup.strip()


def test_readiness_probes_import_the_production_application():
    launcher = LAUNCHER_PROBE_RE.search(_text(LAUNCHER)).group(1)
    setup = SETUP_PROBE_RE.search(_text(SETUP)).group(1)
    for probe in (launcher, setup):
        assert "from src.application import CourseLensApplication" in probe
        # import-only readiness: the probe must never construct the application
        assert "CourseLensApplication(" not in probe


def test_setup_probe_runs_from_project_root_without_bytecode():
    content = _text(SETUP)
    assert "Set-Location -LiteralPath $ProjectRoot" in content
    assert 'PYTHONDONTWRITEBYTECODE = "1"' in content


def test_open_launcher_is_the_single_double_click_entrypoint():
    content = _text(ENTRY_CMD)
    assert content.isascii()
    assert 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_fudan_courselens.ps1" %*' in content
    assert "StartFudanCourseLens.cmd" not in content
    assert not (ROOT / "StartFudanCourseLens.cmd").exists()
    swallow = content.index('-1073741510')
    pause = content.index("pause")
    assert swallow < pause


def test_open_launcher_has_one_failure_pause():
    content = _text(ROOT / "OpenFudanCourseLens.cmd")
    assert content.casefold().count("pause") == 1


def test_port_selection_falls_back_to_an_os_assigned_loopback_port():
    content = _text(LAUNCHER)
    function = re.search(
        r"function Find-AvailablePort \{(?P<body>.*?)\n\}\n\n\nfunction Test-PythonDependencies",
        content,
        re.DOTALL,
    )
    assert function
    body = function.group("body")
    assert "Test-PortAvailable -Port $port" in body
    assert "TcpListener]::new([System.Net.IPAddress]::Loopback, 0)" in body
    assert "$listener.LocalEndpoint.Port" in body
    assert "Configured ports $StartPort-$EndPort are unavailable" in body
    assert "$listener.Stop()" in body


def test_runtime_check_finishes_before_remote_configuration_summary():
    content = _text(LAUNCHER)
    check = re.search(
        r'if \(\$CheckRuntime\) \{\s*'
        r'Write-Step "Runtime check completed successfully"\s*'
        r'exit 0\s*\}',
        content,
    )
    assert check
    remote_summary = content.index(
        'Write-Step "Online service: $(Get-RemoteComputeSummary -PythonPath $PythonExe)"'
    )
    assert not content[check.end():remote_summary].strip()


def test_setup_fallback_download_is_tls_and_user_agent_hardened():
    content = _text(SETUP)
    fallback = content[content.index("} else {"):]
    assert "[Net.SecurityProtocolType]::Tls12" in fallback
    assert '-UserAgent "CourseLens-Setup/1"' in fallback
    assert "-UseBasicParsing" in fallback
    assert "$previousProtocol" in fallback


def test_installer_anchors_relative_roots_and_ships_bundled_python312():
    content = _text(INSTALLER)
    assert "[IO.Path]::IsPathRooted($InstallRoot)" in content
    # 卡⑦B (R-OP7): the bundled portable python312 tree ships as-is; the
    # installer must not copy or repair a venv anywhere.
    assert 'Join-Path $SourceRoot "tools\\python312"' in content
    assert "pyvenv.cfg" not in content
    assert ".venv-client-py310" not in content
    # the installer-only trust exclusion stays intact
    assert "client-update-trust.json" in content


def test_installer_allow_existing_empty_root_only_opens_empty_roots():
    content = _text(INSTALLER)
    assert "[switch]$AllowExistingEmptyRoot" in content
    # The empty-root gate must consult the switch AND the emptiness probe;
    # a non-existing check plus a non-empty refusal is the fail-closed shape.
    gate = content[content.index("if (Test-Path -LiteralPath $InstallRoot)"):content.index("$version =")]
    assert "$AllowExistingEmptyRoot" in gate
    assert "Get-ChildItem -LiteralPath $InstallRoot -Force | Select-Object -First 1" in gate
    assert 'throw "InstallRoot must not already exist"' in gate


def test_managed_launcher_runs_the_bundled_python312_runtime():
    content = _text(MANAGED_LAUNCHER)
    assert 'Join-Path $LauncherRoot "python312\\python.exe"' in content
    assert ".venv-client-py310" not in content


def test_installer_generates_the_pythonw_silent_host_inside_trust_boundary():
    """P66 INSTALLER-UX-1: the console-free shortcut chain is built by the
    managed installer itself. pythonw.exe (GUI subsystem) runs a GENERATED
    start_managed_courselens.pyw — no new repo file — whose powershell child
    runs the byte-identical managed launcher under a hidden-but-existing
    console (CREATE_NEW_CONSOLE + SW_HIDE): -NoNewWindow children inherit it,
    so no process in the tree can allocate a fresh visible console. The
    generation strictly precedes the trust manifest build, so the silent host
    is hashed like every other launcher asset, and launcher-chain refusals
    land in state\\last-launch.host.log instead of an invisible console."""
    content = _text(INSTALLER)
    assert "start_managed_courselens.pyw" in content
    assert "CREATE_NEW_CONSOLE" in content
    assert "STARTF_USESHOWWINDOW" in content
    assert "last-launch.host.log" in content
    assert '"-NoProfile",' in content
    assert '"-ExecutionPolicy",' in content
    assert '"-File",' in content
    # the host runs before the managed launcher can set PYTHONDONTWRITEBYTECODE,
    # so it must disable bytecode itself: stray __pycache__ files inside the
    # launcher tree would trip the trust walk ("untrusted asset") on launch
    assert "sys.dont_write_bytecode = True" in content
    # generation strictly precedes the trust manifest build
    assert content.index("start_managed_courselens.pyw") < content.index("$launcherFiles = ")
    # no silent host file ships in the repo: it exists only inside the
    # managed root, generated and trust-hashed at install time
    assert not (ROOT / "scripts" / "start_managed_courselens.pyw").exists()


def test_installer_upgrades_managed_roots_with_failclosed_guards():
    """N9-G U2: an existing non-empty root upgrades only when it is a genuine
    managed install; downgrades (and unreadable state) fail closed; the data
    root is never a removal target."""
    content = _text(INSTALLER)
    assert "$ManagedUpgrade" in content
    assert "courselens.managed-install.v1" in content
    assert 'state\\install-layout.json' in content
    # fail-closed: downgrade refusal + unreadable current state refusal
    assert "Managed upgrade refused: installed version" in content
    assert "Managed upgrade refused: state\\current.json is missing" in content
    assert "Managed upgrade refused: state\\current.json is unreadable" in content
    assert "Get-VersionTriple" in content
    # the same closed-set refusal stays for foreign roots (AppId-mismatch proxy)
    assert content.count('throw "InstallRoot must not already exist"') >= 2
    # refresh-in-place: launcher and the incoming slot are rebuilt; the data
    # directory is never removed anywhere in the script
    assert "$StaleLauncher" in content and "$StaleSlot" in content
    assert not re.search(r"Remove-Item[^\r\n]*\\\"?data", content)


def test_managed_launcher_captures_child_streams_to_state_logs():
    """N9-G U3: a failed launch must leave the child's own words on disk.
    stdout (plain-language hints) → state\last-launch.log; stderr (tracebacks)
    → state\last-launch.err.log; the python process stays the direct child."""
    content = _text(MANAGED_LAUNCHER)
    assert '"last-launch.log"' in content
    assert '"last-launch.err.log"' in content
    assert "-RedirectStandardOutput $logPath" in content
    assert "-RedirectStandardError $errLogPath" in content
    # -WindowStyle cannot combine with stream redirects (PS 5.1 hard rule);
    # the direct-child contract (health PID match) requires no cmd wrapper,
    # which the -RedirectStandard* pair below already implies.
    assert "-NoNewWindow" in content
    assert "-WindowStyle Hidden" not in content


def test_managed_launcher_trust_verify_hashes_in_parallel_runspace_pool():
    """N9-S LAUNCH-FAST-1: the trust segment keeps every inventory check
    inline (reparse point, untrusted asset) and every closed-set refusal
    byte-identical; only the per-file SHA256 moves into a runspace pool
    (Windows PowerShell 5.1 has no ForEach-Object -Parallel), bounded at
    8 workers, with one files/ms/workers telemetry line."""
    content = _text(MANAGED_LAUNCHER)
    # execution shape: runspace pool, .NET SHA256 inside the worker
    assert "RunspaceFactory]::CreateRunspacePool(" in content
    assert "[System.Security.Cryptography.SHA256]::Create()" in content
    # the per-file cmdlet hash is gone from the walk
    assert "Get-FileHash" not in content
    # the walk still rejects reparse points and untrusted assets inline,
    # before any hashing happens
    assert content.index('throw "Stable launcher contains an unsafe reparse point"') < content.index(
        "CreateRunspacePool"
    )
    assert content.index('throw "Stable launcher contains an untrusted asset"') < content.index(
        "CreateRunspacePool"
    )
    # every closed-set refusal survives verbatim
    assert 'throw "Stable launcher trust manifest is invalid"' in content
    assert 'throw "Stable launcher trust manifest is empty"' in content
    assert 'throw "Stable launcher asset failed validation"' in content
    assert 'throw "Stable launcher asset inventory is incomplete"' in content
    # a worker that cannot read a file fails closed, not open
    assert '$parts[0] -eq "E"' in content
    # bounded worker count + the single telemetry line
    assert "[Math]::Min(8, [Environment]::ProcessorCount)" in content
    assert '"[courselens] trust-verify: {0} files in {1:N0} ms (workers={2})"' in content


def test_trust_walk_refusal_is_never_a_silent_dead_double_click():
    """N15-W1（T1 F1）：走查 fail-closed 拒绝必须有窗级人话——catch 段四要素
    =host 日志行（闭集理由+具名 finding）/原生 MessageBoxW/最小权限具名框架
    文案/非零退出；拒绝 throw 消息族逐字不动，catch 只消费不改判。"""
    content = _text(MANAGED_LAUNCHER)
    walk_end = content.index("CreateRunspacePool")
    telemetry = content.index('"[courselens] trust-verify:')
    catch_start = content.index("} catch {", telemetry)
    catch = content[catch_start:]
    catch = catch[:catch.index("exit 2") + len("exit 2")]
    # catch 段在走查之后（只消费不参与判定）
    assert catch_start > telemetry
    # 四要素：host 日志行 / 原生窗 / 具名 finding 透传 / 非零退出
    assert "trust walk refused launch" in catch
    assert "MessageBoxW" in catch
    assert "$WalkFinding" in catch
    assert "exit 2" in catch
    # finding 在三处内联拒绝点被捕获（reparse/多出件/哈希不符），消息逐字未动
    assert content.count("$WalkFinding = ") >= 3
    assert 'throw "Stable launcher contains an untrusted asset"' in content
    assert 'throw "Stable launcher asset failed validation"' in content
    # 学生文案：最小权限+具名框架，含数据不受影响的诚实安抚与 reinstall 出路
    assert "启动前的安全检查没有通过" in catch
    assert "你的学习数据不受影响" in catch
    assert "重新安装 CourseLens" in catch
    assert walk_end > 0


def test_dotnet_sha256_matches_get_filehash_on_powershell_5_1():
    """N9-S mechanical proof on the real Windows PowerShell 5.1: the runspace
    worker's .NET digest is byte-equal to Get-FileHash's for the same bytes,
    so the parallel swap cannot change any trust verdict."""
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("powershell.exe is not available")
    script = textwrap.dedent(
        """
        $dir = Join-Path $env:TEMP ("cl-hashproof-{0}" -f [guid]::NewGuid())
        New-Item -ItemType Directory -Path $dir | Out-Null
        try {
            $mismatch = 0
            $checked = 0
            foreach ($spec in @("a.bin", "b.txt", "c.dat", "小写名.bin")) {
                $path = Join-Path $dir $spec
                $bytes = [byte[]]::new((Get-Random -Minimum 1 -Maximum 8192))
                [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
                [IO.File]::WriteAllBytes($path, $bytes)
                $cmdlet = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
                $sha = [System.Security.Cryptography.SHA256]::Create()
                $stream = [IO.File]::OpenRead($path)
                try {
                    $dotnet = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace("-", "").ToLowerInvariant()
                } finally { $stream.Dispose() }
                $sha.Dispose()
                $checked++
                if ($cmdlet -ne $dotnet) { $mismatch++ }
            }
            Write-Output ("CHECKED:" + $checked)
            Write-Output ("MISMATCH:" + $mismatch)
        } finally {
            Remove-Item -LiteralPath $dir -Recurse -Force
        }
        """
    )
    with tempfile.TemporaryDirectory(prefix="cl-hashproof-") as tmp:
        path = Path(tmp) / "proof.ps1"
        # UTF-8 BOM: Windows PowerShell 5.1 decodes BOM-less files as ANSI,
        # which corrupts the non-ASCII filename under test (P61 discipline).
        path.write_text(script, encoding="utf-8-sig")
        completed = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    output = completed.stdout + completed.stderr
    assert "CHECKED:4" in output, output
    assert "MISMATCH:0" in output, output


def test_managed_launcher_restarts_at_most_once_per_session_for_pending_switch():
    content = _text(MANAGED_LAUNCHER)
    loop = re.search(
        r"\$automaticRestarts = 0\r?\n\s*while \(\$true\) \{(?P<body>.*?)\r?\n\s*\}\r?\n\} finally \{",
        content,
        re.DOTALL,
    )
    assert loop, "the managed restart loop must carry the per-session counter"
    body = loop.group("body")
    # The verified switch applies at the top of every iteration, before any probe.
    assert body.index("& $PythonExe $Helper apply --install-root $InstallRoot") < body.index(
        "Start-And-Probe $active"
    )
    assert content.count("Wait-Process -Id") == 1
    # P68: opening is the server's job (--window / --no-open); the launcher
    # never opens a URL itself anywhere in the (re)start loop.
    assert 'Start-Process "http://127.0.0.1:$Port/"' not in body
    # After the service exits: restart only for a pending verified switch,
    # and the session counter hard-caps automatic restarts at one.
    guard = body[body.index("Wait-Process -Id $process.Id"):]
    assert 'if (-not (Test-Path -LiteralPath $pending -PathType Leaf)) { break }' in guard
    assert 'if ($automaticRestarts -ge 1) { break }' in guard
    assert '$automaticRestarts += 1' in guard


def test_managed_launcher_serves_without_keep_server_and_scans_fallback_ports():
    """P67 ZERO-CONSOLE-1 (DEF-1/DEF-2①): the managed launcher must not pass
    --keep-server (it silently disabled the frontend-session registry, so a
    closed browser page never ended the process — P67B live finding), and the
    fixed 6268 port must fall back to a bounded candidate band, then to a
    system-assigned port (the dev Find-AvailablePort idiom), so an occupied
    port can no longer turn a double-click into a dead refusal. P68
    NATIVE-WINDOW-1: the serve open mode is the launcher/server contract —
    interactive launches pass --window (the server opens its own native shell
    window), the -NoOpen daily task stays --no-open (headless)."""
    content = _text(MANAGED_LAUNCHER)
    assert '"-m", "src", "serve", "--port", "$Port", $serveOpenMode' in content
    assert '$serveOpenMode = if ($NoOpen) { "--no-open" } else { "--window" }' in content
    assert "--keep-server" not in content
    assert "[int]$Port = 6268" in content
    scan = re.search(
        r"function Find-ManagedAvailablePort \{(?P<body>.*?)\n\}", content, re.DOTALL
    )
    assert scan
    body = scan.group("body")
    assert "Test-ManagedPortAvailable -Port $port" in body
    assert "TcpListener]::new([System.Net.IPAddress]::Loopback, 0)" in body
    assert "$listener.LocalEndpoint.Port" in body
    # the serving port is re-resolved right before every start (restarts included)
    assert content.index("$Port = Find-ManagedAvailablePort -PreferredPort $Port") < content.index(
        "Start-And-Probe $active"
    )


def test_managed_launcher_repulls_the_page_when_another_instance_serves():
    """P67 U3c (DEF-2②): mutex rejection must never be a silent no-op. A
    second double-click surfaces the serving instance; the -NoOpen
    daily-schedule launch never opens one; both exit cleanly instead of
    refusing invisibly in state\\last-launch.host.log. P68: the serving
    instance shows its own native window, so the launcher focuses that
    window (closed-set title match) and keeps the URL re-pull only as the
    degraded-browser fallback."""
    content = _text(MANAGED_LAUNCHER)
    assert 'throw "Another managed CourseLens launcher is running"' not in content
    repull = content[content.index("if (-not $owns) {"):content.index("function Get-ActiveRoot")]
    assert "Find-ManagedServiceUrl -PreferredPort $Port" in repull
    assert "Find-ManagedWindowHandle" in repull
    assert "SetForegroundWindow" in repull
    assert "Start-Process $existingUrl" in repull
    assert "if (-not $NoOpen) {" in repull
    assert "exit 0" in repull
    # the re-pull probe only trusts the closed-set service identity: any 200
    # that does not say fudan-courselens is somebody else's port
    probe = re.search(
        r"function Find-ManagedServiceUrl \{(?P<body>.*?)\n\}", content, re.DOTALL
    )
    assert probe
    assert '$health.service -eq "fudan-courselens"' in probe.group("body")
    # the native focus helper is closed-set: it matches the server process
    # from THIS install root whose main window is titled CourseLens (never a
    # global user32 title search), and any miss returns Zero so the caller
    # falls back to the URL re-pull. Only the handle crosses P/Invoke —
    # string marshaling stays out of the launcher (PS 5.1 MemberDefinition
    # marshals DllImport string params ANSI-side, which silently breaks
    # every title comparison).
    focus = re.search(
        r"function Find-ManagedWindowHandle \{(?P<body>.*?)\n\}", content, re.DOTALL
    )
    assert focus
    assert 'StartsWith("$LauncherRoot\\python312\\")' in focus.group("body")
    assert '$_.MainWindowTitle -eq "CourseLens"' in focus.group("body")
    assert "return [IntPtr]::Zero" in focus.group("body")
    assert "FindWindow" not in focus.group("body")
    assert "FindWindow" not in repull


def test_daily_task_registration_is_windowless_and_pythonw_shaped():
    """P67 U2: the 07:30 task action is the pythonw silent host inside a
    managed install (the old powershell.exe action flashed a console on the
    student's desktop every morning); schtasks itself must not allocate a
    console under the console-less pythonw parent; the dev powershell form
    survives only for launcher-less roots."""
    scheduler = _text(ROOT / "src" / "runtime" / "scheduler_windows.py")
    assert 'getattr(subprocess, "CREATE_NO_WINDOW", 0)' in scheduler
    assert scheduler.count("creationflags=_CREATE_NO_WINDOW") == 2
    application = _text(ROOT / "src" / "application.py")
    block = application[
        application.index("def configure_daily_schedule"):
        application.index("def _load_daily_schedule_account")
    ]
    assert 'os.environ.get("COURSELENS_INSTALL_ROOT")' in block
    assert '"python312" / "pythonw.exe"' in block
    assert "start_managed_courselens.pyw" in block
    assert '-B "{silent_host}" -InstallRoot "{install_root}" -NoOpen' in block
    # the task XML is written under the working directory: it must land in
    # the managed root, never inside the trust-walked launcher tree
    # (P67 live-sandbox finding: a stray runtime/daily-sync-task.xml under
    # launcher\ bricks the task's next fire with "untrusted asset")
    assert "working_directory=str(Path(install_root))" in block
    assert "working_directory=str(launcher_root)" not in block
    # dev fallback: launcher-less roots keep the historical powershell action
    assert 'command="powershell.exe"' in block


def test_server_stderr_lines_are_stringified_before_tee():
    """NIGHT2-W5: Windows PowerShell 5.1 renders the ErrorRecords merged in by
    2>&1 as red console error text. The launcher must stringify every pipeline
    item before Tee-Object; the tee target and $LASTEXITCODE stay untouched."""
    content = _text(LAUNCHER)
    stage = '& $PythonExe @PythonArgs 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $serverLogPath'
    assert stage in content
    assert "$ErrorActionPreference = \"Continue\"" in content


def test_stderr_error_records_are_stringified_by_the_tee_stage_on_powershell_5_1():
    """NIGHT2-W5 mechanical proof on the real Windows PowerShell 5.1: the old
    pipeline shape yields ErrorRecord objects (rendered red in the console),
    the new shape yields plain strings; both tee files still carry the line."""
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("powershell.exe is not available")
    script = textwrap.dedent(
        """
        param([string]$PythonExe)
        $ErrorActionPreference = "Continue"
        $tmpOld = Join-Path $env:TEMP ("cl-stderr-old-{0}.log" -f [guid]::NewGuid())
        $tmpNew = Join-Path $env:TEMP ("cl-stderr-new-{0}.log" -f [guid]::NewGuid())
        try {
            $old = @(& $PythonExe -c "import sys; sys.stderr.write('boom-line\\n')" 2>&1 | Tee-Object -FilePath $tmpOld)
            $new = @(& $PythonExe -c "import sys; sys.stderr.write('boom-line\\n')" 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $tmpNew)
            Write-Output ("OLD:" + $old[0].GetType().Name)
            Write-Output ("NEW:" + $new[0].GetType().Name)
            Write-Output ("OLDLOG:" + ((Get-Content -LiteralPath $tmpOld -Raw) -match 'boom-line'))
            Write-Output ("NEWLOG:" + ((Get-Content -LiteralPath $tmpNew -Raw) -match 'boom-line'))
        } finally {
            Remove-Item -LiteralPath $tmpOld -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $tmpNew -ErrorAction SilentlyContinue
        }
        """
    )
    with tempfile.TemporaryDirectory(prefix="cl-stderr-proof-") as tmp:
        path = Path(tmp) / "proof.ps1"
        path.write_text(script, encoding="ascii")
        completed = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path), "-PythonExe", sys.executable],
            capture_output=True,
            text=True,
            timeout=120,
        )
    output = completed.stdout + completed.stderr
    assert "OLD:ErrorRecord" in output, output
    assert "NEW:String" in output, output
    assert "OLDLOG:True" in output, output
    assert "NEWLOG:True" in output, output


def test_managed_launcher_second_instance_fallback_covers_tray_hidden_windows():
    """夜10-A T1/T6：托盘隐藏窗（Form.Hide 后 MainWindowHandle=0）的拉起回退。

    静态钉：进程扫描未命中后的 EnumWindows 回退必须保留——属主校验是
    「本安装根 python312 的进程 pid」，标题恰好 CourseLens；绝不做全局
    标题搜索（P68 闭集身份不回退）。委托在 Add-Type 内声明（.NET Framework
    无内建 EnumWindowsProc），GetWindowText 必须 Unicode 口径。
    """
    content = _text(MANAGED_LAUNCHER)
    fallback = content[content.index("Find-ManagedWindowHandle"):]
    assert "EnumWindows" in fallback
    assert "EnumWindowsProc" in fallback
    assert "CharSet.Unicode" in fallback
    assert 'StartsWith("$LauncherRoot\python312\\")' in fallback
    assert 'if ($title.ToString() -eq "CourseLens")' in fallback
    # 回退只在进程扫描未命中时发生（先 MainWindowTitle 扫描，后 EnumWindows）。
    assert fallback.index("Get-Process python") < fallback.index("EnumWindows")


MANAGED_ARP_GUID = "{EE7FDDF2-E429-4E21-BEAE-FA0EB749FCB7}"
SANDBOX_ARP_GUID = "{11111111-2222-3333-4444-555555555555}"
SANDBOX_INNO_GUID = "{12345678-90AB-4CDE-8F01-234567890ABC}"
UNINSTALLER = ROOT / "scripts" / "uninstall_managed_client.ps1"


def test_managed_installer_ships_uninstaller_and_registers_arp_entry():
    """ARPFIX-1: the managed/dev install path must be uninstallable from
    Windows Settings. The Inno setup.exe shell always had its own entry, but
    direct managed installs (scripts/install_managed_client.ps1) left no
    trace. Pins: the uninstaller travels with the install (the entry must
    survive repository moves), the icon lands in the stable launcher before
    the trust manifest build (DisplayIcon survives version-slot rotation and
    the icon is trust-hashed like every launcher asset), the entry carries
    the full field set with idempotent upserts (upgrade/reinstall refreshes,
    never duplicates), Inno keeps sole ownership while its own entry exists,
    and registration can never fail the install."""
    content = _text(INSTALLER)
    assert "uninstall_managed_client.ps1" in content
    # the uninstaller copy precedes the registration call that points at it
    assert content.index('Join-Path $InstallRoot "uninstall_managed_client.ps1"') < content.index(
        "Register-ManagedUninstallEntry -InstallRoot"
    )
    # DisplayIcon lives in the stable launcher and is trust-hashed: the copy
    # must precede the trust manifest build. The icon source resolves the
    # shipped {app} root first (setup.exe payload layout, installer\ is not
    # shipped) and falls back to the repo/dev installer\ layout — a fresh
    # setup.exe install must never fail the managed layout at this copy.
    assert content.index('$IconSource = Join-Path $SourceRoot "courselens-icon.ico"') < content.index(
        "$launcherFiles = [ordered]@{"
    )
    assert 'Join-Path $SourceRoot "installer\\courselens-icon.ico"' in content
    assert 'Join-Path $Launcher "courselens-icon.ico"' in content
    # the managed entry never shares the Inno AppId key; the Inno key carries
    # Inno's default "_is1" suffix
    assert "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{EE7FDDF2-E429-4E21-BEAE-FA0EB749FCB7}" in content
    assert "{A2A00F7A-AD6C-4822-B23F-5155DDC320E0}_is1" in content
    for field in (
        "DisplayName", "DisplayVersion", "DisplayIcon", "InstallLocation",
        "UninstallString", "Publisher", "NoModify", "NoRepair",
        "SystemComponent", "InstallDate",
    ):
        assert '-Name "%s"' % field in content
    # every value write is an upsert: an upgrade refreshes in place
    for line in re.findall(r"New-ItemProperty[^\r\n]*", content):
        assert line.rstrip().endswith("-Force | Out-Null")
    assert "-PropertyType DWord" in content
    assert "System32\\WindowsPowerShell\\v1.0\\powershell.exe" in content
    # Inno coexistence + best-effort registration (never fails the install);
    # COPY-FIX-1: the pins follow the Chinese student-facing messages.
    assert "不重复登记" in content
    assert "没能登记卸载入口" in content
    # the data red line stays untouched by the new registration code
    assert not re.search(r"Remove-Item[^\r\n]*\\\"?data", content)


def test_managed_uninstaller_pins_data_red_line_and_closed_set_processes():
    """ARPFIX-1: the shipped uninstaller mirrors the Inno uninstaller's data
    promise (default keep; deletion only behind an explicit switch plus a
    typed second confirmation) and inherits the launcher's closed-set process
    identity: only executables under the install root are stopped, never a
    name-based sweep."""
    raw = UNINSTALLER.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "uninstall script must stay UTF-8 with BOM (P61)"
    content = raw.decode("utf-8-sig")
    # fail-closed fingerprint gate before any deletion
    assert "courselens.managed-install.v1" in content
    assert "拒绝卸载" in content
    # closed-set process identity; no taskkill sweep
    assert "Get-Process -Name python, pythonw" in content
    assert 'StartsWith($InstallRoot + "\\", [StringComparison]::OrdinalIgnoreCase)' in content
    assert "taskkill" not in content
    # exactly the four program subdirectories are removal targets
    assert '@("launcher", "versions", "state", "trust")' in content
    # data red line: default keeps; deletion only behind -RemoveData + typed Y
    assert "[switch]$RemoveData" in content
    assert '-ceq "Y"' in content
    assert "你的学习数据已完整保留" in content
    assert "不可恢复" in content
    # unreadable input (unattended run) must land in the keep branch
    confirm = content[content.index("Read-Host") - 200:]
    confirm = confirm[:confirm.index("没有确认删除")]
    assert "catch" in confirm
    assert '-ceq "Y"' in confirm
    # registry parity with the installer + daily task cleanup
    assert MANAGED_ARP_GUID in content
    assert 'Uninstall\\" + $ManagedUninstallKeyName' in content
    assert "FudanCourseLens-DailySync" in content


def _arp_key_path(guid: str) -> str:
    return r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\%s" % guid


def _run_powershell(args):
    powershell = shutil.which("powershell.exe")
    return subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass"] + list(args),
        capture_output=True, text=True, timeout=300, input="",
    )


def _build_sandbox_source(sandbox: Path, uninstaller_bytes: bytes) -> None:
    """A minimal fake SourceRoot with exactly the files the managed installer
    reads or copies (plus the patched uninstaller it ships)."""
    (sandbox / "scripts" / "splash").mkdir(parents=True)
    (sandbox / "tools" / "python312").mkdir(parents=True)
    (sandbox / "config").mkdir()
    (sandbox / "installer").mkdir()
    (sandbox / "courselens-version.json").write_text('{"version": "9.9.9"}', encoding="utf-8")
    for rel in (
        "tools/python312/python.exe",
        "scripts/client_update_helper.py",
        "scripts/start_managed_courselens.ps1",
        "scripts/splash/splash.pyw",
        "scripts/splash/splash.html",
        "config/client-update-trust.json",
    ):
        (sandbox / rel).write_bytes(b"stub")
    shutil.copyfile(ROOT / "installer" / "courselens-icon.ico", sandbox / "installer" / "courselens-icon.ico")
    (sandbox / "scripts" / "uninstall_managed_client.ps1").write_bytes(uninstaller_bytes)


def _arp_probe_command(key: str) -> str:
    return (
        "$k = Get-ItemProperty -LiteralPath '%s'; "
        "'NAME={0}|VER={1}|LOC={2}|ICON={3}|UNINS={4}|DWORDS={5}{6}{7}|DATE={8}' -f "
        "$k.DisplayName, $k.DisplayVersion, $k.InstallLocation, "
        "(Test-Path -LiteralPath $k.DisplayIcon), $k.UninstallString, "
        "$k.NoModify, $k.NoRepair, $k.SystemComponent, $k.InstallDate" % key
    )


def _arp_presence_command(key: str) -> str:
    return "if (Test-Path -LiteralPath '%s') { 'PRESENT' } else { 'ABSENT' }" % key


def test_managed_uninstall_round_trip_in_sandbox():
    """ARPFIX-1 mechanical proof on the real Windows PowerShell 5.1: install
    into throwaway roots under a throwaway HKCU key, then uninstall. Covers
    the entry fields, the idempotent same-version reinstall upsert, the
    default-keeps-data round trip, the -RemoveData unreadable-input fail-safe,
    the foreign-root refusal, and the Inno-ownership dedupe. Both sandbox
    registry keys are removed afterwards; the real machine's CourseLens
    install is never touched (all sandbox roots live in temp directories and
    every GUID is patched to a sandbox one)."""
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("powershell.exe is not available")
    install_source = _text(INSTALLER)
    # COPY-FIX-1: the installer now speaks Chinese to students exactly like
    # its uninstaller twin; both stay UTF-8 with BOM so Windows PowerShell 5.1
    # parses the Chinese messages correctly on every machine.
    assert install_source.startswith("\ufeff"), "the managed installer must keep its UTF-8 BOM (Chinese student-facing messages)"
    sandbox_install = install_source.replace(MANAGED_ARP_GUID, SANDBOX_ARP_GUID).replace(
        "{A2A00F7A-AD6C-4822-B23F-5155DDC320E0}", SANDBOX_INNO_GUID
    )
    sandbox_uninstaller = UNINSTALLER.read_bytes().replace(
        MANAGED_ARP_GUID.encode("ascii"), SANDBOX_ARP_GUID.encode("ascii")
    )
    # The BOM read back as \ufeff re-encodes to EF BB BF, so the patched
    # sandbox copy stays a valid BOM'd UTF-8 script.
    patched_installer_bytes = sandbox_install.encode("utf-8")
    managed_key = _arp_key_path(SANDBOX_ARP_GUID)
    inno_key = _arp_key_path(SANDBOX_INNO_GUID) + "_is1"

    def install_sandbox(source: Path, install_root: Path, work: Path):
        patched = work / "install.sandbox.ps1"
        patched.write_bytes(patched_installer_bytes)
        return _run_powershell(["-File", str(patched), "-SourceRoot", str(source), "-InstallRoot", str(install_root)])

    try:
        # --- root A: install -> entry fields -> same-version reinstall (upsert)
        with tempfile.TemporaryDirectory(prefix="cl-arp-a-") as tmp:
            out = Path(tmp)
            source = out / "source"
            install_root = out / "CourseLens"
            _build_sandbox_source(source, sandbox_uninstaller)
            first = install_sandbox(source, install_root, out)
            assert first.returncode == 0, first.stdout + first.stderr
            assert "登记好卸载入口" in first.stdout, first.stdout + first.stderr
            probe = _run_powershell(["-Command", _arp_probe_command(managed_key)])
            flat = (probe.stdout + probe.stderr).replace("\r", "")
            assert "NAME=CourseLens|VER=9.9.9" in flat, flat
            assert ("LOC=" + str(install_root)) in flat, flat
            assert "ICON=True" in flat, flat
            assert "powershell.exe" in flat and "uninstall_managed_client.ps1" in flat, flat
            assert "DWORDS=110" in flat, flat
            assert "DATE=20" in flat, flat
            assert (install_root / "uninstall_managed_client.ps1").is_file()
            assert (install_root / "launcher" / "courselens-icon.ico").is_file()
            # managed upgrade of the same version: the entry is refreshed in
            # place (still exactly one key, still correct fields)
            second = install_sandbox(source, install_root, out)
            assert second.returncode == 0, second.stdout + second.stderr
            probe = _run_powershell(["-Command", _arp_probe_command(managed_key)])
            flat = (probe.stdout + probe.stderr).replace("\r", "")
            assert "NAME=CourseLens|VER=9.9.9" in flat, flat
            # --- root A: plain uninstall keeps the data and removes the entry
            (install_root / "data").mkdir(exist_ok=True)
            (install_root / "data" / "notes.txt").write_text("learning", encoding="utf-8")
            uninstall = _run_powershell(["-File", str(install_root / "uninstall_managed_client.ps1")])
            assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr
            for gone in ("launcher", "versions", "state", "trust"):
                assert not (install_root / gone).exists(), gone
            assert (install_root / "data" / "notes.txt").exists(), "default uninstall must keep the data"
            presence = _run_powershell(["-Command", _arp_presence_command(managed_key)])
            assert "ABSENT" in presence.stdout, presence.stdout

        # --- root B: -RemoveData with unreadable input keeps the data
        with tempfile.TemporaryDirectory(prefix="cl-arp-b-") as tmp:
            out = Path(tmp)
            source = out / "source"
            install_root = out / "CourseLens"
            _build_sandbox_source(source, sandbox_uninstaller)
            installed = install_sandbox(source, install_root, out)
            assert installed.returncode == 0, installed.stdout + installed.stderr
            (install_root / "data").mkdir(exist_ok=True)
            (install_root / "data" / "notes.txt").write_text("learning", encoding="utf-8")
            declined = _run_powershell(
                ["-NonInteractive", "-File", str(install_root / "uninstall_managed_client.ps1"), "-RemoveData"]
            )
            assert declined.returncode == 0, declined.stdout + declined.stderr
            assert (install_root / "data" / "notes.txt").exists(), "unreadable confirmation input must keep the data"
            assert not (install_root / "launcher").exists()
            presence = _run_powershell(["-Command", _arp_presence_command(managed_key)])
            assert "ABSENT" in presence.stdout, presence.stdout

        # --- root C: a foreign root without the managed fingerprint is refused
        with tempfile.TemporaryDirectory(prefix="cl-arp-c-") as tmp:
            foreign = Path(tmp) / "not-courselens"
            (foreign / "state").mkdir(parents=True)
            (foreign / "state" / "important.txt").write_bytes(b"keep me")
            (foreign / "uninstall_managed_client.ps1").write_bytes(sandbox_uninstaller)
            refused = _run_powershell(["-File", str(foreign / "uninstall_managed_client.ps1")])
            assert refused.returncode == 1, refused.stdout + refused.stderr
            assert (foreign / "state" / "important.txt").exists(), "a foreign root must stay untouched"

        # --- root D: while the (sandbox) Inno entry exists, no duplicate is created
        with tempfile.TemporaryDirectory(prefix="cl-arp-d-") as tmp:
            out = Path(tmp)
            source = out / "source"
            install_root = out / "CourseLens"
            _build_sandbox_source(source, sandbox_uninstaller)
            seeded = _run_powershell(["-Command", "New-Item -Path '%s' -Force | Out-Null" % inno_key])
            assert seeded.returncode == 0, seeded.stdout + seeded.stderr
            dup = install_sandbox(source, install_root, out)
            assert dup.returncode == 0, dup.stdout + dup.stderr
            assert "不重复登记" in dup.stdout, dup.stdout + dup.stderr
            presence = _run_powershell(["-Command", _arp_presence_command(managed_key)])
            assert "ABSENT" in presence.stdout, presence.stdout
            presence = _run_powershell(["-Command", _arp_presence_command(inno_key)])
            assert "PRESENT" in presence.stdout, presence.stdout
    finally:
        for key in (managed_key, inno_key):
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Remove-Item -LiteralPath '%s' -Recurse -Force -ErrorAction SilentlyContinue" % key],
                capture_output=True, timeout=120, input="",
            )
