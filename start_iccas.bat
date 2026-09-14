@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup_venv.bat first.
  exit /b 1
)
".venv\Scripts\python.exe" iccas_sim.py %*
exit /b %errorlevel%
