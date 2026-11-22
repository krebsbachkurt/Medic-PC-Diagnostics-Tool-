@echo off
cd /d "%~dp0"
set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PYTHON%" (
  echo Python 3.13 was not found at its expected install location.
  echo Reinstall Python 3.13 from python.org and try again.
  pause
  exit /b 1
)
"%PYTHON%" -c "import psutil, reportlab" >nul 2>&1
if errorlevel 1 (
  echo Installing dashboard packages...
  "%PYTHON%" -m pip install -r requirements.txt
  if errorlevel 1 (pause & exit /b 1)
)
set "PYTHONW=%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
if not exist "%PYTHONW%" (
  echo pythonw.exe was not found alongside Python 3.13.
  pause
  exit /b 1
)
start "" "%PYTHONW%" "%~dp0pulse_gpu_dashboard.py"

