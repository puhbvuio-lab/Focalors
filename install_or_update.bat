@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

rem One-click installer/updater for Social Platform Scraper.
rem Usage:
rem   install_or_update.bat
rem   install_or_update.bat --dir D:\tools\social-platform-scraper
rem   install_or_update.bat --no-start
rem   install_or_update.bat --check-only
rem Optional:
rem   set SCRAPER_INSTALL_DIR=D:\tools\social-platform-scraper

set "APP_NAME=Social Platform Scraper"
set "REPO_URL=https://github.com/puhbvuio-lab/funina.git"
set "REPO_ZIP_URL=https://codeload.github.com/puhbvuio-lab/funina/zip/refs/heads/main"
set "DEFAULT_DIR=%USERPROFILE%\social-platform-scraper"
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

set "NO_START=0"
set "CHECK_ONLY=0"
set "NO_PAUSE=0"
set "ARG_INSTALL_DIR="

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--dir" (
    if "%~2"=="" (
        echo [ERROR] --dir requires a directory path.
        exit /b 1
    )
    set "ARG_INSTALL_DIR=%~2"
    shift
    shift
    goto parse_args
)
if /I "%~1"=="--install-dir" (
    if "%~2"=="" (
        echo [ERROR] --install-dir requires a directory path.
        exit /b 1
    )
    set "ARG_INSTALL_DIR=%~2"
    shift
    shift
    goto parse_args
)
if /I "%~1"=="--no-start" set "NO_START=1"
if /I "%~1"=="--check-only" set "CHECK_ONLY=1"
if /I "%~1"=="--no-pause" set "NO_PAUSE=1"
if /I "%~1"=="--help" goto help
if /I "%~1"=="/?" goto help
shift
goto parse_args

:args_done
call :choose_install_dir || goto failed

echo.
echo ============================================================
echo   %APP_NAME% installer / updater
echo ============================================================
echo Target directory: "%INSTALL_DIR%"
echo Repository:       %REPO_URL%
echo.

call :require_git || goto failed
call :find_python || goto failed
call :check_python_version || goto failed
call :check_browsers

if "%CHECK_ONLY%"=="1" (
    echo.
    echo [OK] Check-only mode completed. No files were changed.
    goto success
)

call :prepare_source || goto failed
call :setup_venv || goto failed
call :install_requirements || goto failed
call :install_playwright || goto failed
call :verify_runtime || goto failed
call :ensure_runtime_dirs || goto failed
call :print_notes

if "%NO_START%"=="1" (
    echo.
    echo [OK] Install/update completed. Start later with run.bat.
    goto success
)

if exist "%INSTALL_DIR%\run.bat" (
    echo.
    echo Starting %APP_NAME% ...
    start "" "%INSTALL_DIR%\run.bat"
) else (
    echo.
    echo [WARN] run.bat was not found. You can start manually:
    echo        "%INSTALL_DIR%\venv\Scripts\python.exe" "%INSTALL_DIR%\main.py"
)

goto success

:help
echo.
echo Usage:
echo   install_or_update.bat              Install/update, then start the app
echo   install_or_update.bat --dir PATH   Install/update to PATH without prompting
echo   install_or_update.bat --no-start   Install/update only
echo   install_or_update.bat --check-only Check Git, Python, browser only
echo.
echo Optional environment variable:
echo   SCRAPER_INSTALL_DIR=D:\tools\social-platform-scraper
echo.
exit /b 0

:detect_suggested_dir
if exist "%SCRIPT_DIR%\main.py" (
    if exist "%SCRIPT_DIR%\requirements.txt" (
        set "SUGGESTED_DIR=%SCRIPT_DIR%"
        exit /b 0
    )
)
set "SUGGESTED_DIR=%DEFAULT_DIR%"
exit /b 0

:choose_install_dir
call :detect_suggested_dir

