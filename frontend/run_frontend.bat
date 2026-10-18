@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting Huaibeixi SHM Alert & Analytics Frontend on http://localhost:5173 ...
npm run dev
