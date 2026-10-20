@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")

if not exist ".venv\Scripts\pythonw.exe" (
    echo Setting up CuePilot for the first time. This takes a minute or two...
    %PY% -m venv .venv
    if errorlevel 1 goto :nopython
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :pipfail
)

start "" ".venv\Scripts\pythonw.exe" cuepilot.py
exit /b 0

:nopython
echo.
echo Python was not found.
echo Install Python 3.10 or newer from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" in the installer), then run this file again.
pause
exit /b 1

:pipfail
echo.
echo Installing PySide6 failed. Check your internet connection or company proxy,
echo then run this file again.
rmdir /s /q .venv
pause
exit /b 1
