# Runbook de Operación — Segal Case Tracker

Guía práctica para operar el sistema (el doc de "no improvisar a las 2 AM").
Comandos concretos. Asume acceso a la Mac residencial (scraper), `gcloud` (VM/DB)
y el repo.

---

## 1. Arquitectura en 30 segundos
- **Scraper (freshness)** → corre HEADFUL en la **Mac residencial** (PJUD bloquea
  headless + IPs de datacenter). Supervisado por `scripts/run_freshness.sh`.
- **API + worker** → **VM `casetracker-segal-qa`** (34.121.223.42), docker compose
  (`~/casetracker/docker-compose.qa.yml` + `~/casetracker/.env.qa`).
- **DB** → Cloud SQL `casetracker-segal-qa-db` (Postgres 16).
- **Front** → React (repo `segal-case-tracker-front`), dev server local proxyea a la VM.

SSH a la VM: `gcloud compute ssh casetracker-segal-qa --zone us-central1-a`

---

## 2. Scraper: pausar / reanudar / estado
**Pausar** (antes de apagar la Mac, o para correr el smoke):
```
pkill -9 -f run_freshness.sh; pkill -9 -f freshness_sync.py; pkill -9 -f caffeinate; pkill -9 -f ms-playwright/chromium
```
**Reanudar** (desde el repo, en `main`):
```
nohup caffeinate -dimsu scripts/run_freshness.sh > /tmp/freshness.log 2>&1 &
```
- Tiene **singleton lock** (`${TMPDIR}/segal_freshness.lock.d`): un segundo lanzamiento se rechaza solo. Si quedó un lock viejo tras un kill -9, el próximo arranque lo detecta (PID muerto) y lo toma.
**Estado / una sola ventana:**
```
pgrep -fl run_freshness.sh; pgrep -fl freshness_sync.py
ps aux | grep -c '[m]s-playwright/chromium'   # ~9 = UNA ventana (sano)
```
**Logs:** `tail -f /tmp/freshness.log` (rondas, logins, errores).
**Progreso:** ver `/stats/admin` (abajo) o consultar `last_detail_checked_at` en la BD.

---

## 3. Smoke autenticado (validación profunda, on-demand)
Corre con el **scraper PAUSADO** (headful, una sesión PJUD a la vez):
```
scripts/run_smoke.sh        # corre el smoke + alerta si hay canal (PJUD_ALERT_WEBHOOK_URL)
# o directo:
PYTHONPATH=$(pwd) <venv>/bin/python scripts/smoke_pjud.py
```
Salida JSON: `login_ok / list_ok / detail_ok / parse_ok` + exit code (0 = OK).
En falla deja screenshot/HTML redacted en `SMOKE_FAILURE_DIR` (default `/tmp`).

---

## 4. Health checks
- **VM liveness:** `curl http://34.121.223.42:8000/health` → `{"status":"healthy"}`
- **VM readiness (DB):** `curl http://34.121.223.42:8000/readyz` → `{"ready":true,"db":"ok"}` (503 = DB caída). El deploy falla si esto no da 200.
- **Frescura / calidad:** `GET /api/v1/stats/admin` (auth) → `last_checked_at`, pipeline de docs, calidad de datos.

---

## 5. Rotar ENCRYPTION_KEY (ventana de mantenimiento)
1. Pausar scraper (§2).
2. Generar key Fernet real: `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"` → guardar en archivo chmod 600.
3. Re-encriptar (atómico, ambas columnas):
   ```
   OLD_ENCRYPTION_KEY=<actual> NEW_ENCRYPTION_KEY=<nueva> DATABASE_URL=<url> python scripts/reencrypt_credentials.py        # dry-run
   ... python scripts/reencrypt_credentials.py --apply
   ```
4. Setear `ENCRYPTION_KEY=<nueva>` en la VM (`~/casetracker/.env.qa`, hacer backup antes) y local (`.env.backfill`).
5. Deploy del código (merge a main) → la VM bootea con strict + key nueva.
6. Verificar: `/readyz` 200 + `docker exec casetracker-api-1 python -c "...decrypt..."`.
7. Reanudar scraper.
Rollback: re-correr reencrypt con OLD/NEW invertidos + restaurar el `.env.qa.bak`.

**Rotación SIN downtime (MultiFernet) — preferido:** no hace falta pausar.
1. `ENCRYPTION_KEY=<nueva>` + `ENCRYPTION_KEY_FALLBACKS=<vieja>` en `.env.qa`/`.env.backfill` → deploy. La app desencripta con cualquiera de las dos y **escribe con la nueva**.
2. Re-encriptar con calma (`reencrypt_credentials.py --apply`) — sin ventana.
3. Cuando todo está re-encriptado, **sacar `ENCRYPTION_KEY_FALLBACKS`** → deploy. La vieja queda muerta.
`ENCRYPTION_KEY_FALLBACKS` acepta varias keys separadas por coma (todas se prueban al desencriptar; todas deben ser Fernet reales en prod).

---

