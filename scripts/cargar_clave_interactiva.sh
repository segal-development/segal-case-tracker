#!/usr/bin/env bash
# Cargar 2ª claves PJUD de forma INTERACTIVA y segura.
#
# Te pregunta el RUT y la clave uno por uno. La clave NO se ve mientras la
# tipeás, se encripta al instante y se guarda. Sin archivos, sin texto plano en
# disco, sin pasar por el chat.
#
# Corré esto en la app Terminal de la Mac (no en el chat):
#   cd /Users/marcelo/Projects/segal-case-tracker
#   bash scripts/cargar_clave_interactiva.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# Proxy a Cloud SQL (si no está ya corriendo, p. ej. por el worker).
if ! pgrep -f 'cloud-sql-proxy.*casetracker' >/dev/null; then
  echo "Levantando cloud-sql-proxy…"
  nohup cloud-sql-proxy grupo-segal:us-central1:casetracker-segal-qa-db --port 5433 >/tmp/csqlproxy.log 2>&1 &
  sleep 5
fi

# Entorno: credenciales de la DB desde .env.backfill (nunca impresas), DB por el proxy.
set -a; source .env.backfill; set +a
export DATABASE_URL="$(python3 -c "import os,re;u=os.environ['DATABASE_URL'];u=re.sub(r'@[^/?]+','@127.0.0.1:5433',u,1);u=re.sub(r'[?&]sslmode=[^&]*','',u);print(u+('&' if '?' in u else '?')+'sslmode=disable')")"
export PYTHONPATH="$(pwd)"
VENV=$(ls -d "$HOME"/Library/Caches/pypoetry/virtualenvs/segal-case-tracker-*-py3.11/bin/python | head -1)

"$VENV" scripts/cargar_clave_pjud_interactivo.py
