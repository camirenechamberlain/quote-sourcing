@echo off
rem Starts the Quoteschmote website (port 8000) and the admin app (port 8501), each in its own window.
rem Leave BOTH windows open while you are testing. Closing a window stops that part.
cd /d "%~dp0"
start "Quoteschmote site" cmd /k "start_site.bat"
start "Quoteschmote admin" cmd /k "start_admin.bat"
echo Started. Site: http://localhost:8000   Admin: http://localhost:8501
timeout /t 5 >nul
