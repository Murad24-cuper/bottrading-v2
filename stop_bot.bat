@echo off
setlocal
cd /d "%~dp0"
echo [INFO] Bot dayandirilir...

:: HALT = tam dayanma. start_bot.bat bu fayli avtomatik silmir (yalniz "start_bot.bat force").
echo HALT> STOP

:: supervisor.stop fayli: supervisor yeniden baslatmasin
if exist ".venv\Scripts\python.exe" (
  .venv\Scripts\python.exe -c "import sys; from pathlib import Path; sys.path.insert(0, str(Path.cwd())); from src.paths import project_paths; project_paths(Path.cwd())[0].joinpath('supervisor.stop').write_text('stop_bot.bat invoked\n', encoding='utf-8')"
)

:: Evvelce botun ozu temiz dayanmasi ucun 30 saniye gozleyirik (mt5.shutdown, lock release).
:: Yalniz bundan sonra qalan prosesler mecburi dayandirilir.
echo [INFO] Botun temiz dayanmasi gozlenilir (30 saniyeye qeder)...
powershell -NoProfile -Command "$p='scripts\\run_bot.py|scripts\\run_forever.py'; for($i=0;$i -lt 30;$i++){ if(-not (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match $p })){ exit 0 }; Start-Sleep -Seconds 1 }; Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match $p } | ForEach-Object { Write-Output ('[WARN] Mecburi dayandirilir: PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo [OK] Bot ve supervisor dayandirildi.
echo MT5 terminali aciq qalibsa ve istemirsinizse, onu manual baglaya bilersiniz.
powershell -NoProfile -Command "Start-Sleep -Seconds 2"
exit /b 0
