@echo off
title 防火墙放行 5000 端口 - 自动提权版
cd /d "%~dp0"

echo ============================================
echo   船舶收尾异常管理看板 - 防火墙放行工具
echo ============================================
echo.

REM ---- 检查是否管理员权限，不是则自动请求提权 ----
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [提示] 当前不是管理员权限，正在请求管理员权限...
    echo        请在弹窗中点击【是】！
    echo.
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo [1/2] 添加放行规则：TCP 5000 端口（专用/公用网络）...
netsh advfirewall firewall delete rule name="Shipyard_Kanban_5000" >nul 2>&1
netsh advfirewall firewall add rule name="Shipyard_Kanban_5000" dir=in action=allow protocol=TCP localport=5000 profile=any >nul 2>&1

echo [2/2] 验证规则是否生效...
netsh advfirewall firewall show rule name="Shipyard_Kanban_5000" >nul 2>&1
if %errorlevel%==0 (
    echo.
    echo ============================================
    echo   放行成功！同一网络下的手机现在可以访问：
    echo.
    for /f "tokens=2 delims=:" %%i in ('ipconfig ^| findstr /i "IPv4"') do echo   http://%%i:5000
    echo.
    echo   （若电脑有多个 IP，逐个试；手机需与电脑连同一个网络）
    echo ============================================
) else (
    echo.
    echo [失败] 规则未生效！可能被杀毒软件拦截，请重试。
)
echo.
pause
