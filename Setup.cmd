@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" setup.py
    goto done
)
py -3.12 -c "import sys; assert sys.maxsize > 2**32" >nul 2>&1
if not errorlevel 1 (
    py -3.12 setup.py
    goto done
)
python -c "import sys; assert sys.version_info[:2] == (3,12) and sys.maxsize > 2**32" >nul 2>&1
if not errorlevel 1 (
    python setup.py
    goto done
)
echo Install Python 3.12 for Windows, 64-bit, with Tcl/Tk and the Python launcher.
echo Download: https://www.python.org/downloads/release/python-31210/
:done
echo.
pause
