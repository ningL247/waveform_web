@echo off
cd /d "%~dp0"

REM ===== COM port config =====
REM To auto-connect on startup, uncomment and set the COM port:
REM set COM=COM8
REM set BAUD=115200
REM ===========================
set PORT=8090

echo ========================================
echo   Lower Limb Blood Flow Monitor
echo   URL: http://localhost:%PORT%
echo   Select COM port in the right panel
echo   Close: close this window or Ctrl+C
echo ========================================
echo.

REM Kill any process already using port %PORT%
echo Checking port %PORT%...
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":%PORT% "') do (
    taskkill /F /PID %%a >nul 2>&1
)
echo.

REM Open browser after 3 seconds
start "" cmd /c "timeout /t 3 /nobreak >nul 2>&1 && start http://localhost:%PORT%"

if defined COM (
    python server.py --serial %COM% --baud %BAUD% --port %PORT% --fs 250
) else (
    python server.py --port %PORT% --fs 250
)
pause