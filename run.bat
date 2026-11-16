@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo [INFO] Virtualni prostredi nebylo nalezeno, spoustim prvotni instalaci...
    call setup.bat
    exit /b
)

start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0app.py"
