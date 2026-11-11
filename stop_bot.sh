#!/bin/sh
# Linux server: botu tam dayandirir (stop_bot.bat ile eyni mena).
# Brokerdeki aciq pozisiyalar SL/TP ile qalir.
set -eu
cd "$(dirname "$0")"
COMPOSE="sudo docker compose -f deploy/linux/compose.yaml"
echo "HALT" > STOP
mkdir -p data
echo "stop_bot.sh invoked" > data/supervisor.stop
echo "[INFO] Botun temiz dayanmasi gozlenilir (30 saniyeye qeder)..."
i=0
while [ "$i" -lt 30 ] && $COMPOSE exec -T mt5bot pgrep -f run_forever.py >/dev/null 2>&1; do
  sleep 1; i=$((i+1))
done
$COMPOSE stop mt5bot dashboard
echo "[OK] Bot dayandirildi."
