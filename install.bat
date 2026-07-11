@echo off
echo.
echo  Elite Dangerous Expedition Tracker -- Setup
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo  Python was not found on your PATH.
    echo  Download it from https://python.org and ensure
    echo  "Add Python to PATH" is ticked during install.
    echo.
    pause
    exit /b 1
)

python install.py
if errorlevel 1 (
    echo.
    echo  Setup encountered an error. See above for details.
    echo.
    pause
    exit /b 1
)

pause
