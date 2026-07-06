# Runbook: unificar-modelo-causas — migraciones 022/023/024 en QA Cloud SQL

**Alcance:** aplicar manualmente las migraciones 022 (`case_lawyer_source`),
023 (`case_merge_audit`) y 024 (fusión de Case duplicados por ROL) contra la
instancia QA de Cloud SQL. **Este documento es solo el procedimiento — no
ejecuta nada.** Requiere aprobación explícita y ejecución manual por un
operador humano, con snapshot previo obligatorio.

> ⚠️ 024 es una migración de DATOS irreversible en el sentido práctico (borra
> filas `Case` "perdedoras" tras fusionarlas). La red de seguridad real es el
> snapshot de Cloud SQL tomado en el paso 1, no el `downgrade()` de Alembic
> (que es best-effort, ver más abajo).

## Datos del entorno (no secretos)

- Proyecto GCP: `grupo-segal`
- Instancia Cloud SQL: `casetracker-segal-qa-db` (región `us-central1`,
  connection name `grupo-segal:us-central1:casetracker-segal-qa-db`)
- IP pública DB (ver `docs/db-rotation-runbook.md` si rotó): `34.45.250.221:5432`
- VM API QA: `34.121.223.42:8000`
- Usuario DB: `casetracker_app`
- Base: `casetracker`
- Convención de este repo: `alembic upgrade` corre automáticamente en el
  contenedor `migrate` en cada deploy (`docker compose ... up -d`, ver
  `docs/DEPLOY_QA.md` §7/§11) contra la VM/Cloud SQL de QA. **Para esta
  migración de datos NO se usa ese camino automático** — se aplica a mano,
  en una ventana controlada, con snapshot previo y verificación manual antes
  de dejar que el deploy normal la re-ejecute (024 es idempotente: si ya
  corrió, un `alembic upgrade` posterior del pipeline normal es un no-op
  seguro).

## Pre-requisitos

- `gcloud` autenticado con permisos sobre `grupo-segal`.
- `cloud-sql-proxy` instalado (o acceso SSH a la VM `segal-qa-vm` que ya lo
  usa vía Docker).
- Confirmar que PR1a + PR1b (attribution/scope/ingest/alerts) ya están
  desplegados en QA y estables — 024 depende de que el ingest ya no cree
  duplicados nuevos (si no, el merge se repetiría en cada sync).
- Ventana de mantenimiento anunciada (breve corte de escritura recomendado,
  ver paso 4).

## 1. Snapshot de la base ANTES de tocar nada (rollback primario)

```bash
gcloud sql backups create \
  --instance=casetracker-segal-qa-db \
  --description="pre-unificar-modelo-causas-migration-022-023-024"

# Confirmar que el backup quedó listo:
gcloud sql backups list --instance=casetracker-segal-qa-db --limit=5
```

Anotar el `backup_id` resultante — es el rollback primario (paso 7).

## 2. Exportar un snapshot lógico adicional de las tablas afectadas (opcional pero recomendado)

Para poder inspeccionar/diffear sin depender solo del backup binario:

```bash
gcloud sql export sql casetracker-segal-qa-db \
  gs://<bucket-temporal>/pre-merge-cases-export.sql \
  --database=casetracker \
  --table=cases,movements,documents,case_litigantes,case_notificaciones,case_escritos,case_exhortos,case_deadlines,alerts,case_lawyer_source
```

## 3. Conectar vía Cloud SQL Auth Proxy

```bash
cloud-sql-proxy grupo-segal:us-central1:casetracker-segal-qa-db --port 5433 &
psql "host=127.0.0.1 port=5433 dbname=casetracker user=casetracker_app"
```

## 4. Pausar escrituras concurrentes (breve corte)

El scraper/worker y cualquier sync de la extensión deben estar detenidos
mientras corre 024, para que el conteo de "duplicados" no cambie a mitad de
camino:

```bash
# En la VM QA (systemd o docker compose, según cómo esté desplegado el worker):
docker compose -f docker-compose.yml -f docker-compose.qa.yml stop worker
```

## 5. Verificar la revisión actual de Alembic

```sql
SELECT version_num FROM alembic_version;
-- Debe mostrar '021' (o la última aplicada antes de esta migración)
```

## 6. Aplicar 022, 023 y 024

Por la convención de este repo, el camino recomendado y más simple es dejar
correr el `migrate` container normal UNA VEZ, apuntado solo a este rango,
en lugar de DDL manual — es exactamente el mismo código que corre en CI
contra la base de test, ya verificado en PR2. Si por política de la VM el
`migrate` container no puede aislarse a un rango de revisiones:

