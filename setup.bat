@echo off
REM One-time setup for Windows. Double-click this file.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed. Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^), then run this again.
  pause
  exit /b 1
)

echo [1/5] Creating virtual environment...
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat

echo [2/5] Installing packages...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt || goto :fail
python -m playwright install chromium || goto :fail

echo [3/5] Email settings...
if not exist .env copy .env.example .env >nul
echo Fill in your email settings in the file that opens, save it, then close Notepad.
notepad .env
python monitor.py --test-email

echo [4/5] Instagram login...
python monitor.py --login

echo [5/5] Scheduling the daily check...
call schedule_windows.bat

echo.
echo Setup finished. To run a check right now: double-click run.bat
pause
exit /b 0

:fail
echo Something went wrong during installation. Check the messages above.
pause
exit /b 1
