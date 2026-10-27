@echo off
rem ===========================================================================
rem  Portable wrapper. Finds a Python interpreter automatically:
rem    1) .venv inside this package (if you created one)
rem    2) python / python3 / py on PATH
rem  No hard-coded interpreter location.
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

if "%~1"=="" (
    "%PY%" "%~dp0run.py" --help
    echo.
    pause
    exit /b 0
)

"%PY%" "%~dp0run.py" %*
set "RC=%ERRORLEVEL%"
if not "%~1"=="run" if not "%~1"=="resume" if not "%~1"=="dry-run" pause
exit /b %RC%