if defined ARG_INSTALL_DIR (
    set "INSTALL_DIR=!ARG_INSTALL_DIR!"
    for %%I in ("!INSTALL_DIR!") do set "INSTALL_DIR=%%~fI"
    exit /b 0
)

if defined SCRAPER_INSTALL_DIR (
    set "INSTALL_DIR=!SCRAPER_INSTALL_DIR!"
    for %%I in ("!INSTALL_DIR!") do set "INSTALL_DIR=%%~fI"
    exit /b 0
)

if "%CHECK_ONLY%"=="1" (
    set "INSTALL_DIR=!SUGGESTED_DIR!"
    for %%I in ("!INSTALL_DIR!") do set "INSTALL_DIR=%%~fI"
    exit /b 0
)

echo.
echo Choose install/update directory.
echo Suggested: "%SUGGESTED_DIR%"
echo.
echo   - Type or paste a target directory path.
echo   - Type B to open a folder picker.
echo   - Press Enter to use the suggested directory.
echo.
set "INSTALL_DIR="
set /p "INSTALL_DIR=Install directory: "
if /I "!INSTALL_DIR!"=="B" call :browse_install_dir
if not defined INSTALL_DIR set "INSTALL_DIR=!SUGGESTED_DIR!"
for %%I in ("!INSTALL_DIR!") do set "INSTALL_DIR=%%~fI"
exit /b 0

:browse_install_dir
set "PICKED_DIR="
for /f "usebackq delims=" %%D in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$shell = New-Object -ComObject Shell.Application; $folder = $shell.BrowseForFolder(0, 'Choose the install/update directory for Social Platform Scraper', 0, '%SUGGESTED_DIR%'); if ($folder) { $folder.Self.Path }"`) do set "PICKED_DIR=%%D"
if defined PICKED_DIR (
    set "INSTALL_DIR=%PICKED_DIR%"
) else (
    echo [WARN] Folder picker was cancelled. Using suggested directory.
    set "INSTALL_DIR=%SUGGESTED_DIR%"
)
exit /b 0

:require_git
where git >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Git was not found.
    echo Install Git for Windows first: https://git-scm.com/download/win
    exit /b 1
)
for /f "tokens=*" %%V in ('git --version 2^>^&1') do echo Git: %%V
exit /b 0

:find_python
set "PY_CMD="
set "PY_VERSION_TEXT="
where py >nul 2>nul
if not errorlevel 1 (
    py -3.13 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY_CMD=py -3.13"
)
if not defined PY_CMD (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)" >nul 2>nul
        if not errorlevel 1 set "PY_CMD=python"
    )
)
if not defined PY_CMD (
    echo [ERROR] Python 3.13 was not found.
    echo Install Python 3.13 first: https://www.python.org/downloads/windows/
    exit /b 1
)
for /f "tokens=*" %%V in ('!PY_CMD! --version 2^>^&1') do set "PY_VERSION_TEXT=%%V"
echo Python: !PY_VERSION_TEXT!
exit /b 0

:check_python_version
!PY_CMD! -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)"
if errorlevel 1 (
    echo [ERROR] Python 3.13 is required.
    exit /b 1
)
exit /b 0

:check_browsers
set "FOUND_CHROME=0"
set "FOUND_EDGE=0"
set "CHROME_HINT="
set "EDGE_HINT="
for %%P in (
    "%ProgramFiles%\Google\Chrome\Application\chrome.exe"
    "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
    "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
) do (
    if exist "%%~P" (
        set "FOUND_CHROME=1"
        set "CHROME_HINT=%%~P"
    )
)
for %%P in (
    "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
    "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
    "%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"
) do (
    if exist "%%~P" (
        set "FOUND_EDGE=1"
        set "EDGE_HINT=%%~P"
    )
)
where chrome >nul 2>nul
if not errorlevel 1 (
    set "FOUND_CHROME=1"
    if not defined CHROME_HINT set "CHROME_HINT=chrome.exe from PATH"
)
where msedge >nul 2>nul
if not errorlevel 1 (
    set "FOUND_EDGE=1"
    if not defined EDGE_HINT set "EDGE_HINT=msedge.exe from PATH"
)
if "%FOUND_CHROME%"=="1" echo Chrome: !CHROME_HINT!
if "%FOUND_EDGE%"=="1" echo Edge:   !EDGE_HINT!
if "%FOUND_CHROME%"=="0" if "%FOUND_EDGE%"=="0" (
    echo [WARN] Chrome/Edge was not detected. Browser-based scraping needs one of them.
)
exit /b 0

