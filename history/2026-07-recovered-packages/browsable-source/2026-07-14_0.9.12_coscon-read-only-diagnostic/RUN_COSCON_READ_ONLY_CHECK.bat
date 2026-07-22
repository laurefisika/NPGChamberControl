@echo off
setlocal

title COSCON IS Read-Only Diagnostic
cd /d "%~dp0"

echo.
echo ============================================================
echo  COSCON IS Read-Only UDP Diagnostic
echo ============================================================
echo Target defaults: 192.168.236.186 UDP port 2005
echo This diagnostic cannot send Operate, Degas, Standby, Off,
echo Reset, network changes, or preset write/delete commands.
echo.

if not exist "pyproject.toml" (
    echo ERROR: pyproject.toml was not found.
    echo Keep this file in the root folder of the project.
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating local virtual environment in .venv ...
    py -3 -m venv .venv
    if errorlevel 1 python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Could not create the virtual environment.
        echo Make sure Python is installed and available from CMD.
        pause
        exit /b 1
    )
)

set "PYTHON=.venv\Scripts\python.exe"

"%PYTHON%" -m pip show npg-chamber >nul 2>&1
if errorlevel 1 (
    echo Installing the project in editable mode ...
    "%PYTHON%" -m pip install -e .
    if errorlevel 1 goto install_error
) else (
    "%PYTHON%" -m pip install -e . --no-deps --quiet
    if errorlevel 1 goto install_error
)

echo.
"%PYTHON%" diagnostic_tools\check_coscon_read_only.py
set "EXITCODE=%ERRORLEVEL%"

echo.
if "%EXITCODE%"=="0" (
    echo Diagnostic completed successfully.
) else (
    echo Diagnostic finished with code %EXITCODE%.
    echo Review the saved report and the messages above.
)
echo.
pause
exit /b %EXITCODE%

:install_error
echo.
echo ERROR: Project installation/update failed.
echo Try manually:
echo     .venv\Scripts\activate
echo     python -m pip install -e .
echo.
pause
exit /b 1
