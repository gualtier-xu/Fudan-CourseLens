param(
    [Parameter(Mandatory = $true)][string]$SourceRoot,
    [Parameter(Mandatory = $true)][string]$InstallRoot,
    [switch]$AllowExistingEmptyRoot,
    # INSTALL-FIX-1: the Inno installer splits one long hidden run into three
    # real-completion phases so the wizard can show honest progress per stage.
    # "all" (default) keeps the single-shot behavior for direct/dev installs
    # and the sandbox round-trip tests.
    [string]$Phase = "all"
)

# ARPFIX-1 (2026-10): the managed/dev install path now ships its own
# uninstaller. scripts/uninstall_managed_client.ps1 is copied to the install
# root and a per-user Add/Remove-Programs entry is registered under HKCU
# (idempotent on every reinstall; automatically withdrawn while the Inno
# setup.exe install owns this machine, so Settings never shows a duplicate).
# The uninstaller stops only processes whose executable lives under the
# install root, removes launcher/versions/state/trust, and keeps data/ by
# default (-RemoveData plus a typed confirmation to delete) - the same data
# red line as the Inno uninstaller ([Code] CurUninstallStepChanged) and the
# deliberately empty [UninstallDelete] section.
#
# INSTALL-FIX-1 (2026-10): the reported "正在布置课程资料目录…" stall had two
# measured root causes - Copy-Item -Recurse on the 4100-file bundled runtime
# (~2.6x slower than robocopy /MT) and per-file Get-FileHash over the same
# tree (~12 ms/file under real-time AV: 59 s for 4125 files). Fixes shipped
# here: robocopy for both big tree copies; the trust manifests hash through a
# runspace pool (the audited N9-S LAUNCH-FAST-1 launch pattern, measured
# 15.2s -> 1.5s there); the process-level pre-check below closes the gap the
# fixed-port health probes left open (instances on system-assigned ports were
# invisible to InitializeSetup); and the managed-install layout marker is
# written BEFORE the first long copy, so a cancelled or power-lost install is
# recognized as our interrupted half-done install and cleaned up + redone on
# the next run instead of being refused as a foreign root.

$ErrorActionPreference = "Stop"
# The student-facing messages below are Chinese: switch the console to UTF-8
# so redirected/captured output (and modern consoles) read them correctly.
# Guarded: a host without a writable console must never fail the install.
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # Keep installing; the messages still print, worst case with legacy encoding.
}
if (@("all", "runtime", "program", "finalize") -notcontains $Phase) {
    throw "Unknown install phase '$Phase' (expected all/runtime/program/finalize)"
}
$SourceRoot = (Resolve-Path -LiteralPath $SourceRoot).Path
if (-not [IO.Path]::IsPathRooted($InstallRoot)) {
    $InstallRoot = Join-Path (Get-Location -PSProvider FileSystem).ProviderPath $InstallRoot
}
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$RoboCopyExe = Join-Path $env:SystemRoot "System32\robocopy.exe"
if (-not (Test-Path -LiteralPath $RoboCopyExe -PathType Leaf)) {
    throw "robocopy.exe was not found under SystemRoot; cannot deploy safely"
}
# Three-state root gate (N9-G INSTALLER-POLISH-1 U2):
#   missing / existing-empty-with-switch -> first install (unchanged);
#   existing non-empty WITH a valid managed-install layout -> managed upgrade,
#     or - NEW (INSTALL-FIX-1) - our own interrupted install (the layout
#     marker is written before any long copy, so a valid layout without a
#     readable current.json can only be a half-done CourseLens install;
#     the partial product trees are wiped and the run proceeds as a first
#     install instead of refusing the student a retry);
#   anything else -> the same fail-closed refusal as before (a foreign root
#   that merely squats on the path must never be adopted).
$ManagedUpgrade = $false
$InterruptedInstall = $false
if (Test-Path -LiteralPath $InstallRoot) {
    # R-OP7 (Card 7B): the bundled runtime is a relocated portable Python, so a
    # fresh install no longer needs to write a venv. Inno pre-creates the
    # destination directory, so an existing EMPTY root is accepted only when
    # -AllowExistingEmptyRoot is passed; a non-empty root is upgraded only when
    # it is a genuine managed install (layout schema check below).
    $empty = -not (Get-ChildItem -LiteralPath $InstallRoot -Force | Select-Object -First 1)
    if ($empty) {
        if (-not $AllowExistingEmptyRoot) {
            throw "InstallRoot must not already exist"
        }
    } else {
        $LayoutPath = Join-Path $InstallRoot "state\install-layout.json"
        $Layout = $null
        if (Test-Path -LiteralPath $LayoutPath -PathType Leaf) {
            try {
                $Layout = Get-Content -Raw -Encoding UTF8 $LayoutPath | ConvertFrom-Json
            } catch {
                $Layout = $null
            }
        }
        if ($null -eq $Layout -or $Layout.schema -ne "courselens.managed-install.v1") {
            throw "InstallRoot must not already exist"
        }
        $CurrentPath = Join-Path $InstallRoot "state\current.json"
        if (-not (Test-Path -LiteralPath $CurrentPath -PathType Leaf)) {
            # INSTALL-FIX-1 cancel-safety: layout present + no current.json =
            # our own interrupted install (markers are written before any long
            # copy). Wipe the partial product trees below and rebuild; the
            # data directory is never a wipe target.
            $InterruptedInstall = $true
        } else {
            $ManagedUpgrade = $true
        }
    }
}
$version = (Get-Content -Raw -Encoding UTF8 (Join-Path $SourceRoot "courselens-version.json") | ConvertFrom-Json).version

