@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-Windows.ps1"
if errorlevel 1 (
  echo Installation did not finish. Read the message above, then try again.
  pause
  exit /b 1
)
pause