:prepare_source
if exist "%INSTALL_DIR%\.git" (
    echo.
    echo Updating existing repository...
    pushd "%INSTALL_DIR%" || exit /b 1
    git rev-parse --is-inside-work-tree >nul 2>nul
    if errorlevel 1 (
        popd
        echo [WARN] Existing .git directory looks incomplete.
        call :move_incomplete_target || exit /b 1
        goto prepare_source
    )
    if not exist "%INSTALL_DIR%\main.py" (
        popd
        echo [WARN] Existing checkout looks incomplete.
        call :move_incomplete_target || exit /b 1
        goto prepare_source
    )
    set "CURRENT_REMOTE="
    for /f "delims=" %%R in ('git remote get-url origin 2^>nul') do set "CURRENT_REMOTE=%%R"
    if defined CURRENT_REMOTE (
        if /I not "!CURRENT_REMOTE!"=="%REPO_URL%" (
            echo [WARN] Current origin is "!CURRENT_REMOTE!".
            echo        Expected "%REPO_URL%". Pulling from the current origin.
        )
    ) else (
        echo [WARN] No git origin was found. Skipping git update.
        popd
        exit /b 0
    )
    git fetch --all --prune
    if errorlevel 1 (
        popd
        exit /b 1
    )
    git pull --ff-only
    if errorlevel 1 (
        echo.
        echo [ERROR] Git update failed. Local code changes may conflict with the remote branch.
        echo        Commit/stash your local changes, or install to a clean directory.
        popd
        exit /b 1
    )
    popd
    exit /b 0
)

if exist "%INSTALL_DIR%\main.py" (
    echo.
    echo [WARN] Source files exist, but this is not a git clone.
    echo        Dependencies will be installed, but automatic git update is unavailable.
    exit /b 0
)

if exist "%INSTALL_DIR%" (
    dir /b "%INSTALL_DIR%" 2>nul | findstr /r "." >nul
    if not errorlevel 1 (
        if not defined INSTALL_REDIRECTED (
            echo.
            echo [WARN] Selected directory exists, is not empty, and is not a git repository:
            echo        "%INSTALL_DIR%"
            echo        Installing into a child directory instead:
            echo        "%INSTALL_DIR%\social-platform-scraper"
            set "INSTALL_DIR=%INSTALL_DIR%\social-platform-scraper"
            set "INSTALL_REDIRECTED=1"
            goto prepare_source
        )
        echo [ERROR] Target directory exists but is not empty and not a git repository:
        echo        "%INSTALL_DIR%"
        echo        Choose another directory, or clean this directory first.
        exit /b 1
    )
) else (
    for %%I in ("%INSTALL_DIR%") do set "PARENT_DIR=%%~dpI"
    if not exist "!PARENT_DIR!" mkdir "!PARENT_DIR!"
)

echo.
call :ensure_install_parent_writable || exit /b 1
call :clone_source
if errorlevel 1 (
    echo.
    echo [WARN] Git clone failed after retries. Trying ZIP download fallback...
    call :download_source_zip || exit /b 1
)
exit /b 0

