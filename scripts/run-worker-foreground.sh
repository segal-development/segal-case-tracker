#!/usr/bin/env bash
# run-worker-foreground.sh — the sync worker as a FOREGROUND process for launchd.
#
# Why: a `nohup ... &` child started by a launchd job dies when the job's main
# process exits (launchd reaps the process group). So the old start-sync-mac.sh
# approach only survived when launched from an interactive shell. Here we set up
# the env and then `exec` the worker, so the launchd job process BECOMES the
# worker — launchd manages it directly and (with KeepAlive) restarts it on crash
# or reboot. The Cloud SQL proxy runs in its own agent (com.segal.sqlproxy).
#
# Backfill knobs come from the LaunchAgent's EnvironmentVariables (plist):
#   DETAIL_PENDING_DOCS_FIRST, DETAIL_BATCH, SYNC_INTERVAL, DOWNLOAD_PDFS,
#   ROTATION_SCOPE, DOC_MAX, DOC_RECENT.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env.qa ]     || { echo "run-worker: falta .env.qa"; exit 1; }
[ -f sa-key.json ] || { echo "run-worker: falta sa-key.json"; exit 1; }
VENV=$(ls -d "$HOME"/Library/Caches/pypoetry/virtualenvs/segal-case-tracker-*-py3.11/bin/python 2>/dev/null | head -1)
[ -n "${VENV:-}" ] || { echo "run-worker: no encuentro el venv de poetry"; exit 1; }

# Wait for the Cloud SQL proxy (its own agent) to be listening before starting,
# so we don't crash-loop on a not-yet-ready DB. Non-fatal: after ~60s proceed
# and let the worker retry / KeepAlive handle it.
for _ in $(seq 1 30); do
  if nc -z 127.0.0.1 5433 2>/dev/null; then break; fi
  sleep 2
done

set -a; source .env.qa; set +a
export DATABASE_URL="$(python3 -c "import os,re;u=os.environ['DATABASE_URL'];u=re.sub(r'@[^/?]+','@127.0.0.1:5433',u,1);u=re.sub(r'[?&]sslmode=[^&]*','',u);print(u+'?sslmode=disable')")"
export REDIS_URL="redis://localhost:6379/0"
export GOOGLE_APPLICATION_CREDENTIALS="$(pwd)/sa-key.json"
export ENABLE_SCHEDULER=true
export PYTHONPATH="$(pwd)"
export DOC_DOWNLOAD_ENABLED="${DOWNLOAD_PDFS:-${DOC_DOWNLOAD_ENABLED:-true}}"
export DETAIL_BATCH_SIZE="${DETAIL_BATCH:-${DETAIL_BATCH_SIZE:-30}}"
export SYNC_INTERVAL_HOURS="${SYNC_INTERVAL:-${SYNC_INTERVAL_HOURS:-4}}"

# The ones below are set UNCONDITIONALLY, on purpose. `source .env.qa` above runs
# with `set -a`, so it OVERWRITES anything the LaunchAgent put in the
# environment. Settings that share a name between the plist and .env.qa would
# therefore be silently decided by .env.qa, and changing the plist would do
# nothing. That is why the lines above rename (DETAIL_BATCH -> DETAIL_BATCH_SIZE)
# instead of reusing the app's own name. These follow the same rule: the
# plist's short name wins, and the fallback is the value WE want, not whatever
# .env.qa happens to carry.
#
# DETAIL_PENDING_DOCS_FIRST orders the detail rotation by "most pending PDFs
# first", i.e. it picks the most EXPENSIVE causas every cycle. It is a PDF
# backfill mode and it collapses freshness while on; default off.
export DETAIL_PENDING_DOCS_FIRST="${PENDING_DOCS_FIRST:-false}"
# Dispatch budget per sync. Alerts are ALWAYS persisted — this gates only the
# email/webhook send, so lowering it loses no data.
export NOTIFY_MAX_PER_SYNC="${NOTIFY_MAX:-25}"
# Which side of the freshness/backlog cut this station works on: all | fresh |
# backlog (see DETAIL_ROTATION_SCOPE in app/config.py). "all" = no cut. An invalid
# value makes the worker fail at startup on purpose. Turning on the freshness
# station is a deliberate, per-station decision: the plist does not set it yet.
export DETAIL_ROTATION_SCOPE="${ROTATION_SCOPE:-all}"
# Historical PDFs downloaded per causa visit (0 = no cap). PDFs tied to the
# movements dated within DOC_RECENT_DAYS are never capped. Deferred ones stay pending.
export DOC_MAX_PER_CASE="${DOC_MAX:-0}"
# A PDF is never capped when its movement is newer than this many days.
export DOC_RECENT_DAYS="${DOC_RECENT:-30}"

exec "$VENV" -m app.workers.sync_scheduler
