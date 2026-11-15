@echo off
rem Keeps the email worker running; restarts it 15 seconds after it stops.
rem Started hidden at logon by the "EmailAutomation-worker" scheduled task.
cd /d "%~dp0.."
if not exist logs mkdir logs
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
:loop
echo [%date% %time%] starting worker>> logs\worker.log
".venv\Scripts\python.exe" manage.py run_worker >> logs\worker.log 2>&1
echo [%date% %time%] worker stopped (exit %errorlevel%), restarting in 15 seconds>> logs\worker.log
ping -n 16 127.0.0.1 > nul
goto loop
