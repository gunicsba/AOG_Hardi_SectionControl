@echo off
echo Building AOG-HARDI Bridge...
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
pyinstaller --onefile --console --name "AOG-HARDI-Bridge" %ICON_FLAG% "%~dp0AOG_HARDI_bridge.py"
if exist "%~dp0dist\AOG-HARDI-Bridge.exe" (
    copy "%~dp0dist\AOG-HARDI-Bridge.exe" "%~dp0AOG-HARDI-Bridge.exe" >nul
    echo.
    echo Built: AOG-HARDI-Bridge.exe
)
pause
