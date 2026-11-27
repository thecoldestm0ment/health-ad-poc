@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>nul
    if errorlevel 1 (python -m venv .venv) else (py -3 -m venv .venv)
    if errorlevel 1 goto :fail
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail
if not exist .env copy /y .env.example .env >nul
if not exist data\products.csv copy /y data\products.template.csv data\products.csv >nul
echo Setup complete. Edit .env, then run: run.bat check
echo Offline demo: run.bat demo
exit /b 0
:fail
echo Setup failed. Install Python 3.10 or later and retry.
exit /b 1
