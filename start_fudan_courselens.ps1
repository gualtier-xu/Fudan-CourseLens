param(
    [ValidateRange(1, 65535)]
    [int]$PreferredPort = 8765,
    [ValidateRange(1, 65535)]
    [int]$MaxPort = 8775,
    [switch]$NoOpen,
    [switch]$RepairRuntime,
    [switch]$CheckRuntime,
    [switch]$Restart
)

$ErrorActionPreference = "Stop"
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $OutputEncoding = [Console]::OutputEncoding
} catch {
}


function Write-Step {
    param([string]$Message)
    Write-Host "[FudanCourseLens] $Message"
}


function Get-ProjectToken {
    param([string]$Root)
    # Canonicalize the root before hashing: the Python side derives
    # PROJECT_INSTANCE_ID from Path.resolve(), so launching through a
    # junction/subst/symlink must hash the same final path or the attach and
    # restart identity silently diverges. Best effort: fall back to the raw
    # path when the Win32 query is unavailable.
    try {
        if (-not ("CourseLens.Native.Path" -as [type])) {
            Add-Type -Namespace CourseLens.Native -Name PathX -MemberDefinition @"
[DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
public static extern System.IntPtr CreateFileW(string fileName, uint desiredAccess, uint shareMode, System.IntPtr securityAttributes, uint creationDisposition, uint flagsAndAttributes, System.IntPtr templateFile);
[DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
public static extern System.IntPtr GetFinalPathNameByHandleW(System.IntPtr handle, System.Text.StringBuilder buffer, uint length, uint flags);
[DllImport("kernel32.dll")]
public static extern bool CloseHandle(System.IntPtr handle);
"@
        }
        # OPEN_EXISTING(3), FILE_SHARE_READ|WRITE|DELETE(7), FILE_FLAG_BACKUP_SEMANTICS(0x02000000)
        $handle = [CourseLens.Native.PathX]::CreateFileW($Root, 0, 7, [System.IntPtr]::Zero, 3, 0x02000000, [System.IntPtr]::Zero)
        if ($handle -ne [System.IntPtr]::Zero) {
            try {
                $buffer = [System.Text.StringBuilder]::new(1024)
                $length = [CourseLens.Native.PathX]::GetFinalPathNameByHandleW($handle, $buffer, 1024, 0)
                if ([int]$length -gt 0 -and [int]$length -lt 1024) {
                    $final = $buffer.ToString(0, [int]$length)
                    if ($final.StartsWith('\\?\')) { $final = $final.Substring(4) }
                    if ($final -and (Test-Path -LiteralPath $final -PathType Container)) { $Root = $final }
                }
            } finally {
                [void][CourseLens.Native.PathX]::CloseHandle($handle)
            }
        }
    } catch {
    }
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Root.ToLowerInvariant())
        $digest = $sha256.ComputeHash($bytes)
        return (-join ($digest[0..7] | ForEach-Object { $_.ToString("x2") }))
    } finally {
        $sha256.Dispose()
    }
}


function Get-ServiceHealth {
    param([int]$Port, [string]$ExpectedInstanceId, [int]$TimeoutMs = 400)
    $response = $null
    $reader = $null
    try {
        $request = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/api/health")
        $request.Proxy = $null
        $request.Timeout = $TimeoutMs
        $request.ReadWriteTimeout = $TimeoutMs
        $response = $request.GetResponse()
        $reader = [System.IO.StreamReader]::new($response.GetResponseStream(), [System.Text.Encoding]::UTF8)
        $payload = $reader.ReadToEnd() | ConvertFrom-Json
        if ($payload.ok -eq $true -and $payload.service -eq "fudan-courselens" -and $payload.instance_id -eq $ExpectedInstanceId) {
            return "http://127.0.0.1:$Port/"
        }
    } catch {
        return $null
    } finally {
        if ($reader) { $reader.Dispose() }
        if ($response) { $response.Dispose() }
    }
    return $null
}


function Find-RunningService {
    param([int]$StartPort, [int]$EndPort, [string]$ExpectedInstanceId, [string]$DataRoot)
    # The live service publishes its identity into the data root
    # (runtime/lifecycle.py InstanceLock.publish); read that evidence file
    # first, then fall back to the bounded health scan.
    $instanceFile = Join-Path $DataRoot "server-instance.json"
    if (Test-Path -LiteralPath $instanceFile -PathType Leaf) {
        try {
            $instance = Get-Content -LiteralPath $instanceFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $instancePort = [int]$instance.port
            if ($instance.instance_id -eq $ExpectedInstanceId -and $instancePort -ge $StartPort -and $instancePort -le $EndPort) {
                $url = Get-ServiceHealth -Port $instancePort -ExpectedInstanceId $ExpectedInstanceId
                if ($url) { return $url }
            }
        } catch {
        }
    }
    for ($port = $StartPort; $port -le $EndPort; $port++) {
        $url = Get-ServiceHealth -Port $port -ExpectedInstanceId $ExpectedInstanceId
        if ($url) { return $url }
    }
    return $null
}


function Find-LauncherWindow {
    param([string]$Title)
    # Twenty-first case: a second launch points at the live instance by
    # bringing its launcher window to the front. Best effort: desktop
    # shells without a matching console window stay silent here.
    try {
        if (-not ("CourseLens.Native.Window" -as [type])) {
            Add-Type -Namespace CourseLens.Native -Name Window -MemberDefinition @"
[DllImport("user32.dll", CharSet = CharSet.Unicode)]
public static extern System.IntPtr FindWindowW(string className, string windowTitle);
[DllImport("user32.dll")]
public static extern bool SetForegroundWindow(System.IntPtr handle);
[DllImport("user32.dll")]
public static extern bool ShowWindow(System.IntPtr handle, int command);
"@
        }
    } catch {
    }
    try {
        $handle = [CourseLens.Native.Window]::FindWindowW($null, $Title)
        if ($handle -ne [System.IntPtr]::Zero) {
            [void][CourseLens.Native.Window]::ShowWindow($handle, 9)
            [void][CourseLens.Native.Window]::SetForegroundWindow($handle)
            return $true
        }
    } catch {
    }
    return $false
}


function Get-InstanceEvidence {
    param([string]$DataRoot)
    $path = Join-Path $DataRoot "server-instance.json"
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        try {
            return Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
        } catch {
        }
    }
    return $null
}


function Test-InstanceProcessIdentity {
    param([int]$OwnerPid, [long]$StartedAtMicroseconds)
    $process = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$OwnerPid" -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $false }
    if ($process.Name -notmatch '(?i)^python') { return $false }
    if ($StartedAtMicroseconds -le 0) { return $false }
    try {
        $createdUtc = [System.Management.ManagementDateTimeConverter]::ToDateTime($process.CreationDate).ToUniversalTime()
        $createdMicroseconds = [DateTimeOffset]::new($createdUtc).ToUnixTimeMilliseconds() * 1000
    } catch {
        return $false
    }
    # lifecycle stores psutil create_time in microseconds; the WMI/DMTF round
    # trip adds timezone and precision noise, so equality is a 2-second window.
    return ([Math]::Abs($createdMicroseconds - $StartedAtMicroseconds) -le 2000000)
}


function Stop-LiveInstance {
    param([string]$DataRoot, [string]$ExpectedInstanceId)
    # Twenty-first case: a restart stops the previous backend only when the
    # instance file pid, the live health identity, and the process creation
    # time all agree. Anything else stays fail-closed.
    $evidence = Get-InstanceEvidence -DataRoot $DataRoot
    if ($null -eq $evidence) { return $false }
    $ownerPid = 0
    $port = 0
    try {
        $ownerPid = [int]$evidence.pid
        $port = [int]$evidence.port
    } catch {
        return $false
    }
    if ($ownerPid -le 0 -or $port -le 0) { return $false }
    if ([string]$evidence.instance_id -ne $ExpectedInstanceId) { return $false }
    if ($null -eq (Get-ServiceHealth -Port $port -ExpectedInstanceId $ExpectedInstanceId)) { return $false }
    $startedAt = 0L
    try { $startedAt = [long]$evidence.process_started_at } catch { return $false }
    if (-not (Test-InstanceProcessIdentity -OwnerPid $ownerPid -StartedAtMicroseconds $startedAt)) { return $false }
    Write-Step "Stopping the previous CourseLens service (pid $ownerPid) for a clean restart"
    Stop-Process -Id $ownerPid -Force
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($null -eq (Get-ServiceHealth -Port $port -ExpectedInstanceId $ExpectedInstanceId)) { break }
        Start-Sleep -Milliseconds 250
    }
    return $true
}


