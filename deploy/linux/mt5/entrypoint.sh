#!/bin/sh
set -eu
mkdir -p /opt/tradingbot/data /opt/tradingbot/logs
export WINEPREFIX WINEARCH WINEDEBUG DISPLAY
Xvfb "$DISPLAY" -ac -screen 0 1280x800x24 -nolisten tcp >/opt/tradingbot/logs/xvfb.log 2>&1 &
XVFB_PID=$!
fluxbox >/opt/tradingbot/logs/window-manager.log 2>&1 &
WM_PID=$!
x11vnc -display "$DISPLAY" -auth /dev/null -forever -shared -nopw -rfbport 5900 >/opt/tradingbot/logs/vnc.log 2>&1 &
VNC_PID=$!
cleanup() { kill "$VNC_PID" "$WM_PID" "$XVFB_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
sleep 2
PY='C:\Program Files\Python312\python.exe'
if [ ! -f '/opt/wine/drive_c/Program Files/Python312/python.exe' ]; then
  wineboot --init >>/opt/tradingbot/logs/wine.log 2>&1 || echo 'Wine initialization has a pending GUI or runtime prompt; waiting for Wine readiness.' >>/opt/tradingbot/logs/wine.log
  until wine cmd /c echo WINE_READY >/dev/null 2>&1; do sleep 5; done
  winetricks -q vcrun2022 >>/opt/tradingbot/logs/wine.log 2>&1 || echo 'VC runtime bootstrap failed; continuing to the Python package import check.' >>/opt/tradingbot/logs/wine.log
  wine /opt/python.exe /quiet InstallAllUsers=1 PrependPath=1 Include_test=0 >>/opt/tradingbot/logs/wine.log 2>&1
  wine "$PY" -m pip install --upgrade pip >>/opt/tradingbot/logs/wine.log 2>&1
fi
# Windows ile eyni paket versiyalari: hemise requirements.txt-den qurulur.
# Fayl deyisibse (git pull), paketler yeniden qurulur.
REQ_STAMP=/opt/wine/.requirements.sha256
REQ_SUM=$(sha256sum /opt/tradingbot/requirements.txt | cut -d' ' -f1)
if [ ! -f "$REQ_STAMP" ] || [ "$(cat "$REQ_STAMP")" != "$REQ_SUM" ]; then
  wine "$PY" -m pip install -r 'Z:\opt\tradingbot\requirements.txt' >>/opt/tradingbot/logs/wine.log 2>&1 && echo "$REQ_SUM" > "$REQ_STAMP"
fi
terminal=$(find /opt/wine/drive_c -type f -iname terminal64.exe -print -quit)
if [ -z "$terminal" ]; then
  firefox-esr https://www.xm.com/mt5 >/opt/tradingbot/logs/installer-browser.log 2>&1 &
  while [ -z "$terminal" ]; do
    sleep 10
    terminal=$(find /opt/wine/drive_c -type f -iname terminal64.exe -print -quit)
  done
fi
wine "$terminal" /portable >/opt/tradingbot/logs/mt5.log 2>&1 &
MT5_PID=$!
until wine "$PY" -c 'import MetaTrader5' >/dev/null 2>&1; do sleep 5; done
# Windows ile eyni mena:
#   bos STOP      = pauza: bot isleyir, yeni emr acmir, aciq pozisiyalari idare edir
#   STOP + HALT   = tam dayanma: bot baslamir
#   supervisor.stop = tam dayanma
halted() {
  [ -e /opt/tradingbot/data/supervisor.stop ] && return 0
  for f in /opt/tradingbot/STOP /opt/tradingbot/STOP.txt; do
    [ -e "$f" ] && grep -qiw HALT "$f" && return 0
  done
  return 1
}
while kill -0 "$MT5_PID" 2>/dev/null; do
  if halted; then sleep 3; continue; fi
  wine "$PY" scripts/run_forever.py >>/opt/tradingbot/logs/bot_stdout.log 2>&1 || true
  sleep 3
done
echo 'MT5 process exited; container will restart and reinitialize it.' >&2
exit 1
