@echo off
cd /d "%~dp0"
py -3 -m pip check
if errorlevel 1 goto failed
py -3 -m py_compile screen_app_inspect.py inspect_support.py inspect_uia.py
if errorlevel 1 goto failed
py -3 -m unittest discover -s tests -v
if errorlevel 1 goto failed
echo Verification passed.
pause
exit /b 0
:failed
echo Verification failed. Read the errors above.
pause
exit /b 1