:clone_source
set "CLONE_TMP=%TEMP%\sct_gitclone_%RANDOM%%RANDOM%"
if exist "!CLONE_TMP!" rmdir /s /q "!CLONE_TMP!"
for /L %%A in (1,1,3) do (
    echo.
    echo Cloning repository, attempt %%A/3...
    git -c http.version=HTTP/1.1 clone --depth 1 "%REPO_URL%" "!CLONE_TMP!"
    if not errorlevel 1 (
        call :replace_empty_target_with "!CLONE_TMP!"
        if errorlevel 1 (
            if exist "!CLONE_TMP!" rmdir /s /q "!CLONE_TMP!"
            exit /b 1
        )
        exit /b 0
    )
    if exist "!CLONE_TMP!" rmdir /s /q "!CLONE_TMP!"
    if not "%%A"=="3" (
        echo [WARN] Clone attempt %%A failed. Retrying in 5 seconds...
        timeout /t 5 /nobreak >nul
    )
)
exit /b 1

:download_source_zip
set "ZIP_TMP=%TEMP%\ct_source_%RANDOM%%RANDOM%.zip"
set "EXTRACT_TMP=%TEMP%\ct_source_%RANDOM%%RANDOM%"
set "ZIP_ROOT="
if exist "!ZIP_TMP!" del /q "!ZIP_TMP!"
if exist "!EXTRACT_TMP!" rmdir /s /q "!EXTRACT_TMP!"

echo.
echo Downloading repository ZIP...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri $env:REPO_ZIP_URL -OutFile $env:ZIP_TMP -UseBasicParsing -Headers @{ 'User-Agent' = 'SocialPlatformScraperInstaller' }"
if errorlevel 1 (
    echo [ERROR] ZIP download failed. Check network access to github.com.
    echo        ZIP URL: %REPO_ZIP_URL%
    if exist "!ZIP_TMP!" del /q "!ZIP_TMP!"
    exit /b 1
)

echo Extracting repository ZIP...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%ZIP_TMP%' -DestinationPath '%EXTRACT_TMP%' -Force"
if errorlevel 1 (
    echo [ERROR] ZIP extraction failed.
    if exist "!ZIP_TMP!" del /q "!ZIP_TMP!"
    if exist "!EXTRACT_TMP!" rmdir /s /q "!EXTRACT_TMP!"
    exit /b 1
)

for /d %%D in ("!EXTRACT_TMP!\*") do (
    if not defined ZIP_ROOT set "ZIP_ROOT=%%~fD"
)
if not defined ZIP_ROOT (
    echo [ERROR] ZIP package did not contain a source directory.
    if exist "!ZIP_TMP!" del /q "!ZIP_TMP!"
    if exist "!EXTRACT_TMP!" rmdir /s /q "!EXTRACT_TMP!"
    exit /b 1
)

call :replace_empty_target_with "!ZIP_ROOT!" || (
    if exist "!ZIP_TMP!" del /q "!ZIP_TMP!"
    if exist "!EXTRACT_TMP!" rmdir /s /q "!EXTRACT_TMP!"
    exit /b 1
)
if exist "!ZIP_TMP!" del /q "!ZIP_TMP!"
if exist "!EXTRACT_TMP!" rmdir /s /q "!EXTRACT_TMP!"
echo [OK] Installed from ZIP fallback. Future automatic git update is unavailable for this copy.
exit /b 0

