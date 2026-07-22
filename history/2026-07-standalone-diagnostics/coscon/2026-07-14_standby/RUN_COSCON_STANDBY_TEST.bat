@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  COSCON IS Supervised Standby Test
echo  Sequence: Off -^> Standby -^> Off
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
    echo Install Python or copy this folder into the project folder containing .venv.
    pause
    exit /b 1
)

%PYTHON_CMD% COSCON_safe_standby_test.py
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo Test completed successfully.
) else if "%EXIT_CODE%"=="2" (
    echo Test cancelled before Standby.
) else (
    echo Test did not complete successfully. Review the generated report.
)
echo.
pause
exit /b %EXIT_CODE%
