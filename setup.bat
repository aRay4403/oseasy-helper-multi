@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo.
echo   Setting up the Python environment for oseasy-helper ...
echo.

set "PYCMD="
where py >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD (
    where python >nul 2>nul && set "PYCMD=python"
)
if not defined PYCMD (
    echo   [!] Python 3.10+ not found on PATH.
    echo   Install Python from https://www.python.org/downloads/ and retry.
    echo.
    pause
    exit /b 1
)

if exist "%~dp0.venv\Scripts\python.exe" (
    echo   Environment already exists, skipping creation.
) else (
    echo   Creating virtual environment .venv ...
    %PYCMD% -m venv "%~dp0.venv" || goto :fail
)

echo   Installing dependency: psutil ...
"%~dp0.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
"%~dp0.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q psutil || goto :fail

echo.
echo   Done. Now edit  classrooms.conf  and run  start.bat
echo.
pause
exit /b 0

:fail
echo.
echo   [!] Setup failed. Check the messages above.
echo.
pause
exit /b 1
