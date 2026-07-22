@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  TEST 2 - COSCON Active Operating Test
echo  10 mA, 2250 V, 5-second pulse
echo  Manual argon valve; XGS600 pressure monitoring on COM6
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

%PYTHON_CMD% 02_COSCON_supervised_operate_test.py
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo Active test completed successfully.
) else if "%EXIT_CODE%"=="2" (
    echo Active test cancelled before high voltage.
) else if "%EXIT_CODE%"=="3" (
    echo Pressure safety stop triggered. Review the report.
) else (
    echo Active test did not complete successfully. Review the report.
)
echo.
pause
exit /b %EXIT_CODE%