## 6. Rotar contraseña de la DB
1. Pausar scraper.
2. Nueva password: `python -c "import secrets;print(secrets.token_urlsafe(36))"` → archivo chmod 600.
3. Actualizar `DATABASE_URL` en VM `~/casetracker/.env.qa` (sed solo el password, backup antes) y local `.env.backfill`.
4. `gcloud sql users set-password casetracker_app --instance=casetracker-segal-qa-db --password="$(cat <archivo>)"`
5. Recrear containers: SSH → `cd ~/casetracker && sudo docker compose -f docker-compose.qa.yml up -d`
6. Verificar: `/readyz` 200 + `SELECT 1` dentro del container api.
7. Reanudar scraper.

---

## 7. Backups y restore (Cloud SQL)
- **Backups automáticos: HABILITADOS** — diarios 07:00 UTC, retención 14, instancia `casetracker-segal-qa-db`.
- Listar: `gcloud sql backups list --instance=casetracker-segal-qa-db`
- **Restore de un backup a una instancia EXISTENTE** (sobrescribe):
  ```
  gcloud sql backups restore <BACKUP_ID> --restore-instance=<DESTINO> --backup-instance=casetracker-segal-qa-db
  ```
- **Recuperar a una instancia SEPARADA** (drill / recovery sin tocar prod):
  ```
  gcloud sql instances clone casetracker-segal-qa-db <NUEVA_INSTANCIA>
  ```
- ✅ **Drill probado** (clone → instancia separada): recuperación **~15 min**, data íntegra (2541 causas, igual que prod), instancia drill borrada. Para verificar: conectar y comparar row counts; **borrar la instancia drill al terminar** (`gcloud sql instances delete <NUEVA_INSTANCIA>`) para no dejar costo.
- ⚠️ Opcional pendiente: PITR (`--enable-point-in-time-recovery`, puede reiniciar la instancia).

---

## 8. Si PJUD bloquea / challengea
**Regla de oro: bajar carga / frenar. NO rotar agresivo ni usar 2Captcha para "pasar".**
1. Pausar el scraper (§2).
2. Cooldown (horas).
3. Revisar el smoke + screenshot/HTML redacted para ver qué pasó.
4. Si hay challenge persistente: revisión humana, bajar frecuencia (rate limits §10), confirmar IP residencial.
5. Reanudar gradual.

---

## 9. Sesión / Redis
- El **scraper de frescura NO usa Redis** (sesión en memoria, in-process). Si la sesión PJUD muere, el worker re-loguea solo (Clave Única, auto-reauth).
- Redis (en la VM) lo usa el path API/worker. Si muere: `docker compose up -d` lo levanta.

---

## 10. Rate limits (configurables por env)
Token bucket por acción (conservadores, pacean — no fallan):
`PJUD_RL_LOGIN_RATE/BURST`, `PJUD_RL_LIST_*`, `PJUD_RL_DETAIL_*`, `PJUD_RL_DOCUMENT_*`, `PJUD_RL_WAIT_TIMEOUT`.
Ajustar en `.env.backfill` (local) / `.env.qa` (VM). Más conservador = bajar `*_RATE`.

---

## 11. Dónde viven los secretos (Mac, chmod 600, fuera de git)
- `~/segal_new_encryption_key.txt` — key Fernet actual.
- `~/segal_new_db_password.txt` — password DB actual.
- `~/segal_old_pjud_ciphertext.txt` — backup del ciphertext pre-rotación.
- `.env.backfill` (repo, gitignored) — DATABASE_URL, ENCRYPTION_KEY, CU_RUT/CU_PASSWORD, rate limits, TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID.
⚠️ Mover a Secret Manager/KMS es follow-up pendiente. **Nunca** pegar secretos en chat/commits (gitleaks corre en CI).

---

## 12. Alertas (Telegram — al ADMIN, no a los abogados)
Canal: Telegram (`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` en `.env.backfill`). Las alertas son **operativas** (scraper caído/colgado, smoke fail) → al admin; las alertas de causa de los abogados son in-app (Novedades/semáforo).
- **Monitor de frescura** (`scripts/freshness_monitor.py`): alerta 🔴 si no se re-sincronizó ninguna causa en `FRESHNESS_STALE_HOURS` (default 2h) y 🟢 al recuperarse. No usa browser, mira el `last_detail_checked_at` de la DB compartida.
- **Schedule: corre en la VM** (siempre prendida — cierra el punto ciego de que la Mac se duerma). Cron de **root** en la VM, cada 30 min:
  ```
  */30 * * * * docker exec casetracker-api-1 python scripts/freshness_monitor.py >> /tmp/segal_monitor.log 2>&1
  ```
  Las creds de Telegram están en `~/casetracker/.env.qa` (env_file del container). Editar: `sudo crontab -e` en la VM.
  - El runner local `scripts/run_monitor.sh` queda solo para chequeos manuales desde la Mac (NO está en cron — se sacó para no duplicar alertas).
- **Smoke**: `scripts/run_smoke.sh` alerta a Telegram si el smoke falla (scraper pausado).
- Probar el canal: `curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" --data-urlencode "chat_id=$TELEGRAM_CHAT_ID" --data-urlencode "text=test"`
