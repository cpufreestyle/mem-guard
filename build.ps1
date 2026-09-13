# 打包 MemGuard 为单文件 exe（GUI 子系统，无控制台窗口）
# 用法：在仓库根目录执行  .\build.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# 1) 生成程序图标（仅当不存在时）
if (-not (Test-Path mem_guard.ico)) {
    python make_icon.py
}

# 2) PyInstaller 打包
python -m PyInstaller --noconsole --onefile --name mem_guard --icon mem_guard.ico --hidden-import pystray._win32 mem_guard.py

Write-Host ""
Write-Host "构建完成：dist\mem_guard.exe"
