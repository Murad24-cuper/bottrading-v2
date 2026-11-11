#!/bin/sh
set -eu
test -f /opt/tradingbot/data/bot_status.json || true
pgrep -f 'Xvfb' >/dev/null
pgrep -f 'x11vnc' >/dev/null
[ -e /opt/tradingbot/data/supervisor.stop ] && exit 0
for f in /opt/tradingbot/STOP /opt/tradingbot/STOP.txt; do
  if [ -e "$f" ] && grep -qiw HALT "$f"; then exit 0; fi
done
pgrep -f 'terminal64.exe|terminal64' >/dev/null
pgrep -f 'run_forever.py' >/dev/null
