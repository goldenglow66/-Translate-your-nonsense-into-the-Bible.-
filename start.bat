@echo off
chcp 65001 >nul
title 经意 · 圣经语义检索
cd /d "%~dp0"

rem ---- 如果服务已经在跑，就别再起一个，直接开浏览器 ----
powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; try{ $r=Invoke-WebRequest 'http://127.0.0.1:8765/api/stats' -TimeoutSec 2 -UseBasicParsing; if($r.Content -match 'verses'){exit 0} }catch{}; exit 1" >nul 2>nul
if %errorlevel%==0 (
  echo.
  echo   服务已在运行，正在打开 http://127.0.0.1:8765/
  echo.
  start "" "http://127.0.0.1:8765/"
  timeout /t 2 >nul
  exit /b 0
)

rem ---- 找 Python ----
set "PY="
if exist "%~dp0.runtime\python.exe" set "PY=%~dp0.runtime\python.exe"
if not defined PY if exist "C:\Users\Lenovo\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set "PY=C:\Users\Lenovo\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo.
  echo   [!] 没有找到 Python，请先安装 Python 3.9 以上版本。
  echo       下载地址：https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

echo.
echo   正在启动「经意」，首次运行约需 2 秒建立索引…
echo   浏览器会自动打开；如果没有，请手动访问 http://127.0.0.1:8765/
echo.

"%PY%" server.py --open %*

echo.
echo   服务已停止。
pause
