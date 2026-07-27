@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "APP_DIR=%~dp0"
set "WORKSPACE_DIR=%APP_DIR:~0,-1%"
cd /d "%APP_DIR%"

echo Observation Harvester
echo Working directory: %APP_DIR%
echo.

set "PYTHON_BIN="
where py >nul 2>nul
if not errorlevel 1 (
  py -3.12 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
  if not errorlevel 1 set "PYTHON_BIN=py -3.12"
)

if "%PYTHON_BIN%"=="" (
  where python >nul 2>nul
  if not errorlevel 1 (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYTHON_BIN=python"
  )
)

if "%PYTHON_BIN%"=="" (
  echo Python 3.12 or newer is required.
  echo Install Python 3.12+, then double-click this launcher again.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating local virtual environment...
  %PYTHON_BIN% -m venv .venv
  if errorlevel 1 (
    echo Failed to create .venv.
    pause
    exit /b 1
  )
)

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo Failed to activate .venv.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m pip --version >nul 2>nul
if errorlevel 1 (
  echo Bootstrapping pip in the local virtual environment...
  ".venv\Scripts\python.exe" -m ensurepip --upgrade
  if errorlevel 1 (
    echo Failed to bootstrap pip in .venv.
    pause
    exit /b 1
  )
)

echo Installing/updating local app dependencies...
".venv\Scripts\python.exe" -m pip install -e ".[app]"
if errorlevel 1 (
  echo Failed to install app dependencies.
  pause
  exit /b 1
)

set "CODEX_BIN=%OBSERVATION_HARVESTER_CODEX_BIN%"
if not "%CODEX_BIN%"=="" if not exist "%CODEX_BIN%" set "CODEX_BIN="

if "%CODEX_BIN%"=="" (
  for /f "delims=" %%I in ('where codex.exe 2^>nul') do (
    if "!CODEX_BIN!"=="" set "CODEX_BIN=%%I"
  )
)

if "%CODEX_BIN%"=="" (
  echo.
  echo Codex CLI was not found on PATH.
  echo Install Codex CLI from:
  echo https://chatgpt.com/codex
  echo.
  echo Then authenticate Codex CLI and double-click this launcher again.
  pause
  exit /b 1
)

"%CODEX_BIN%" --version >nul 2>nul
if errorlevel 1 (
  echo.
  echo The Codex CLI path could not be executed:
  echo %CODEX_BIN%
  echo Set OBSERVATION_HARVESTER_CODEX_BIN to a working codex.exe.
  echo.
  pause
  exit /b 1
)

if "%OBSERVATION_HARVESTER_PORT%"=="" (
  set "APP_PORT=8771"
) else (
  set "APP_PORT=%OBSERVATION_HARVESTER_PORT%"
)

echo.
echo Starting Observation Harvester at http://127.0.0.1:%APP_PORT%
echo Keep this Command Prompt window open while using the app.
echo.

".venv\Scripts\python.exe" -m pdt_observer app --workspace "%WORKSPACE_DIR%" --port "%APP_PORT%" --codex-bin "%CODEX_BIN%"
pause
