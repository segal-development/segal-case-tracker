#!/usr/bin/env bash
# Carga 2ª claves PJUD desde un archivo local hacia Cloud SQL (QA), encriptadas.
#
# Uso:
#   bash scripts/cargar_claves.sh /tmp/claves.txt
#
# El archivo /tmp/claves.txt tiene una línea por abogado:  rut=clave
#   19456852-5=ClaveDePablo
#   17814818-4=ClaveDeMelissa
#
# Este script levanta el proxy a Cloud SQL, arma el entorno y llama a
# load_pjud_passwords.py (que encripta y NUNCA loguea la clave). Las claves
# nunca pasan por el chat — solo viven en tu archivo local, que borrás después.
set -euo pipefail

CLAVES="${1:?Uso: bash scripts/cargar_claves.sh /ruta/al/archivo_de_claves.txt}"
[ -f "$CLAVES" ] || { echo "No existe el archivo: $CLAVES"; exit 1; }

cd "$(dirname "$0")/.."

# 1) proxy a Cloud SQL (si no está corriendo)
if ! pgrep -f 'cloud-sql-proxy.*casetracker' >/dev/null; then
  echo "Levantando cloud-sql-proxy…"
  nohup cloud-sql-proxy grupo-segal:us-central1:casetracker-segal-qa-db --port 5433 >/tmp/csqlproxy.log 2>&1 &
  sleep 5
fi

# 2) entorno (credenciales de la DB desde .env.backfill, nunca impresas)
set -a; source .env.backfill; set +a
export DATABASE_URL="$(python3 -c "import os,re;u=os.environ['DATABASE_URL'];u=re.sub(r'@[^/?]+','@127.0.0.1:5433',u,1);u=re.sub(r'[?&]sslmode=[^&]*','',u);print(u+('&' if '?' in u else '?')+'sslmode=disable')")"
export PYTHONPATH="$(pwd)"
VENV=$(ls -d "$HOME"/Library/Caches/pypoetry/virtualenvs/segal-case-tracker-*-py3.11/bin/python | head -1)

# 3) cargar (encripta y guarda en lawyers.encrypted_pjud_password)
CLAVES_FILE="$CLAVES" "$VENV" scripts/load_pjud_passwords.py

echo ""
echo "⚠ Cuando termines, BORRÁ el archivo:  shred -u $CLAVES"
