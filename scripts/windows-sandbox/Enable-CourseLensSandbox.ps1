param(
    [switch]$Restart
)

$ErrorActionPreference = "Stop"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "Run this script from an elevated PowerShell window."
}

$feature = Get-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM
if ($feature.State -ne "Enabled") {
    Enable-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM -All -NoRestart | Out-Null
}

$feature = Get-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM
if ($feature.State -ne "Enabled") {
    throw "Windows Sandbox could not be enabled."
}

Write-Output "Windows Sandbox is enabled. A restart is required before acceptance testing."
if ($Restart) {
    Restart-Computer
}
