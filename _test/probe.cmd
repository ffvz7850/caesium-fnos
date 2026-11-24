@echo off
python "%~dp0probe.py" %*
exit /b %ERRORLEVEL%