function Stop-OwnBackendChildren {
    # Exit sweep: after the server pipeline ends for any reason, give a
    # graceful drain a short grace window, then stop only direct python
    # children of this launcher process, never an unrelated process.
    $children = @()
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        $children = @(Get-CimInstance -ClassName Win32_Process -Filter "ParentProcessId=$PID" -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '(?i)^python' })
        if (-not $children) { return }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    foreach ($child in $children) {
        try { Stop-Process -Id ([int]$child.ProcessId) -Force -ErrorAction Stop } catch {
        }
    }
}


function Open-ServiceUi {
    param([string]$Url)
    if (-not $NoOpen) { Start-Process $Url }
}


function Test-PortAvailable {
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


function Test-CourseLensServicePort {
    param([string]$HostName, [int]$Port, [int]$TimeoutMs = 500)
    if (-not (Test-LocalTcpOpen -HostName $HostName -Port $Port -TimeoutMs $TimeoutMs)) { return $false }
    $response = $null
    $reader = $null
    try {
        $request = [System.Net.HttpWebRequest]::Create("http://$HostName`:$Port/api/health")
        $request.Proxy = $null
        $request.Timeout = $TimeoutMs
        $request.ReadWriteTimeout = $TimeoutMs
        $response = $request.GetResponse()
        $reader = [System.IO.StreamReader]::new($response.GetResponseStream(), [System.Text.Encoding]::UTF8)
        $payload = $reader.ReadToEnd() | ConvertFrom-Json
        return ($payload.service -eq "fudan-courselens")
    } catch {
        return $false
    } finally {
        if ($reader) { $reader.Dispose() }
        if ($response) { $response.Dispose() }
    }
}


function Find-AvailablePort {
    param([int]$StartPort, [int]$EndPort)
    for ($port = $StartPort; $port -le $EndPort; $port++) {
        if (Test-PortAvailable -Port $port) { return $port }
    }
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        $port = [int]$listener.LocalEndpoint.Port
        Write-Step "Configured ports $StartPort-$EndPort are unavailable; using system-assigned port $port."
        return $port
    } finally {
        $listener.Stop()
    }
}


