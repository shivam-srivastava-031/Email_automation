@echo off
rem Keeps the web app running on http://localhost:5000; restarts it 15 seconds after it stops
rem (or keeps retrying while another server already uses port 5000).
rem Started hidden at logon by the "EmailAutomation-web" scheduled task.
cd /d "%~dp0.."
if not exist logs mkdir logs
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
:loop
echo [%date% %time%] starting web app>> logs\web.log
".venv\Scripts\python.exe" manage.py runserver 5000 --noreload >> logs\web.log 2>&1
echo [%date% %time%] web app stopped (exit %errorlevel%), restarting in 15 seconds>> logs\web.log
ping -n 16 127.0.0.1 > nul
goto loop
