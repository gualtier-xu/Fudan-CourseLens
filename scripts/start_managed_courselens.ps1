param(
    [Parameter(Mandatory = $true)][string]$InstallRoot,
    [int]$Port = 6268,
    [switch]$NoOpen
)

$ErrorActionPreference = "Stop"
$InstallRoot = (Resolve-Path -LiteralPath $InstallRoot).Path
$LauncherRoot = Join-Path $InstallRoot "launcher"
$PythonExe = Join-Path $LauncherRoot "python312\python.exe"
$Helper = Join-Path $LauncherRoot "client_update_helper.py"
$StateRoot = Join-Path $InstallRoot "state"
$DataRoot = Join-Path $InstallRoot "data"
[Environment]::SetEnvironmentVariable("PYTHONUTF8", "1", "Process")
[Environment]::SetEnvironmentVariable("PYTHONDONTWRITEBYTECODE", "1", "Process")

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Managed CourseLens runtime is missing"
}
if (-not (Test-Path -LiteralPath $Helper -PathType Leaf)) {
    throw "Trusted CourseLens update helper is missing"
}

# ===========================================================================
# LAUNCH-FIX-1 B/C（2026-10-03）：先探活再走查 + 开机 splash。
#
# 顺序即语义：双击后先做既有实例探测（Find-ManagedAttachTarget：service 名/
# 安装槽版本/属主 pid 三元指纹）。命中既有实例即走挂接路径——优先聚焦其原生
# 窗口，找不到窗口则新开一枚挂接原生窗，浏览器只作最后兜底——并立即退出。
# 挂接对象是上一个已验证启动器拉起的在跑实例，与既有挂接信任等同，只是更快
# 到达，因此跳过全量信任走查。未命中（真冷启动）才继续：先单点快核并拉起
# splash 载体，再走与既往逐字节相同的全量信任走查、单实例互斥与启动链。
# ===========================================================================

# P67 ZERO-CONSOLE-1 U3b (DEF-1): the managed default port stays 6268-first,
# but a single hardcoded port turned an occupied port into a dead double-click
# (P67B: a local proxy held 6268 and the launcher just failed). The scan keeps
# the dev Find-AvailablePort idiom: prefer the fixed port, walk a small
# candidate band, then let the OS assign one; system-reserved (WSAEACCES)
# ports fail the probe and are skipped the same way busy ones are.
function Test-ManagedPortAvailable {
    param([int]$Port)
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($listener) { $listener.Stop() }
    }
}

function Find-ManagedAvailablePort {
    param([int]$PreferredPort)
    for ($port = $PreferredPort; $port -le ($PreferredPort + 10); $port++) {
        if (Test-ManagedPortAvailable -Port $port) { return $port }
    }
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        $port = [int]$listener.LocalEndpoint.Port
        Write-Host ("[courselens] ports {0}-{1} are all unavailable; using system-assigned port {2}" -f $PreferredPort, ($PreferredPort + 10), $port)
        return $port
    } finally {
        if ($listener) { $listener.Stop() }
    }
}

# P67 ZERO-CONSOLE-1 U3c (DEF-2): a second double-click while an instance is
# serving must re-pull the existing page instead of dying silently in
# state\last-launch.host.log. The probe walks the same candidate band the
# launcher itself may have fallen back to, and only trusts the closed-set
# service identity: any 200 that does not say fudan-courselens is someone
# else's port and stays untouched.
function Find-ManagedServiceUrl {
    param([int]$PreferredPort)
    for ($port = $PreferredPort; $port -le ($PreferredPort + 10); $port++) {
        # LAUNCH-FIX-1 B 修正（真机矩阵实测）：裸死端口可能被安全软件静默丢包
        # 而非立即拒绝，逐口 1s HTTP 超时会把冷启动探活拖成 11-14s。先用
        # 250ms TCP 连接预过滤——只有真正接受连接的端口才值得发 HTTP；
        # 拒绝与丢弃都 <250ms 判负，接受者才进入闭集健康核对。
        $client = [System.Net.Sockets.TcpClient]::new()
        try {
            if (-not $client.ConnectAsync("127.0.0.1", $port).Wait(250)) { continue }
        } catch {
            continue
        } finally {
            $client.Dispose()
        }
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 1 -Proxy $null
            if ($health.ok -eq $true -and $health.service -eq "fudan-courselens") {
                return "http://127.0.0.1:$port/"
            }
        } catch {
        }
    }
    return $null
}

