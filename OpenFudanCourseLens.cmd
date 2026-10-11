@echo off
setlocal
chcp 65001 >nul
title Fudan CourseLens
cd /d "%~dp0"

echo [FudanCourseLens] Preparing the local learning interface...
echo [FudanCourseLens] Online playback and encrypted cloud learning tasks are enabled.
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_fudan_courselens.ps1" %*
set "EXITCODE=%ERRORLEVEL%"

if "%EXITCODE%"=="-1073741510" exit /b 0
if "%EXITCODE%"=="3221225786" exit /b 0
if not "%EXITCODE%"=="0" (
    echo [FudanCourseLens] Startup failed with code %EXITCODE%.
    pause
)

exit /b %EXITCODE%
