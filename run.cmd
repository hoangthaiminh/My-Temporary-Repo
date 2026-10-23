@echo off
py -3 "%~dp0screen_app_inspect.py" %*
if errorlevel 1 pause
