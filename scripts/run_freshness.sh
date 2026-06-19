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

# Singleton: only one supervisor at a time — two would compete for the single
# allowed PJUD session. Portable lock (macOS + Linux) with stale-PID takeover
# (survives a prior kill -9 that skipped the trap).
LOCKDIR="${TMPDIR:-/tmp}/segal_freshness.lock.d"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  OLDPID=$(cat "$LOCKDIR/pid" 2>/dev/null)
  if [ -n "$OLDPID" ] && kill -0 "$OLDPID" 2>/dev/null; then
    echo "run_freshness.sh already running (PID $OLDPID) — exiting."
    exit 0
  fi
  echo "removing stale lock (PID ${OLDPID:-unknown} not alive)"
fi
echo "$$" > "$LOCKDIR/pid"
trap 'rm -f "$LOCKDIR/pid"; rmdir "$LOCKDIR" 2>/dev/null' EXIT

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
