# Duplicados en `cases` por abogado: clave (rol, tribunal, cuaderno)

Root cause medido 2026-10-21: `SyncService.sync_cases` escribe el abogado individual
(`sync_service.py:1365`) en vez del dueño canónico (Approach C). 3.083 filas sobrantes,
todas de abogados distintos. Relacionado: `bug-de-identidad-rol-tribunal.md`.

## Specs

- S1: La clave única de `cases` pasa a `(lawyer_id, rol, court_id, procedure)`. Es superconjunto
  de la actual: no puede fallar por datos. `procedure` NULL = 0 filas (medido).
- S2: El merge agrupa por `(rol, court_id, procedure)`, nunca por ROL solo: el mismo ROL en
  tribunales distintos son juicios distintos ("Principal" y "Medida prejudicial" también).
- S3: El merge re-apunta las 14 tablas con FK a `cases` (hoy lista 8; faltan `hitos`,
  `liberacion_requests`, `generated_documents`, `cartera_snapshots`, `case_lawyer_source` está
  cubierta aparte) y la verificación previa al borrado las revisa todas.
- S4: Historiales repartidos (84 grupos, 284 movimientos) se UNEN, no se pierden: gana el más rico
  y `_merge_movements` re-apunta lo que le falta.
- S5: Orden obligatorio: restricción, merge generalizado, ingesta (sync + ingest), limpieza real.
  Limpiar antes de arreglar la ingesta se deshace solo.

## Tasks

