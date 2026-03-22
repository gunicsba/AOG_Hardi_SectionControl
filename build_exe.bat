@echo off
echo Building AOG-TUVR Bridge...
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    pip install pyinstaller
)
pip show pyserial >nul 2>&1
if errorlevel 1 (
    echo Installing pyserial...
    pip install pyserial
)
if exist "%~dp0icon.ico" (
    set ICON_FLAG=--icon="%~dp0icon.ico"
    echo Using icon: icon.ico
) else (
    set ICON_FLAG=
    echo WARNING: icon.ico not found!
)
pyinstaller --onefile --console --name "AOG-TUVR-Bridge" %ICON_FLAG% "%~dp0AOG_TUVR_bridge.py"
if exist "%~dp0dist\AOG-TUVR-Bridge.exe" (
    copy "%~dp0dist\AOG-TUVR-Bridge.exe" "%~dp0AOG-TUVR-Bridge.exe" >nul
    echo.
    echo Built: AOG-TUVR-Bridge.exe
)
pause
