#Requires -RunAsAdministrator
<#
 .SYNOPSIS
    Create a fixed-size pagefile (virtual memory) on drive D to relieve "out of memory" errors.
#>

$ErrorActionPreference = 'Stop'

$TargetDrive = 'D:'
$InitialMB   = 16384
$MaximumMB   = 32768

function Write-Info($m) { Write-Host ('[*] ' + $m) -ForegroundColor Cyan }
function Write-Warn($m) { Write-Host ('[!] ' + $m) -ForegroundColor Yellow }
function Write-Ok($m)   { Write-Host ('[+] ' + $m) -ForegroundColor Green }

# 1. check drive D
$d = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$TargetDrive'"
if (-not $d) {
    Write-Warn ('Cannot find drive ' + $TargetDrive + '. Edit the $TargetDrive variable at the top of this script.')
    exit 1
}
$freeGB = [math]::Round($d.FreeSpace / 1GB, 1)
Write-Info ('Target drive ' + $TargetDrive + ' free space: ' + $freeGB + 'GB')
if ($d.FreeSpace -lt ($MaximumMB * 1.05 * 1MB)) {
    Write-Warn ('Not enough free space. Need at least ' + [math]::Round($MaximumMB / 1024, 1) + 'GB. Clean drive D or lower MaximumMB.')
    exit 1
}

# 3. disable automatic management
Write-Info 'Disabling system-managed pagefile...'
Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management' -Name AutomaticManagedPagefile -Value 0 -Type DWord

# 4. remove existing pagefile settings
Write-Info 'Cleaning existing pagefile settings...'
Get-CimInstance Win32_PageFileSetting | ForEach-Object {
    Write-Host ('    removing ' + $_.Name)
    Remove-CimInstance $_ -ErrorAction SilentlyContinue
}

# 5. create new pagefile on D
Write-Info ('Creating pagefile on ' + $TargetDrive + ' (initial ' + $InitialMB + 'MB / max ' + $MaximumMB + 'MB)...')
New-CimInstance -ClassName Win32_PageFileSetting -Property @{
    Name        = ($TargetDrive + '\pagefile.sys')
    InitialSize = [uint64]$InitialMB
    MaximumSize = [uint64]$MaximumMB
} | Out-Null

# 6. registry fallback
Write-Info 'Writing registry PagingFiles...'
Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management' -Name PagingFiles -Value ($TargetDrive + '\pagefile.sys ' + $InitialMB + ' ' + $MaximumMB) -Type MultiString

Write-Ok 'Done! Pagefile takes effect after a REBOOT.'
Write-Warn 'Save your work and reboot (current session unaffected).'
Write-Host 'After reboot, verify with: Get-CimInstance Win32_PageFileUsage'
