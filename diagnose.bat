@echo off
cd /d "%~dp0"
echo Starting wx_ROBOT in diagnostic mode...
echo.
set "PY=%~dp0runtime\python.exe"
if not exist "%PY%" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" app.py
echo.
echo Program exited. Please screenshot the error above.
pause
