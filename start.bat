@echo off
rem Pattern Desk - double-click to start. Keep this window open while trading.
title Pattern Desk
cd /d "%~dp0"
echo.
echo   Pattern Desk is starting...
echo   Open http://127.0.0.1:8002 in your browser.
echo   Keep this window open. Close it only when no trade is open.
echo.
start "" http://127.0.0.1:8002
:run
python -m desk.main
echo.
echo   Pattern Desk stopped. Restarting in 10 seconds (close this window to stop for good)...
timeout /t 10 /nobreak >nul
goto run
