@echo off
setlocal
cd /d "%~dp0"

REM ============================================================
REM  AutoSync - incremental upload (mirror, no re-upload)
REM  uploads changed jars only + deletes server extras
REM
REM  Required environment variable:
REM    AUTOSYNC_HOST     server host or IP
REM  Optional:
REM    AUTOSYNC_PORT     SSH port            (default 22)
REM    AUTOSYNC_USER     SSH user            (default root)
REM    AUTOSYNC_KEY      private key path    (default ~/.ssh/id_ed25519)
REM    AUTOSYNC_MODS     local mods folder   (default <repo>\mods)
REM    AUTOSYNC_ROOT     remote deploy root  (default /opt/autosync)
REM    AUTOSYNC_PYTHON   remote python cmd   (default python3)
REM
REM  See docs/部署-Linux.md for details.
REM ============================================================

if "%AUTOSYNC_HOST%"=="" (
  echo [ERROR] AUTOSYNC_HOST is not set.
  echo         Example:  set AUTOSYNC_HOST=sync.example.com
  echo         Optional: set AUTOSYNC_PORT=22 ^&^& set AUTOSYNC_USER=root
  pause
  exit /b 2
)

set "PY=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if not exist "%PY%" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not exist "%PY%" set "PY=python"

echo ============================================
echo   AutoSync - Incremental upload (mirror)
echo   changed only + delete extra = exact match
echo   host: %AUTOSYNC_HOST%
echo ============================================
echo.

chcp 65001 >nul

"%PY%" deploy.py --delete %*
set RC=%ERRORLEVEL%

echo.
echo --------------------------------------------
if "%RC%"=="0" (echo Done. exit code 0) else (echo Finished with exit code %RC%)
echo --------------------------------------------
pause
exit /b %RC%
