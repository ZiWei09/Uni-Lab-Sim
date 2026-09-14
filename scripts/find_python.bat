@echo off
REM find_python.bat -- Locate an exact Python 3.11 interpreter.
REM Priority: project .venv, PYTHON, Windows py launcher, known paths, PATH.
for %%I in ("%~dp0..") do set "PLCSIM_ROOT=%%~fI"
set "PY="
set "PLCSIM_EXISTING_VENV_INVALID="

if exist "%PLCSIM_ROOT%\.venv\Scripts\python.exe" (
  call :accept_python "%PLCSIM_ROOT%\.venv\Scripts\python.exe"
  if not defined PY set "PLCSIM_EXISTING_VENV_INVALID=1"
  goto :eof
)

if defined PYTHON (
  call :accept_python "%PYTHON%"
  if defined PY goto :eof
)

for /f "delims=" %%p in ('py -3.11 -c "import sys; print(sys.executable)" 2^>nul') do (
  call :accept_python "%%p"
  if defined PY goto :eof
)

if exist "D:\miniforge3\envs\unilab\python.exe" (
  call :accept_python "D:\miniforge3\envs\unilab\python.exe"
  if defined PY goto :eof
)
if exist "D:\miniforge3\python.exe" (
  call :accept_python "D:\miniforge3\python.exe"
  if defined PY goto :eof
)

for /f "delims=" %%p in ('where.exe python3.11 2^>nul ^| findstr /V /I "WindowsApps"') do (
  call :accept_python "%%p"
  if defined PY goto :eof
)
for /f "delims=" %%p in ('where.exe python 2^>nul ^| findstr /V /I "WindowsApps"') do (
  call :accept_python "%%p"
  if defined PY goto :eof
)
goto :eof

:accept_python
if not exist "%~1" exit /b 0
"%~1" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
exit /b 0
