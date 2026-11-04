@echo off
REM Runs one check. The daily schedule calls this file.
cd /d "%~dp0"
if not exist logs mkdir logs
.venv\Scripts\python.exe monitor.py >> logs\monitor.log 2>&1
