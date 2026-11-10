@echo off
title 船舶收尾异常管理看板 - 启动器
cd /d "%~dp0"

REM ---- 清理旧实例（防止多个 app/monitor 同时运行抢数据库和端口）----
wmic process where "name like 'python%' and commandline like '%monitor.py%'" call terminate >nul 2>&1
wmic process where "name like 'python%' and commandline like '%app.py%'" call terminate >nul 2>&1
timeout /t 2 /nobreak >nul

echo ============================================
echo    船舶收尾异常管理看板 启动中...
echo ============================================
echo.

REM ---- 检查 Python ----
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+
    echo 下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

REM ---- 检查依赖是否已安装 ----
python -c "import flask, pymysql, requests, openpyxl" >nul 2>&1
if %errorlevel% neq 0 (
    echo [提示] 首次运行，正在安装依赖包...
    pip install flask pymysql requests openpyxl
    echo.
)

REM ---- 初始化数据库（自动建表 + 从 Excel 迁移数据）----
echo [1/4] 初始化数据库（自动建表 + 迁移 Excel 数据）...
python init_db.py
echo.

REM ---- 启动巡检程序（后台窗口）----
echo [2/4] 启动巡检推送程序 (monitor.py)...
start "巡检推送程序" /D "%~dp0" %COMSPEC% /k python monitor.py

REM ---- 启动 Web 服务 ----
echo [3/4] 启动 Web 服务 (app.py)...
start "Web服务" /D "%~dp0" %COMSPEC% /k python app.py

REM ---- 等待服务启动后打开浏览器 ----
echo [4/4] 正在打开浏览器...
timeout /t 3 /nobreak >nul
start http://127.0.0.1:5000

echo.
echo ============================================
echo  启动完成！
echo  看板地址: http://127.0.0.1:5000
echo  关闭服务: 直接关闭弹出的两个窗口
echo ============================================
echo.
pause
