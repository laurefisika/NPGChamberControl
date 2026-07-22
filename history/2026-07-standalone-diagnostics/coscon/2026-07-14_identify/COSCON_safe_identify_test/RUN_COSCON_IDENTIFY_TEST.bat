@echo off
setlocal
title COSCON IS Safe Identify LED Test
cd /d "%~dp0"

echo.
echo ============================================================
echo  COSCON IS Safe Identify LED Test
echo ============================================================
echo This test can only blink the front-panel "It's me" LED.
echo It cannot send Operate, Degas, Standby, Off, Reset,
echo network changes, or preset commands.
echo.

if not exist "COSCON_safe_identify_test.py" (
    echo ERROR: COSCON_safe_identify_test.py was not found.
    echo Keep the .bat and .py files in the same folder.
    echo.
    pause
    exit /b 1
)

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" COSCON_safe_identify_test.py
    set "EXITCODE=%ERRORLEVEL%"
    goto finished
)

where python >nul 2>&1
if not errorlevel 1 (
    python COSCON_safe_identify_test.py
    set "EXITCODE=%ERRORLEVEL%"
    goto finished
)

where py >nul 2>&1
if not errorlevel 1 (
    py -3 COSCON_safe_identify_test.py
    set "EXITCODE=%ERRORLEVEL%"
    goto finished
)

echo ERROR: Python was not found.
echo Install Python or place these files in the project folder that contains .venv.
set "EXITCODE=1"

:finished
echo.
if "%EXITCODE%"=="0" (
    echo Identify test completed successfully.
) else if "%EXITCODE%"=="2" (
    echo Identify test was cancelled before any write command was sent.
) else (
    echo Identify test finished with code %EXITCODE%.
    echo Review the messages and the saved report.
)
echo.
pause
exit /b %EXITCODE%
