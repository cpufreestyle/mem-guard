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

rem 优先用打包好的 exe（无控制台、免 Python）；没有则退回源码方式
if exist "%~dp0mem_guard.exe" (
    start "" "%~dp0mem_guard.exe"
) else (
    set "PYW="
    for /f "delims=" %%i in ('where pythonw.exe 2^>nul') do (
        if not defined PYW set "PYW=%%i"
    )
    if defined PYW (
        start "" "%PYW%" "%~dp0mem_guard.py"
    ) else (
        start "" /min python.exe "%~dp0mem_guard.py"
    )
)
