@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================================
echo   BIONIC MEDIA ^& AUDIO STUDIO - AUTOMATICKA INSTALACE (RTX 5060 Ti + Ryzen)
echo ============================================================================
echo.

echo [1/4] Kontroluji Python a vytvarim virtualni prostredi (.venv)...
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 (
        echo [CHYBA] Python nebyl nalezen! Nainstalujte prosim Python 3.10+ z python.org.
        pause
        exit /b 1
    )
)

echo [2/4] Instaluji knihovny, FFmpeg engine a NVIDIA CUDA 12 akceleraci...
".\.venv\Scripts\python.exe" -m pip install --upgrade pip
".\.venv\Scripts\pip.exe" install -r requirements.txt

echo [3/4] Generuji ikonu a vytvarim zastupce "Bionic Media & Audio Studio" na Plose...
".\.venv\Scripts\python.exe" generate_icon.py
".\.venv\Scripts\python.exe" create_shortcut.py

echo [4/4] Hotovo! Spoustim aplikaci...
echo ============================================================================
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0app.py"
