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
#   DETAIL_PENDING_DOCS_FIRST, DETAIL_BATCH, SYNC_INTERVAL, DOWNLOAD_PDFS.
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
# DETAIL_PENDING_DOCS_FIRST is read straight from the environment by pydantic.

exec "$VENV" -m app.workers.sync_scheduler
