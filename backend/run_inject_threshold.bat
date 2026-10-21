@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo Injecting single-channel threshold samples into t_monitor...
if exist ".venv\bin\python.exe" (
    ".venv\bin\python.exe" tools\inject_threshold.py %*
) else (
    ".venv\Scripts\python.exe" tools\inject_threshold.py %*
)
