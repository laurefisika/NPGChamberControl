@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  COSCON IS Supervised Brief Degas Test
echo  Sequence: Off -^> Degassing -^> Off
echo  Pressure monitoring: XGS600 on COM6
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
    echo Copy this folder into the project folder containing .venv,
    echo or install Python and pyserial.
    pause
    exit /b 1
)

%PYTHON_CMD% COSCON_safe_degas_test.py
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo Degas transition test completed successfully.
) else if "%EXIT_CODE%"=="2" (
    echo Test cancelled before Degas.
) else if "%EXIT_CODE%"=="3" (
    echo Pressure safety stop was triggered. Review the report.
) else (
    echo Test did not complete successfully. Review the report.
)
echo.
pause
exit /b %EXIT_CODE%
