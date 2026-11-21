@echo off
REM Runs one jobwatch check. Schedule it with Task Scheduler (see README).
cd /d "%~dp0"
python jobwatch.py >> jobwatch.log 2>&1
