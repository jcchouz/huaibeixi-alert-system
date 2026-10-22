@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting Alert Analysis Frontend on http://localhost:5174 ...
npm run dev
