@echo off
rem Dupcheck launcher - starts the duplicate image finder GUI.
rem Ensure dependencies are installed first:  pip install -r requirements.txt

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on your PATH.
    echo Install Python from https://www.python.org/downloads/ and try again.
    pause
    exit /b 1
)

python "%~dp0find_duplicates.py"
if errorlevel 1 (
    echo.
    echo The app exited with an error. Check output above.
    pause
)