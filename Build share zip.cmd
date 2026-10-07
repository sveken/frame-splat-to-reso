@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" build_share.py
) else (
    py -3.12 build_share.py
)
echo.
pause
