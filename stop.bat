@echo off
setlocal
cd /d "%~dp0"
echo Stopping tactile...

powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*tactile.py*' -and $_.Name -like 'python*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

timeout /t 1 /nobreak >nul

rem 找得到 python 就顺手自检一下；找不到就算了（自检不是必须的）
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY ( where py >nul 2>nul && set "PY=py" )
if defined PY (
  "%PY%" selftest.py
  if errorlevel 1 (
    echo.
    echo !!! Still running - check manually
    pause
    exit /b 1
  )
)

echo Done.
pause