:ensure_install_parent_writable
for %%I in ("%INSTALL_DIR%") do set "TARGET_PARENT=%%~dpI"
if not defined TARGET_PARENT set "TARGET_PARENT=%USERPROFILE%\"
if not exist "!TARGET_PARENT!" mkdir "!TARGET_PARENT!" 2>nul
if not exist "!TARGET_PARENT!" (
    echo [ERROR] Parent directory does not exist and could not be created:
    echo        "!TARGET_PARENT!"
    exit /b 1
)
set "CREATED_INSTALL_TEST_DIR=0"
if not exist "%INSTALL_DIR%" (
    mkdir "%INSTALL_DIR%" 2>nul
    if errorlevel 1 (
        echo [ERROR] Cannot create the selected install directory:
        echo        "%INSTALL_DIR%"
        echo.
        echo Choose a user-writable directory, for example:
        echo        "%USERPROFILE%\social-platform-scraper"
        echo or rerun this installer as Administrator if you intentionally want Program Files.
        exit /b 1
    )
    set "CREATED_INSTALL_TEST_DIR=1"
)
set "WRITE_TEST_FILE=%INSTALL_DIR%\.sct_write_test_%RANDOM%%RANDOM%.tmp"
break > "!WRITE_TEST_FILE!" 2>nul
if errorlevel 1 (
    echo [ERROR] Cannot write to the selected install location:
    echo        "%INSTALL_DIR%"
    echo.
    echo Choose a user-writable directory, for example:
    echo        "%USERPROFILE%\social-platform-scraper"
    echo or rerun this installer as Administrator if you intentionally want Program Files.
    if "!CREATED_INSTALL_TEST_DIR!"=="1" rmdir "%INSTALL_DIR%" >nul 2>nul
    exit /b 1
)
del /q "!WRITE_TEST_FILE!" >nul 2>nul
if "!CREATED_INSTALL_TEST_DIR!"=="1" rmdir "%INSTALL_DIR%" >nul 2>nul
exit /b 0

:replace_empty_target_with
set "SOURCE_DIR=%~1"
if not exist "%SOURCE_DIR%\main.py" (
    echo [ERROR] Source directory is invalid: "%SOURCE_DIR%"
    exit /b 1
)
if exist "%INSTALL_DIR%" (
    dir /b "%INSTALL_DIR%" 2>nul | findstr /r "." >nul
    if not errorlevel 1 (
        echo [ERROR] Target directory is no longer empty:
        echo        "%INSTALL_DIR%"
        exit /b 1
    )
    rmdir "%INSTALL_DIR%" 2>nul
)
for %%I in ("%INSTALL_DIR%") do set "PARENT_DIR=%%~dpI"
if not exist "!PARENT_DIR!" mkdir "!PARENT_DIR!"
move /y "%SOURCE_DIR%" "%INSTALL_DIR%" >nul
if errorlevel 1 (
    echo [ERROR] Failed to move source into target directory.
    exit /b 1
)
exit /b 0

:move_incomplete_target
set "BROKEN_DIR=%INSTALL_DIR%_incomplete_%RANDOM%%RANDOM%"
echo Moving incomplete directory aside:
echo   "%INSTALL_DIR%"
echo   -^> "!BROKEN_DIR!"
move /y "%INSTALL_DIR%" "!BROKEN_DIR!" >nul
if errorlevel 1 (
    echo [ERROR] Failed to move incomplete directory aside.
    exit /b 1
)
exit /b 0

:setup_venv
set "VENV_PY=%INSTALL_DIR%\venv\Scripts\python.exe"
if exist "%VENV_PY%" (
    "%VENV_PY%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)" >nul 2>nul
    if errorlevel 1 (
        echo.
        for /f "tokens=*" %%V in ('"%VENV_PY%" --version 2^>^&1') do echo [WARN] Existing virtual environment uses unsupported Python: %%V
        set "OLD_VENV=%INSTALL_DIR%\venv_unsupported_%RANDOM%%RANDOM%"
        echo Moving old venv aside:
        echo   "%INSTALL_DIR%\venv"
        echo   -^> "!OLD_VENV!"
        move /y "%INSTALL_DIR%\venv" "!OLD_VENV!" >nul
        if errorlevel 1 (
            echo [ERROR] Failed to move unsupported virtual environment aside.
            exit /b 1
        )
    ) else (
        echo.
        echo Virtual environment: "%INSTALL_DIR%\venv"
        exit /b 0
    )
)
if exist "%INSTALL_DIR%\venv" (
    echo.
    echo [ERROR] Existing venv directory is present but no usable python.exe was found:
    echo        "%INSTALL_DIR%\venv"
    echo        Rename or remove it, then rerun this installer.
    exit /b 1
)
echo.
echo Creating virtual environment...
!PY_CMD! -m venv "%INSTALL_DIR%\venv"
if errorlevel 1 exit /b 1
exit /b 0

