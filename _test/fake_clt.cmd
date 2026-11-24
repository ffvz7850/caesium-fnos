@echo off
python "%~dp0fake_clt.py" %*
exit /b %ERRORLEVEL%
