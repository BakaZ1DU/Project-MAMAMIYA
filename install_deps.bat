@echo off
cd /d "%~dp0"
echo Installing / updating Pillow...
set "PY=%~dp0runtime\python.exe"
if not exist "%PY%" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m pip install --upgrade pillow
echo.
pause
