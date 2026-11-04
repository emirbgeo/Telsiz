@echo off
REM Starts Telsiz. Creates a local Python environment on first run, and installs
REM new libraries automatically whenever requirements.txt changes (after an update).
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Setting up Telsiz for the first time, this takes a minute...
    py -m venv .venv
    if errorlevel 1 (
        echo Python was not found. Install Python 3.10 or newer from python.org and tick "Add python.exe to PATH".
        pause
        exit /b 1
    )
)
fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1
if errorlevel 1 (
    echo Installing the libraries Telsiz needs...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo Installing the libraries failed. See the messages above.
        pause
        exit /b 1
    )
    copy /y requirements.txt ".venv\requirements.installed" >nul
)
start "" ".venv\Scripts\pythonw.exe" telsiz.py