function Get-VersionTriple([string]$Value) {
    # Closed-set version shape: dot-separated unsigned integers (0.1.0).
    # Anything else fails closed instead of guessing an ordering.
    $parts = @([string]$Value).Split(".")
    if ($parts.Count -lt 2 -or $parts.Count -gt 4) {
        throw "Version '$Value' does not match the expected Major.Minor.Patch shape"
    }
    $numbers = @()
    foreach ($part in $parts) {
        if ($part -notmatch "^[0-9]+$") {
            throw "Version '$Value' does not match the expected Major.Minor.Patch shape"
        }
        $numbers += [int]$part
    }
    return $numbers
}

function Assert-NoCourseLensProcessUnderInstallRoot {
    # INSTALL-FIX-1 lock pre-check: a running instance served from the install
    # root locks the very trees this script rebuilds (the fixed-port health
    # probes in InitializeSetup cannot see instances on system-assigned
    # ports). Closed-set identity like the shipped uninstaller: only
    # executables UNDER the install root count, never a name-based sweep.
    $prefix = $InstallRoot.TrimEnd("\") + "\"
    $running = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) })
    if ($running.Count -gt 0) {
        $names = (@($running | ForEach-Object { $_.Name } | Select-Object -Unique) -join ", ")
        throw ("CourseLens 正在运行，暂时无法安装或升级。请先完全退出 CourseLens" +
            "（右键点击屏幕右下角托盘里的 CourseLens 图标 → 退出），再重新运行安装。" +
            "你的学习数据不会受影响。（检测到 {0} 个来自安装目录的进程：{1}）" -f $running.Count, $names)
    }
}

function Copy-TreeWithRobocopy {
    # INSTALL-FIX-1: Copy-Item -Recurse measured ~2.6x slower than robocopy
    # /MT on the bundled runtime tree. robocopy exit codes are a bitmask and
    # values below 8 (1 = copied, 2 = extras, 4 = mismatches) are SUCCESS;
    # only >= 8 means real failure.
    param([string]$From, [string]$To, [string[]]$ExcludeDirs = @())
    $roboArgs = @($From, $To, "/E", "/MT:16", "/NP", "/NFL", "/NDL", "/R:1", "/W:1")
    foreach ($dir in $ExcludeDirs) { $roboArgs += @("/XD", $dir) }
    & $RoboCopyExe @roboArgs | Out-Null
    $code = $LASTEXITCODE
    if ($code -ge 8) {
        throw ("文件部署失败（robocopy 退出码 {0}）。请关闭可能占用文件的其他程序后重试；你的学习数据不受影响。" -f $code)
    }
}

