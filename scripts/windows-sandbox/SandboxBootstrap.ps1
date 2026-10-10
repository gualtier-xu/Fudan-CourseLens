$ErrorActionPreference = "Stop"
$SourceRoot = "C:\CourseLensSource"
$InstallRoot = "C:\CourseLens"
$ReportRoot = "C:\CourseLensReports"
New-Item -ItemType Directory -Force -Path $ReportRoot | Out-Null

$tools = @("python", "node", "conda", "ffmpeg") | ForEach-Object {
    $command = Get-Command $_ -ErrorAction SilentlyContinue
    [pscustomobject]@{ name = $_; present = [bool]$command }
}
$baseline = [ordered]@{
    schema = "courselens.windows-sandbox-baseline.v1"
    observed_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    computer = $env:COMPUTERNAME
    tools = $tools
    source_has_git = Test-Path -LiteralPath (Join-Path $SourceRoot ".git")
    source_has_runtime = Test-Path -LiteralPath (Join-Path $SourceRoot "runtime")
    source_has_downloads = Test-Path -LiteralPath (Join-Path $SourceRoot "downloads")
}
$baseline | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $ReportRoot "baseline.json") -Encoding UTF8
if (($tools | Where-Object present) -or $baseline.source_has_git -or $baseline.source_has_runtime -or $baseline.source_has_downloads) {
    throw "The sandbox baseline is not clean. Review C:\CourseLensReports\baseline.json."
}

if (Test-Path -LiteralPath $InstallRoot) {
    Remove-Item -LiteralPath $InstallRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
Copy-Item -Path (Join-Path $SourceRoot "*") -Destination $InstallRoot -Recurse -Force

$instructions = @"
CourseLens clean Windows acceptance

1. Wait for the launcher to download and verify its private Python runtime.
2. Complete GitHub Device Flow and repair the Worker.
3. Never paste secrets into this file, a terminal, screenshots, traces, or logs.
4. Enter rotated iCourse and DeepSeek credentials only in the local browser form.
5. Before closing the sandbox, confirm no active run, Artifact, Mailbox Issue, or temporary token remains.
"@
Set-Content -LiteralPath (Join-Path $ReportRoot "README.txt") -Value $instructions -Encoding UTF8

Start-Process -FilePath (Join-Path $InstallRoot "OpenFudanCourseLens.cmd") -WorkingDirectory $InstallRoot