# P68 NATIVE-WINDOW-1: opening the UI is the server's job now. Interactive
# launches pass --window (the server process opens its own native pywebview
# shell window once ready, with the browser only as its internal fallback);
# the -NoOpen daily-schedule task stays --no-open (headless, never a window).
# This launcher therefore never opens the page itself anymore.
function Find-ManagedWindowHandle {
    # P68: focus the serving instance's native window. That window belongs
    # to the server process itself, so the closed-set identity is "a python
    # process from THIS install root whose main window is titled
    # CourseLens" - no global user32 title search that could catch an
    # unrelated Explorer window of the same name (and no string-marshaling
    # surface at all: only the handle crosses the P/Invoke boundary).
    $server = Get-Process python -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and $_.Path.StartsWith("$LauncherRoot\python312\")
    } | Where-Object { $_.MainWindowTitle -eq "CourseLens" } |
        Select-Object -First 1
    if ($server) { return $server.MainWindowHandle }
    # 夜10-A T1/T6: a window minimized to the tray (Form.Hide) has no
    # MainWindowHandle, so the process scan above misses it. Fall back to a
    # title search whose OWNER is verified to be a python process from THIS
    # install root - the closed-set identity is preserved by the pid check,
    # not by the title. Any failure keeps the browser fallback path.
    if (-not ("CourseLens.NativeWindowFocus2" -as [type])) {
        Add-Type -Namespace CourseLens -Name NativeWindowFocus2 -MemberDefinition @'
public delegate bool EnumWindowsProc(System.IntPtr windowHandle, System.IntPtr lParam);
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc callback, System.IntPtr lParam);
[System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)] public static extern int GetWindowText(System.IntPtr windowHandle, System.Text.StringBuilder text, int count);
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(System.IntPtr windowHandle, out uint processId);
'@
    }
    $callback = [CourseLens.NativeWindowFocus2+EnumWindowsProc]{
        param([IntPtr]$windowHandle, [IntPtr]$lParam)
        $ownerPid = [uint32]0
        [void][CourseLens.NativeWindowFocus2]::GetWindowThreadProcessId($windowHandle, [ref]$ownerPid)
        if ($ownerPid -le 0) { return $true }
        try {
            $owner = Get-Process -Id $ownerPid -ErrorAction Stop
            if (-not $owner.Path -or -not $owner.Path.StartsWith("$LauncherRoot\python312\")) { return $true }
        } catch { return $true }
        $title = New-Object System.Text.StringBuilder 256
        [void][CourseLens.NativeWindowFocus2]::GetWindowText($windowHandle, $title, 256)
        if ($title.ToString() -eq "CourseLens") {
            $script:_trayHiddenWindow = $windowHandle
            return $false
        }
        return $true
    }
    $script:_trayHiddenWindow = [IntPtr]::Zero
    try { [void][CourseLens.NativeWindowFocus2]::EnumWindows($callback, [IntPtr]::Zero) } catch {}
    if ($script:_trayHiddenWindow -ne [IntPtr]::Zero) { return $script:_trayHiddenWindow }
    return [IntPtr]::Zero
}

# LAUNCH-FIX-1 B: the attach path runs before the trust walkthrough and must
# not execute trusted launcher binaries early, so the active root is resolved
# from state\current.json directly (read-only JSON, no helper run). The
# version string doubles as the probe's expected-version fingerprint, and the
# closed-set shape check keeps it inside versions\.
function Resolve-ManagedActiveRoot {
    try {
        $current = Get-Content -Raw -Encoding UTF8 (Join-Path $StateRoot "current.json") | ConvertFrom-Json
    } catch {
        return $null
    }
    $version = [string]$current.version
    if (-not $version -or $version -notmatch "^[0-9A-Za-z._-]+$") { return $null }
    $candidate = Join-Path $InstallRoot "versions\$version"
    if (-not (Test-Path -LiteralPath $candidate -PathType Container)) { return $null }
    try { return (Resolve-Path -LiteralPath $candidate).Path } catch { return $null }
}

# LAUNCH-FIX-1 B: the existing-instance fingerprint is the ps1:315-319 health
# identity generalized to a foreign instance: ok:true + service name +
# version equal to the installed slot + owner pid alive and belonging to THIS
# install root's python312 runtime (the same closed-set identity
# Find-ManagedWindowHandle enforces). Any miss means "no attachable instance"
# and falls through to the full cold-start chain.
function Find-ManagedAttachTarget {
    param([int]$PreferredPort)
    $activeRoot = Resolve-ManagedActiveRoot
    if (-not $activeRoot) { return $null }
    $expectedVersion = Split-Path -Leaf $activeRoot
    $serviceUrl = Find-ManagedServiceUrl -PreferredPort $PreferredPort
    if (-not $serviceUrl) { return $null }
    try {
        $health = Invoke-RestMethod -Uri "$($serviceUrl)api/health" -TimeoutSec 1 -Proxy $null
        if ($health.ok -ne $true -or
            $health.service -ne "fudan-courselens" -or
            -not [string]::Equals([string]$health.version, $expectedVersion, [StringComparison]::Ordinal)) {
            return $null
        }
        $ownerPid = [int]$health.pid
    } catch {
        return $null
    }
    if ($ownerPid -le 0) { return $null }
    try {
        $owner = Get-Process -Id $ownerPid -ErrorAction Stop
    } catch {
        return $null
    }
    if (-not $owner.Path -or -not $owner.Path.StartsWith("$LauncherRoot\python312\")) { return $null }
    return @{ Url = $serviceUrl; ActiveRoot = $activeRoot }
}

# LAUNCH-FIX-1 A: the thin windowless-attach implementation - pythonw opens
# one native shell window attached to the serving instance's URL. The window
# body is src/app.py's --attach-window thin mode (P68 window creation/state/
# degraded paths reused); the attach process owns no service and no lifecycle,
# so closing the window only closes the window.
function Start-ManagedAttachWindow {
    param([string]$ServiceUrl, [string]$ActiveRoot)
    if (-not $ServiceUrl -or -not $ActiveRoot) { return $false }
    $pythonw = Join-Path $LauncherRoot "python312\pythonw.exe"
    if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) { return $false }
    try {
        [Environment]::SetEnvironmentVariable("COURSELENS_DATA_DIR", $DataRoot, "Process")
        Start-Process -FilePath $pythonw -ArgumentList @("-m", "src", "--attach-window", $ServiceUrl) -WorkingDirectory $ActiveRoot
        return $true
    } catch {
        return $false
    }
}

# LAUNCH-FIX-1 A: the browser stays the last resort; when it is used because
# the attach window could not start, the degraded reason gets one host-log
# line (best-effort, never fails the launch).
function Write-ManagedHostLog {
    param([string]$Message)
    try {
        Add-Content -LiteralPath (Join-Path $StateRoot "last-launch.host.log") `
            -Value ("[courselens] {0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message) `
            -Encoding UTF8
    } catch {
    }
}

if (-not $NoOpen) {
    $attachTarget = Find-ManagedAttachTarget -PreferredPort $Port
    if ($attachTarget) {
        $windowHandle = Find-ManagedWindowHandle
        if ($windowHandle -ne [IntPtr]::Zero) {
            if (-not ("CourseLens.NativeWindowFocus" -as [type])) {
                Add-Type -Namespace CourseLens -Name NativeWindowFocus -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr windowHandle);
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool ShowWindowAsync(System.IntPtr windowHandle, int command);
'@
            }
            [void][CourseLens.NativeWindowFocus]::ShowWindowAsync($windowHandle, 9)
            [void][CourseLens.NativeWindowFocus]::SetForegroundWindow($windowHandle)
            Write-Host "[courselens] focused the running CourseLens window."
        } elseif (Start-ManagedAttachWindow -ServiceUrl $attachTarget.Url -ActiveRoot $attachTarget.ActiveRoot) {
            Write-Host "[courselens] attached a new CourseLens window to the running service."
        } else {
            Write-ManagedHostLog -Message ("no CourseLens window found and the attach window could not start; opened the browser instead for {0}" -f $attachTarget.Url)
            Start-Process $attachTarget.Url
            Write-Host "[courselens] opened the running CourseLens page in the browser (attach window unavailable)."
        }
        exit 0
    }
}

# ===========================================================================
# LAUNCH-FIX-1 C: 开机 splash —— 只在真冷启动路径（探活未命中）出现。
# 时序：单点快核（launcher-trust.json 预置哈希 × 仅 splash 壳+HTML 两件，
# 毫秒级）→ pythonw 拉起载体 → 全量信任走查照旧。任何异常一律静默跳过，
# 绝不阻断或拖慢启动；收口=服务就绪时 Start-And-Probe 落旗标，载体另有父
# 进程退场监视与 45 秒兜底退出，无残留进程。挂接路径（上方已 exit）不显示
# splash。-NoOpen（每日任务）同样不显示。
# ===========================================================================
$script:SplashFlagPath = Join-Path $StateRoot "splash-dismiss.flag"
$script:SplashLaunched = $false

function Complete-ManagedSplash {
    if (-not $script:SplashLaunched) { return }
    try {
        Set-Content -LiteralPath $script:SplashFlagPath -Value ("ready {0}" -f [DateTime]::UtcNow.ToString("o")) -Encoding ASCII
    } catch {
    }
}

function Start-ManagedSplash {
    if ($NoOpen) { return }
    $carrier = Join-Path $LauncherRoot "splash\splash.pyw"
    $asset = Join-Path $LauncherRoot "splash\splash.html"
    $pythonw = Join-Path $LauncherRoot "python312\pythonw.exe"
    if (-not ((Test-Path -LiteralPath $carrier -PathType Leaf) -and
              (Test-Path -LiteralPath $asset -PathType Leaf) -and
              (Test-Path -LiteralPath $pythonw -PathType Leaf))) { return }
    try {
        # 单点快核：哈希基准预置在既有信任清单里，只核对 splash 自身两件；
        # 清单缺失/条目缺失/哈希不符任何一项都静默跳过 splash（退回无 splash
        # 的既有启动形态），绝不阻断。
        $manifest = Get-Content -Raw -Encoding UTF8 (Join-Path $LauncherRoot "launcher-trust.json") | ConvertFrom-Json
        foreach ($pair in @(@("splash/splash.pyw", $carrier), @("splash/splash.html", $asset))) {
            $expected = $null
            try { $expected = [string]$manifest.files.PSObject.Properties[$pair[0]].Value } catch { }
            if ($expected -notmatch "^[0-9a-f]{64}$") { return }
            $sha = [System.Security.Cryptography.SHA256]::Create()
            try {
                $stream = [IO.File]::OpenRead($pair[1])
                try {
                    $actual = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace("-", "").ToLowerInvariant()
                } finally { $stream.Dispose() }
            } finally { $sha.Dispose() }
            if ($actual -ne $expected) { return }
        }
        Remove-Item -LiteralPath $script:SplashFlagPath -Force -ErrorAction SilentlyContinue
        Start-Process -FilePath $pythonw -ArgumentList @(('"{0}"' -f $carrier), ('"{0}"' -f $script:SplashFlagPath), "$PID")
        $script:SplashLaunched = $true
        Write-Host "[courselens] splash started"
    } catch {
        Write-Host "[courselens] splash skipped"
    }
}

Start-ManagedSplash

# N15-W1 (T1 F1): the walk refusals keep their byte-identical closed-set
# throws, but the whole segment now sits in a try/catch so a fail-closed
# refusal is never a silent dead double-click: one host-log line with the
# closed-set reason and finding, one native window-level message in the
# minimal-permission + named-finding framing, then a non-zero exit.
$WalkFinding = ""
try {
$TrustManifest = Join-Path $LauncherRoot "launcher-trust.json"
$trustVerifyStopwatch = [System.Diagnostics.Stopwatch]::StartNew()
if (-not (Test-Path -LiteralPath $TrustManifest -PathType Leaf) -or
    ((Get-Item -LiteralPath $TrustManifest -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw "Stable launcher trust manifest is invalid"
}
$trust = Get-Content -Raw -Encoding UTF8 $TrustManifest | ConvertFrom-Json
if ($trust.schema -ne "courselens.launcher-trust.v2" -or -not $trust.files) {
    throw "Stable launcher trust manifest is invalid"
}
$expectedFiles = [Collections.Generic.Dictionary[string,string]]::new([StringComparer]::Ordinal)
foreach ($property in $trust.files.PSObject.Properties) {
    $name = [string]$property.Name
    $hash = [string]$property.Value
    if (-not $name -or $name -eq "launcher-trust.json" -or $hash -notmatch "^[0-9a-f]{64}$" -or $expectedFiles.ContainsKey($name)) {
        throw "Stable launcher trust manifest is invalid"
    }
    $expectedFiles.Add($name, $hash)
}
if ($expectedFiles.Count -eq 0) { throw "Stable launcher trust manifest is empty" }
$actualFiles = [Collections.Generic.Dictionary[string,string]]::new([StringComparer]::Ordinal)
$walkRel = [Collections.Generic.List[string]]::new()
$walkFull = [Collections.Generic.List[string]]::new()
Get-ChildItem -LiteralPath $LauncherRoot -Recurse -Force | ForEach-Object {
    if ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        $WalkFinding = $_.FullName
        throw "Stable launcher contains an unsafe reparse point"
    }
    if (-not $_.PSIsContainer) {
        $relative = $_.FullName.Substring($LauncherRoot.Length + 1).Replace("\", "/")
        if ($relative -ne "launcher-trust.json") {
            if (-not $expectedFiles.ContainsKey($relative)) {
                $WalkFinding = $relative
                throw "Stable launcher contains an untrusted asset"
            }
            $walkRel.Add($relative)
            $walkFull.Add($_.FullName)
        }
    }
}
# N9-S LAUNCH-FAST-1: the per-file SHA256 moves into a runspace pool (Windows
# PowerShell 5.1 has no ForEach-Object -Parallel). The walk above keeps every
# inventory check inline and unchanged, the same full manifest set is hashed,
# and every refusal below stays byte-identical; only the execution shape
# changed (measured on the 5965-file launcher tree: ~15.2s with the
# per-file cmdlet hash -> ~1.5s; hash-line protocol "H|rel|digest", "E|rel"
# on a file that cannot be read).
$trustWorkers = [Math]::Min(8, [Environment]::ProcessorCount)
$chunkSize = [Math]::Ceiling($walkFull.Count / $trustWorkers)
$runspacePool = [Management.Automation.Runspaces.RunspaceFactory]::CreateRunspacePool(
    1, $trustWorkers, [Management.Automation.Runspaces.InitialSessionState]::CreateDefault(), $Host)
$runspacePool.Open()
$hashJobs = [Collections.Generic.List[object]]::new()
for ($chunkStart = 0; $chunkStart -lt $walkFull.Count; $chunkStart += $chunkSize) {
    $chunkCount = [Math]::Min($chunkSize, $walkFull.Count - $chunkStart)
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
    }).AddArgument([object[]]$walkRel.GetRange($chunkStart, $chunkCount).ToArray()).AddArgument([object[]]$walkFull.GetRange($chunkStart, $chunkCount).ToArray())
    $hashJobs.Add(@{ Powershell = $hashPowershell; Handle = $hashPowershell.BeginInvoke() })
}
foreach ($hashJob in $hashJobs) {
    foreach ($jobOutput in @($hashJob.Powershell.EndInvoke($hashJob.Handle))) {
        if ($null -eq $jobOutput) { continue }
        foreach ($line in @($jobOutput)) {
            if ($null -eq $line) { continue }
            $parts = $line.Split("|")
            if ($parts[0] -eq "E") {
                $WalkFinding = $parts[1]
                throw "Stable launcher asset failed validation"
            }
            $actualFiles[$parts[1]] = $parts[2]
        }
    }
    $hashJob.Powershell.Dispose()
}
$runspacePool.Close()
foreach ($name in $expectedFiles.Keys) {
    $actualHash = $null
    if (-not $actualFiles.TryGetValue($name, [ref]$actualHash)) { continue }
    if (-not [string]::Equals($expectedFiles[$name], $actualHash, [StringComparison]::Ordinal)) {
        $WalkFinding = $name
        throw "Stable launcher asset failed validation"
    }
}
if ($actualFiles.Count -ne $expectedFiles.Count) {
    throw "Stable launcher asset inventory is incomplete"
}
$trustVerifyStopwatch.Stop()
Write-Host ("[courselens] trust-verify: {0} files in {1:N0} ms (workers={2})" -f $actualFiles.Count, $trustVerifyStopwatch.Elapsed.TotalMilliseconds, $trustWorkers)
} catch {
    # T1 F1：fail-closed 拒绝不再静默。人话窗 + host 日志各一条；拒绝语义
    # （throw 消息族）逐字未动，本段只消费不改判。
    $reason = [string]$_.Exception.Message
    Write-ManagedHostLog -Message ("trust walk refused launch: {0}; finding: {1}" -f $reason, $WalkFinding)
    if (-not ("CourseLens.NativeMessageBox" -as [type])) {
        Add-Type -Namespace CourseLens -Name NativeMessageBox -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)] public static extern int MessageBoxW(System.IntPtr windowHandle, string text, string caption, uint type);
'@
    }
    $findingLine = ""
    if ($WalkFinding) {
        $findingLine = "。不符项：{0}（完整清单见 state\last-launch.host.log）" -f $WalkFinding
    }
    $message = (
        "启动前的安全检查没有通过：启动器目录与安装清单不一致{0}。`n`n" +
        "这通常是安装目录被其它程序放进或改动了文件（例如解释器缓存）。你的学习数据不受影响。`n`n" +
        "重新安装 CourseLens 可以重建启动器目录、解除这个提示。"
    ) -f $findingLine
    [void][CourseLens.NativeMessageBox]::MessageBoxW([IntPtr]::Zero, $message, "CourseLens 无法启动", 0x30)
    exit 2
}

