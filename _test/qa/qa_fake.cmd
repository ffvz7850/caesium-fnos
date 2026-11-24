@echo off
python "%~dp0qa_fake_clt.py" %*
exit /b %ERRORLEVEL%
