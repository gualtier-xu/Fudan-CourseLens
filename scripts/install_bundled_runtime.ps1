# PKG-MIGRATION-1 unit 2 (Card 7B / R-OP7). Unlike the development recipe
# (setup_fudan_courselens_runtime.ps1, which keeps tools/python310 as a
# pristine base plus a dedicated client environment), the bundled runtime
# installs the pinned client dependencies directly into the portable python312
# tree. A portable tree with in-place site-packages survives any destination
# path — including machine accounts with Chinese usernames — because there is
# no virtual environment, no interpreter-config rewriting, and no
# activation-script rewriting anywhere in this recipe.
#
# The script is offline-idempotent: an existing validated tree is kept as-is.

param(
    [switch]$Repair
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $ProjectRoot
$env:PYTHONDONTWRITEBYTECODE = "1"

$BundledVersion = "3.12.10"
$BundledDir = Join-Path $ProjectRoot "tools\python312"
$BundledPython = Join-Path $BundledDir "python.exe"
$NupkgUrl = "https://api.nuget.org/v3-flatcontainer/python/$BundledVersion/python.$BundledVersion.nupkg"
$NupkgSha256 = "0EB85C2DFCCCCF1B17352DE4C397F69194035B7D37149EACC16F1147D93DE3B8"
$PythonExeSha256 = "4D6F5F81A4BCA11191C4C7C6B43632694D0A4CE74E068619D8FDC161D469859A"
$LockFile = Join-Path $ProjectRoot "requirements-client-py312.lock.txt"
$CacheDir = Join-Path $ProjectRoot "runtime\cache\runtime-bootstrap"

function Write-Step {
    param([string]$Message)
    Write-Host "[FudanCourseLensBundledRuntime] $Message"
}


function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}


function Test-BundledPython {
    if (-not (Test-Path -LiteralPath $BundledPython -PathType Leaf)) {
        return $false
    }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $version = & $BundledPython -c "import platform; print(platform.python_version())" 2>$null
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($exitCode -ne 0 -or $version.Trim() -ne $BundledVersion) {
        return $false
    }
    return (Get-Sha256 $BundledPython) -eq $PythonExeSha256
}


function Install-BundledPython {
    if (Test-Path -LiteralPath $BundledDir) {
        throw "tools/python312 exists but failed validation; remove it only after preserving runtime/data"
    }
    if (Test-Path -LiteralPath (Join-Path $ProjectRoot "tools\python312.temp")) {
        Remove-Item -LiteralPath (Join-Path $ProjectRoot "tools\python312.temp") -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
    $archive = Join-Path $CacheDir "python-$BundledVersion-amd64.nupkg"
    $staging = Join-Path $CacheDir "python312-nuget-staging"
    if (-not (Test-Path -LiteralPath $archive -PathType Leaf) -or (Get-Sha256 $archive) -ne $NupkgSha256) {
        if (Test-Path -LiteralPath $archive) {
            Remove-Item -LiteralPath $archive -Force
        }
        $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
        Write-Step "Downloading Python $BundledVersion (nuget package)"
        if ($curl) {
            & $curl.Source --location --fail --retry 3 --output $archive $NupkgUrl
            if ($LASTEXITCODE -ne 0) {
                throw "Download failed"
            }
        } else {
            $previousProtocol = [Net.ServicePointManager]::SecurityProtocol
            try {
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                Invoke-WebRequest -Uri $NupkgUrl -OutFile $archive -UserAgent "CourseLens-Setup/1" -UseBasicParsing
            } finally {
                [Net.ServicePointManager]::SecurityProtocol = $previousProtocol
            }
        }
        if ((Get-Sha256 $archive) -ne $NupkgSha256) {
            Remove-Item -LiteralPath $archive -Force
            throw "Downloaded runtime failed SHA-256 verification"
        }
    } else {
        Write-Step "Using verified cache: $(Split-Path -Leaf $archive)"
    }
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    Write-Step "Extracting the bundled Python $BundledVersion"
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
        Move-Item -LiteralPath $source -Destination $BundledDir
    } finally {
        if (Test-Path -LiteralPath $staging) {
            Remove-Item -LiteralPath $staging -Recurse -Force
        }
    }
    if (-not (Test-BundledPython)) {
        throw "The bundled Python runtime failed validation"
    }
}


function Test-BundledDependencies {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $BundledPython -c "import requests, curl_cffi, pypdf, Crypto, markdown, psutil, pygments, nacl, zstandard, PIL; from zoneinfo import ZoneInfo; ZoneInfo('Asia/Shanghai'); from src.application import CourseLensApplication" 2>$null
        $exitCode = $LASTEXITCODE
        if ($exitCode -eq 0) {
            & $BundledPython -m pip check 2>$null | Out-Null
            $exitCode = $LASTEXITCODE
        }
    } finally {
        $ErrorActionPreference = $previous
    }
    return $exitCode -eq 0
}


if ($Repair -or -not (Test-BundledPython)) {
    Install-BundledPython
}
Write-Step "Bundled Python ready: $BundledPython"

if ($Repair -or -not (Test-BundledDependencies)) {
    Write-Step "Installing the pinned client dependencies into the bundled runtime"
    & $BundledPython -m pip install --disable-pip-version-check --no-warn-script-location --timeout 180 --retries 10 -r $LockFile
    if ($LASTEXITCODE -ne 0 -or -not (Test-BundledDependencies)) {
        throw "Bundled client dependency installation or validation failed"
    }
}
Write-Step "Bundled CourseLens runtime completed"
