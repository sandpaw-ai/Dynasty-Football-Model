@echo off
REM Dynasty Model — Windows launcher
REM Double-click this file. It does everything from scratch.

cd /d "%~dp0"

echo.
echo ========================================================
echo   DYNASTY MODEL — Windows launcher
echo ========================================================
echo.

REM --- Check Python --------------------------------------------------------
echo Checking for Python 3.11+...

set PYTHON=
for %%P in (python py python3) do (
  %%P -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
  if not errorlevel 1 (
    set PYTHON=%%P
    goto :found_python
  )
)

echo.
echo   Could not find Python 3.11 or newer.
echo.
echo   Please install Python from:  https://www.python.org/downloads/
echo   IMPORTANT: On the first install screen, check "Add Python to PATH".
echo.
echo   Then double-click this file again.
echo.
pause
exit /b 1

:found_python
echo   Found %PYTHON%
echo.

REM --- Set up virtual environment -----------------------------------------
if not exist ".venv\" (
  echo First-time setup: creating virtual environment...
  %PYTHON% -m venv .venv
  echo   Done.
  echo.
)

call .venv\Scripts\activate.bat

REM --- Install dependencies (only if not already installed) ---------------
python -c "import dynasty" >nul 2>&1
if errorlevel 1 (
  echo Installing dependencies ^(one-time, ~1 minute^)...
  python -m pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt
  pip install --quiet -e .
  echo   Installed.
  echo.
)

REM --- Run the model ------------------------------------------------------
python -m dynasty.launcher

REM --- Keep window open ---------------------------------------------------
echo.
echo.
pause
