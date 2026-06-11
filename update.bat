@echo off
REM Updates the Donor Maps API on this PC from the git repo and restarts the service.
REM Must be run from inside a git clone of the repo, as Administrator
REM (service stop/start requires elevation).
REM One-time setup: install Git for Windows, then clone the repo into the service folder:
REM   git clone https://github.com/AlteonSolutions/DonorHeatMaps.git C:\DonorMaps

cd /d %~dp0

echo Stopping service...
net stop DonorMapsAPI

echo Pulling latest code...
git pull
if errorlevel 1 (
    echo Git pull failed - check network/credentials.
    net start DonorMapsAPI
    exit /b 1
)

echo Installing dependencies...
python -m pip install -r requirements.txt --quiet

echo Starting service...
net start DonorMapsAPI

echo Done.