function Get-ManagedLayoutOrThrow {
    # Phases program/finalize continue a run whose runtime phase already
    # passed the full gate; they only require our layout marker on disk.
    $LayoutPath = Join-Path $InstallRoot "state\install-layout.json"
    if (-not (Test-Path -LiteralPath $LayoutPath -PathType Leaf)) {
        throw "安装状态异常：找不到安装布局标记。请从头重新运行安装程序；你的学习数据不受影响。"
    }
}

function Write-InstallStateMarkers {
    # INSTALL-FIX-1 cancel-safety: BOTH markers are written BEFORE the first
    # long copy. The layout marker is what a later run's gate recognizes as
    # "this is our product tree"; the in-progress marker documents the run.
    # Any cancel/power loss after this point leaves a state the next run
    # auto-recovers (wipe partial product trees, rebuild), never a refusal.
    $StateRoot = Join-Path $InstallRoot "state"
    @{ schema = "courselens.managed-install.v1" } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $StateRoot "install-layout.json") -Encoding UTF8
    @{ schema = "courselens.install-inprogress.v1"; version = $version; started_utc = (Get-Date).ToUniversalTime().ToString("o") } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $StateRoot "install-inprogress.json") -Encoding UTF8
}

# Byte sums accumulated by the two manifest walks; the ARP entry's
# EstimatedSize uses these instead of a third full-tree walk of the
# finished install root.
$script:LauncherBytes = [long]0
$script:VersionBytes = [long]0
$script:LauncherFileCount = 0

