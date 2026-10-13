@echo off
cd /d "%~dp0"
uv run garmin-dashboard %*
pause
