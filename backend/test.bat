@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

".venv\Scripts\ruff.exe" check app tests tools
if errorlevel 1 exit /b 1

".venv\Scripts\python.exe" -m pytest --cov=app --cov-report=term-missing
