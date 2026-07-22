@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  COSCON Manual Process - READ-ONLY Logger
echo  No Degas / Operate / Standby / Off commands are available
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
    echo Copy this folder into the project folder containing .venv.
    pause
    exit /b 1
)

%PYTHON_CMD% COSCON_manual_readonly_logger.py
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo Logger completed. Review the reports folder.
) else if "%EXIT_CODE%"=="2" (
    echo Logger cancelled before starting.
) else (
    echo Logger ended with an error. Review the console and any raw log.
)
echo.
pause
exit /b %EXIT_CODE%