function Invoke-RuntimePhase {
    # ---- full gate already ran above; check locks, prepare the root ----
    Assert-NoCourseLensProcessUnderInstallRoot
    $Launcher = Join-Path $InstallRoot "launcher"
    $VersionRoot = Join-Path $InstallRoot ("versions\" + $version)
    $StateRoot = Join-Path $InstallRoot "state"
    $TrustRoot = Join-Path $InstallRoot "trust"
    # -Force: on the upgrade path state/, trust/ and data/ already exist and must
    # be adopted as-is (the first install created them; New-Item without -Force
    # would throw ResourceExists and abort a valid upgrade mid-flight).
    New-Item -ItemType Directory -Force -Path $Launcher, $VersionRoot, $StateRoot, $TrustRoot, (Join-Path $InstallRoot "data") | Out-Null
    Write-InstallStateMarkers
    if ($InterruptedInstall) {
        # INSTALL-FIX-1 cancel-safety: wipe ONLY the partial product trees the
        # interrupted run was building (launcher, trust, the incoming version
        # slot). Older version slots (the update chain's rollback surface),
        # state and the data directory are never touched.
        Write-Host "检测到上一次安装没有完成，正在自动清理后重新安装（学习数据不受影响）…"
        foreach ($partial in @($Launcher, $TrustRoot, $VersionRoot)) {
            if (Test-Path -LiteralPath $partial) {
                Remove-Item -LiteralPath $partial -Recurse -Force
            }
        }
        New-Item -ItemType Directory -Force -Path $Launcher, $TrustRoot | Out-Null
    }
    if ($ManagedUpgrade) {
        # AppId-mismatch / non-managed-root refusals happened above; the remaining
        # fail-closed guard for an upgrade is the version comparison against the
        # installed slot: a downgrade (or an unreadable current state) is refused.
        $CurrentPath = Join-Path $InstallRoot "state\current.json"
        try {
            $Current = Get-Content -Raw -Encoding UTF8 $CurrentPath | ConvertFrom-Json
        } catch {
            throw "Managed upgrade refused: state\current.json is unreadable"
        }
        $InstalledVersion = [string]$Current.version
        $Installed = Get-VersionTriple $InstalledVersion
        $Incoming = Get-VersionTriple $version
        $Comparison = 0
        for ($index = 0; $index -lt [Math]::Max($Installed.Count, $Incoming.Count); $index++) {
            $left = if ($index -lt $Installed.Count) { $Installed[$index] } else { 0 }
            $right = if ($index -lt $Incoming.Count) { $Incoming[$index] } else { 0 }
            if ($left -ne $right) {
                if ($left -gt $right) { $Comparison = 1 } else { $Comparison = -1 }
                break
            }
        }
        if ($Comparison -eq 0) {
            Write-Host "检测到同版本（$version）已经装过，这次原地刷新程序，学习数据原地不动"
        }
        if ($Comparison -gt 0) {
            throw "Managed upgrade refused: installed version $InstalledVersion is newer than $version (downgrade)"
        }
        # Refresh-in-place preparation: the stable launcher and the incoming
        # version slot are rebuilt from SourceRoot; existing version slots (the
        # update chain's rollback surface), state and data are never touched.
        $StaleLauncher = Join-Path $InstallRoot "launcher"
        if (Test-Path -LiteralPath $StaleLauncher) {
            Remove-Item -LiteralPath $StaleLauncher -Recurse -Force
        }
        $StaleSlot = Join-Path $InstallRoot "versions\$version"
        if (Test-Path -LiteralPath $StaleSlot) {
            Remove-Item -LiteralPath $StaleSlot -Recurse -Force
        }
        New-Item -ItemType Directory -Force -Path $Launcher | Out-Null
    }
    # ---- runtime deployment: the bundled portable interpreter travels as-is.
    # This is a controlled pilot installer, not a public installer. It copies
    # the already-validated project-local runtime and source tree without
    # invoking a system Python or downloading executable code. The bundled
    # runtime is the portable python312 tree with the client dependencies
    # installed in-place, so nothing here rewrites interpreter paths and no
    # per-machine relocation step exists (R-OP7: Chinese-username machines
    # install exactly like ASCII ones).
    # Layout contract: the old Copy-Item ran with an EXISTING destination dir
    # (New-Item above), so its semantics were "copy INTO" - the installed
    # shape is launcher\python312\python.exe (the .iss shortcuts and the
    # managed launcher both pin launcher\python312\pythonw.exe). robocopy
    # reproduces that nested shape exactly; a flat copy would brick the
    # launcher chain. ----
    Write-Host "正在部署运行时环境（通常几十秒内完成）…"
    Copy-TreeWithRobocopy -From (Join-Path $SourceRoot "tools\python312") -To (Join-Path $Launcher "python312")
    Copy-Item -LiteralPath (Join-Path $SourceRoot "scripts\client_update_helper.py") -Destination $Launcher
    Copy-Item -LiteralPath (Join-Path $SourceRoot "scripts\start_managed_courselens.ps1") -Destination $Launcher
    # LAUNCH-FIX-1 C: the boot splash ships inside the trust boundary. The
    # carrier (splash.pyw) and its single-file HTML asset are hashed by the
    # launcher-trust manifest built below, so the launcher pre-walk quick
    # check verifies exactly what it is about to execute; WebView2 runs the
    # splash in private mode with a TEMP user-data folder, so the splash can
    # never drop files into this tree and trip a later trust walk.
    New-Item -ItemType Directory -Force -Path (Join-Path $Launcher "splash") | Out-Null
    Copy-Item -LiteralPath (Join-Path $SourceRoot "scripts\splash\splash.pyw") -Destination (Join-Path $Launcher "splash")
    Copy-Item -LiteralPath (Join-Path $SourceRoot "scripts\splash\splash.html") -Destination (Join-Path $Launcher "splash")
    # ARPFIX-1: the product icon travels into the stable launcher so the ARP
    # entry registered below can point its DisplayIcon at a path that survives
    # update-chain version-slot rotation. The copy precedes the trust manifest
    # build, so the icon is hashed like every other launcher asset.
    # The icon exists at two layouts: the setup.exe payload ships it at the
    # {app} root (installer\ is not shipped), while repo/dev direct installs
    # carry it under installer\. Resolve either; fail loudly on neither.
    $IconSource = Join-Path $SourceRoot "courselens-icon.ico"
    if (-not (Test-Path -LiteralPath $IconSource -PathType Leaf)) {
        $IconSource = Join-Path $SourceRoot "installer\courselens-icon.ico"
    }
    Copy-Item -LiteralPath $IconSource -Destination (Join-Path $Launcher "courselens-icon.ico")
    # P66 INSTALLER-UX-1: the shortcuts run the launcher under pythonw.exe (GUI
    # subsystem) so no console window ever exists. The silent host is GENERATED
    # here rather than shipped as a repo file, and it lands inside the trust
    # boundary: the manifest built below hashes it like every other launcher
    # asset, and start_managed_courselens.ps1 stays byte-identical to the audited
    # chain. The powershell child gets a hidden-but-existing console
    # (CREATE_NEW_CONSOLE + SW_HIDE), not a console-less one (CREATE_NO_WINDOW):
    # the managed launcher starts the service with -NoNewWindow, and a
    # console-less parent would make that child allocate a fresh VISIBLE console
    # window instead. Launcher-chain refusals land in state\last-launch.host.log.
    $SilentHost = Join-Path $Launcher "start_managed_courselens.pyw"
    $SilentHostBody = @'
"""CourseLens silent host (P66 INSTALLER-UX-1).

pythonw.exe runs this file, so double-clicking a CourseLens shortcut never
shows a console window. The real launch logic stays in
start_managed_courselens.ps1, byte-identical to the audited chain; this host
runs it under powershell with a hidden (but existing) console and forwards
the exit code.

The console must be hidden rather than absent: start_managed_courselens.ps1
starts the service with -NoNewWindow, and a console-less parent would make
that child allocate a fresh visible console window. Launcher-chain refusals
(trust verification, single-instance mutex, health) land in
state\\last-launch.host.log instead of a window nobody can read.
"""

import sys
# The host runs BEFORE start_managed_courselens.ps1 can set
# PYTHONDONTWRITEBYTECODE: without this flag the imports below would write
# __pycache__ files into the trust-verified launcher tree and the trust walk
# would (correctly) refuse the next launch with "untrusted asset".
sys.dont_write_bytecode = True

import os
import subprocess

SW_HIDE = 0
CREATE_NEW_CONSOLE = 0x00000010


def main():
    launcher_root = os.path.dirname(os.path.abspath(__file__))
    install_root = os.path.dirname(launcher_root)
    script = os.path.join(launcher_root, "start_managed_courselens.ps1")
    if not os.path.isfile(script):
        return 2

    forwarded = sys.argv[1:]
    if forwarded and forwarded[0].lower() in ("-installroot", "--install-root"):
        if len(forwarded) < 2:
            return 2
        install_root = forwarded[1]
        forwarded = forwarded[2:]

    windir = os.environ.get("SystemRoot", r"C:\Windows")
    powershell = os.path.join(
        windir, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
    )
    if not os.path.isfile(powershell):
        powershell = "powershell.exe"

    command = [
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        script,
        "-InstallRoot",
        install_root,
    ] + forwarded

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = SW_HIDE

    log_handle = None
    try:
        try:
            log_handle = open(
                os.path.join(install_root, "state", "last-launch.host.log"), "wb"
            )
        except OSError:
            log_handle = None
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NEW_CONSOLE,
                startupinfo=startupinfo,
            )
        except OSError as error:
            if log_handle is not None:
                log_handle.write(b"silent host failed to start powershell: "
                                 + str(error).encode("utf-8", "replace") + b"\n")
            return 1
        return completed.returncode
    finally:
        if log_handle is not None:
            log_handle.close()


if __name__ == "__main__":
    sys.exit(main())
'@
    [IO.File]::WriteAllText($SilentHost, $SilentHostBody, [System.Text.UTF8Encoding]::new($false))
    if (-not (Test-Path -LiteralPath $SilentHost -PathType Leaf)) {
        throw "Silent host generation failed"
    }
    Copy-Item -LiteralPath (Join-Path $SourceRoot "config\client-update-trust.json") -Destination $TrustRoot
    Build-LauncherTrustManifest
    Write-Host ("运行时环境已就位（{0} 个文件）。" -f $script:LauncherFileCount)
}

