@echo off
setlocal
if not exist "%~dp0.venv\Scripts\pythonw.exe" goto missing
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0reader.pyw" %*
exit /b
:missing
echo The local Python environment is missing. See README.md.
pause
