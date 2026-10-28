@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

title OsEasy - push to GitHub
"%PY%" "%~dp0push_github.py" %*
echo.
pause
endlocal
