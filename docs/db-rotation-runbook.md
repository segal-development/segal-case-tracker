# Runbook: rotación de contraseña de la base de datos (ventana overnight)

**Motivo:** el `DATABASE_URL` (con contraseña) se pegó en un chat → valor expuesto. La
contraseña actual es fuerte (48 chars), solo hay que **invalidar el valor expuesto**.
Es un corte duro (todo lo que usa la clave vieja se cae hasta reconfigurar), por eso
se hace en la pausa del overnight, no en horario laboral.

## Datos (no secretos)
- Instancia Cloud SQL: `casetracker-segal-qa-db` (us-central1, POSTGRES_16)
- Usuario DB: `casetracker_app`
- Base: `casetracker`
- Host DB (IP pública): `34.45.250.221:5432`
- VM API: `34.121.223.42:8000`
- `DATABASE_URL` de la VM: **NO** está en `deploy-qa.yml` → se configura directo en la VM
  (a descubrir vía SSH: `~/.env`, unit de systemd, o docker-compose).

## Secuencia (ejecutar en orden)

1. **Pausar el scraper** (Mac):
   ```
   pkill -9 -f run_freshness.sh; pkill -9 -f freshness_sync.py; pkill -9 -f caffeinate; pkill -9 -f ms-playwright/chromium
   ```

2. **Generar clave nueva fuerte** (no reusar, no pegar en chat):
   ```
   NEWPASS=$(python3 -c "import secrets;print(secrets.token_urlsafe(36))")
   ```

3. **Rotar en Cloud SQL**:
   ```
   gcloud sql users set-password casetracker_app --instance=casetracker-segal-qa-db --password="$NEWPASS"
   ```

4. **Actualizar la VM** (SSH a 34.121.223.42):
   - Descubrir dónde se setea `DATABASE_URL` (grep en `~`, `/etc/systemd/system`, o `docker-compose*.yml`).
   - Reemplazar la password en ese `DATABASE_URL`.
   - Reiniciar el servicio de la API (systemd `restart` o `docker compose up -d`).
   - Verificar: `curl -s http://localhost:8000/health` → `healthy`.

5. **Actualizar local** `.env.backfill`:
   - Reemplazar la password dentro de `DATABASE_URL` (mantener user/host/db/port).
   - `chmod 600 .env.backfill` (ya está, mantener).

6. **Relanzar el scraper** (Mac):
   ```
   nohup caffeinate -dimsu scripts/run_freshness.sh > /tmp/freshness.log 2>&1 &
   ```

## Verificación post-rotación
- VM: `GET /health` OK + un endpoint que pegue a la DB (ej. `/api/v1/stats/firm`) responde.
- Scraper: el log muestra login + re-sync sin errores de conexión.
- Query rápida: `SELECT 1` con el nuevo `DATABASE_URL`.

## Rollback
- Si algo falla, volver a setear la password anterior con `gcloud sql users set-password`
  (requiere conocerla) y restaurar los `DATABASE_URL`. Por eso: tener el valor nuevo
  guardado de forma segura ANTES de cerrar la sesión.

## Notas
- La clave NUEVA va a Secret Manager/KMS cuando se haga ese item (1 mes). Por ahora vive
  en la VM + `.env.backfill` (chmod 600), no en el chat.
- No commitear nunca el `DATABASE_URL` (el secret-scan en CI lo bloquea).
