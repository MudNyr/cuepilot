@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")

if not exist ".venv\Scripts\python.exe" (
    %PY% -m venv .venv
    if errorlevel 1 (
        echo Python was not found. Install Python 3.10+ from https://www.python.org/downloads/
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 (
    echo Installing the build tools failed. Check your internet connection and try again.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --windowed ^
    --name CuePilot --icon cuepilot.ico cuepilot.py
if errorlevel 1 (
    echo The build failed. Scroll up for the error.
    pause
    exit /b 1
)

echo.
echo Done. Your app is at: %CD%\dist\CuePilot.exe
echo You can copy that single file anywhere, or pin it to the taskbar.
pause
