@echo off
chcp 65001 >nul 2>&1
title MemGuard

rem 没有管理员权限就自动提权重启自身（提权后的控制台窗口一并隐藏）
fltmc >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs -WindowStyle Hidden"
    exit /b
)

cd /d "%~dp0"

rem 优先用 pythonw.exe 启动：它没有控制台窗口，托盘程序全程静默不占前台
set "PYW="
for /f "delims=" %%i in ('where pythonw.exe 2^>nul') do (
    if not defined PYW set "PYW=%%i"
)
if defined PYW (
    start "" "%PYW%" "%~dp0mem_guard.py"
) else (
    rem 找不到 pythonw 时退回 python.exe（程序内部会隐藏控制台）
    start "" /min python.exe "%~dp0mem_guard.py"
)
