@echo off
chcp 65001 >nul 2>&1
title Pagefile Setup

rem Elevate to administrator if not already
fltmc >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0set_pagefile.ps1"
pause
