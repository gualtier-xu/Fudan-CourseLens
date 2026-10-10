param(
    [string]$PublicWorkerPath = "",
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ClientPython = Join-Path $ProjectRoot ".venv-client-py310\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $ClientPython -PathType Leaf)) {
    throw "CourseLens client Python is missing. Run SetupFudanCourseLensRuntime.cmd first."
}

$AuditRoot = Join-Path $ProjectRoot "runtime\cache\dependency-audit"
$AuditPython = Join-Path $AuditRoot "Scripts\python.exe"
$ReportRoot = Join-Path $ProjectRoot "runtime\reports\dependency-audit"
New-Item -ItemType Directory -Force -Path $ReportRoot | Out-Null

if (-not (Test-Path -LiteralPath $AuditPython -PathType Leaf)) {
    & $ClientPython -m venv $AuditRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the isolated dependency-audit environment."
    }
}
if (-not $SkipInstall) {
    & $AuditPython -m pip install --disable-pip-version-check --quiet `
        "pip==26.1.2" "pip-audit==2.10.1"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install the pinned dependency-audit tool."
    }
}

if (-not $PublicWorkerPath) {
    $PublicWorkerPath = Join-Path (Split-Path $ProjectRoot -Parent) "Fudan-CourseLens"
}

$targets = @(
    @{
        Name = "client"
        Requirements = Join-Path $ProjectRoot "requirements-client-py310.lock.txt"
        Extra = @()
    },
    @{
        Name = "test"
        Requirements = Join-Path $ProjectRoot "requirements-test-py310.lock.txt"
        Extra = @()
    },
    @{
        Name = "worker-source"
        Requirements = Join-Path $ProjectRoot "worker\requirements.txt"
        Extra = @()
    }
)
if (Test-Path -LiteralPath (Join-Path $PublicWorkerPath "requirements.txt") -PathType Leaf) {
    $targets += @{
        Name = "public-worker"
        Requirements = Join-Path $PublicWorkerPath "requirements.txt"
        Extra = @()
    }
}

$failed = $false
$results = foreach ($target in $targets) {
    $report = Join-Path $ReportRoot ($target.Name + ".json")
    $arguments = @(
        "-m", "pip_audit",
        "-r", $target.Requirements,
        "--format", "json",
        "--output", $report
    ) + $target.Extra
    & $AuditPython @arguments
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        $failed = $true
    }
    [pscustomobject]@{
        target = $target.Name
        exit_code = $exitCode
        report = $report.Replace($ProjectRoot + "\", "")
    }
}

$results | ConvertTo-Json
if ($failed) {
    exit 1
}
