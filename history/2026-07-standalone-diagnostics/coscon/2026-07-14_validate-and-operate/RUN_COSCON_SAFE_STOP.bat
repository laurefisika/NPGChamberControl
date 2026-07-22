@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  COSCON Safe Stop Helper
echo  Requests Standby, then Off, and verifies final Mode=Off
echo ============================================================
echo.

set "PYTHON_CMD="
if exist ".venv\Scripts\python.exe" (
    set "PYTHON_CMD=.venv\Scripts\python.exe"
) else if exist "..\.venv\Scripts\python.exe" (
    set "PYTHON_CMD=..\.venv\Scripts\python.exe"
) else (
    where python >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
    where py >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=py"
)
if not defined PYTHON_CMD (
    echo ERROR: Python was not found.
    pause
    exit /b 1
)

%PYTHON_CMD% 02_COSCON_supervised_operate_test.py --safe-stop-only
echo.
pause