- [x] T1 · S1 · inline · migración 072 + modelo · 8596ab6
- [x] T2 · S2-S4 · worker · merge generalizado (clave + tablas hijas) · 7fabee8
- [x] T3a · S5 · worker · migración 073 backfill `case_lawyer_source` desde `Case.lawyer_id` + `sync_cases` registra el avistamiento (aditivo, sin cambio de comportamiento)
- [x] T3b · S5 · PR aparte · rotación, `detect_and_sync_movements`, `client_sync`, `estado_diario_probe`, `/status` y scripts leen `lawyer_id == L OR case_lawyer_source(L)` (transitorio)
- [x] T3c · S5 · PR aparte · `sync_cases` deduplica por (rol, court, procedure) entre dueños y crea bajo `firm_lawyer_id`; `ingest_service` lookups (:482,:601,:697)
- [x] T4 · S5 · PR aparte · hash de documentos incluye tribunal (prerrequisito, ver bug-de-identidad)
- [x] T5a · S5 · worker · informe de limpieza SOLO LECTURA (`scripts/dedup_cases_report.py`) + runbook; sin escribir en la base
- [x] T5b1 · S5 · worker · `run_case_merge` por lotes (`group_limit`/`only_group_keys`), verificación y re-own acotados al lote (#396)
- [ ] T5b2 · S5 · worker · ejecutor `scripts/dedup_cases_run.py` (dry-run por defecto, `--apply` + `--backup-ref` + worker quieto + confirmación MERGE) y runbook; la CORRIDA REAL requiere visto bueno de Marcelo

## Log

- L1: (2026-10-21) Marcelo, aprobando el plan: "si" a "arrancar con la medición y el diseño de la
  migración de la clave `(rol, court_id, procedure)`", y "Si" a "el merge generalizado, con tests en RED".
- L2: Medido: 2.942 grupos duplicados; 1.191 con historial en 2+ copias: 1.107 el más rico contiene
  a las demás, 84 repartidos (284 movimientos). Dueño canónico `lawyer_id=1` (5.698 filas).
- L3: (2026-10-21) Exploración: cambiar `sync_cases` al dueño canónico rompe en silencio `detect_and_sync_movements` (sync_service.py:2790), la rotación (:1994), `client_sync.py:110` y otros; Approach C movió la atribución a `case_lawyer_source` pero el camino del worker nunca se migró. Marcelo aprobó dividir T3 en T3a/T3b/T3c ("si").
- L4: #388 (T1) y #390 (T2) mergeados en main. #389 se mergeó contra una rama squasheada y no llegó a main: no apilar PRs sobre ramas que se squashean.
- L5: T3a mergeada (#391). T3b: `case_belongs_to_lawyer` (dueño OR avistamiento) en rotación, `detect_and_sync_movements`, `client_sync`, `estado_diario_probe`, `/status`. Requisito para T3c: `detect_and_sync_movements` busca por ROL (`.order_by(Case.id).first()`); con el mismo ROL en dos tribunales por abogado puede elegir el equivocado: resolver con el tribunal. Scripts pendientes: import_cases_html, consulta_sync, freshness_sync, backfill_detail.
- L6: (2026-10-22) T4: cambiar la fórmula re-descargaría todos los documentos y la extensión calcula el mismo hash. Hoy el prefetch global por hash hace que la 2da causa con el mismo ROL en otro tribunal adopte el documento de la 1ra y nunca reciba los suyos. Marcelo aprobó el hash compatible hacia atrás ("si").
- L7: T4 hecha: `_effective_hash` califica con `court:<id>` solo si el dueño del hash legacy es OTRA causa de OTRO tribunal. Filas hermanas del mismo tribunal (duplicados por abogado) siguen compartiendo documentos hasta T5. La extensión (ingest_service) sigue con la fórmula legacy: limitación conocida para T3c.
- L8: T3c hecha (un solo commit atómico, excede las 400 líneas: la identidad y el arreglo de la rotación no se pueden separar sin dejar un commit intermedio con inanición). Riesgo hallado en revisión: `api_lookup` por ROL hacía que filas hermanas (mismo ROL, Principal + Medida) quedaran siempre primeras en la rotación; corregido con selección por (ROL, tribunal) y sello `_mark_detail_checked` de hermanas. Semántica: la huella del listado ya no ve cambios in-place de tribunal/cuaderno (son identidad). Limitaciones: ingest_movements/documentos siguen por (dueño, rol); ingest_cases solo empareja filas del dueño canónico; sin probar en Postgres ni con syncs concurrentes. Siguiente: T5 limpieza (dry-run + backup + visto bueno).
- L9: Verificación independiente de T3c (solo lectura) halló 2 defectos reales, corregidos antes de publicar: (7) la rotación prestaba el api_case de otro tribunal a una fila cuyo tribunal no está en el listado (doble scrape + fila hermana primera para siempre); ahora esa fila se salta ese ciclo salvo que el ROL tenga un solo tribunal. (5) una creación concurrente de la misma identidad bajo el dueño canónico (dos abogados) rompía todo el chunk con PendingRollbackError; ahora la creación va en SAVEPOINT y ante IntegrityError se reusa la fila existente. Avisos sin corregir: tribunal vacío es '' en sync y 'Desconocido' en ingest; cuaderno sin normalizar (igual que el índice).
- L10: #394 (T3c) mergeado y desplegado; worker reiniciado 12:16 local, primer ciclo con código nuevo esperado ~14:16. Marcelo aprobó armar el dry-run de T5 en paralelo ("Si"): informe de solo lectura, nunca un rollback-transaction sobre la base real.
- L11: Informe real (2026-10-22): 2.945 grupos, 3.086 filas sobrantes, 0 vacías y 0 solo-avistamiento: todas cuelgan cartera_snapshots (6.117) y case_lawyer_source (3.288), 28.157 alerts, 25.118 movimientos (24.284 ya en el ganador, 834 en 205 grupos se unirían). Corrige la estimación previa de "1.968 borrables sin pérdida" (solo miraba movimientos/plazos/alertas): no hay pasada 1 sin riesgo. El merge por lotes (group_limit en run_case_merge) pasa a ser requisito de T5b. Costo del merge en una transacción: ~4,6 h.
- L12: Marcelo aprobó armar el ejecutor por lotes ("si"). Solo herramienta + tests; ninguna corrida sobre datos. #395 y #396 mergeados y desplegados en QA (la corrida 751 de Deploy QA quedó con estado colgado en GitHub pese a que sus dos trabajos terminaron en success; la API de cancelación responde 409; sin impacto).
- L13: (2026-10-23) Limpieza real, primeras corridas (backup bajo demanda 1792758607478 y 1792762340275, ambos SUCCESSFUL). Lote de prueba de 5 grupos desde la Mac: 64 s (12,9 s/grupo), 5 filas borradas, integridad OK, ganadores sin pérdida. Marcelo autorizó explícitamente el borrado de las copias sobrantes ya fusionadas ("si autorizo ... no borres nada más"). Lote de 25 grupos desde la VM de QA (misma región que la base, 2,7 ms por consulta contra 135 ms por el proxy): CONFIRMADO en la base, 2.940 -> 2.915 grupos, 16.248 -> 16.222 causas (26 perdedoras, un grupo tenía 3 copias), 0 huérfanas en 13 tablas, todas las filas sobrevivientes del estudio; duró ~10,5 min (25 s/grupo, PEOR que desde la Mac): el tiempo NO es latencia. Causa medida: cuatro FKs a `movements.id` y `cartera_snapshots.case_id` sin índice (documents.movement_id 248 MB / 277k filas, alerts.movement_id 46 MB / 288k filas, case_deadlines.source_movement_id y verdict_movement_id): cada DELETE de un movimiento recorre esas tablas (~1,4 s) y el merge borra ~18 movimientos por grupo. Fix: migración 074 con 5 índices (aditiva). Queda: 2.915 grupos, ~3.055 filas sobrantes; la corrida completa espera la 074 desplegada. Incidente de proceso: corté el SSH a los 280 s con `timeout`; el runner siguió vivo en la VM y terminó solo (transacción de ~10 min, confirmada y verificada); en adelante, comandos largos con `nohup` en la VM y lectura del avance por la base.
- L14: (2026-10-23 14:30 UTC) Con la 074 desplegada: lote medido de 25 grupos desde la VM = 19 s (0,76 s/grupo, 33 veces más rápido que los 25 s previos), integridad OK, 7 criterios de verificación cumplidos; 2.915 -> 2.890 grupos, 28 perdedoras borradas. La corrida completa (lotes de 100, backup 1792765127572) se DETUVO sola en el lote 1 y lo revirtió entero (nada borrado, 2.890 grupos, 16.194 causas intactas): `IntegrityError` `case_merge_audit_winner_case_id_fkey` al borrar la causa 1584 (C-4892-2023), que la migración 024 había registrado como ganadora. Defecto mío: indiqué excluir `case_merge_audit.winner_case_id` por "apuntar al ganador"; es cierto para las filas nuevas, no para las viejas cuyo ganador pierde un merge posterior (229 filas de auditoría anteriores, 167 causas aún duplicadas entre ellas). Fix: `_repoint_audit_winner` (las filas viejas siguen a la nueva ganadora, sin borrar la auditoría) + chequeo en `_verify_invariants`. Los lotes de 5 y 25 no tocaron ninguna de esas causas.
