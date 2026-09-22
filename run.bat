@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe idx_download.py --days 5
if errorlevel 1 echo IDX direct download unavailable - using existing files in downloads\
.venv\Scripts\python.exe idx_summary.py
.venv\Scripts\python.exe pipeline.py
if errorlevel 1 (
  echo pipeline.py failed - skipping news.
  pause
  exit /b 1
)
.venv\Scripts\python.exe news.py
pause
