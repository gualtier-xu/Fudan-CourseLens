# Build (or statically validate) the CourseLens per-user installer shell.
#
# PKG-MIGRATION-1 unit 3. The iss shell itself is installer/courselens.iss;
# this script stages the payload from a clean working tree, hands it to ISCC,
# and produces the student-readable SHA-256 receipt (校验单.txt).
# Code signing is intentionally NOT part of this script: it belongs to
# release runbook R2 in a separately authorized batch.
#
# Usage:
#   installer\build_installer.ps1 -ValidateOnly          # static checks + staging drill, no ISCC
#   installer\build_installer.ps1                        # full build with ISCC
#   installer\build_installer.ps1 -AllowDirty            # stage from a dirty tree (local drills only)

param(
    [string]$Version = "",
    [string]$OutDir = "",
    [string]$IsccPath = "",
    [int]$MaxSetupMB = 300,
    [switch]$ValidateOnly,
    [switch]$AllowDirty
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$IssFile = Join-Path $PSScriptRoot "courselens.iss"
if (-not $Version) {
    $Version = (Get-Content -Raw -Encoding UTF8 (Join-Path $RepoRoot "courselens-version.json") | ConvertFrom-Json).version
}
if (-not $OutDir) {
    $OutDir = Join-Path $RepoRoot "output\installer"
}


function Write-Step {
    param([string]$Message)
    Write-Host "[CourseLensInstaller] $Message"
}


function Assert-IssStaticContract {
    $raw = [IO.File]::ReadAllBytes($IssFile)
    if ($raw.Length -lt 3 -or $raw[0] -ne 0xEF -or $raw[1] -ne 0xBB -or $raw[2] -ne 0xBF) {
        throw "courselens.iss must be UTF-8 with BOM (Chinese installer messages)"
    }
    $iss = [IO.File]::ReadAllText($IssFile, [System.Text.UTF8Encoding]::new($true))
    $required = @(
        'AppId={{A2A00F7A-AD6C-4822-B23F-5155DDC320E0}',
        'DefaultDirName={localappdata}\Programs\CourseLens',
        'PrivilegesRequired=lowest',
        'MessagesFile: "ChineseSimplified.isl"',
        # INSTALLER-ICON: the uninstall entry must carry the product icon —
        # without it Inno writes no DisplayIcon value and 设置→应用 shows
        # the generic icon (0.1.0 defect).
        'UninstallDisplayIcon={app}\courselens-icon.ico',
        '-AllowExistingEmptyRoot',
        '{localappdata}\CourseLens',
        'WinHttp.WinHttpRequest.5.1',
        'fudan-courselens',
        'CurUninstallStepChanged',
        # UNINST-FIX-1: pin the corrected DelTree call form, not just the token
        # prefix — the old (False, False, True) arguments compiled clean while
        # deleting nothing (wildcard mode + no file deletion = no-op).
        'dataRoot := ExpandConstant(''{localappdata}\CourseLens'');',
        'DelTree(dataRoot, True, True, True);'
    )
    foreach ($token in $required) {
        if (-not $iss.Contains($token)) {
            throw "courselens.iss is missing the required contract token: $token"
        }
    }
    # 数据保护红线: the automatic uninstall-delete section must stay empty;
    # data removal only happens behind the uninstall prompt's explicit "No".
    $uninstallDelete = $iss.Substring($iss.IndexOf("[UninstallDelete]"))
    if ($uninstallDelete.Contains("DestDir:") -or $uninstallDelete.Contains("DelTree")) {
        throw "[UninstallDelete] must stay empty (data protection red line)"
    }
    # the Simplified Chinese language file is vendored next to this script
    # (official Inno installers do not ship it); fail here with a precise
    # message instead of a confusing ISCC lookup error on a fresh checkout
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot "ChineseSimplified.isl"))) {
        throw "installer\ChineseSimplified.isl is missing; it is vendored and must travel with the repo"
    }
    Write-Step "iss static contract: OK"
}


