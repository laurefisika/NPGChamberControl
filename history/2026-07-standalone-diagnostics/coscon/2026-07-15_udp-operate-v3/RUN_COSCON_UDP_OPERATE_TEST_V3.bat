@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  COSCON UDP Operating Verification V3
echo  Requires natural Standby after complete Degas
echo  60 s Standby conditioning + 60 s stable Operating test
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
    echo Copy this folder into the npg_chamber_project folder containing .venv.
    pause
    exit /b 1
)

%PYTHON_CMD% COSCON_UDP_operate_test_v3.py
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo Test completed successfully.
) else if "%EXIT_CODE%"=="2" (
    echo Test cancelled before activation.
) else if "%EXIT_CODE%"=="3" (
    echo Pressure safety stop triggered.
) else if "%EXIT_CODE%"=="4" (
    echo COSCON reported a device/output fault.
) else if "%EXIT_CODE%"=="5" (
    echo Communication fault. Check the final confirmed COSCON state locally.
) else (
    echo Test did not complete successfully.
)
echo.
echo Review the files in "COSCON UDP Test Reports".
pause
exit /b %EXIT_CODE%