```bash
# Opción A (preferida): dejar correr el pipeline de deploy normal, que
# ejecuta `alembic upgrade head` dentro del contenedor migrate — 022/023/024
# se aplican en orden, 024 corre el pipeline de merge dentro de una
# transacción con verify-before-delete.
docker compose -f docker-compose.yml -f docker-compose.qa.yml up -d migrate
docker compose -f docker-compose.yml -f docker-compose.qa.yml logs -f migrate

# Opción B (manual, si el pipeline de deploy no está disponible): ejecutar
# alembic directamente desde un shell con acceso a la DB QA vía el proxy
# del paso 3, apuntando DATABASE_URL a 127.0.0.1:5433:
DATABASE_URL="postgresql://casetracker_app:<password>@127.0.0.1:5433/casetracker" \
  PYTHONPATH=. alembic -c alembic/alembic.ini upgrade head
```

Ambas opciones ejecutan el mismo código (`app.services.case_merge.run_case_merge`
dentro de la transacción de la migración 024) — si `_verify_invariants`
detecta un problema, levanta `CaseMergeVerificationError` y Alembic hace
rollback de la migración completa (nada queda borrado ni a medio re-apuntar).

## 7. Verificación post-migración (antes de reanudar escrituras)

```sql
-- (a) cero ROLs duplicados sobrevivientes (civil)
SELECT rol, COUNT(*) FROM cases WHERE competencia = 'civil'
GROUP BY rol HAVING COUNT(*) > 1;
-- Debe devolver 0 filas

-- (b) cero filas hijas huérfanas (ejemplo con movements; repetir para las 8 tablas)
SELECT COUNT(*) FROM movements m
LEFT JOIN cases c ON c.id = m.case_id
WHERE c.id IS NULL;
-- Debe ser 0

-- (c) todos los Case sobrevivientes son del lawyer_id de la firma
SELECT COUNT(*) FROM cases c
JOIN lawyers l ON l.id = c.lawyer_id
WHERE c.competencia = 'civil' AND l.rut <> '16021492-9';
-- Debe ser 0 (o el valor de FIRM_LAWYER_RUT configurado en QA)

-- (d) el ledger de auditoría quedó poblado (una fila por Case perdedor fusionado)
SELECT COUNT(*) FROM case_merge_audit;

-- (e) alembic_version avanzó
SELECT version_num FROM alembic_version;
-- Debe mostrar '024'
```

Revisar también los logs del contenedor `migrate` (paso 6) — la migración
024 imprime un resumen (`case_merge: merged_rols=N losers_deleted=N
audit_rows=N reowned=N`) al finalizar.

## 8. Reanudar escrituras

```bash
docker compose -f docker-compose.yml -f docker-compose.qa.yml start worker
curl -s http://34.121.223.42:8000/health
```

## Rollback

**Primario (recomendado):** restaurar el snapshot del paso 1.

```bash
gcloud sql backups restore <BACKUP_ID> --restore-instance=casetracker-segal-qa-db
```

**Secundario (best-effort, solo si restaurar el snapshot no es viable):**
`alembic downgrade 023` invoca `app.services.case_merge.reverse_case_merge`,
que recrea una fila `Case` "esqueleto" (mismo `rol`, `court_id`/`competencia`
copiados del winner, `lawyer_id` = el perdedor original) por cada fila en
`case_merge_audit`. **Limitación importante:** las filas hijas (movements,
documents, litigantes, etc.) NO se re-dividen entre winner/loser — quedan
todas con el winner. El downgrade es una reconstrucción parcial para
recuperar visibilidad de "qué ROL tenía qué lawyer antes", no una reversión
completa de datos. Por eso el snapshot (paso 1) es el mecanismo real de
rollback.

## Verificación de que la migración es idempotente

Si por error se corre 024 dos veces (p. ej. un segundo deploy antes de que
alguien note que ya corrió), es seguro: la segunda corrida encuentra cero
ROLs duplicados y cero Cases no pertenecientes a la firma, y no borra ni
re-apunta nada (`merged_rols=0 losers_deleted=0`). Esto está cubierto por
`tests/services/test_case_merge.py::TestIdempotency`.

## Qué NO hacer

- NO correr `alembic upgrade` contra QA fuera de esta ventana anunciada sin
  el snapshot del paso 1.
- NO editar manualmente `case_merge_audit` — es el único rastro de qué
  `case_id` perdedor se fusionó a cuál winner.
- NO reanudar el worker/scraper (paso 8) antes de confirmar el paso 7 en su
  totalidad.
