@echo off
rem ===========================================================================
rem  Daemon mode: keep deleting until nothing left.
rem  Handles rate-limit automatically (record -> wait 1h -> probe -> continue).
rem  Stop with Ctrl+C, or create a file named ".stop" in this folder.
rem  Python is auto-detected; no absolute interpreter path required.
rem ===========================================================================
setlocal

set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY (
    for %%P in (python python3 py) do (
        if not defined PY (
            %%P --version >nul 2>&1
            if not errorlevel 1 set "PY=%%P"
        )
    )
)
if not defined PY (
    echo [ERROR] Python 3.10+ was not found.
    echo         Install Python from python.org, or create .venv in this folder.
    pause
    exit /b 1
)

set "PYTHONUTF8=1"
cd /d "%~dp0"

echo [INFO] Starting daemon. Press Ctrl+C to stop.
"%PY%" -u daemon.py %*
echo.
echo [INFO] Daemon exited. Progress is saved in state.json.
pause