# LAUNCH-FIX-1 B/C: the port/probe/window-handle functions now live above the
# trust walk (the probe-first attach path needs them before it); the walk
# itself is byte-identical to the audited chain.

$mutex = [Threading.Mutex]::new($false, "Local\FudanCourseLensManagedUpdate")
$owns = $false
try {
    try { $owns = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $owns = $true }
    if (-not $owns) {
        # P67 U3c (DEF-2): re-pull the serving instance's page (the student
        # double-clicked because they want the app in front of them). A
        # -NoOpen launch (the daily schedule task) never opens a page. When
        # no service answers yet, the first launcher is still preparing and
        # will open the page itself once ready; either way this second
        # launcher exits cleanly instead of refusing in an invisible log.
        if (-not $NoOpen) {
            $existingUrl = Find-ManagedServiceUrl -PreferredPort $Port
            if ($existingUrl) {
                # P68: the serving instance shows its own native window --
                # bring that window to the front instead of opening a browser
                # tab. The URL re-pull below stays as the fallback for a
                # serving instance whose window cannot be found (degraded
                # browser mode or an odd shell state).
                $windowHandle = Find-ManagedWindowHandle
                if ($windowHandle -ne [IntPtr]::Zero) {
                    if (-not ("CourseLens.NativeWindowFocus" -as [type])) {
                        Add-Type -Namespace CourseLens -Name NativeWindowFocus -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr windowHandle);
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool ShowWindowAsync(System.IntPtr windowHandle, int command);
'@
                    }
                    [void][CourseLens.NativeWindowFocus]::ShowWindowAsync($windowHandle, 9)
                    [void][CourseLens.NativeWindowFocus]::SetForegroundWindow($windowHandle)
                } else {
                    # LAUNCH-FIX-1 A: no native window found - open a thin
                    # attach window on the serving instance instead of falling
                    # straight to the browser. The browser stays the last
                    # resort, with a one-line host-log note about why.
                    $attachRoot = Resolve-ManagedActiveRoot
                    if (Start-ManagedAttachWindow -ServiceUrl $existingUrl -ActiveRoot $attachRoot) {
                        Write-Host "[courselens] attached a new CourseLens window to the running service."
                    } else {
                        Write-ManagedHostLog -Message ("no CourseLens window found and the attach window could not start; opened the browser instead for {0}" -f $existingUrl)
                        Start-Process $existingUrl
                    }
                }
            }
        }
        Write-Host "[courselens] another CourseLens launcher owns the service; focused the existing page instead."
        exit 0
    }

    function Get-ActiveRoot {
        $value = & $PythonExe $Helper current --install-root $InstallRoot
        if ($LASTEXITCODE -ne 0 -or -not $value) { throw "Active CourseLens version is invalid" }
        return (Resolve-Path -LiteralPath $value.Trim()).Path
    }

    function Start-And-Probe([string]$ActiveRoot, [string]$ExpectedVersion) {
        $environment = @{
            "COURSELENS_DATA_DIR" = $DataRoot
            "COURSELENS_INSTALL_ROOT" = $InstallRoot
            "PYTHONUTF8" = "1"
            "PYTHONDONTWRITEBYTECODE" = "1"
        }
        foreach ($item in $environment.GetEnumerator()) {
            [Environment]::SetEnvironmentVariable($item.Key, $item.Value, "Process")
        }
        # N9-G INSTALLER-POLISH-1 U3: the child service's own words must be
        # recoverable after a failed start (A1 finding #1: a silent launch
        # failure left nothing to read). serve speaks on stdout (plain-language
        # hints such as the reserved-port explanation) and tracebacks go to
        # stderr, so both streams land in state\ as last-launch evidence.
        # Start-Process forbids -WindowStyle together with stream redirects,
        # and the python process must stay the direct child (the health check
        # matches its PID), so the child now shares this console (-NoNewWindow).
        # P67 ZERO-CONSOLE-1 U3c (DEF-2): the server-keeping switch stayed in
        # this argument list since the cleanroom bootstrap and silently
        # disabled the frontend-session registry (stop_when_frontend_closes=
        # False), so a closed browser page never ended the process (P67B
        # finding). Dropping it restores the audited fast path (close beacon,
        # then a 15s grace) and the lease path; running work still protects
        # the service via has_active_work.
        # P68 NATIVE-WINDOW-1: the serve open mode is the launcher/server
        # contract for who opens the UI. --window (interactive) makes the
        # server open its own native shell window once ready; --no-open (the
        # -NoOpen daily task) stays headless. The server-keeping switch stays
        # banned in this argument list for the P67B reason recorded above.
        $serveOpenMode = if ($NoOpen) { "--no-open" } else { "--window" }
        $logPath = Join-Path $StateRoot "last-launch.log"
        $errLogPath = Join-Path $StateRoot "last-launch.err.log"
        $process = Start-Process -FilePath $PythonExe -ArgumentList @(
            "-m", "src", "serve", "--port", "$Port", $serveOpenMode
        ) -WorkingDirectory $ActiveRoot -PassThru -NoNewWindow `
            -RedirectStandardOutput $logPath -RedirectStandardError $errLogPath
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        while ([DateTime]::UtcNow -lt $deadline -and -not $process.HasExited) {
            try {
                $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 1 -Proxy $null
                if ($health.ok -eq $true -and
                    $health.service -eq "fudan-courselens" -and
                    [int]$health.pid -eq $process.Id -and
                    [string]::Equals([string]$health.version, $ExpectedVersion, [StringComparison]::Ordinal)) {
                    # LAUNCH-FIX-1 C: the service is ready - dismiss the boot
                    # splash (best-effort flag, no-ops when no splash ran).
                    Complete-ManagedSplash
                    return $process
                }
            } catch {
            }
            Start-Sleep -Milliseconds 200
        }
        if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
        return $null
    }

    # A verified pending switch restarts the client after the service exits.
    # The restart authority stays in this trusted launcher; the dying process
    # never spawns detached helpers. At most one automatic restart per
    # launcher session guards against switch loops.
    $automaticRestarts = 0
    while ($true) {
        $pending = Join-Path $StateRoot "pending.json"
        if (Test-Path -LiteralPath $pending -PathType Leaf) {
            & $PythonExe $Helper apply --install-root $InstallRoot
            if ($LASTEXITCODE -ne 0) { throw "Verified update switch failed" }
        }

        $current = Get-Content -Raw -Encoding UTF8 (Join-Path $StateRoot "current.json") | ConvertFrom-Json
        $active = Get-ActiveRoot
        if (-not [string]::Equals((Split-Path -Leaf $active), [string]$current.version, [StringComparison]::Ordinal)) {
            throw "Active CourseLens version identity is invalid"
        }
        # P67 U3b (DEF-1): resolve the serving port right before each start so
        # an occupied or system-reserved 6268 falls back instead of dying.
        $Port = Find-ManagedAvailablePort -PreferredPort $Port
        $process = Start-And-Probe $active ([string]$current.version)
        if ($process -and $current.awaiting_health) {
            & $PythonExe $Helper confirm --install-root $InstallRoot --version $current.version
            if ($LASTEXITCODE -ne 0) {
                Stop-Process -Id $process.Id -Force
                $process = $null
            }
        }
        if (-not $process -and $current.awaiting_health) {
            & $PythonExe $Helper rollback --install-root $InstallRoot
            if ($LASTEXITCODE -ne 0) { throw "Updated client failed health check and rollback was unavailable" }
            $current = Get-Content -Raw -Encoding UTF8 (Join-Path $StateRoot "current.json") | ConvertFrom-Json
            $active = Get-ActiveRoot
            if (-not [string]::Equals((Split-Path -Leaf $active), [string]$current.version, [StringComparison]::Ordinal)) {
                throw "Rolled-back CourseLens version identity is invalid"
            }
            $process = Start-And-Probe $active ([string]$current.version)
        }
        if (-not $process) { throw "CourseLens failed its startup health check" }
        # P68: opening is the server's job now (--window opens the native
        # shell window; the browser is only the server's own fallback), so
        # nothing is opened here anymore.
        Wait-Process -Id $process.Id

        # The service exited on its own: restart only for a pending verified
        # switch, and only within the per-session automatic-restart budget.
        $pending = Join-Path $StateRoot "pending.json"
        if (-not (Test-Path -LiteralPath $pending -PathType Leaf)) { break }
        if ($automaticRestarts -ge 1) { break }
        $automaticRestarts += 1
    }
} finally {
    if ($owns) { try { $mutex.ReleaseMutex() } catch {} }
    $mutex.Dispose()
}
