@echo off
REM Media Resource Manager launcher
REM No arguments  -> open the desktop GUI
REM With arguments -> run the CLI, e.g.:  run.bat dl <url> --out D:\pics

setlocal
set "EXE=%~dp0dist\SweetWeb.exe"

if not exist "%EXE%" (
    echo Cannot find the application:
    echo   %EXE%
    echo Please build it first:
    echo   pyinstaller build\pyinstaller.spec --noconfirm
    pause
    exit /b 1
)

"%EXE%" %*
endlocal
