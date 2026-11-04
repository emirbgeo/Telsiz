@echo off
REM Builds dist\Telsiz.exe, a single file you can send to friends.
cd /d "%~dp0"
py -m pip install --disable-pip-version-check -r requirements.txt "pyinstaller>=6.9"
if errorlevel 1 (
    echo Python or pip was not found. Install Python 3.10 or newer from python.org first.
    pause
    exit /b 1
)
py -c "import telsiz; telsiz.make_icon('idle', 256).save('telsiz.ico', sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])"
set ICON=
if exist telsiz.ico set ICON=--icon telsiz.ico
py -m PyInstaller --noconfirm --onefile --windowed --name Telsiz %ICON% ^
    --add-data "ui;ui" ^
    --collect-all av --collect-all sounddevice --collect-all _sounddevice_data ^
    --collect-all webview --collect-all pythonnet --collect-all clr_loader --hidden-import clr ^
    --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 ^
    --hidden-import pystray._win32 --collect-submodules mss --exclude-module tkinter ^
    telsiz.py
echo.
echo Done. Your program is dist\Telsiz.exe
pause
