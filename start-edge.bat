@echo off
rem ===========================================================================
rem  Launch Edge with a dedicated profile + CDP debug port.
rem  Double-click this file. Log in to Channels (WeChat) once, and the session
rem  will be reused by every later run.
rem  Keep this window / Edge running while the Python tool works.
rem ===========================================================================
setlocal EnableDelayedExpansion

set "PORT=9222"
set "PROFILE=%~dp0.edge-profile"
set "TARGET=https://channels.weixin.qq.com/platform/post/list"

set "EDGE="
if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if exist "%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe" set "EDGE=%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"

if "%EDGE%"=="" (
    echo [ERROR] msedge.exe not found.
    echo         Please set edge.exe in config.yaml manually.
    pause
    exit /b 1
)

echo [INFO] Edge      : %EDGE%
echo [INFO] Profile   : %PROFILE%
echo [INFO] Debug port: %PORT%

netstat -ano | findstr /C:":%PORT% " | findstr /C:"LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [OK]   Port %PORT% is already listening. Reusing the running instance.
    start "" "%EDGE%" --remote-debugging-port=%PORT% --user-data-dir="%PROFILE%" "%TARGET%"
    goto :verify
)

if not exist "%PROFILE%" mkdir "%PROFILE%"

echo [INFO] Starting a new Edge instance with isolated profile ...
start "" "%EDGE%" ^
 --remote-debugging-port=%PORT% ^
 --user-data-dir="%PROFILE%" ^
 --remote-allow-origins=* ^
 --no-first-run ^
 --no-default-browser-check ^
 --disable-background-timer-throttling ^
 --disable-backgrounding-occluded-windows ^
 --disable-renderer-backgrounding ^
 --disable-features=CalculateNativeWinOcclusion ^
 "%TARGET%"

:verify
echo [INFO] Waiting for the debug port ...
set /a TRY=0
:waitloop
set /a TRY+=1
ping -n 2 127.0.0.1 >nul
netstat -ano | findstr /C:":%PORT% " | findstr /C:"LISTENING" >nul 2>&1
if not errorlevel 1 goto :ready
if %TRY% GEQ 30 goto :timeout
goto :waitloop

:ready
echo.
echo [OK] Debug port %PORT% is ready.
echo.
echo Next steps:
echo   1. In the Edge window that just opened, scan the QR code and log in.
echo   2. Keep Edge open. Do NOT close this window.
echo   3. Then run:  python run.py probe
echo.
pause
exit /b 0

:timeout
echo.
echo [ERROR] Debug port did not come up within 30 seconds.
echo         Most likely an Edge instance is already running and swallowed the flags.
echo         Close ALL Edge windows, then run this script again.
echo.
pause
exit /b 1
