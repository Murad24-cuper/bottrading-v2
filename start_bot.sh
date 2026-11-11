#!/bin/sh
# Linux server: botu Docker konteynerinde baslatir (start_bot.bat ile eyni mena).
#   ./start_bot.sh        pauzani (bos STOP) goturur ve baslayir; HALT varsa baslamir
#   ./start_bot.sh force  HALT faylini da silib baslayir (yalniz el ile)
set -eu
cd "$(dirname "$0")"
COMPOSE="sudo docker compose -f deploy/linux/compose.yaml"
halted=0
for f in STOP STOP.txt; do
  if [ -e "$f" ] && grep -qiw HALT "$f"; then halted=1; fi
done
if [ "$halted" = 1 ] && [ "${1:-}" != "force" ]; then
  echo "[STOP] STOP faylinda HALT yazilib: bot bilerekden dayandirilib ve baslamayacaq."
  echo "       Yeniden baslatmaq ucun: ./start_bot.sh force"
  exit 2
fi
rm -f STOP STOP.txt data/supervisor.stop
$COMPOSE up -d
echo "[OK] Bot baslandi. Dayandirmaq ucun ./stop_bot.sh, pauza ucun ./pause_bot.sh"
