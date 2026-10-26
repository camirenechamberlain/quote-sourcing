@echo off
rem Starts the Quoteschmote admin app on this PC (http://localhost:8501). Leave this window open while you use it.
cd /d "%~dp0"
echo Quoteschmote admin starting on http://localhost:8501  (close this window to stop it)
".venv\Scripts\streamlit.exe" run app.py --server.port 8501
pause
