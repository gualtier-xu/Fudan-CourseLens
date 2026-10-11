param(
    [switch]$Repair
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $ProjectRoot
$env:PYTHONDONTWRITEBYTECODE = "1"
$Manifest = Get-Content -Raw -Encoding UTF8 (Join-Path $ProjectRoot "runtime-assets.json") | ConvertFrom-Json
$CacheDir = Join-Path $ProjectRoot "runtime\cache\runtime-bootstrap"
$PythonDir = Join-Path $ProjectRoot $Manifest.python.install_dir
$PythonExe = Join-Path $PythonDir "python.exe"
$ClientVenv = Join-Path $ProjectRoot $Manifest.client_environment.install_dir
$ClientPython = Join-Path $ClientVenv "Scripts\python.exe"
$ClientLockFile = Join-Path $ProjectRoot $Manifest.client_environment.lock_file


function Write-Step {
    param([string]$Message)
    Write-Host "[FudanCourseLensSetup] $Message"
}


function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}


function Assert-ManagedPath {
    param([string]$Path, [string]$Expected)
    $resolvedParent = (Resolve-Path -LiteralPath (Split-Path -Parent $Path)).Path
    $resolved = Join-Path $resolvedParent (Split-Path -Leaf $Path)
    if (-not [string]::Equals($resolved, $Expected, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify unexpected path: $resolved"
    }
    if (-not $resolved.StartsWith($ProjectRoot + [IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the project: $resolved"
    }
}


function Get-VerifiedDownload {
    param([string]$Url, [string]$Destination, [string]$ExpectedSha256)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
    $expected = $ExpectedSha256.ToUpperInvariant()
    if ((Test-Path -LiteralPath $Destination -PathType Leaf) -and (Get-Sha256 $Destination) -eq $expected) {
        Write-Step "Using verified cache: $(Split-Path -Leaf $Destination)"
        return
    }
    if (Test-Path -LiteralPath $Destination) {
        Remove-Item -LiteralPath $Destination -Force
    }
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    Write-Step "Downloading $(Split-Path -Leaf $Destination)"
    if ($curl) {
        & $curl.Source --location --fail --retry 3 --output $Destination $Url
        if ($LASTEXITCODE -ne 0) {
            throw "Download failed"
        }
    } else {
        $previousProtocol = [Net.ServicePointManager]::SecurityProtocol
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $Url -OutFile $Destination -UserAgent "CourseLens-Setup/1" -UseBasicParsing
        } finally {
            [Net.ServicePointManager]::SecurityProtocol = $previousProtocol
        }
    }
    if ((Get-Sha256 $Destination) -ne $expected) {
        Remove-Item -LiteralPath $Destination -Force
        throw "Downloaded runtime failed SHA-256 verification"
    }
}


function Test-PythonBase {
    if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
        return $false
    }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $version = & $PythonExe -c "import platform; print(platform.python_version())" 2>$null
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($exitCode -ne 0 -or $version.Trim() -ne [string]$Manifest.python.version) {
        return $false
    }
    $expectedHash = [string]$Manifest.python.python_exe_sha256
    return (-not $expectedHash) -or ((Get-Sha256 $PythonExe) -eq $expectedHash.ToUpperInvariant())
}


function Install-PythonBase {
    if (Test-Path -LiteralPath $PythonDir) {
        throw "tools/python310 exists but failed validation; remove it only after preserving runtime/data"
    }
    if ([string]$Manifest.python.distribution -ne "nuget") {
        throw "Unsupported Python runtime distribution"
    }
    $archive = Join-Path $CacheDir "python-$($Manifest.python.version)-amd64.nupkg"
    $staging = Join-Path $CacheDir "python-nuget-staging"
    Get-VerifiedDownload -Url $Manifest.python.url -Destination $archive -ExpectedSha256 $Manifest.python.sha256
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $PythonDir) | Out-Null
    Assert-ManagedPath -Path $PythonDir -Expected (Join-Path $ProjectRoot "tools\python310")
    Assert-ManagedPath -Path $staging -Expected (Join-Path $CacheDir "python-nuget-staging")
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    Write-Step "Extracting project-local Python $($Manifest.python.version)"
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [System.IO.Compression.ZipFile]::ExtractToDirectory($archive, $staging)
        $source = Join-Path $staging "tools"
        if (-not (Test-Path -LiteralPath (Join-Path $source "python.exe") -PathType Leaf)) {
            throw "Python NuGet archive is missing tools/python.exe"
        }
        $unsafe = Get-ChildItem -LiteralPath $source -Recurse -Force | Where-Object {
            $_.Attributes -band [IO.FileAttributes]::ReparsePoint
        }
        if ($unsafe) {
            throw "Python NuGet archive contains an unsafe link"
        }
        Move-Item -LiteralPath $source -Destination $PythonDir
    } finally {
        if (Test-Path -LiteralPath $staging) {
            Remove-Item -LiteralPath $staging -Recurse -Force
        }
    }
    if (-not (Test-PythonBase)) {
        throw "Project-local Python NuGet runtime failed validation"
    }
}


function Test-ClientEnvironment {
    if (-not (Test-Path -LiteralPath $ClientPython -PathType Leaf)) {
        return $false
    }
    $code = "import requests, curl_cffi, pypdf, Crypto, markdown, psutil, pygments, nacl, zstandard, PIL; from zoneinfo import ZoneInfo; ZoneInfo('Asia/Shanghai'); from src.application import CourseLensApplication"
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $ClientPython -c $code 2>$null
        $exitCode = $LASTEXITCODE
        if ($exitCode -eq 0) {
            & $ClientPython -m pip check 2>$null | Out-Null
            $exitCode = $LASTEXITCODE
        }
    } finally {
        $ErrorActionPreference = $previous
    }
    return $exitCode -eq 0
}


function Install-ClientEnvironment {
    if (Test-Path -LiteralPath $ClientVenv) {
        Assert-ManagedPath -Path $ClientVenv -Expected (Join-Path $ProjectRoot ".venv-client-py310")
        Remove-Item -LiteralPath $ClientVenv -Recurse -Force
    }
    Write-Step "Creating the CourseLens client environment"
    & $PythonExe -m venv $ClientVenv
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the client environment"
    }
    & $ClientPython -m pip install --disable-pip-version-check --timeout 180 --retries 10 "pip==$($Manifest.client_environment.pip_version)"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install pinned pip"
    }
    & $ClientPython -m pip install --disable-pip-version-check --timeout 180 --retries 10 -r $ClientLockFile
    if ($LASTEXITCODE -ne 0 -or -not (Test-ClientEnvironment)) {
        throw "Client dependency installation or validation failed"
    }
}


New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
if (-not (Test-PythonBase)) {
    Install-PythonBase
}
Write-Step "Python base ready: $PythonExe"

if ($Repair -or -not (Test-ClientEnvironment)) {
    Install-ClientEnvironment
}
Write-Step "Client environment ready: $ClientPython"
Write-Step "CourseLens runtime setup completed"