:install_requirements
set "VENV_PY=%INSTALL_DIR%\venv\Scripts\python.exe"
if not exist "%INSTALL_DIR%\requirements.txt" (
    echo [ERROR] requirements.txt was not found in "%INSTALL_DIR%".
    exit /b 1
)
echo.
echo Installing Python dependencies...
"%VENV_PY%" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 exit /b 1
"%VENV_PY%" -m pip install -r "%INSTALL_DIR%\requirements.txt"
if errorlevel 1 exit /b 1
exit /b 0

:install_playwright
set "VENV_PY=%INSTALL_DIR%\venv\Scripts\python.exe"
echo.
echo Installing Playwright Chromium runtime...
"%VENV_PY%" -m playwright install chromium
if errorlevel 1 (
    echo [WARN] Playwright browser installation failed.
    echo        The app can still use local Chrome/Edge via CDP, but rerun this command later if needed:
    echo        "%VENV_PY%" -m playwright install chromium
)
exit /b 0

:verify_runtime
set "VENV_PY=%INSTALL_DIR%\venv\Scripts\python.exe"
echo.
echo Verifying runtime imports...
pushd "%INSTALL_DIR%" || exit /b 1
"%VENV_PY%" -c "import PyQt5, openpyxl, playwright; import src.studio.qt_app; print('Runtime import OK')"
set "VERIFY_EXIT=%ERRORLEVEL%"
popd
if not "%VERIFY_EXIT%"=="0" (
    echo [ERROR] Runtime import check failed.
    exit /b 1
)
exit /b 0

:ensure_runtime_dirs
if not exist "%INSTALL_DIR%\output" mkdir "%INSTALL_DIR%\output"
if not exist "%INSTALL_DIR%\user_data" mkdir "%INSTALL_DIR%\user_data"
if not exist "%INSTALL_DIR%\user_data_edge" mkdir "%INSTALL_DIR%\user_data_edge"
if defined LOCALAPPDATA (
    if not exist "%LOCALAPPDATA%\SocialPlatformScraper\browser_profiles\chrome" mkdir "%LOCALAPPDATA%\SocialPlatformScraper\browser_profiles\chrome"
    if not exist "%LOCALAPPDATA%\SocialPlatformScraper\browser_profiles\edge" mkdir "%LOCALAPPDATA%\SocialPlatformScraper\browser_profiles\edge"
    if not exist "%LOCALAPPDATA%\SocialPlatformScraper\checkpoints" mkdir "%LOCALAPPDATA%\SocialPlatformScraper\checkpoints"
)
if not exist "%INSTALL_DIR%\.env" (
    echo.
    echo [WARN] .env was not found. YouTube API / AIGC tools may need API keys.
    if exist "%INSTALL_DIR%\.env.example" (
        echo        See "%INSTALL_DIR%\.env.example".
    )
)
exit /b 0

:print_notes
echo.
echo Notes:
echo   - Re-run install_or_update.bat any time to update code and dependencies.
echo   - Chrome CDP uses port 9222; Edge CDP uses port 9223.
echo   - Browser login profiles and checkpoints use %%LOCALAPPDATA%%\SocialPlatformScraper by default.
echo   - X tools can switch browser in the app global settings.
echo   - output, config, legacy user_data/user_data_edge and .env are kept locally.
exit /b 0

:failed
echo.
echo [FAILED] Install/update did not complete.
if "%NO_PAUSE%"=="1" exit /b 1
pause
exit /b 1

:success
echo.
echo [DONE]
if "%NO_PAUSE%"=="1" exit /b 0
pause
exit /b 0
