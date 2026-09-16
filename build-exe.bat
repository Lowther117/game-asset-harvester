@echo off
setlocal EnableDelayedExpansion
title Game Asset Harvester - build
set "APP=GameAssetHarvester"
set "HERE=%~dp0"
cd /d "%HERE%"
set "LOG=%HERE%build-win-log.txt"
set "VENV=%HERE%.venv-build"
set "VPY=%VENV%\Scripts\python.exe"

> "%LOG%" echo Game Asset Harvester build started %DATE% %TIME%

call :head "1/6  Finding a usable Python"
set "PY="
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%ensure_python.ps1" 2^>^> "%LOG%"`) do set "PY=%%P"
if not defined PY goto :fail_python
echo     using !PY!
>> "%LOG%" echo using !PY!

call :head "2/6  Creating the build environment"
if exist "%VENV%" rmdir /s /q "%VENV%"
"!PY!" -m venv "%VENV%" >> "%LOG%" 2>&1
if not exist "%VPY%" goto :fail_venv

call :head "3/6  Installing PyInstaller"
"%VPY%" -m pip install --upgrade pip --only-binary :all: >> "%LOG%" 2>&1
"%VPY%" -m pip install --only-binary :all: pyinstaller >> "%LOG%" 2>&1
if errorlevel 1 goto :fail_pip

call :head "4/6  Downloading the extraction backends"
echo     (CUE4Parse.CLI, umodel, AssetStudioModCLI, Godot RE Tools, QuickBMS)
echo     Each comes from its own official source. This can take a few minutes.
"%VPY%" "%HERE%harvester_app.py" fetch-backends >> "%LOG%" 2>&1
if errorlevel 1 (
  echo     WARNING: at least one backend could not be downloaded.
  echo     The app still builds and can fetch the rest later from its Backends tab.
  >> "%LOG%" echo WARNING: fetch-backends reported problems
)

call :head "5/6  Building %APP%.exe"
rem --onedir, not --onefile: the bundled backends are hundreds of MB and onefile
rem would unpack the lot into TEMP on every single launch.
"%VPY%" -m PyInstaller --noconfirm --clean --onedir --windowed ^
  --name "%APP%" ^
  --add-data "backends;backends" ^
  --collect-submodules harvester ^
  --hidden-import tkinter ^
  --hidden-import tkinter.ttk ^
  --hidden-import tkinter.filedialog ^
  --hidden-import tkinter.messagebox ^
  "%HERE%harvester_app.py" >> "%LOG%" 2>&1
if not exist "%HERE%dist\%APP%\%APP%.exe" goto :fail_build

call :head "6/6  Self-test"
del /q "%HERE%dist\%APP%\harvester-selftest.txt" 2>nul
start /wait "" "%HERE%dist\%APP%\%APP%.exe" selftest
if exist "%HERE%dist\%APP%\harvester-selftest.txt" (
  type "%HERE%dist\%APP%\harvester-selftest.txt"
  type "%HERE%dist\%APP%\harvester-selftest.txt" >> "%LOG%"
  findstr /c:"PROBLEMS FOUND" "%HERE%dist\%APP%\harvester-selftest.txt" >nul && goto :fail_selftest
) else (
  echo     PROBLEMS FOUND: the exe did not write a self-test report.
  goto :fail_selftest
)

echo.
echo ================================================================
echo  Built OK:  dist\%APP%\%APP%.exe
echo  Copy the whole dist\%APP% folder wherever you want it.
echo ================================================================
echo.
pause
exit /b 0

:head
echo.
echo == %~1
>> "%LOG%" echo.
>> "%LOG%" echo == %~1
exit /b 0

:fail_python
echo.
echo PROBLEMS FOUND: no usable Python and one could not be installed.
goto :tail
:fail_venv
echo.
echo PROBLEMS FOUND: the build virtual environment was not created.
goto :tail
:fail_pip
echo.
echo PROBLEMS FOUND: PyInstaller would not install.
goto :tail
:fail_build
echo.
echo PROBLEMS FOUND: PyInstaller did not produce dist\%APP%\%APP%.exe
goto :tail
:fail_selftest
echo.
echo PROBLEMS FOUND: the built exe failed its self-test.
goto :tail

:tail
echo.
echo ---- last 40 lines of %LOG% ----
powershell -NoProfile -Command "Get-Content -Tail 40 '%LOG%'"
echo --------------------------------
pause
exit /b 1
