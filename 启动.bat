@echo off
setlocal
cd /d "%~dp0"

set "PYW=%~dp0runtime\pythonw.exe"
if not exist "%PYW%" set "PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
set "PYARGS="
if not exist "%PYW%" (
  where pyw >nul 2>nul
  if not errorlevel 1 (
    set "PYW=pyw"
    set "PYARGS=-3.12"
  ) else (
    set "PYW=pythonw"
  )
)

"%PYW%" %PYARGS% -c "import tkinter, PIL" >nul 2>nul
if errorlevel 1 (
  echo.
  echo [wx_ROBOT] Missing dependency: tkinter or Pillow.
  echo Please run "install_deps.bat" first.
  echo.
  pause
  exit /b 1
)

start "" /D "%~dp0" "%PYW%" %PYARGS% "%~dp0app.py"
exit /b 0
