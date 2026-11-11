@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. Create it and install requirements.txt first.
  exit /b 1
)

:: "start_bot.bat force" HALT faylini da silib botu baslatir (yalniz el ile istifade edin)
set "FORCE=0"
if /i "%~1"=="force" set "FORCE=1"

:: HALT yazilmis STOP fayli bilerekden qoyulmus tam dayanma emridir.
:: Avtomatik baslatma (Task Scheduler) onu silmemelidir.
set "HALTED=0"
if exist "STOP" findstr /i /r "\<HALT\>" "STOP" >nul 2>&1 && set "HALTED=1"
if exist "STOP.txt" findstr /i /r "\<HALT\>" "STOP.txt" >nul 2>&1 && set "HALTED=1"
if "%HALTED%"=="1" if "%FORCE%"=="0" (
  echo [STOP] STOP faylinda HALT yazilib: bot bilerekden dayandirilib ve baslamayacaq.
  echo        Yeniden baslatmaq ucun: start_bot.bat force
  exit /b 2
)

:: Pauza (bos STOP) ve ya force rejiminde kohne stop fayllarini temizleyirik
if exist "STOP" del /f /q "STOP"
if exist "STOP.txt" del /f /q "STOP.txt"
.venv\Scripts\python.exe -c "import sys; from pathlib import Path; sys.path.insert(0, str(Path.cwd())); from src.paths import project_paths; project_paths(Path.cwd())[0].joinpath('supervisor.stop').unlink(missing_ok=True)"
if errorlevel 1 exit /b %ERRORLEVEL%

echo [INFO] Bot supervisor basladilir...
start "" /b .venv\Scripts\pythonw.exe scripts\run_forever.py
echo [OK] Bot fonda basladildi. Dayandirmaq ucun stop_bot.bat, pauza ucun pause_bot.bat istifade edin.
exit /b 0
