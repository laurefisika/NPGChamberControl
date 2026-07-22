@echo off
setlocal

title NPG Chamber Controller
cd /d "%~dp0"

echo.
echo ============================================================
echo  NPG Chamber Controller
echo ============================================================
echo Project folder: %CD%
echo.

if not exist "pyproject.toml" (
    echo ERROR: pyproject.toml was not found.
    echo This file must stay in the root folder of the project.
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating local virtual environment in .venv ...
    py -3 -m venv .venv
    if errorlevel 1 (
        python -m venv .venv
    )
    if errorlevel 1 (
        echo.
        echo ERROR: Could not create the virtual environment.
        echo Make sure Python is installed and available from CMD.
        echo.
        pause
        exit /b 1
    )
)

set "PYTHON=.venv\Scripts\python.exe"

echo Checking package installation ...
"%PYTHON%" -m pip show npg-chamber >nul 2>&1
if errorlevel 1 (
    echo First-time setup: installing package and dependencies ...
    "%PYTHON%" -m pip install --upgrade pip
    if errorlevel 1 goto install_error
    "%PYTHON%" -m pip install -e .
    if errorlevel 1 goto install_error
) else (
    echo Package already installed. Refreshing editable link ...
    "%PYTHON%" -m pip install -e . --no-deps --quiet
    if errorlevel 1 goto install_error
)

echo.
echo Starting graphical launcher ...
echo.
"%PYTHON%" -m npg_chamber
set "EXITCODE=%ERRORLEVEL%"

echo.
if not "%EXITCODE%"=="0" (
    echo The launcher exited with code %EXITCODE%.
    echo.
    pause
)
exit /b %EXITCODE%

:install_error
echo.
echo ERROR: Installation/update failed.
echo Check the messages above. You can also try manually:
echo     .venv\Scripts\activate
echo     python -m pip install -e .
echo.
pause
exit /b 1