function Assert-StagingSource {
    param([string]$Root)
    $required = @(
        "courselens-version.json",
        "tools\python312\python.exe",
        "scripts\install_managed_client.ps1",
        "scripts\start_managed_courselens.ps1",
        "scripts\client_update_helper.py",
        "scripts\install_bundled_runtime.ps1",
        "config\client-update-trust.json",
        "requirements-client-py312.lock.txt",
        "start_fudan_courselens.ps1",
        "OpenFudanCourseLens.cmd",
        "SetupFudanCourseLensRuntime.cmd",
        "credentials.py",
        "path_utils.py",
        "runtime-assets.json",
        "frontend",
        "src",
        "shared"
    )
    foreach ($item in $required) {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $item))) {
            throw "Staging source is missing required item: $item"
        }
    }
}


function Assert-NoLeakage {
    param([string]$Root)
    foreach ($forbidden in @("runtime\data", "runtime\logs", ".git", ".venv-client-py310", ".venv-client-py312")) {
        if (Test-Path -LiteralPath (Join-Path $Root $forbidden)) {
            throw "Staged payload must not contain $forbidden"
        }
    }
    # PS 5.1 quirk: -Include with -LiteralPath + -Recurse does not filter;
    # extension filtering must go through Where-Object. Certificate bundles
    # shipped inside third-party site-packages (certifi) are public data,
    # not secret material.
    $secretFiles = Get-ChildItem -LiteralPath $Root -Recurse -Force -File |
        Where-Object {
            $_.Extension -in ".pfx", ".pem", ".key", ".sig", ".nupkg" -and
            $_.FullName -notmatch "\\site-packages\\"
        } |
        Select-Object -First 1
    if ($secretFiles) {
        throw "Staged payload contains a forbidden artifact: $($secretFiles.FullName)"
    }
    # line-anchored PEM blocks only, project files only: third-party
    # site-packages legitimately carry cert bundles (certifi) and standards
    # test vectors (pycryptodome self-tests); validator scripts carry the
    # pattern inside regex literals (build_client_update.py).
    $keyMaterial = Get-ChildItem -LiteralPath $Root -Recurse -Force -File |
        Where-Object {
            $_.Extension -in ".py", ".ps1", ".json", ".txt", ".js" -and
            $_.FullName -notmatch "\\site-packages\\"
        } |
        Select-String -Pattern "^-----BEGIN [A-Z ]*PRIVATE KEY-----" -List |
        Select-Object -First 1
    if ($keyMaterial) {
        throw "Staged payload contains private-key material: $($keyMaterial.Path)"
    }
    Write-Step "leakage scan: clean"
}


Assert-IssStaticContract

$status = & git -C $RepoRoot status --porcelain
if ($status -and -not $AllowDirty) {
    throw "Working tree is dirty; stage releases from a clean tree (or pass -AllowDirty for local drills)"
}

