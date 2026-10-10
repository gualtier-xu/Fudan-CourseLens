param(
    [string]$OutputRoot = "",
    [switch]$Launch
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if (-not $OutputRoot) {
    $OutputRoot = Join-Path $ProjectRoot "output\windows-sandbox"
}
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
$expectedPrefix = $ProjectRoot + [IO.Path]::DirectorySeparatorChar
if (-not $OutputRoot.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Sandbox output must stay inside the CourseLens project output directory."
}

$dirty = git -C $ProjectRoot status --porcelain
if ($dirty) {
    throw "Commit or discard tracked changes before generating the sandbox bundle."
}

$SourceRoot = Join-Path $OutputRoot "source"
$ReportRoot = Join-Path $OutputRoot "reports"
$Archive = Join-Path $OutputRoot "source.zip"
$Config = Join-Path $OutputRoot "CourseLensCleanAcceptance.wsb"
New-Item -ItemType Directory -Force -Path $OutputRoot,$ReportRoot | Out-Null
if (Test-Path -LiteralPath $SourceRoot) {
    Remove-Item -LiteralPath $SourceRoot -Recurse -Force
}
if (Test-Path -LiteralPath $Archive) {
    Remove-Item -LiteralPath $Archive -Force
}

git -C $ProjectRoot archive --format=zip --output=$Archive HEAD
if ($LASTEXITCODE -ne 0) { throw "Could not create a clean Git archive." }
Expand-Archive -LiteralPath $Archive -DestinationPath $SourceRoot -Force
Remove-Item -LiteralPath $Archive -Force

function Escape-Xml([string]$Value) {
    return [Security.SecurityElement]::Escape($Value)
}
$sourceXml = Escape-Xml $SourceRoot
$reportXml = Escape-Xml $ReportRoot
$xml = @"
<Configuration>
  <VGpu>Disable</VGpu>
  <Networking>Enable</Networking>
  <AudioInput>Disable</AudioInput>
  <VideoInput>Disable</VideoInput>
  <PrinterRedirection>Disable</PrinterRedirection>
  <ClipboardRedirection>Disable</ClipboardRedirection>
  <ProtectedClient>Enable</ProtectedClient>
  <MemoryInMB>8192</MemoryInMB>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>$sourceXml</HostFolder>
      <SandboxFolder>C:\CourseLensSource</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
    <MappedFolder>
      <HostFolder>$reportXml</HostFolder>
      <SandboxFolder>C:\CourseLensReports</SandboxFolder>
      <ReadOnly>false</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\CourseLensSource\scripts\windows-sandbox\SandboxBootstrap.ps1</Command>
  </LogonCommand>
</Configuration>
"@
[IO.File]::WriteAllText($Config, $xml, [Text.UTF8Encoding]::new($false))

$manifest = @{
    schema = "courselens.windows-sandbox-bundle.v1"
    commit = (git -C $ProjectRoot rev-parse HEAD).Trim()
    generated_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    source_read_only = $true
    clipboard_disabled = $true
    report_root = $ReportRoot
    config = $Config
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $OutputRoot "manifest.json") -Encoding UTF8
Write-Output ($manifest | ConvertTo-Json)

if ($Launch) {
    $sandbox = Join-Path $env:WINDIR "System32\WindowsSandbox.exe"
    if (-not (Test-Path -LiteralPath $sandbox -PathType Leaf)) {
        throw "Windows Sandbox is not enabled. Enable Containers-DisposableClientVM and reboot first."
    }
    Start-Process -FilePath $sandbox -ArgumentList @($Config)
}
