@echo off
call "%~dp0start_iccas_process.bat" --device-model --device-state-dir "%~dp0data\runtime\iccas-devices" %*
exit /b %errorlevel%
