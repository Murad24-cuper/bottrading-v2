#!/bin/sh
# Linux server: pauza (pause_bot.bat ile eyni mena).
# Yeni emr acilmir, bot aciq pozisiyalari idare etmeye davam edir.
# Davam etdirmek ucun: ./start_bot.sh
set -eu
cd "$(dirname "$0")"
: > STOP
echo "[OK] Pauza aktivdir: yeni emr acilmayacaq, aciq pozisiyalar idare olunur."