$payload = Join-Path $OutDir "payload"
if (Test-Path -LiteralPath $payload) {
    Remove-Item -LiteralPath $payload -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $payload | Out-Null
Write-Step "Staging payload from the working tree"

$copyDirs = @("frontend", "src", "shared", "scripts", "config", "docs\repository-readmes", "tools\python312")
foreach ($dir in $copyDirs) {
    $source = Join-Path $RepoRoot $dir
    if (Test-Path -LiteralPath $source) {
        # keep the declared nesting (tools\python312, docs\repository-readmes):
        # ensure the parent exists inside the payload, then copy onto the full
        # destination path so the leaf name is preserved
        $destParent = Join-Path $payload (Split-Path $dir -Parent)
        if (-not (Test-Path -LiteralPath $destParent)) {
            New-Item -ItemType Directory -Force -Path $destParent | Out-Null
        }
        Copy-Item -LiteralPath $source -Destination (Join-Path $payload $dir) -Recurse
    }
}
$copyFiles = @(
    "courselens-version.json",
    "credentials.py",
    "path_utils.py",
    "runtime-assets.json",
    "requirements-client-py310.lock.txt",
    "requirements-client-py312.lock.txt",
    "OpenFudanCourseLens.cmd",
    "SetupFudanCourseLensRuntime.cmd",
    "start_fudan_courselens.ps1"
)
foreach ($file in $copyFiles) {
    Copy-Item -LiteralPath (Join-Path $RepoRoot $file) -Destination $payload
}
# managed-state and dev-runtime exclusions that must never ride along.
# Top level only: recursive name matching would eat product code such as
# src\runtime. The copy list above is already selective; this is a net.
foreach ($junk in @("runtime", ".git", ".pytest_cache")) {
    $candidate = Join-Path $payload $junk
    if (Test-Path -LiteralPath $candidate) {
        Remove-Item -LiteralPath $candidate -Recurse -Force
    }
}

# N9-G INSTALLER-POLISH-1 U1: dev bytecode caches must not ride along. The
# managed launcher runs with PYTHONDONTWRITEBYTECODE=1, so compiled bytecode
# is pure download weight (measured -6.91MB / -24.3% with no startup penalty,
# N9-A2 U7 A/B) and would never be regenerated inside the managed install.
Get-ChildItem -LiteralPath $payload -Recurse -Force -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $payload -Recurse -Force -File |
    Where-Object { $_.Extension -in ".pyc", ".pyo" } |
    Remove-Item -Force
$bytecodeLeft = @(Get-ChildItem -LiteralPath $payload -Recurse -Force -File |
    Where-Object { $_.Extension -in ".pyc", ".pyo" -or $_.Name -eq "__pycache__" }).Count
if ($bytecodeLeft -ne 0) {
    throw "Bytecode purge left $bytecodeLeft artifacts in the staged payload"
}
Write-Step "bytecode purge: 0 pyc/pyo files in staged payload"

Assert-StagingSource -Root $payload
Assert-NoLeakage -Root $payload

$payloadMB = [Math]::Round((Get-ChildItem -LiteralPath $payload -Recurse -Force -File |
    Measure-Object -Property Length -Sum).Sum / 1MB, 1)
Write-Step ("Staged payload size: {0} MB" -f $payloadMB)

if ($ValidateOnly) {
    Write-Step "ValidateOnly: staging drill passed, ISCC build skipped"
    exit 0
}

if (-not $IsccPath) {
    $candidates = @()
    foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
        if ($base) {
            $candidates += (Join-Path $base "Inno Setup 6\ISCC.exe")
        }
    }
    $IsccPath = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
}
if (-not $IsccPath -or -not (Test-Path -LiteralPath $IsccPath -PathType Leaf)) {
    throw "ISCC.exe was not found; install Inno Setup 6.3+ or pass -IsccPath"
}

Write-Step "Compiling the installer with ISCC"
& $IsccPath ("/DAppVersion={0}" -f $Version) ('/DSourceRoot="{0}"' -f $payload) ('/O"{0}"' -f $OutDir) $IssFile
if ($LASTEXITCODE -ne 0) {
    throw "ISCC failed with exit code $LASTEXITCODE"
}

$setupExe = Join-Path $OutDir ("CourseLens-{0}-setup.exe" -f $Version)
if (-not (Test-Path -LiteralPath $setupExe -PathType Leaf)) {
    throw "ISCC reported success but the setup executable is missing: $setupExe"
}
$setupMB = [Math]::Round((Get-Item -LiteralPath $setupExe).Length / 1MB, 1)
if ($setupMB -gt $MaxSetupMB) {
    throw ("Setup size {0} MB exceeds the {1} MB ceiling" -f $setupMB, $MaxSetupMB)
}
$setupHash = (Get-FileHash -LiteralPath $setupExe -Algorithm SHA256).Hash.ToUpperInvariant()
$receipt = Join-Path $OutDir ("CourseLens-{0}-校验单.txt" -f $Version)
@(
    "CourseLens $Version 安装包校验单",
    "文件: CourseLens-$Version-setup.exe",
    "SHA-256: $setupHash",
    "大小: $setupMB MB",
    "",
    "对一下：在安装包所在的文件夹，地址栏输入 powershell 回车，执行 certutil -hashfile CourseLens-$Version-setup.exe SHA256，把输出的一串数字和上面的 SHA-256 对照，完全一致再安装（像核对快递单号一样仔细）。"
) | Set-Content -LiteralPath $receipt -Encoding UTF8
Write-Step ("Build complete: {0} ({1} MB)" -f $setupExe, $setupMB)
Write-Step "Receipt: $receipt"
