@echo off
rem Starts the Quoteschmote website on this PC (port 8000). Leave this window open while the site should be live.
cd /d "%~dp0"
echo Quoteschmote site starting on http://127.0.0.1:8000  (close this window to stop it)
".venv\Scripts\python.exe" -m uvicorn server.main:app --host 127.0.0.1 --port 8000
pause
