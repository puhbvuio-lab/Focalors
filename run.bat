@echo off
setlocal EnableExtensions
chcp 65001 >nul

rem ===== Social Platform Scraper - Launcher =====
cd /d "%~dp0"

set "PY=%~dp0venv\Scripts\python.exe"
set "CHECK_ONLY=0"
if /I "%~1"=="--check" set "CHECK_ONLY=1"

if not exist "%~dp0main.py" (
    echo [ERROR] main.py not found. Place this BAT in the project root.
    pause
    exit /b 1
)

if not exist "%PY%" (
    if "%CHECK_ONLY%"=="1" (
        echo [ERROR] Python venv not found: %PY%
        exit /b 1
    )
    if exist "%~dp0install_or_update.bat" (
        echo Python venv not found. Running installer first...
        call "%~dp0install_or_update.bat" --dir "%~dp0" --no-start --no-pause
        if errorlevel 1 (
            echo.
            echo [ERROR] Installer failed.
            pause
            exit /b 1
        )
    ) else (
        echo [ERROR] Python venv not found: %PY%
        echo Please run install_or_update.bat first.
        echo.
        pause
        exit /b 1
    )
)

"%PY%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)" >nul 2>nul
if errorlevel 1 (
    echo.
    for /f "tokens=*" %%V in ('"%PY%" --version 2^>^&1') do echo [ERROR] Unsupported venv Python: %%V
    echo This app requires Python 3.13.
    echo Please rerun install_or_update.bat after installing Python 3.13.
    if "%CHECK_ONLY%"=="1" exit /b 1
    pause
    exit /b 1
)

if "%CHECK_ONLY%"=="1" (
    "%PY%" -c "import PyQt5, openpyxl, playwright; import src.studio.qt_app; print('Runtime import OK')"
    exit /b %ERRORLEVEL%
)

echo Starting Social Platform Scraper ...
"%PY%" main.py
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
    echo.
    echo [INFO] Exit code: %EXITCODE%
    pause
)

endlocal
exit /b %EXITCODE%
