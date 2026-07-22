@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  COSCON IS Supervised Operating Re-test V2
echo  10 mA, 2250 V, 3-second pulse
echo  Initial Mode accepted: Off or Standby
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

%PYTHON_CMD% COSCON_supervised_operate_retest_v2.py
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
    echo Re-test completed successfully.
) else if "%EXIT_CODE%"=="2" (
    echo Test cancelled before high voltage.
) else if "%EXIT_CODE%"=="3" (
    echo Pressure safety stop triggered. Review the report.
) else (
    echo Test did not complete successfully. Review the report.
)
echo.
pause
exit /b %EXIT_CODE%
