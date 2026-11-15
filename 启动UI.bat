@echo off
rem Dual Color Wave - double click to open the UI in your browser.
rem Keep this window open while using the UI; close it (or Ctrl+C) to stop.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python was not found in PATH.
  echo Install Python 3.10+ first, then run:
  echo   python -m pip install -r requirements.txt
  pause
  exit /b 1
)
echo Starting Dual Color Wave UI at http://127.0.0.1:8765/
echo The browser should open automatically. If not, open the address above.
python wave_gui.py --port 8765
pause
