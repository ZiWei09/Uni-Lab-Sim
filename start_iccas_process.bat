@echo off
call "%~dp0start_iccas.bat" --station postprocessing --timing-mode process --time-scale 60 --seed-process-recipe --filter-extensions --state-file "%~dp0data\runtime\iccas-process-state.json" %*
exit /b %errorlevel%
