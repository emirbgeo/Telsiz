@echo off
REM Telsiz firewall helper, for the person who HOSTS. Run once.
REM Right click this file and choose "Run as administrator".
net session >nul 2>&1
if errorlevel 1 (
    echo Please right click allow_firewall.bat and choose "Run as administrator".
    pause
    exit /b 1
)
cd /d "%~dp0"
echo Removing old inbound rules for Python and Telsiz, including any BLOCK rules...
for /f "usebackq delims=" %%P in (`py -c "import sys,os;print(os.path.join(sys.base_prefix,'pythonw.exe'));print(os.path.join(sys.base_prefix,'python.exe'))"`) do (
    netsh advfirewall firewall delete rule name=all dir=in program="%%P" >nul 2>&1
)
netsh advfirewall firewall delete rule name=all dir=in program="%~dp0.venv\Scripts\pythonw.exe" >nul 2>&1
netsh advfirewall firewall delete rule name=all dir=in program="%~dp0.venv\Scripts\python.exe" >nul 2>&1
netsh advfirewall firewall delete rule name=all dir=in program="%~dp0dist\Telsiz.exe" >nul 2>&1
echo Opening UDP port 50505 for every network type (Private, Public, Domain)...
netsh advfirewall firewall delete rule name="Telsiz UDP 50505" >nul 2>&1
netsh advfirewall firewall add rule name="Telsiz UDP 50505" dir=in action=allow protocol=UDP localport=50505 profile=any
echo.
echo Done. Other Python programs that accept connections will ask for permission again.
echo If you host on another port, change 50505 in this file and run it again.
pause
