@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "BACKEND_DIR=%CD%\backend"
set "FRONTEND_DIR=%CD%\frontend"
set "PYTHON_EXE=%BACKEND_DIR%\.venv\Scripts\python.exe"
set "BACKEND_URL=http://127.0.0.1:8090"
set "FRONTEND_URL=http://127.0.0.1:5174"
set "WITH_SIMULATOR=1"

if /I "%~1"=="--no-simulator" set "WITH_SIMULATOR=0"

echo ======================================================================
echo   Huaibeixi SHM Alert System - Local Launcher
echo ======================================================================
echo   Frontend : %FRONTEND_URL%
echo   Backend  : %BACKEND_URL%/docs
echo   Simulator: local t_monitor data only; backend runs detection
echo ======================================================================

if not exist "%BACKEND_DIR%\run_backend.bat" goto :missing_files
if not exist "%FRONTEND_DIR%\run_frontend.bat" goto :missing_files
if not exist "%BACKEND_DIR%\tools\continuous_simulator.py" goto :missing_files
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=%BACKEND_DIR%\.venv\bin\python.exe"
if not exist "%PYTHON_EXE%" goto :missing_venv
if not exist "%FRONTEND_DIR%\node_modules" goto :missing_node_modules
if not exist "%BACKEND_DIR%\.env" goto :missing_env

where npm >nul 2>nul
if errorlevel 1 goto :missing_npm

set "BACKEND_ALREADY_RUNNING=0"
netstat -ano | findstr /R /C:":8090 .*LISTENING" >nul
if errorlevel 1 goto :backend_port_free
powershell -NoProfile -ExecutionPolicy Bypass -Command "try{$r=Invoke-WebRequest -UseBasicParsing -Uri '%BACKEND_URL%/api/ready' -TimeoutSec 3; if($r.StatusCode -eq 200){exit 0}}catch{}; exit 1"
if errorlevel 1 goto :backend_port_in_use
set "BACKEND_ALREADY_RUNNING=1"
:backend_port_free

echo.
if "%BACKEND_ALREADY_RUNNING%"=="1" (
    echo [1/3] Backend is already ready; reusing port 8090.
) else (
    echo [1/3] Starting backend on port 8090...
    start "Huaibeixi-Backend-8090" /D "%BACKEND_DIR%" cmd /k "run_backend.bat"
    echo Waiting for backend readiness...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$deadline=(Get-Date).AddSeconds(90); while((Get-Date)-lt $deadline){try{$r=Invoke-WebRequest -UseBasicParsing -Uri '%BACKEND_URL%/api/ready' -TimeoutSec 2; if($r.StatusCode -eq 200){exit 0}}catch{}; Start-Sleep -Seconds 1}; exit 1"
    if errorlevel 1 goto :backend_not_ready
    echo Backend is ready.
)

set "FRONTEND_ALREADY_RUNNING=0"
netstat -ano | findstr /R /C:":5174 .*LISTENING" >nul
if errorlevel 1 goto :frontend_port_free
powershell -NoProfile -ExecutionPolicy Bypass -Command "try{$r=Invoke-WebRequest -UseBasicParsing -Uri '%FRONTEND_URL%' -TimeoutSec 3; if($r.StatusCode -eq 200){exit 0}}catch{}; exit 1"
if errorlevel 1 goto :frontend_port_in_use
set "FRONTEND_ALREADY_RUNNING=1"
:frontend_port_free

echo.
if "%FRONTEND_ALREADY_RUNNING%"=="1" (
    echo [2/3] Frontend is already ready; reusing port 5174.
) else (
    echo [2/3] Starting frontend on port 5174...
    start "Huaibeixi-Frontend-5174" /D "%FRONTEND_DIR%" cmd /k "run_frontend.bat"
    echo Waiting for frontend...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$deadline=(Get-Date).AddSeconds(45); while((Get-Date)-lt $deadline){try{$r=Invoke-WebRequest -UseBasicParsing -Uri '%FRONTEND_URL%' -TimeoutSec 2; if($r.StatusCode -eq 200){exit 0}}catch{}; Start-Sleep -Seconds 1}; exit 1"
    if errorlevel 1 goto :frontend_not_ready
    echo Frontend is ready.
)

if "%WITH_SIMULATOR%"=="0" (
    echo.
    echo [3/3] Simulator skipped by --no-simulator.
    goto :started
)

echo.
echo [3/3] Starting local t_monitor data source...
start "Huaibeixi-Simulator" /D "%BACKEND_DIR%" cmd /k "run_simulator.bat"

:started
echo.
echo ======================================================================
echo   Startup complete.
echo   Frontend : %FRONTEND_URL%
echo   Swagger  : %BACKEND_URL%/docs
echo   Close each service window to stop that service.
echo ======================================================================
start "" "%FRONTEND_URL%"
ping 127.0.0.1 -n 4 >nul
exit /b 0

:missing_files
echo.
echo ERROR: Required launcher files are missing.
goto :fail

:missing_venv
echo.
echo ERROR: Backend virtual environment not found.
echo Run:
echo   cd backend
echo   python -m venv .venv
echo   .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
goto :fail

:missing_node_modules
echo.
echo ERROR: Frontend dependencies are not installed.
echo Run:
echo   cd frontend
echo   npm ci
goto :fail

:missing_env
echo.
echo ERROR: backend\.env is missing.
echo Copy backend\.env.example to backend\.env and configure the database.
goto :fail

:missing_npm
echo.
echo ERROR: npm was not found in PATH.
goto :fail

:backend_port_in_use
echo.
echo ERROR: Port 8090 is already in use. Stop the existing backend first.
goto :fail

:frontend_port_in_use
echo.
echo ERROR: Port 5174 is already in use. Stop the existing frontend first.
goto :fail

:backend_not_ready
echo.
echo ERROR: Backend did not become ready within 90 seconds.
echo Check the backend window and database settings in backend\.env.
goto :fail

:frontend_not_ready
echo.
echo ERROR: Frontend did not become ready within 45 seconds.
echo Check the frontend window for npm or Vite errors.
goto :fail

:fail
echo.
pause
exit /b 1
