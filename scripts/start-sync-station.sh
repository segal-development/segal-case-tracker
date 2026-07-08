#!/usr/bin/env bash
# start-sync-station.sh — arranca la ESTACIÓN DE SYNC de PJUD en esta máquina
# (Mac de oficina o PC dedicada): worker (auto-login + sync) + redis + cloud-sql-proxy.
#
# Idempotente: corrélo cuantas veces quieras. Uso:
#   chmod +x scripts/start-sync-station.sh   (una sola vez)
#   ./scripts/start-sync-station.sh
set -euo pipefail

cd "$(dirname "$0")/.."   # ir a la raíz del repo (este script vive en scripts/)

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.qa.yml"
FAIL=0

echo "🔎 Verificando requisitos de la estación de sync…"

# 1) Docker corriendo
if ! docker info >/dev/null 2>&1; then
  echo "  ❌ Docker no está corriendo → abrí Docker Desktop y reintentá."
  FAIL=1
else
  echo "  ✅ Docker corriendo."
fi

# 2) .env.qa (config + credenciales)
if [ ! -f .env.qa ]; then
  echo "  ❌ Falta .env.qa → copialo del VM (con el ENCRYPTION_KEY de producción)."
  FAIL=1
else
  echo "  ✅ .env.qa presente."
  if grep -qE '^ENCRYPTION_KEY=dev-32-byte' .env.qa 2>/dev/null; then
    echo "  ❌ ENCRYPTION_KEY es el DEFAULT de dev → no va a desencriptar las claves. Poné el de producción."
    FAIL=1
  fi
  if ! grep -qE '^SMTP_HOST=.+' .env.qa 2>/dev/null; then
    echo "  ⚠️  SMTP_HOST vacío → el email al supervisor NO se envía (el resto sincroniza igual)."
  fi
fi

# 3) sa-key.json (clave de servicio GCP, para Cloud SQL + documentos)
if [ ! -f sa-key.json ]; then
  echo "  ❌ Falta sa-key.json → copiala del VM."
  FAIL=1
else
  echo "  ✅ sa-key.json presente."
fi

if [ "$FAIL" -ne 0 ]; then
  echo ""
  echo "🛑 Faltan requisitos (ver arriba). Corregilos y volvé a correr este script."
  exit 1
fi

echo ""
echo "🚀 Levantando la estación de sync (cloud-sql-proxy + redis + worker)…"
$COMPOSE up -d cloud-sql-proxy redis worker

echo ""
echo "✅ ESTACIÓN DE SYNC ARRIBA."
echo "   El worker sincroniza SOLO cada 4hs — auto-login Clave Única + 2ª clave, sin toque humano."
echo ""
echo "   Estado:   $COMPOSE ps"
echo "   Logs:     $COMPOSE logs -f worker"
echo "   Frenar:   $COMPOSE stop worker"
echo ""
echo "   💡 Podés apagar la máquina cuando quieras: el sync se pausa y RETOMA SOLO al prender."
echo "      (En Docker Desktop → Settings → 'Start Docker Desktop when you log in' para que vuelva sin tocar nada.)"
