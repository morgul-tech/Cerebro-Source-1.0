@echo off
rem Graceful stop (writes the STOP file the server polls). Usage: stop.cmd C:\path\to\config.json
py -3 -m cerebrobase stop --config "%~1"
