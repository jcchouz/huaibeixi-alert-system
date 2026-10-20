@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo Starting local t_monitor data source (alert detection runs in the backend)...
set PATH=C:\msys64\ucrt64\bin;%PATH%
if exist ".venv\bin\python.exe" (
    ".venv\bin\python.exe" tools\continuous_simulator.py %*
) else (
    ".venv\Scripts\python.exe" tools\continuous_simulator.py %*
)
