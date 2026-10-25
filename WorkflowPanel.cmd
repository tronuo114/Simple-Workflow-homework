@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem 入口优先用仓库根目录的 exe（随仓库提交，评审可直接双击）
if exist "%~dp0WorkflowPanel.exe" (
    start "" "%~dp0WorkflowPanel.exe"
    exit /b 0
)
if exist "%~dp0dist\WorkflowPanel.exe" (
    start "" "%~dp0dist\WorkflowPanel.exe"
    exit /b 0
)

echo 还没打包 exe：没找到 WorkflowPanel.exe
echo.
echo 先执行一次（只需一次）：
echo     powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_exe.ps1
echo.
echo 或者用源码方式打开面板（需要 Python 3.9+）：
echo     python launcher\app.py
echo.
pause
exit /b 1
