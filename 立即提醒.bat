@echo off
cd /d "%~dp0"
python remind.py --spawn
if errorlevel 1 pause
