@echo off
rem Local DEV/STAGING preview on Windows. Usage: run.cmd C:\path\to\config.json   (from the release directory)
py -3 -m cerebrobase preflight --config "%~1" --phase pre-start || exit /b %errorlevel%
py -3 -m cerebrobase serve --config "%~1"
