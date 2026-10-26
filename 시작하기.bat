@echo off
setlocal
cd /d "%~dp0"
set "RUNTIME_ENV=%LOCALAPPDATA%\JindoWidgetDevEnv"
if not exist "%RUNTIME_ENV%\Scripts\python.exe" (
  echo Preparing a private Python environment...
  python -m venv "%RUNTIME_ENV%"
  if errorlevel 1 goto setup_failed
)

"%RUNTIME_ENV%\Scripts\python.exe" -c "import PIL" >nul 2>&1
if errorlevel 1 (
  echo Installing runtime packages. Internet is needed the first time.
  "%RUNTIME_ENV%\Scripts\python.exe" -m pip install -r requirements-runtime.txt
  if errorlevel 1 goto setup_failed
)

"%RUNTIME_ENV%\Scripts\python.exe" app.py
if errorlevel 1 goto run_failed
exit /b 0

:setup_failed
echo Setup failed. Check Python 3 with Tcl/Tk and your internet connection.
pause
exit /b 1

:run_failed
echo The widget could not start. Check the error shown above.
pause
exit /b 1
