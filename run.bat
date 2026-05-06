@echo off
setlocal

set PYTHONUTF8=1
set PROJECT=v:\Work\Space42\Projects\weather_forecast
set PYTHON=%PROJECT%\.venv\Scripts\python.exe
set LOGDIR=%PROJECT%\logs
set LOGFILE=%LOGDIR%\weather_report.log

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

echo. >> "%LOGFILE%"
echo === %DATE% %TIME% === >> "%LOGFILE%"
"%PYTHON%" "%PROJECT%\main.py" >> "%LOGFILE%" 2>&1

endlocal
