# 打包 MemGuard（GUI 子系统，无控制台窗口）
#
#   .\build.ps1              单文件 dist\mem_guard.exe（默认，便于跨机器分发）
#   .\build.ps1 -OneDir      目录版 dist\mem_guard\mem_guard.exe（启动快、只有单进程）
#   .\build.ps1 -NoTk        精简：排除 tkinter/tcl-tk，体积再小约 10MB
#                            （代价：无 tkinter 时"内存趋势"窗口不可用、"Top10"回退 MessageBox）
#   .\build.ps1 -Upx         启用 UPX 压缩（体积约减半；可能提高杀软误报率、启动略慢）
#                            自动查找 PATH 中的 upx 或 tools\upx-*\upx.exe
param(
    [switch]$OneDir,
    [switch]$NoTk,
    [switch]$Upx
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# 清理旧产物，避免残留干扰
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

# 1) 生成程序图标（仅当不存在时）
if (-not (Test-Path mem_guard.ico)) {
    python make_icon.py
}

# 2) 组装 PyInstaller 参数
$pyiArgs = @('--noconsole', '--name', 'mem_guard', '--icon', 'mem_guard.ico',
             '--hidden-import', 'pystray._win32', '--clean')
if (-not $OneDir) { $pyiArgs += '--onefile' }
if ($NoTk)        { $pyiArgs += @('--exclude-module', 'tkinter') }

# 排除开发机上可能被连带打包、但本程序完全用不到的大模块
# （例如本机装了 numpy 时 PyInstaller 会顺带打进 numpy.libs，体积多出约 10MB）
$pyiArgs += @(
    '--exclude-module', 'numpy',
    '--exclude-module', 'pandas',
    '--exclude-module', 'matplotlib',
    '--exclude-module', 'scipy',
    '--exclude-module', 'pytest',
    '--exclude-module', 'setuptools'
)

# UPX 压缩：默认关闭（压缩后的 exe 更易被杀软启发式误报），需显式 -Upx 才启用
if ($Upx) {
    $upxPath = $null
    $cmd = Get-Command upx -ErrorAction SilentlyContinue
    if ($cmd) { $upxPath = $cmd.Source }
    if (-not $upxPath) {
        $found = Get-ChildItem -Path (Join-Path $PSScriptRoot 'tools') -Recurse -Filter upx.exe -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($found) { $upxPath = $found.FullName }
    }
    if ($upxPath) {
        $pyiArgs += @('--upx-dir', (Split-Path -Parent $upxPath))
        Write-Host ('[i] 启用 UPX 压缩：' + $upxPath)
    } else {
        Write-Host '[!] 未找到 upx.exe（可放到 tools\upx-*\upx.exe 或安装到 PATH），本次忽略 -Upx'
    }
}

Write-Host ('[i] PyInstaller: ' + ($pyiArgs -join ' '))

# 输出重定向到文件：PyInstaller 的 INFO 走 stderr，
# 在 $ErrorActionPreference='Stop' 下会被 PowerShell 误判为失败并中断，故调用时临时降级
$prevEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
# 日志写在系统临时目录并带时间戳：避免历史文件被其他进程占用导致构建中断
$logFile = Join-Path $env:TEMP ('mem_guard_build_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '.log')
python -m PyInstaller @pyiArgs mem_guard.py *> $logFile
$code = $LASTEXITCODE
$ErrorActionPreference = $prevEap

if ($code -ne 0) {
    Get-Content $logFile -Tail 30
    throw ('打包失败，详见 ' + $logFile)
}
Get-Content $logFile -Tail 2
try { Remove-Item $logFile -Force -ErrorAction Stop } catch { }

$exeRel = if ($OneDir) { 'dist\mem_guard\mem_guard.exe' } else { 'dist\mem_guard.exe' }
$exePath = Join-Path $PSScriptRoot $exeRel
if (-not (Test-Path $exePath)) {
    throw ('未找到构建产物：' + $exeRel)
}

# 冒烟测试：运行产物自检（GUI 子系统没有控制台输出，只看退出码）
$prevEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $exePath --selftest | Out-Null
$smoke = $LASTEXITCODE
$ErrorActionPreference = $prevEap
if ($smoke -ne 0) {
    throw ('冒烟测试失败：产物自检退出码 ' + $smoke)
}

Write-Host ''
if ($OneDir) {
    Write-Host ('构建完成（目录版，单进程 / 启动快；冒烟自检通过）：dist\mem_guard\mem_guard.exe')
    Write-Host '分发请整目录拷贝（压缩后解压即可用）。'
} else {
    Write-Host ('构建完成（单文件 {0:N1} MB；冒烟自检通过）：dist\mem_guard.exe' -f ((Get-Item $exePath).Length / 1MB))
    Write-Host '注：单文件版运行时会解压到临时目录，进程列表里会看到"引导器 + 程序"两个进程，属正常现象。'
}
