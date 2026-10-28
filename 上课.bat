@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo.
    echo   [!] Python environment not found.
    echo   Please run  setup.bat  first to install it.
    echo.
    pause
    exit /b 1
)

title OsEasy - official student client mode
"%PY%" "%~dp0official.py" %*
echo.
pause
endlocal
