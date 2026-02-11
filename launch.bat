@echo off
setlocal

cd /d "%~dp0"

:: Ensure we are running as Administrator (required for some UNC/network access)
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting Administrator privileges...
  powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

if "%PORT%"=="" set PORT=8099
if "%HOST%"=="" set HOST=0.0.0.0

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] Virtualenv not found at .venv\Scripts\python.exe
  echo Activate your venv or install dependencies, then retry.
  pause
  exit /b 1
)

:: Keep the server window open and show errors if it exits
start "HA Auto Agent Server" cmd /k ""%PY%" -m uvicorn agent_server:app --host %HOST% --port %PORT%"

:: Wait for the server to be reachable (up to ~30s)
set "HEALTH_URL=http://localhost:%PORT%/api/health"
for /l %%i in (1,1,30) do (
  powershell -Command "try { (Invoke-WebRequest -UseBasicParsing %HEALTH_URL%).StatusCode | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
  if not errorlevel 1 goto OPEN
  timeout /t 1 >nul
)
:OPEN
start "" "http://localhost:%PORT%/ui/"

echo Server started on http://localhost:%PORT%/ui/
echo Close the "HA Auto Agent Server" window to stop it.
