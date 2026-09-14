@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul

call "%~dp0scripts\find_python.bat"
set "BOOTSTRAP_PY=!PY!"

if "%BOOTSTRAP_PY%"=="" (
  if defined PLCSIM_EXISTING_VENV_INVALID (
    echo [X] Existing .venv is not Python 3.11. Move it away and run this script again.
  ) else (
    echo [X] Python 3.11 not found. PLC-Sim supports Python 3.11.x only.
  )
  exit /b 1
)

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo [1/3] Creating virtual environment...
  "%BOOTSTRAP_PY%" -m venv "%~dp0.venv"
  if errorlevel 1 exit /b 1
)

echo [2/3] Upgrading pip...
"%~dp0.venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b 1

echo [3/3] Installing dependencies...
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 exit /b 1

echo.
echo [OK] Environment ready: %~dp0.venv
echo Run start_iccas_devices.bat next.
endlocal
