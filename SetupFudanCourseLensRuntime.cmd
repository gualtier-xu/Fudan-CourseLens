@echo off
setlocal
chcp 65001 >nul
title Fudan CourseLens Runtime Setup
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_fudan_courselens_runtime.ps1" %*
set "EXITCODE=%ERRORLEVEL%"

echo.
if not "%EXITCODE%"=="0" (
    echo [FudanCourseLensSetup] Setup failed with code %EXITCODE%.
    pause
    exit /b %EXITCODE%
)

echo [FudanCourseLensSetup] Setup completed successfully.
pause
