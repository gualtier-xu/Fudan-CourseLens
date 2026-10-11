param(
    [ValidateRange(1, 120)]
    [int]$TimeoutSeconds = 30,
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [ValidateRange(1, 30)]
    [int]$SettlingSeconds = 3
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Launcher = Join-Path $ProjectRoot "start_fudan_courselens.ps1"
if (-not (Test-Path -LiteralPath $Launcher -PathType Leaf)) {
    throw "CourseLens launcher is missing."
}

$healthUrl = "http://127.0.0.1:$Port/api/health"
$existing = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($existing) {
    throw "Refusing to measure an occupied port"
}
$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
$launcherProcess = $null
$servicePid = 0
$healthy = $false
$idleMemory = 0
$cleanupConfirmed = $false
try {
    $launcherProcess = Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $Launcher,
            "-PreferredPort", "$Port", "-MaxPort", "$Port", "-NoOpen"
        ) `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -PassThru

    while ($stopwatch.Elapsed.TotalSeconds -lt $TimeoutSeconds -and -not $healthy) {
        Start-Sleep -Milliseconds 100
        try {
            $payload = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 1
            $healthy = $payload.ok -eq $true -and $payload.service -eq "fudan-courselens"
            if ($healthy) { $servicePid = [int]$payload.pid }
        } catch {
            $healthy = $false
        }
    }
    $stopwatch.Stop()
    if ($healthy) {
        Start-Sleep -Seconds $SettlingSeconds
        $service = Get-Process -Id $servicePid -ErrorAction SilentlyContinue
        if ($service) { $idleMemory = [int64]$service.WorkingSet64 }
    }
} finally {
    if ($servicePid) {
        Stop-Process -Id $servicePid -Force -ErrorAction SilentlyContinue
    }
    if ($launcherProcess -and -not $launcherProcess.HasExited) {
        Stop-Process -Id $launcherProcess.Id -Force -ErrorAction SilentlyContinue
    }
    $deadline = (Get-Date).AddSeconds(5)
    do {
        Start-Sleep -Milliseconds 100
        $remaining = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    } while ($remaining -and (Get-Date) -lt $deadline)
    $cleanupConfirmed = -not $remaining
}

$result = [pscustomobject]@{
    schema = "courselens.startup-measurement.v2"
    healthy = $healthy
    port = $Port
    startup_seconds = [Math]::Round($stopwatch.Elapsed.TotalSeconds, 3)
    idle_memory_bytes = $idleMemory
    launcher_pid = if ($launcherProcess) { $launcherProcess.Id } else { 0 }
    service_pid = $servicePid
    cleanup_confirmed = $cleanupConfirmed
}
$result | ConvertTo-Json

if (-not $healthy -or -not $cleanupConfirmed) {
    exit 1
}
