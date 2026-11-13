@echo off
setlocal
"%~dp0..\.venv-dev\Scripts\python.exe" -X utf8 -m lab_control.instruments.devices.joinwit_jw8105.ssh_read %*
exit /b %errorlevel%
