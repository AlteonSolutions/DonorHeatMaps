@echo off
REM Updates the Donor Maps API on this PC from the git repo and restarts the service.
REM One-time setup: install Git for Windows, then clone the repo into the service folder:
REM   git clone https://github.com/AlteonSolutions/DonorHeatMaps.git C:\DonorMaps
REM Run this script (or schedule it in Task Scheduler) whenever the repo changes.

cd /d %~dp0

echo Pulling latest code...
git pull
if errorlevel 1 (
    echo Git pull failed - check network/credentials.
    exit /b 1
)

echo Installing dependencies...
python -m pip install -r requirements.txt --quiet

echo Restarting service...
nssm restart DonorMapsAPI

echo Done.
