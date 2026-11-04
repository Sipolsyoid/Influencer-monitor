#!/usr/bin/env bash
# One-time setup for macOS / Linux:  bash setup.sh
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -r requirements.txt
python -m playwright install chromium
[ -f .env ] || cp .env.example .env
echo "Fill in your email settings in .env, then press Enter."
${EDITOR:-nano} .env
python monitor.py --test-email || true
python monitor.py --login
RUN_TIME=$(grep -E '^RUN_TIME=' .env | cut -d= -f2); RUN_TIME=${RUN_TIME:-13:00}
H=${RUN_TIME%%:*}; M=${RUN_TIME##*:}
DIR=$(pwd)
mkdir -p logs
LINE="$((10#$M)) $((10#$H)) * * * cd \"$DIR\" && .venv/bin/python monitor.py >> logs/monitor.log 2>&1"
( crontab -l 2>/dev/null | grep -v "monitor.py" ; echo "$LINE" ) | crontab -
echo "Setup finished. Daily check scheduled at $RUN_TIME (crontab -l to see it)."
