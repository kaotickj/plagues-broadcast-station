@echo off
setlocal
set "PBS_ROOT=%~dp0"
if exist "%PBS_ROOT%PlaguesBroadcastStation.exe" (
  set "PBS_EXE=%PBS_ROOT%PlaguesBroadcastStation.exe"
  goto packaged
)
if exist "%PBS_ROOT%dist\PlaguesBroadcastStation\PlaguesBroadcastStation.exe" (
  set "PBS_EXE=%PBS_ROOT%dist\PlaguesBroadcastStation\PlaguesBroadcastStation.exe"
  goto packaged
)
if not exist "%PBS_ROOT%.venv\Scripts\python.exe" (
  echo Build with build-windows.ps1, or create .venv and install this project first.
  exit /b 1
)
"%PBS_ROOT%.venv\Scripts\python.exe" "%PBS_ROOT%launch.py" %*
exit /b %ERRORLEVEL%
:packaged
"%PBS_EXE%" %*
exit /b %ERRORLEVEL%
