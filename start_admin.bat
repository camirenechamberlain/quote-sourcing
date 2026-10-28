@echo off
rem Starts the Quoteschmote admin app on this PC (http://localhost:8501). Leave this window open while you use it.
cd /d "%~dp0"
echo Quoteschmote admin starting on http://localhost:8501  (close this window to stop it)
echo When it says "You can now view your Streamlit app", open http://localhost:8501 in your browser.
".venv\Scripts\streamlit.exe" run app.py --server.port 8501 --server.headless true --browser.gatherUsageStats false
pause
