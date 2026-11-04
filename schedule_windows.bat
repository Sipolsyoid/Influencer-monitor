@echo off
REM Creates (or updates) a daily Windows Task Scheduler task that runs run.bat.
cd /d "%~dp0"
set RUN_TIME=13:00
if exist .env for /f "tokens=1,* delims==" %%a in ('findstr /b "RUN_TIME=" .env') do set RUN_TIME=%%b
schtasks /Create /F /SC DAILY /ST %RUN_TIME% /TN "Instagram Influencer Monitor" /TR "\"%~dp0run.bat\""
if errorlevel 1 (
  echo Could not create the scheduled task.
) else (
  echo Daily check scheduled at %RUN_TIME%. You can see it in Task Scheduler as "Instagram Influencer Monitor".
)