function Test-PythonDependencies {
    param([string]$PythonPath)
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { return $false }
    try {
        & $PythonPath -c "import requests, curl_cffi, pypdf, Crypto, markdown, psutil, pygments, nacl, zstandard, PIL; from zoneinfo import ZoneInfo; ZoneInfo('Asia/Shanghai'); from src.application import CourseLensApplication" 2>$null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}


function Find-PythonExecutable {
    param([string]$Root)
    $clientPython = Join-Path $Root ".venv-client-py310\Scripts\python.exe"
    if (-not (Test-PythonDependencies -PythonPath $clientPython)) {
        throw "The CourseLens client environment is missing or incomplete; run start_fudan_courselens.ps1 -RepairRuntime after SetupFudanCourseLensRuntime.cmd"
    }
    return (Resolve-Path -LiteralPath $clientPython).Path
}


function Ensure-LocalRuntime {
    param([string]$Root)
    $clientPython = Join-Path $Root ".venv-client-py310\Scripts\python.exe"
    $ready = Test-PythonDependencies -PythonPath $clientPython
    if ($ready -and -not $RepairRuntime) {
        Write-Step "Lightweight client runtime is ready; setup skipped."
        return
    }
    $setupScript = Join-Path $Root "scripts\setup_fudan_courselens_runtime.ps1"
    if (-not (Test-Path -LiteralPath $setupScript -PathType Leaf)) {
        throw "Runtime setup script is missing"
    }
    Write-Step "Preparing the CourseLens client runtime"
    if ($RepairRuntime) { & $setupScript -Repair } else { & $setupScript }
    if ($LASTEXITCODE -ne 0 -or -not (Test-PythonDependencies -PythonPath $clientPython)) {
        throw "Client runtime setup failed"
    }
}


function Test-LocalTcpOpen {
    param([string]$HostName, [int]$Port, [int]$TimeoutMs = 500)
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync($HostName, $Port)
        return $task.Wait($TimeoutMs) -and $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}


function Get-RemoteComputeSummary {
    param([string]$PythonPath)
    $code = @"
from credentials import CredentialStore
c = CredentialStore()
configured = all(c.has_secret(name) for name in ('github_app_access_token', 'github_worker_repo', 'github_mailbox_repo', 'worker_box_public_key', 'worker_signing_public_key'))
enabled = c.has_secret('remote_enabled') and c.load_secret('remote_enabled') == '1'
print(('configured' if configured else 'not configured') + ', ' + ('online service enabled' if enabled else 'setup required'))
"@
    try {
        $summary = & $PythonPath -c $code 2>$null
        if ($LASTEXITCODE -eq 0 -and $summary) { return $summary.Trim() }
    } catch {
    }
    return "status unavailable"
}


if ($MaxPort -lt $PreferredPort) {
    throw "MaxPort must be greater than or equal to PreferredPort"
}
$ProjectRoot = (Resolve-Path $PSScriptRoot).Path
Set-Location $ProjectRoot
$ProjectToken = Get-ProjectToken -Root $ProjectRoot
try { $Host.UI.RawUI.WindowTitle = "FudanCourseLens $ProjectToken" } catch {
}
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$DataRoot = if ($env:COURSELENS_DATA_DIR) {
    [System.IO.Path]::GetFullPath($env:COURSELENS_DATA_DIR)
} else {
    Join-Path $ProjectRoot "runtime\data"
}
New-Item -ItemType Directory -Force -Path $DataRoot, (Join-Path $ProjectRoot "runtime\cache"), (Join-Path $ProjectRoot "runtime\logs"), (Join-Path $ProjectRoot "runtime\reports") | Out-Null
$env:COURSELENS_DATA_DIR = $DataRoot

$runningUrl = if ($CheckRuntime) { $null } else { Find-RunningService -StartPort $PreferredPort -EndPort $MaxPort -ExpectedInstanceId $ProjectToken -DataRoot $DataRoot }
if ($runningUrl) {
    $null = Find-LauncherWindow -Title "FudanCourseLens $ProjectToken"
    if ($RepairRuntime) { throw "Stop the running service before repairing the runtime" }
    if ($Restart) {
        if (-not (Stop-LiveInstance -DataRoot $DataRoot -ExpectedInstanceId $ProjectToken)) {
            throw "A CourseLens service is already running, but its identity could not be verified for an automatic restart. Stop it from its own CourseLens window, then start again."
        }
        Write-Step "Previous service stopped; starting a fresh CourseLens service"
    } else {
        Write-Step "The service is already running: $runningUrl"
        Write-Step "Its CourseLens window was brought to the front; this window exits now. Use -Restart for a clean restart."
        Open-ServiceUi -Url $runningUrl
        exit 0
    }
}

$mutex = [System.Threading.Mutex]::new($false, "Local\FudanCourseLens_$ProjectToken")
$ownsMutex = $false
$transcriptStarted = $false
try {
    try { $ownsMutex = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) {
        throw "Another CourseLens launcher is already preparing the service"
    }
    $logDir = Join-Path $ProjectRoot "runtime\logs\server"
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $logPath = Join-Path $logDir ("server-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
    try {
        Start-Transcript -LiteralPath $logPath -Force | Out-Null
        $transcriptStarted = $true
    } catch {
    }
    Ensure-LocalRuntime -Root $ProjectRoot
    $PythonExe = Find-PythonExecutable -Root $ProjectRoot
    Write-Step "Using Python: $PythonExe"
    Write-Step "Student data directory: $DataRoot"
    if ($CheckRuntime) {
        Write-Step "Runtime check completed successfully"
        exit 0
    }
    Write-Step "Online service: $(Get-RemoteComputeSummary -PythonPath $PythonExe)"
    if (Test-LocalTcpOpen -HostName "127.0.0.1" -Port 6268) {
        if (Test-CourseLensServicePort -HostName "127.0.0.1" -Port 6268) {
            Write-Step "A CourseLens service is already listening on port 6268; not treated as the GitHub proxy"
        } else {
            Write-Step "Detected local proxy http://127.0.0.1:6268 for the GitHub integration"
        }
    }
    $Port = Find-AvailablePort -StartPort $PreferredPort -EndPort $MaxPort
    Write-Step "Starting local UI on http://127.0.0.1:$Port/"
    $PythonArgs = @("-m", "src", "serve", "--port", "$Port")
    if ($NoOpen) { $PythonArgs += "--no-open" }
    # BACKEND-DEATH-1 (unit 2): the transcript does not reliably capture native child output;
    # tee python stdout/stderr into a per-session log so every shutdown leaves a trace.
    # NIGHT2-W5: Windows PowerShell 5.1 renders the ErrorRecords merged in by 2>&1 as
    # red console error text. Stringify every pipeline item before Tee-Object so the
    # console stays calm; the tee file bytes and $LASTEXITCODE semantics are unchanged.
    $serverLogPath = Join-Path $logDir ("server-python-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
    Write-Step "Server log: $serverLogPath"
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $PythonExe @PythonArgs 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $serverLogPath
        $serverExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($serverExitCode -ne 0) { throw "Server exited with code $serverExitCode" }
} finally {
    Stop-OwnBackendChildren
    if ($transcriptStarted) { try { Stop-Transcript | Out-Null } catch {} }
    if ($ownsMutex) { try { $mutex.ReleaseMutex() } catch {} }
    $mutex.Dispose()
}
