@echo off
setlocal
cd /d "%~dp0"
:: Pauza: yeni emr acilmir, bot aciq pozisiyalari idare etmeye davam edir.
:: Davam etdirmek ucun: start_bot.bat (bos STOP faylini silir).
type nul > STOP
echo [OK] Pauza aktivdir: yeni emr acilmayacaq, aciq pozisiyalar idare olunur.
exit /b 0
