@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

if exist ".venv\Scripts\alembic.exe" (
    ".venv\Scripts\alembic.exe" upgrade head
) else (
    echo Backend virtual environment not found.
    exit /b 1
)
