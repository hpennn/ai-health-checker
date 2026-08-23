@echo off
chcp 65001 >nul
setlocal

echo ========================================
echo  AI Health Checker - Node Agent Setup
echo ========================================
echo.

set NODE_DIR=%~dp0
set NODE_DIR=%NODE_DIR:~0,-1%

REM Find Python
set PYTHON=
where python >nul 2>&1 && set PYTHON=python
if exist "C:\Python314\python.exe" set PYTHON=C:\Python314\python.exe
if exist "C:\Python313\python.exe" set PYTHON=C:\Python313\python.exe
if exist "C:\Python312\python.exe" set PYTHON=C:\Python312\python.exe
if exist "C:\Python311\python.exe" set PYTHON=C:\Python311\python.exe

if "%PYTHON%"=="" (
    echo [ERROR] Python not found. Install Python 3.10+ first.
    pause
    exit /b 1
)

echo [1/4] Using Python: %PYTHON%
%PYTHON% --version

echo.
echo [2/4] Installing dependencies...
%PYTHON% -m pip install -r "%NODE_DIR%\requirements.txt"
if errorlevel 1 (
    echo [ERROR] pip install failed
    pause
    exit /b 1
)

echo.
echo [3/4] Installing Playwright Chromium...
%PYTHON% -m playwright install chromium
if errorlevel 1 (
    echo [WARN] Chromium install failed, browser checks will be unavailable
)

echo.
echo [4/4] Creating watchdog scheduled task (runs at startup as SYSTEM)...
schtasks /create /tn "HealthCheckerWatchdog" /tr "\"%PYTHON%\" \"%NODE_DIR%\watchdog.py\"" /sc onstart /ru System /f
if errorlevel 1 (
    echo [ERROR] Failed to create scheduled task
    pause
    exit /b 1
)

echo.
echo Starting watchdog now...
schtasks /run /tn "HealthCheckerWatchdog"

echo.
echo ========================================
echo  Setup complete!
echo  Node will auto-start on boot.
echo  Check watchdog.log for status.
echo ========================================
timeout /t 5
