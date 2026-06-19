#!/bin/bash
# Cron-friendly runner for the freshness monitor. Sources .env.backfill (DB +
# Telegram creds) and runs one check. Schedule it, e.g.:
#   */30 * * * * /full/path/to/scripts/run_monitor.sh >> /tmp/segal_monitor.log 2>&1
cd "$(cd "$(dirname "$0")/.." && pwd)" || exit 1
set -a; source .env.backfill; set +a
export PYTHONPATH="$(pwd)"
VENV=$(ls -d "$HOME"/Library/Caches/pypoetry/virtualenvs/segal-case-tracker-*-py3.11/bin/python 2>/dev/null | head -1)
"$VENV" scripts/freshness_monitor.py
