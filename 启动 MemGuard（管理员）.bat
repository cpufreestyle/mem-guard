@echo off
chcp 65001 >nul 2>&1
title MemGuard

rem 没有管理员权限就自动提权重启自身
fltmc >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"
start "MemGuard" /min python.exe "%~dp0mem_guard.py"
