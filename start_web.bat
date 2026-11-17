@echo off
rem =====================================================================
rem  Sweet Collector AI - headless web launcher (Windows)
rem  Starts the local server only (no desktop window).
rem  Open http://127.0.0.1:8080 in any browser afterwards.
rem  Same checks as start_gui.bat: Python >= 3.11, deps, data dir, port.
rem =====================================================================
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

echo [1/4] Checking Python...
set "PY="
for %%C in (py python) do (
  if not defined PY (
    %%C -3.11 -c "import sys" >nul 2>&1 && set "PY=%%C -3.11"
  )
  if not defined PY (
    %%C -3.12 -c "import sys" >nul 2>&1 && set "PY=%%C -3.12"
  )
  if not defined PY (
    %%C -3.13 -c "import sys" >nul 2>&1 && set "PY=%%C -3.13"
  )
  if not defined PY (
    %%C -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1 && set "PY=%%C"
  )
)
if not defined PY (
  echo.
  echo   ERROR: Python 3.11 or newer is required but was not found.
  echo   Fix: install it from https://www.python.org/downloads/
  echo   and tick "Add python.exe to PATH" during setup.
  echo.
  pause
  exit /b 1
)
for /f "delims=" %%V in ('%PY% -c "import platform;print(platform.python_version())"') do echo   Found Python %%V

echo [2/4] Checking virtual environment...
if not exist ".venv\Scripts\python.exe" (
  echo   Creating .venv ...
  %PY% -m venv .venv || (echo   ERROR: failed to create venv.& pause & exit /b 1)
)
set "VPY=.venv\Scripts\python.exe"
"%VPY%" -c "import httpx,bs4,yaml,PIL,imagehash,gallery_dl" >nul 2>&1
if errorlevel 1 (
  echo   Installing dependencies, first run only...
  "%VPY%" -m pip install --upgrade pip >nul 2>&1
  "%VPY%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo   ERROR: pip install failed.
    echo   Fix: check your network, or run manually:
    echo     "%VPY%" -m pip install -r requirements.txt
    pause
    exit /b 1
  )
)

echo [3/4] Checking data directory write permission...
if not exist "data" mkdir "data" 2>nul
("%VPY%" -c "import pathlib;p=pathlib.Path('data');(p/'.wtest').write_text('x');(p/'.wtest').unlink()" ) >nul 2>&1
if errorlevel 1 (
  echo   ERROR: cannot write into "%cd%\data".
  echo   Fix: move the whole folder somewhere writable,
  echo   e.g. your own user directory, then run again.
  pause
  exit /b 1
)

echo [4/4] Checking port 8080...
netstat -ano | findstr /r /c:":8080 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo   NOTE: port 8080 is already in use. Close the conflicting
  echo   program, or change web_port in settings before starting again.
  pause
  exit /b 1
)

echo.
echo Starting server (headless)... after it is up, open:
echo     http://127.0.0.1:8080
echo Keep this window open; closing it stops the server.
"%VPY%" -m media_resource_manager.headless
if errorlevel 1 (
  echo.
  echo   The server exited with an error.
  echo   Check logs in data\logs\ .
  pause
)
endlocal
