# Prueba stage: expired plazos with an idle court are "esperando al tribunal", not red
Backend half (frontend follows with the label).

## Specs
- S1. Carla's decision (verbatim question and answer): "Cuando en la etapa de prueba vencen la lista de testigos o las observaciones sin que presentemos nada, y el tribunal no vuelve a moverse, la causa hoy queda en rojo indefinidamente. Ejemplo: C-5631-2022, en rojo desde mayo de 2024. ¿Cómo debería quedar? A) Sigue en rojo, es crítico. B) Pasa a 'esperando al tribunal', sin rojo." -> "B) Pasa a 'esperando al tribunal', sin rojo."
- S2. Prueba-stage plazo types: `termino_probatorio_10d`, `observaciones_prueba_6d`, `lista_testigos_2d`, `reposicion_auto_prueba_3d` (confirm the exact identifiers in app/services/deadline_engine.py and report any other prueba-stage type you find; do not add types not in the engine). A plazo of one of these types whose due date has passed (status expired, or active with due_date before today in Chile) NO LONGER makes the case red (nor amarillo) in the semáforo computation (`_compute_semaforo` / wherever expired or overdue plazos drive rojo). A prueba plazo that is still running (due today or later) keeps counting exactly as today. Every other plazo type is unchanged. The deadline rows themselves are not changed (no status rewrite, no deletes).
- S3. "Esperando al tribunal": a case is `esperando_tribunal = True` when it has at least one prueba-stage plazo past due (S2) and NO plazo (of any type) still running or overdue that keeps it red/amarillo, i.e. the only thing that would have made it red is the expired prueba plazos. Compute it in the engine where the semáforo is computed and persist it (new boolean column `cases.esperando_tribunal`, default false, migration NEXT revision after the current head: check alembic/versions for the latest number, it may be 076 or 077 if other branches landed; use the next free number and set down_revision to the current head on main). The semáforo of such a case is whatever the remaining plazos give (report what that is when there are none: verde or null/sin seguimiento), never rojo/amarillo because of S2.
- S4. Expose `esperando_tribunal: bool` in the case list item and the case detail responses (app/api/v1/cases.py: the Case response model used by GET /cases and GET /cases/{id}), additive.
- S5. Overrides and special cases unchanged: a liberación override (semaforo_override) still wins; medida prejudicial and non-civil unchanged; the poder (acreditar_poder_3d) rule unchanged.
- S6. Strict TDD, tests first, RED seen: the 7 real shapes (Rodrigo Campos, 2026-11-06): C-5631-2022 (observaciones expired 2024-06-14, last movement = notificación del auto de prueba) -> not red, esperando_tribunal True; C-1479-2026 (observaciones active but due 2026-10-29, i.e. overdue) -> not red, esperando True; a prueba plazo due tomorrow -> still counts as today (amarillo/rojo per current rules), esperando False; an expired prueba plazo plus another overdue non-prueba plazo (e.g. a fatal plazo) -> still red, esperando False; liberación override wins; no prueba plazos -> unchanged, esperando False. Existing tests keep passing; report any existing test whose expectation changes and why.
- S7. Out of scope: frontend, data writes in production (the parent runs a guarded recompute), e-mail.

## Tasks
- T1 backend (S2-S6) route: worker. Commit: none (parent reviews, verifier, simulation on production copy, commits).

## Log
L1. Carla Lavín (jefa jurídica), 2026-11-06: "B) Pasa a 'esperando al tribunal', sin rojo."

- T1 done (uncommitted): S2-S6 implemented; migration 076 (down_revision 075); flag computed in DeadlineEngine._compute_semaforo_state, persisted in recompute_case.
