@echo off
setlocal EnableExtensions

title NPG Chamber Controller
cd /d "%~dp0"

set "SOURCE_BUILD=2026.09.04-r20"
rem Keep the Python runtime outside the project folder.  Qt/PySide6 contains
rem deeply nested files and a project extracted under a long Windows path can
rem otherwise exceed the Windows MAX_PATH limit during pip installation.
if defined LOCALAPPDATA (
    set "RUNTIME_BASE=%LOCALAPPDATA%\NPGChamber"
) else (
    set "RUNTIME_BASE=%TEMP%\NPGChamber"
)
set "RUNTIME_DIR=%RUNTIME_BASE%\runtime_%SOURCE_BUILD%"
set "VENV_DIR=%RUNTIME_DIR%\.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

echo.
echo ============================================================
echo  NPG Chamber Controller
echo ============================================================
echo Project folder: %CD%
echo Runtime folder: %VENV_DIR%
echo Source build: %SOURCE_BUILD%
echo.

if not exist "pyproject.toml" goto project_error

rem ---------------------------------------------------------------------------
rem Fast path: verify package availability without initializing phase-specific GUI backends.
rem ---------------------------------------------------------------------------
if exist "%VENV_PY%" (
    "%VENV_PY%" -c "import sys" >nul 2>&1
    if not errorlevel 1 goto check_dependencies
    echo Existing runtime cannot start and will be rebuilt.
    rmdir /s /q "%VENV_DIR%"
    if exist "%VENV_DIR%" goto remove_error
)

goto create_runtime

:check_dependencies
echo Checking local runtime ...
"%VENV_PY%" -c "import importlib.util as u; mods=('serial','matplotlib','requests','webview','clr','PySide6','pyqtgraph'); missing=[m for m in mods if u.find_spec(m) is None]; raise SystemExit(1 if missing else 0)" >nul 2>&1
if errorlevel 1 goto repair_dependencies

goto check_project_link

:check_project_link
"%VENV_PY%" -c "import pathlib; root=pathlib.Path.cwd().resolve(); import npg_chamber; package=pathlib.Path(npg_chamber.__file__).resolve(); raise SystemExit(0 if root==package.parents[1] else 1)" >nul 2>&1
if errorlevel 1 goto repair_project_link

goto verify_source

:repair_project_link
echo Repairing the local project link ...
"%VENV_PY%" -m pip install --disable-pip-version-check --no-cache-dir --no-deps -e .
if errorlevel 1 goto install_error
goto check_runtime

:repair_dependencies
echo Repairing missing local dependencies ...
"%VENV_PY%" -m pip install --disable-pip-version-check --no-cache-dir -e .
if errorlevel 1 goto install_error
goto check_runtime

:create_runtime
where python >nul 2>&1
if errorlevel 1 goto python_error

echo Preparing the local runtime for first use ...
if not exist "%RUNTIME_DIR%" mkdir "%RUNTIME_DIR%"
if errorlevel 1 goto runtime_folder_error

powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Get-Item -LiteralPath '%RUNTIME_DIR%'; $free=$p.PSDrive.Free; if($free -lt 734003200){Write-Host ('ERROR: Less than 700 MB is free on drive '+$p.PSDrive.Name+'. Free disk space before creating the runtime.'); exit 9}"
if errorlevel 1 goto disk_error

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if errorlevel 1 goto python_version_error

python -m venv "%VENV_DIR%"
if errorlevel 1 goto venv_error

echo Installing NPG Chamber dependencies once ...
"%VENV_PY%" -m pip install --disable-pip-version-check --no-cache-dir -e .
if errorlevel 1 goto install_error

goto check_runtime

:check_runtime
"%VENV_PY%" -c "import pathlib, importlib.util as u; root=pathlib.Path.cwd().resolve(); import npg_chamber; package=pathlib.Path(npg_chamber.__file__).resolve(); mods=('serial','matplotlib','requests','webview','clr','PySide6','pyqtgraph'); missing=[m for m in mods if u.find_spec(m) is None]; raise SystemExit(0 if root==package.parents[1] and not missing else 1)" >nul 2>&1
if errorlevel 1 goto runtime_error

goto verify_source

:verify_source
echo Verifying active source files ...
"%VENV_PY%" -m npg_chamber.installation_check --expected-build "%SOURCE_BUILD%"
if errorlevel 1 goto source_mismatch

:launch
echo Runtime ready.
echo Starting graphical launcher ...
echo.
"%VENV_PY%" -m npg_chamber
set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" (
    echo.
    echo The launcher exited with code %EXITCODE%.
    pause
)
exit /b %EXITCODE%

:source_mismatch
echo.
echo ERROR: The source files do not match build %SOURCE_BUILD%.
echo The detailed [FAIL] line above identifies the component that does not match.
echo If this is a mixed/old folder, extract the complete build into a clean folder or overwrite all files.
echo Current project: %CD%
pause
exit /b 1

:project_error
echo ERROR: pyproject.toml was not found in the project root.
pause
exit /b 1

:python_error
echo ERROR: Windows cannot find the "python" command.
echo Install or expose Python on PATH, then run this launcher again.
pause
exit /b 1

:python_version_error
echo ERROR: Python 3.10 or newer is required.
pause
exit /b 1

:disk_error
echo.
echo Runtime setup stopped because the runtime drive has too little free space.
echo Free at least 700 MB and run START_NPG_CHAMBER.bat again.
pause
exit /b 1

:runtime_folder_error
echo.
echo ERROR: Could not create the short runtime folder:
echo %RUNTIME_DIR%
echo Check your Windows user permissions and try again.
pause
exit /b 1

:remove_error
echo.
echo ERROR: Could not remove the unusable runtime at:
echo %VENV_DIR%
echo Close all NPG Chamber and Python windows, then try again.
pause
exit /b 1

:venv_error
echo.
echo ERROR: Could not create the Python virtual environment at:
echo %VENV_DIR%
pause
exit /b 1

:install_error
echo.
echo ERROR: Local runtime installation/repair failed.
echo Runtime folder: %VENV_DIR%
echo Check disk space and internet access, then run this launcher again.
pause
exit /b 1

:runtime_error
echo.
echo ERROR: The local runtime could not be verified after repair.
echo The launcher checks package availability only; it does not initialize WinForms/.NET here.
if exist "%VENV_PY%" "%VENV_PY%" -c "import importlib.util as u; mods=('serial','matplotlib','requests','webview','clr','PySide6','pyqtgraph'); missing=[m for m in mods if u.find_spec(m) is None]; print('Missing Python modules: ' + (', '.join(missing) if missing else 'none detected'))" 2>nul
echo Check the detailed pip output above. Deleting the runtime is not normally required.
pause
exit /b 1
