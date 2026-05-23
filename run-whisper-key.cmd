@echo off
set PYTHONUTF8=1
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -m whisper_key.main
echo.
echo Whisper Key exited. Press any key to close this window.
pause >nul