function Build-LauncherTrustManifest {
    # Walk the finished launcher tree (inventory + reparse-point refusal,
    # unchanged), then hash every file through a small runspace pool - the
    # same shape as the audited N9-S LAUNCH-FAST-1 launch trust walk
    # (per-file cmdlet hashing measured 59 s on the 4125-file runtime tree
    # under real-time AV; the pool hides that per-file latency).
    $Launcher = Join-Path $InstallRoot "launcher"
    $walkRel = [Collections.Generic.List[string]]::new()
    $walkFull = [Collections.Generic.List[string]]::new()
    Get-ChildItem -LiteralPath $Launcher -Recurse -Force | ForEach-Object {
        if ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Stable launcher contains an unsafe reparse point"
        }
        if (-not $_.PSIsContainer) {
            $walkRel.Add($_.FullName.Substring($Launcher.Length + 1).Replace("\", "/"))
            $walkFull.Add($_.FullName)
            $script:LauncherBytes += $_.Length
        }
    }
    if ($walkFull.Count -eq 0) { throw "Stable launcher inventory is empty" }
    $script:LauncherFileCount = $walkFull.Count
    $collected = Invoke-ParallelSha256Walk -RelList $walkRel -FullList $walkFull
    $launcherFiles = [ordered]@{}
    foreach ($key in ($collected.Keys | Sort-Object)) { $launcherFiles[$key] = $collected[$key] }
    @{ schema = "courselens.launcher-trust.v2"; files = $launcherFiles } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Launcher "launcher-trust.json") -Encoding UTF8
}

