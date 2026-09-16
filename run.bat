@echo off
setlocal EnableDelayedExpansion
set "HERE=%~dp0"
cd /d "%HERE%"
set "VENV=%HERE%.venv"
set "VPY=%VENV%\Scripts\python.exe"

if not exist "%VPY%" (
  echo First run - setting up.
  set "PY="
  for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%ensure_python.ps1"`) do set "PY=%%P"
  if not defined PY (
    echo Could not find or install Python. Install it from python.org and re-run.
    pause & exit /b 1
  )
  "!PY!" -m venv "%VENV%"
)
"%VPY%" "%HERE%harvester_app.py" %*
if errorlevel 1 pause
