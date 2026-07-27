#!/usr/bin/env bash
# start-sync-mac.sh — TEMPORARY sync station on a Mac (venv + Cloud SQL Auth Proxy).
#
# Why not Docker? docker-compose.qa.yml targets the VM: it connects to Cloud SQL
# by its PUBLIC IP (the VM's static IP is whitelisted, no proxy). A Mac's IP isn't
# whitelisted and the container browser is unproven from macOS — so here we run the
# worker via the Poetry venv + the Cloud SQL Auth Proxy, using the Mac's own
# (residential) IP for the PJUD logins (the whole point — GCP datacenter IPs are
# Shape-blocked). This is the path validated in the spikes (synced 305 causas).
#
# Re-run this after a reboot. Usage:
#   chmod +x scripts/start-sync-mac.sh   (once)
#   ./scripts/start-sync-mac.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "🔎 Verificando requisitos…"
[ -f .env.qa ]     || { echo "  ❌ Falta .env.qa (copialo del VM: /home/marcelo/casetracker/.env.qa)"; exit 1; }
[ -f sa-key.json ] || { echo "  ❌ Falta sa-key.json (copialo del VM: /home/marcelo/casetracker/sa-key.json)"; exit 1; }
command -v cloud-sql-proxy >/dev/null || { echo "  ❌ Falta cloud-sql-proxy (brew install cloud-sql-proxy)"; exit 1; }
VENV=$(ls -d "$HOME"/Library/Caches/pypoetry/virtualenvs/segal-case-tracker-*-py3.11/bin/python 2>/dev/null | head -1)
[ -n "${VENV:-}" ] || { echo "  ❌ No encuentro el venv de poetry (corré 'poetry install')"; exit 1; }
echo "  ✅ .env.qa, sa-key.json, cloud-sql-proxy, venv"

# --- Cloud SQL Auth Proxy (DB en 127.0.0.1:5433) ---
if ! pgrep -f 'cloud-sql-proxy.*casetracker' >/dev/null; then
  echo "🔌 Arrancando cloud-sql-proxy…"
  nohup cloud-sql-proxy grupo-segal:us-central1:casetracker-segal-qa-db --port 5433 >/tmp/csqlproxy.log 2>&1 &
  sleep 6
fi
pgrep -f 'cloud-sql-proxy.*casetracker' >/dev/null && echo "  ✅ proxy (5433)" || { echo "  ❌ el proxy no arrancó (ver /tmp/csqlproxy.log)"; exit 1; }

# --- Redis (session store) — vía Docker si está; si no, el worker cae en fallback ---
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  docker start segal-redis >/dev/null 2>&1 || docker run -d --name segal-redis -p 6379:6379 redis:7-alpine >/dev/null 2>&1 || true
  echo "  ✅ redis (localhost:6379)"
else
  echo "  ⚠️  Docker apagado → sin redis; el worker usa fallback de sesión (anda igual)."
fi

# --- Worker (venv) — override DB→proxy, redis→local, credenciales→sa-key.json local ---
set -a; source .env.qa; set +a
export DATABASE_URL="$(python3 -c "import os,re;u=os.environ['DATABASE_URL'];u=re.sub(r'@[^/?]+','@127.0.0.1:5433',u,1);u=re.sub(r'[?&]sslmode=[^&]*','',u);print(u+'?sslmode=disable')")"
export REDIS_URL="redis://localhost:6379/0"
export GOOGLE_APPLICATION_CREDENTIALS="$(pwd)/sa-key.json"
export ENABLE_SCHEDULER=true
export PYTHONPATH="$(pwd)"
# Fast detail backfill: `DOWNLOAD_PDFS=false ./scripts/start-sync-mac.sh` runs the
# worker WITHOUT downloading PDFs (docs are the per-case bottleneck), so it powers
# through case detail much faster — while its 4h list-sync keeps live monitoring
# and deadline alerts running. PDFs get a dedicated pass later. Omit the var to
# keep .env.qa's default (PDFs on).
export DOC_DOWNLOAD_ENABLED="${DOWNLOAD_PDFS:-${DOC_DOWNLOAD_ENABLED:-true}}"
# Backfill tuning (optional): a bigger per-cycle detail batch + shorter interval
# makes the worker detail a large chunk near-continuously each cycle (amortizing
# the list-sync), so coverage catches up fast WITHOUT pausing live monitoring.
# Pass e.g. DETAIL_BATCH=200 SYNC_INTERVAL=1. Separate override names because
# .env.qa (sourced above) already sets SYNC_INTERVAL_HOURS — these must win over it.
export DETAIL_BATCH_SIZE="${DETAIL_BATCH:-${DETAIL_BATCH_SIZE:-30}}"
export SYNC_INTERVAL_HOURS="${SYNC_INTERVAL:-${SYNC_INTERVAL_HOURS:-4}}"

if pgrep -f 'app.workers.sync_scheduler' >/dev/null; then
  echo "  ⚠️  El worker YA está corriendo. Para reiniciarlo: pkill -f app.workers.sync_scheduler && ./scripts/start-sync-mac.sh"
  exit 0
fi
echo "🚀 Arrancando el worker de sync…"
nohup "$VENV" -m app.workers.sync_scheduler >/tmp/segal-worker.log 2>&1 &
sleep 4
if pgrep -f 'app.workers.sync_scheduler' >/dev/null; then
  echo "  ✅ Worker ARRIBA."
else
  echo "  ❌ El worker no arrancó — revisá /tmp/segal-worker.log"; exit 1
fi

echo ""
echo "✅ ESTACIÓN DE SYNC (Mac) ARRIBA — primer ciclo en ~5 min, luego cada 4h."
echo "   Logs:   tail -f /tmp/segal-worker.log"
echo "   Frenar: pkill -f app.workers.sync_scheduler"
echo "   💡 Si reiniciás la Mac, volvé a correr: ./scripts/start-sync-mac.sh"