function Invoke-ParallelSha256Walk {
    # Shared hashing engine for both trust manifests (INSTALL-FIX-1): chunks
    # the file list across a runspace pool, protocol "H|rel|digest" /
    # "E|rel" (unreadable file), and returns rel -> lowercase-hex SHA256.
    param(
        [Collections.Generic.List[string]]$RelList,
        [Collections.Generic.List[string]]$FullList,
        [string]$UnreadableMessage
    )
    $results = @{}
    $trustWorkers = [Math]::Min(8, [Environment]::ProcessorCount)
    $chunkSize = [Math]::Ceiling($RelList.Count / $trustWorkers)
    $runspacePool = [Management.Automation.Runspaces.RunspaceFactory]::CreateRunspacePool(
        1, $trustWorkers, [Management.Automation.Runspaces.InitialSessionState]::CreateDefault(), $Host)
    $runspacePool.Open()
    $hashJobs = [Collections.Generic.List[object]]::new()
    for ($chunkStart = 0; $chunkStart -lt $RelList.Count; $chunkStart += $chunkSize) {
        $chunkCount = [Math]::Min($chunkSize, $RelList.Count - $chunkStart)
        $hashPowershell = [PowerShell]::Create()
        $hashPowershell.RunspacePool = $runspacePool
        [void]$hashPowershell.AddScript({
            param([object[]]$ChunkRel, [object[]]$ChunkFull)
            $sha = [System.Security.Cryptography.SHA256]::Create()
            $lines = [Collections.Generic.List[string]]::new()
            for ($i = 0; $i -lt $ChunkFull.Count; $i++) {
                try {
                    $stream = [IO.File]::OpenRead($ChunkFull[$i])
                    try { $digest = $sha.ComputeHash($stream) } finally { $stream.Dispose() }
                    $lines.Add("H|" + $ChunkRel[$i] + "|" + [BitConverter]::ToString($digest).Replace("-", "").ToLowerInvariant())
                } catch {
                    $lines.Add("E|" + $ChunkRel[$i])
                }
            }
            $sha.Dispose()
            ,$lines
        }).AddArgument([object[]]$RelList.GetRange($chunkStart, $chunkCount).ToArray()).AddArgument([object[]]$FullList.GetRange($chunkStart, $chunkCount).ToArray())
        $hashJobs.Add(@{ Powershell = $hashPowershell; Handle = $hashPowershell.BeginInvoke() })
    }
    foreach ($hashJob in $hashJobs) {
        foreach ($jobOutput in @($hashJob.Powershell.EndInvoke($hashJob.Handle))) {
            if ($null -eq $jobOutput) { continue }
            foreach ($line in @($jobOutput)) {
                if ($null -eq $line) { continue }
                $parts = $line.Split("|")
                if ($parts[0] -eq "E") {
                    throw ($UnreadableMessage -f $parts[1])
                }
                $results[$parts[1]] = $parts[2]
            }
        }
        $hashJob.Powershell.Dispose()
    }
    $runspacePool.Close()
    return $results
}

