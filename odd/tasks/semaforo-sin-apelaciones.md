# Semaforo without appeals

## Specs
- S1: Marcelo, 2026-10-23: "me pidieron quitar las apelaciones del semaforo.. que muestre todo lo demas"; answered "si de los dos" (per-case colour and home panel).

## Tasks
- [x] T1 · S1 · inline · `SEMAFORO_EXCLUDED_VALUES` (apelacion_5d) excluded from `_compute_semaforo` and from the panel `por_vencer`; tests in `tests/services/test_semaforo_sin_apelaciones.py`; the #403 "last day is rojo" test now asserts verde.
- [ ] T2 · S1 · after deploy · guarded recompute of causas with an open appeal window (the colour persisted in `cases.semaforo` only changes when a case is recomputed).

## Log
- L1: Marcelo: "me pidieron quitar las apelaciones del semaforo.. que muestre todo lo demas".
- L2: Marcelo: "si de los dos".
- Scope note: `reposicion_auto_prueba_3d` is also optional but was not asked; it still colours.
