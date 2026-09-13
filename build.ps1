# 打包 MemGuard（GUI 子系统，无控制台窗口）
#
#   .\build.ps1              单文件 dist\mem_guard.exe（默认，便于跨机器分发）
#   .\build.ps1 -OneDir      目录版 dist\mem_guard\mem_guard.exe（启动快、只有单进程）
#   .\build.ps1 -NoTk        精简：排除 tkinter/tcl-tk，体积再小约 10MB
#                            （代价：无 tkinter 时"内存趋势"窗口不可用、"Top10"回退 MessageBox）
param(
    [switch]$OneDir,
    [switch]$NoTk
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

# UPX 可用时自动启用压缩（可再缩小约一半体积）
$upx = Get-Command upx -ErrorAction SilentlyContinue
if ($upx) {
    $pyiArgs += @('--upx-dir', (Split-Path -Parent $upx.Source))
    Write-Host ('[i] 检测到 UPX：' + $upx.Source + '（启用压缩）')
}

Write-Host ('[i] PyInstaller: ' + ($pyiArgs -join ' '))

# 输出重定向到文件：PyInstaller 的 INFO 走 stderr，
# 在 $ErrorActionPreference='Stop' 下会被 PowerShell 误判为失败并中断，故调用时临时降级
$prevEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
python -m PyInstaller @pyiArgs mem_guard.py *> build_log.txt
$code = $LASTEXITCODE
$ErrorActionPreference = $prevEap

if ($code -ne 0) {
    Get-Content build_log.txt -Tail 30
    throw '打包失败，详见 build_log.txt'
}
Get-Content build_log.txt -Tail 2
Remove-Item build_log.txt -ErrorAction SilentlyContinue

Write-Host ''
if ($OneDir) {
    Write-Host '构建完成（目录版，单进程 / 启动快）：dist\mem_guard\mem_guard.exe'
    Write-Host '分发请整目录拷贝（压缩后解压即可用）。'
} else {
    Write-Host '构建完成（单文件）：dist\mem_guard.exe'
    Write-Host '注：单文件版运行时会解压到临时目录，进程列表里会看到"引导器 + 程序"两个进程，属正常现象。'
}