function Invoke-ProgramPhase {
    Get-ManagedLayoutOrThrow
    Write-Host "正在部署程序文件…"
    $VersionRoot = Join-Path $InstallRoot ("versions\" + $version)
    New-Item -ItemType Directory -Force -Path $VersionRoot | Out-Null
    # INSTALL-FIX-1: one robocopy replaces the per-top-level-item
    # Copy-Item pipeline. /XD gets ABSOLUTE paths so only the source root's
    # own top-level .git/runtime/tools are excluded (never a nested
    # directory that merely shares the name).
    Copy-TreeWithRobocopy -From $SourceRoot -To $VersionRoot -ExcludeDirs @(
        (Join-Path $SourceRoot ".git"),
        (Join-Path $SourceRoot "runtime"),
        (Join-Path $SourceRoot "tools")
    )
    $versionTrust = Join-Path $VersionRoot "config\client-update-trust.json"
    if (Test-Path -LiteralPath $versionTrust -PathType Leaf) {
        Remove-Item -LiteralPath $versionTrust -Force
    }
    Build-VersionManifest
    @{ schema = "courselens.current-version.v1"; version = $version; awaiting_health = $false } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $InstallRoot "state\current.json") -Encoding UTF8
    # ARPFIX-1: the uninstaller is shipped INSIDE the install root (top level,
    # next to launcher/versions/state/trust) so the registered UninstallString
    # keeps working even after the source repository moves or is deleted. The
    # copy is refreshed on every (re)install, so the entry never points at a
    # stale script.
    $ManagedUninstallScript = Join-Path $InstallRoot "uninstall_managed_client.ps1"
    Copy-Item -LiteralPath (Join-Path $SourceRoot "scripts\uninstall_managed_client.ps1") -Destination $ManagedUninstallScript -Force
    Write-Host "程序文件已就位。"
}

