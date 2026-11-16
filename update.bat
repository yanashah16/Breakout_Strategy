@echo off
rem ============================================================================
rem  Pattern Desk - double-click to get the latest code from GitHub.
rem
rem  Never touched: config\settings.yaml (your settings), state\ (trades),
rem                logs\, data\ (bar cache).
rem  Code files changed on this machine are copied to .update_backups\<time>\
rem  before they are replaced, so nothing is lost.
rem  Also works on a folder that was copied by hand (not cloned): the first run
rem  links it to GitHub.
rem ============================================================================
setlocal
title Pattern Desk - update
cd /d "%~dp0"
set "REPO=https://github.com/yanashah16/Breakout_Strategy.git"
set "BRANCH=main"

where git >nul 2>nul || (echo X Git is not installed on this machine. & goto end)

if not exist ".git" (
  echo First update on this machine: linking this folder to GitHub...
  git init -q || goto fail
  git remote add origin "%REPO%" || goto fail
)

echo Checking GitHub for updates...
git fetch -q origin %BRANCH% || goto fail

set "OLD="
for /f %%i in ('git rev-parse -q --verify HEAD 2^>nul') do set "OLD=%%i"
for /f %%i in ('git rev-parse origin/%BRANCH%') do set "NEW=%%i"
if "%OLD%"=="%NEW%" (
  echo Already up to date.
  goto deps
)

rem ---- back up the code before replacing it
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH-mm-ss"') do set "STAMP=%%i"
set "BK=.update_backups\%STAMP%"
echo Backing up the current code to %BK% ...
robocopy . "%BK%" /E /XD .git .update_backups state logs data __pycache__ .pytest_cache /XF *.log *.log.* /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto fail

rem ---- code = exactly the GitHub version (ignored files such as settings/state stay)
git reset -q --hard origin/%BRANCH% || goto fail
git branch -q -M %BRANCH% >nul 2>nul
git branch -q -u origin/%BRANCH% >nul 2>nul
if defined OLD (echo Updated %OLD:~0,7% -^> %NEW:~0,7%.) else (echo Code set to %NEW:~0,7%.)

:deps
rem ---- make sure every required package is installed (fast when nothing is missing)
echo Checking Python packages...
python -m pip install -q -r requirements.txt || echo ! Could not install the packages - run:  python -m pip install -r requirements.txt
if not exist "config\settings.yaml" echo ! config\settings.yaml is missing - copy config\settings.example.yaml to it and fill it in.
echo.
echo Done. Close the Pattern Desk window and start it again (start.bat) to use the new code.
goto end

:fail
echo.
echo X The update did not finish. Nothing in config\, state\ or logs\ was changed.

:end
echo.
pause
