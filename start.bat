@echo off
rem ===========================================================================
rem  Sentinel orchestrator - start the console and API on this machine.
rem
rem  Double-click it, or run:  start.bat [port]
rem
rem  First run sets everything up: it builds a private virtual environment in
rem  .venv, installs the package into it, and writes a starter configuration.
rem  Later runs skip straight to serving, so they take a second or two.
rem
rem  Nothing here is installed system-wide and nothing outside this folder is
rem  modified.
rem ===========================================================================

setlocal EnableDelayedExpansion

rem Work from the folder holding this file, so double-clicking behaves the same
rem as running it from a prompt somewhere else. %~dp0 keeps its trailing slash.
pushd "%~dp0"

rem The startup banner contains an em dash. Without this, a console running the
rem legacy code page prints a replacement character in the middle of the
rem sentence that tells you whether authentication is on.
set "PYTHONIOENCODING=utf-8"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8080"

set "VENV=.venv"
set "PY=%VENV%\Scripts\python.exe"
set "APP=%VENV%\Scripts\orchestrator.exe"

echo.
echo   Sentinel orchestrator
echo   ---------------------

rem A mistyped port otherwise surfaces as a socket traceback several screens
rem long, which buries the one useful fact.
echo %PORT%| findstr /r /c:"^[0-9][0-9]*$" >nul 2>&1
if errorlevel 1 goto :badport
if %PORT% GTR 65535 goto :badport
if %PORT% LSS 1 goto :badport
goto :portok

:badport
echo.
echo   "%PORT%" is not a usable port. Give a number from 1 to 65535, or no
echo   argument at all to use the default of 8080.
goto :fail

:portok

rem --------------------------------------------------------------- interpreter
rem Prefer the py launcher: it can be told to pick a supported interpreter even
rem when a much older or newer python.exe happens to be first on PATH.
if not exist "%PY%" (
  set "BOOTSTRAP="
  py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
  if not errorlevel 1 set "BOOTSTRAP=py -3"

  if not defined BOOTSTRAP (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
    if not errorlevel 1 set "BOOTSTRAP=python"
  )

  if not defined BOOTSTRAP (
    echo.
    echo   Could not find Python 3.11 or newer.
    echo.
    echo   Install it from https://www.python.org/downloads/ and tick
    echo   "Add python.exe to PATH" in the installer, then run this again.
    goto :fail
  )

  echo   Creating a private environment in %VENV% ...
  !BOOTSTRAP! -m venv "%VENV%"
  if errorlevel 1 (
    echo   Could not create the environment in %VENV%.
    goto :fail
  )
)

rem ------------------------------------------------------------------- install
rem The presence of the launcher is the test. Deleting .venv is therefore a
rem complete reset: the next run rebuilds it from scratch.
if not exist "%APP%" (
  echo   Installing dependencies. This takes a few minutes the first time ...
  echo.
  "%PY%" -m pip install --quiet --upgrade pip
  "%PY%" -m pip install -e ".[cli,api,yaml,http]"
  if errorlevel 1 (
    echo.
    echo   Installation failed. The output above says why; a missing compiler
    echo   or no internet connection are the usual causes.
    goto :fail
  )
  echo.
)

rem -------------------------------------------------------------------- config
rem A fresh clone has no .orchestrator\ - it holds local state and is
rem deliberately not in version control.
if not exist ".orchestrator\config.yaml" (
  echo   Writing a starter configuration ...
  "%APP%" init .
  if errorlevel 1 goto :fail
  echo.
  echo   Created .orchestrator\config.yaml. It starts in the "development"
  echo   profile: loopback only, no token. Read it before you point this at
  echo   anything that matters.
  echo.
)

rem ------------------------------------------------------------------ preflight
rem Catch a bad configuration here, where the message is readable, rather than
rem inside a stack trace during startup.
"%APP%" validate >nul 2>&1
if errorlevel 1 (
  echo   The configuration has a problem:
  echo.
  "%APP%" validate
  goto :fail
)

rem Refuse to start rather than let uvicorn fail on an address already in use -
rem which usually means you already have this running in another window.
netstat -ano | findstr /r /c:"TCP.*:%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo.
  echo   Port %PORT% is already in use - the orchestrator may already be
  echo   running. Open http://127.0.0.1:%PORT%/ to check, or start this one on
  echo   a different port:
  echo.
  echo       start.bat 8081
  goto :fail
)

rem --------------------------------------------------------------------- serve
rem Open the console once the server actually answers, rather than after a
rem guessed delay - on a cold start the first run can take several seconds, and
rem a browser that opens too early just shows a connection error.
start "" /min powershell -NoProfile -WindowStyle Hidden -Command ^
  "$u='http://127.0.0.1:%PORT%/'; for($i=0; $i -lt 60; $i++){ try { Invoke-WebRequest -Uri ($u+'live') -UseBasicParsing -TimeoutSec 1 ^| Out-Null; Start-Process $u; break } catch { Start-Sleep -Milliseconds 500 } }"

echo   Console:  http://127.0.0.1:%PORT%/
echo   Stop it:  Ctrl+C, or just close this window.
echo.

"%APP%" serve --port %PORT%

rem Ctrl+C aborts the script outright, so reaching here means the server either
rem shut down cleanly or failed to start. Treat a failure as a failure: a
rem double-clicked window that closes on the error is a window nobody can read.
if errorlevel 1 (
  echo.
  echo   The server stopped with an error. The output above says why.
  goto :fail
)

echo.
echo   Stopped.
popd
endlocal
exit /b 0

:fail
echo.
popd
endlocal
rem Keep the window open, so a double-clicked failure is readable.
pause
exit /b 1