function Build-VersionManifest {
    $VersionRoot = Join-Path $InstallRoot ("versions\" + $version)
    $walkRel = [Collections.Generic.List[string]]::new()
    $walkFull = [Collections.Generic.List[string]]::new()
    Get-ChildItem -LiteralPath $VersionRoot -Recurse -Force | ForEach-Object {
        if ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Initial version slot contains an unsafe reparse point"
        }
        if (-not $_.PSIsContainer) {
            $relative = $_.FullName.Substring($VersionRoot.Length + 1).Replace("\", "/")
            if ($relative -eq "courselens-package.json") {
                throw "Source root already contains package metadata"
            }
            $walkRel.Add($relative)
            $walkFull.Add($_.FullName)
            $script:VersionBytes += $_.Length
        }
    }
    if ($walkRel.Count -eq 0) { throw "Initial version inventory is empty" }
    $collected = Invoke-ParallelSha256Walk -RelList $walkRel -FullList $walkFull `
        -UnreadableMessage "程序文件无法读取，安装已中止：{0}。请关闭可能占用文件的其他程序后重试。"
    $versionFiles = [ordered]@{}
    foreach ($key in ($collected.Keys | Sort-Object)) { $versionFiles[$key] = $collected[$key] }
    @{
        schema = "courselens.client-package.v1"; version = $version
        release_id = "pilot-local-install"; platform = "windows"; architecture = "x86_64"
        files = $versionFiles
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $VersionRoot "courselens-package.json") -Encoding UTF8
}

function Register-ManagedUninstallEntry {
    # ARPFIX-1: register the per-user "installed app" entry that Windows
    # Settings reads. Idempotent by construction: the key is created when
    # missing and every value below is an upsert (-Force), so a managed
    # upgrade/refresh never leaves a stale version, icon, or script path.
    param([string]$InstallRoot, [string]$Version, [string]$IconPath, [string]$UninstallScriptPath, [long]$PayloadBytes)
    # Fixed GUID for the managed/dev install path. It is deliberately distinct
    # from the Inno AppId ({A2A00F7A-AD6C-4822-B23F-5155DDC320E0}): the two
    # install shells uninstall different trees and must never share a key.
    $UninstallKeyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{EE7FDDF2-E429-4E21-BEAE-FA0EB749FCB7}"
    # Coexistence: while the Inno setup.exe install is present (its key carries
    # Inno's default "_is1" suffix), it owns the app's uninstall lifecycle.
    # Exactly one entry then stays visible: ours is not created, and a stale
    # one from an earlier direct managed install is withdrawn.
    $InnoUninstallKeyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{A2A00F7A-AD6C-4822-B23F-5155DDC320E0}_is1"
    if (Test-Path -LiteralPath $InnoUninstallKeyPath) {
        if (Test-Path -LiteralPath $UninstallKeyPath) {
            Remove-Item -LiteralPath $UninstallKeyPath -Force
            Write-Host "检测到这台电脑已有 setup.exe 安装的 CourseLens，脚本直装的卸载入口已撤下，设置里只保留一个卸载入口"
        } else {
            Write-Host "卸载入口交给 setup.exe 安装的 CourseLens，这里不重复登记"
        }
        return
    }
    if (-not (Test-Path -LiteralPath $UninstallKeyPath)) {
        New-Item -Path $UninstallKeyPath -Force | Out-Null
    }
    $powershellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $uninstallCommand = '"' + $powershellExe + '" -NoProfile -ExecutionPolicy Bypass -File "' + $UninstallScriptPath + '"'
    $estimatedSizeKb = $null
    # INSTALL-FIX-1: the size comes from the byte sums the two manifest walks
    # already accumulated (the old code walked the whole finished install
    # root a third time just for this number).
    if ($PayloadBytes -gt 0) {
        $estimatedSizeKb = [int][Math]::Ceiling($PayloadBytes / 1KB)
    }
    New-ItemProperty -Path $UninstallKeyPath -Name "DisplayName" -Value "CourseLens" -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $UninstallKeyPath -Name "DisplayVersion" -Value $Version -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $UninstallKeyPath -Name "DisplayIcon" -Value $IconPath -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $UninstallKeyPath -Name "InstallLocation" -Value $InstallRoot -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $UninstallKeyPath -Name "UninstallString" -Value $uninstallCommand -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $UninstallKeyPath -Name "Publisher" -Value "CourseLens" -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $UninstallKeyPath -Name "NoModify" -Value 1 -PropertyType DWord -Force | Out-Null
    New-ItemProperty -Path $UninstallKeyPath -Name "NoRepair" -Value 1 -PropertyType DWord -Force | Out-Null
    New-ItemProperty -Path $UninstallKeyPath -Name "SystemComponent" -Value 0 -PropertyType DWord -Force | Out-Null
    if ($null -ne $estimatedSizeKb) {
        New-ItemProperty -Path $UninstallKeyPath -Name "EstimatedSize" -Value $estimatedSizeKb -PropertyType DWord -Force | Out-Null
    }
    New-ItemProperty -Path $UninstallKeyPath -Name "InstallDate" -Value (Get-Date -Format "yyyyMMdd") -PropertyType String -Force | Out-Null
    Write-Host "已在系统「设置 → 应用」登记好卸载入口，以后可以像普通软件一样在这里卸载 CourseLens"
}

function Invoke-FinalizePhase {
    Get-ManagedLayoutOrThrow
    Write-Host "正在完成安装…"
    $ManagedUninstallScript = Join-Path $InstallRoot "uninstall_managed_client.ps1"
    try {
        Register-ManagedUninstallEntry -InstallRoot $InstallRoot -Version $version `
            -IconPath (Join-Path $InstallRoot "launcher\courselens-icon.ico") -UninstallScriptPath $ManagedUninstallScript `
            -PayloadBytes ($script:LauncherBytes + $script:VersionBytes)
    } catch {
        # Registration is an add-on: a refused or sandboxed registry must never
        # turn a completed install into a failed one. The failure stays visible
        # instead of being swallowed.
        Write-Warning ("没能登记卸载入口（不影响应用本身的安装和更新）：{0}" -f $_.Exception.Message)
    }
    # INSTALL-FIX-1: the run finished - retire the in-progress marker so a
    # later run reads this root as a normal completed install.
    $InProgressMarker = Join-Path $InstallRoot "state\install-inprogress.json"
    if (Test-Path -LiteralPath $InProgressMarker -PathType Leaf) {
        Remove-Item -LiteralPath $InProgressMarker -Force
    }
    Write-Host "CourseLens 已装好，位置在 $InstallRoot"
}

# ---- phase dispatch (INSTALL-FIX-1): "all" keeps the single-shot shape for
# direct/dev installs; the Inno installer calls the three phases separately
# so each gets its own real completion point on the wizard progress page.
if ($Phase -in @("all", "runtime")) {
    Invoke-RuntimePhase
}
if ($Phase -in @("all", "program")) {
    Invoke-ProgramPhase
}
if ($Phase -in @("all", "finalize")) {
    Invoke-FinalizePhase
}
