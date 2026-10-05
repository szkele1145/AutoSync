@echo off
setlocal
cd /d "%~dp0"

REM ============================================================
REM  AutoSync - FULL upload (EXACT MIRROR)
REM
REM  The remote mods folder will be made EXACTLY the same as
REM  your local folder. Extra files on the server WILL BE DELETED.
REM
REM  Required environment variable:
REM    AUTOSYNC_HOST     server host or IP
REM  Optional:
REM    AUTOSYNC_PORT / AUTOSYNC_USER / AUTOSYNC_KEY /
REM    AUTOSYNC_MODS / AUTOSYNC_ROOT / AUTOSYNC_PYTHON
REM
REM  See docs/部署-Linux.md for details.
REM ============================================================

echo ============================================
echo   AutoSync - FULL Upload (EXACT MIRROR)
echo ============================================
echo.
echo   [WARN] The remote mods folder will be made
echo          EXACTLY the same as your local folder.
echo          Extra files on the server WILL BE DELETED.
echo.
set /p GO=Type Y to continue (anything else cancels): 
if /i not "%GO%"=="Y" goto cancel
echo.

if "%AUTOSYNC_HOST%"=="" (
  echo [ERROR] AUTOSYNC_HOST is not set.
  echo         Example:  set AUTOSYNC_HOST=sync.example.com
  pause
  exit /b 2
)

set "PY=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if not exist "%PY%" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not exist "%PY%" set "PY=python"

chcp 65001 >nul

"%PY%" deploy.py --full --delete %*
set RC=%ERRORLEVEL%

echo.
echo --------------------------------------------
if "%RC%"=="0" (echo Done. exit code 0) else (echo Finished with exit code %RC%)
echo --------------------------------------------
pause
exit /b %RC%

:cancel
echo.
echo Cancelled. Nothing was changed.
pause
exit /b 0
