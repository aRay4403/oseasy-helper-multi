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

"%PY%" "%~dp0main.py" %*
echo.
pause
endlocal
