@echo off
setlocal
cd /d "%~dp0"

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

%PYTHON_CMD% COSCON_safe_standby_test.py --off-only
pause
