#Requires -RunAsAdministrator
param(
    [ValidateSet('install','uninstall')]
    [string]$Mode = 'install'
)

$TaskName = 'MemGuard'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

if ($Mode -eq 'uninstall') {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host ('[+] Removed scheduled task: ' + $TaskName)
    exit 0
}

# 优先注册打包好的 exe（免 Python）：脚本同目录，或 dist\ 下
$exe = $null
foreach ($cand in @((Join-Path $ScriptDir 'mem_guard.exe'), (Join-Path $ScriptDir 'dist\mem_guard.exe'))) {
    if (Test-Path $cand) { $exe = $cand; break }
}

if ($exe) {
    $action   = New-ScheduledTaskAction -Execute $exe -WorkingDirectory (Split-Path -Parent $exe)
    $trigger  = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 3650)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force | Out-Null
    Write-Host ('[+] Installed scheduled task: ' + $TaskName)
    Write-Host '    Trigger    : at every user logon'
    Write-Host '    Run level  : Highest (administrator, NO UAC prompt)'
    Write-Host ('    Target     : ' + $exe)
    Write-Host '    Mode       : packaged exe (no Python needed)'
    Write-Host '    To remove  : run this script again with -Mode uninstall'
    exit 0
}

# 没有 exe：退回源码方式（pythonw.exe 优先，托盘程序无控制台窗口）
$PyScript = Join-Path $ScriptDir 'mem_guard.py'
$py = $null
foreach ($cand in @('pythonw.exe', 'python.exe', 'py.exe')) {
    try {
        $p = (Get-Command $cand -ErrorAction Stop).Source
        if ($p) { $py = $p; break }
    } catch { }
}
if (-not $py) {
    Write-Error 'No mem_guard.exe found and python/pythonw not in PATH. Run .\build.ps1 first, or install Python.'
    exit 1
}

$action   = New-ScheduledTaskAction -Execute $py -Argument $PyScript -WorkingDirectory $ScriptDir
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force | Out-Null

Write-Host ('[+] Installed scheduled task: ' + $TaskName)
Write-Host '    Trigger    : at every user logon'
Write-Host '    Run level  : Highest (administrator, NO UAC prompt)'
Write-Host ('    Interpreter: ' + $py)
Write-Host '    Mode       : source mode (tip: run .\build.ps1 to use the packaged exe instead)'
Write-Host '    To remove  : run this script again with -Mode uninstall'
