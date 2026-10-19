@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ======================================================================
echo   淮北西桥梁结构健康监测 - 预警与分析系统 (Huaibeixi SHM Alert System)
echo ======================================================================
echo [1/2] 正在启动 FastAPI 预警与分析引擎后端 (http://localhost:8090) ...
start "Huaibeixi-Backend-8090" cmd /k "cd /d %~dp0backend && run_backend.bat"

timeout /t 2 /nobreak >nul

echo [2/2] 正在启动 Vue 3 + ECharts 前端可视化监控台 (http://localhost:5174) ...
start "Huaibeixi-Frontend-5174" cmd /k "cd /d %~dp0frontend && run_frontend.bat"

echo.
echo 启动成功！请打开浏览器访问:
echo   - 前端监控大屏与调参工作台: http://localhost:5174
echo   - 后端 Swagger 接口文档:   http://localhost:8090/docs
pause
