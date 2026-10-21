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

- [ ] T1 · S1 · inline · migración 072 + modelo
- [ ] T2 · S2-S4 · inline · merge generalizado (clave + 14 tablas)
- [ ] T3 · S5 · PR aparte · `sync_cases` escribe `firm_lawyer_id` y mapa por la clave nueva; `ingest_service` lookups
- [ ] T4 · S5 · PR aparte · hash de documentos incluye tribunal (prerrequisito, ver bug-de-identidad)
- [ ] T5 · S5 · requiere visto bueno · limpieza real (dry-run primero, backup antes)

## Log

- L1: (2026-10-21) Marcelo, aprobando el plan: "si" a "arrancar con la medición y el diseño de la
  migración de la clave `(rol, court_id, procedure)`", y "Si" a "el merge generalizado, con tests en RED".
- L2: Medido: 2.942 grupos duplicados; 1.191 con historial en 2+ copias: 1.107 el más rico contiene
  a las demás, 84 repartidos (284 movimientos). Dueño canónico `lawyer_id=1` (5.698 filas).
