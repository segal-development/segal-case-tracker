#!/bin/bash
# Supervised continuous freshness re-sync for PJUD cases (headful, residential).
# Persistent (NOT in /tmp, so it survives reboots). Auto-finds the project venv
# and sources .env.backfill (DB + CU creds). Restarts the worker on any crash.
#
# Launch (keeps the Mac awake while it runs):
#   nohup caffeinate -dimsu scripts/run_freshness.sh > /tmp/freshness.log 2>&1 &
#
# Stop (e.g. before shutting down):
#   pkill -f run_freshness.sh; pkill -f freshness_sync.py; pkill -f caffeinate; pkill -f ms-playwright/chromium

cd "$(cd "$(dirname "$0")/.." && pwd)" || exit 1
set -a; source .env.backfill; set +a
export PYTHONPATH="$(pwd)"

VENV=$(ls -d "$HOME"/Library/Caches/pypoetry/virtualenvs/segal-case-tracker-*-py3.11/bin/python 2>/dev/null | head -1)
if [ -z "$VENV" ]; then
  echo "ERROR: project venv not found under ~/Library/Caches/pypoetry/virtualenvs/"
  exit 1
fi

n=0
while true; do
  n=$((n + 1))
  echo "=== [$(date '+%F %T')] freshness worker start (attempt $n) ==="
  "$VENV" scripts/freshness_sync.py
  echo "=== [$(date '+%F %T')] freshness worker exited (code $?); restarting in 20s ==="
  sleep 20
done
