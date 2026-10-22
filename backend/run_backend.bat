@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo Starting Structural Health Monitoring Alert & Analytics Backend on port 8090...
set PATH=C:\msys64\ucrt64\bin;%PATH%
if /I "%ENVIRONMENT%"=="production" (
    set RELOAD_FLAG=
) else (
    set RELOAD_FLAG=--reload
)
if exist ".venv\bin\python.exe" (
    ".venv\bin\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8090 %RELOAD_FLAG%
) else (
    ".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8090 %RELOAD_FLAG%
)
