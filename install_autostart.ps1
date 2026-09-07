#Requires -RunAsAdministrator
param(
    [ValidateSet('install','uninstall')]
    [string]$Mode = 'install'
)

$TaskName = 'MemGuard'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$PyScript  = Join-Path $ScriptDir 'mem_guard.py'

# locate pythonw.exe first (no console window for a tray app), fall back to python.exe / py.exe
$py = $null
foreach ($cand in @('pythonw.exe', 'python.exe', 'py.exe')) {
    try {
        $p = (Get-Command $cand -ErrorAction Stop).Source
        if ($p) { $py = $p; break }
    } catch { }
}
if (-not $py) {
    Write-Error 'python/pythonw not found in PATH. Install Python and add it to PATH, then retry.'
    exit 1
}

if ($Mode -eq 'uninstall') {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host ('[+] Removed scheduled task: ' + $TaskName)
    exit 0
}

$action = New-ScheduledTaskAction -Execute $py -Argument $PyScript -WorkingDirectory $ScriptDir
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force | Out-Null

Write-Host ('[+] Installed scheduled task: ' + $TaskName)
Write-Host '    Trigger    : at every user logon'
Write-Host '    Run level  : Highest (administrator, NO UAC prompt)'
Write-Host ('    Interpreter: ' + $py)
Write-Host '    To remove  : run this script again with -Mode uninstall'
