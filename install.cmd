@echo off
rem Install or update IEUStudia (double-click). See scripts\install.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
pause
