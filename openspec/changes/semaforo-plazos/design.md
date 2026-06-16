# Design: Semáforo de Plazos — Procedural Deadline Engine (Slice A)

## Technical Approach

A stateful, config-driven rules pipeline turns scraped `Movement` rows into procedural
intelligence. Three new pure-logic services compose bottom-up: `business_days` (días hábiles
via `holidays`) → `procedural_classifier` (movements → `ProceduralState` + triggering movement
per active deadline) → `deadline_engine` (classify → compute deadlines → semáforo → persist).
`DeadlineEngine.recompute_case` is invoked synchronously inside the existing per-case sync loop
(`detect_and_sync_movements`), within the same transaction. Semáforo + criticidad are STORED on
write (recompute-on-sync), never computed-on-read, so the case list sorts via plain SQL. All
output is ADVISORY: INDETERMINATE/non-civil fail safe to GRIS, and every API response carries a
mandatory disclaimer. Delivered as two feature-branch-chained PRs (engine+model+hook, then API).

## Components & Data Flow

```
PJUD detail ─→ sync_movements (commit) ─→ DeadlineEngine.recompute_case(db, case)
                                                │
        ┌───────────────────────────────────────┤
        ▼                  ▼                      ▼
 MovementClassifier   DeadlineEngine        business_days
 (walks movements     (plazos table +       (holidays.Chile,
  chronologically →    REBELDÍA transition   add_business_days,
  state + triggers)    + abandono/prescr.)   business_days_until)
        │                  │
        └────────┬─────────┘
                 ▼
   persist: case_deadlines rows (UNIQUE upsert)
          + Case.procedural_state / semaforo / next_deadline_at
                 ▼
   API: GET /cases/{id}/deadlines · GET /cases?sort_by=criticidad
```

`recompute_case` steps: (1) load `Movement` rows ASC by `movement_date`; (2) guard
`competencia=="civil"` else GRIS + return; (3) classify → `(state, {deadline_type: triggering_movement})`;
(4) for each active trigger compute `due_date = add_business_days(movement_date.date(), n)`,
upsert `case_deadlines`; (5) mark superseded deadlines; (6) compute REBELDÍA (NOTIFICADO + 8 días
hábiles elapsed, no excepciones → state REBELDE, deadline met); (7) compute semáforo from nearest
active deadline + abandono/prescripción flags; (8) write the three denormalized `Case` columns.
Pure-logic services take dates/lists, not the `Session`, so they are trivially unit-testable; only
the engine touches `db`.

## Architecture Decisions (ADR)

| Decision | Choice | Rejected | Rationale |
|---|---|---|---|
| Classifier | Rules-based, config-driven `ClassifierRule` list | ML classifier | Transparent & legally auditable; ~50 cases is far too little training data; PJUD text drift is a one-line rule edit |
| Feriados source | `holidays` lib (pin `^0.58`) | Hand-maintained DB feriados table | Covers fixed + moving (Viernes Santo) + Ley 21.169 feriados with zero annual maintenance. Mitigation for legal trust: exhaustive unit tests vs official dates + Slice B override table |
| Semáforo storage | Stored on `Case`, recomputed on sync | Computed-on-read | Sync is ~4h cadence; stored enables `ORDER BY next_deadline_at` with no N+1. Staleness is acceptable and bounded |
| State location | Denormalized on `Case` + detail rows in `case_deadlines` | State only in separate table | `Case` columns power the fast list/sort; the child table keeps the auditable per-deadline trail (basis, source movement, status) |
| Recompute trigger | Synchronous in existing sync loop | New worker/queue | No new infra; deadlines are derived & idempotent; ~50 cases makes cost negligible |

## Classifier Rule Table (grounded in real movement data)

`ClassifierRule(description_regex, stage_regex, event, next_state, starts_deadline_type, priority)`,
compiled once, applied oldest-first; higher `priority` wins on a tie; no match → state unchanged.

| description_regex | stage_regex | event | next_state | starts_deadline |
|---|---|---|---|---|
| `[Oo]rdena despachar mandamiento` | `.*` | MANDAMIENTO | MANDAMIENTO | — |
| `(NOTIFICACI[ÓO]N DE DEMANDA\|Notificaci[oó]n demanda).*(?i:exitosa)` | `.*` | NOTIFICACION_EXITOSA | NOTIFICADO | EXCEPCIONES_8D |
| `.*` | `Excepciones` | EXCEPCIONES_OPUESTAS | EXCEPCIONES | TRASLADO_EJECUTANTE_4D |
| `.*` | `Contestaci[oó]n Excepciones` | TRASLADO_EJECUTANTE | TRASLADO_EJECUTANTE | — |
| `[Ss]e pronuncia sobre admisibilidad` | `.*` | ADMISIBILIDAD | ADMISIBILIDAD | — |
| `Notificaci[oó]n resoluci[oó]n que recibe.*(?i:prueba)` | `.*` | AUTO_PRUEBA | AUTO_PRUEBA | TERMINO_PROBATORIO_10D (+ LISTA_TESTIGOS_2D) |
| `Cita a Audiencia` | `.*` | CITACION_SENTENCIA | CITACION_SENTENCIA | SENTENCIA_10D |
| `.*` | `Terminada` | TERMINADA | TERMINADA | — |

Empty `stage=""` rows (~12.6% of dataset, "Resolución" trámite) match on `description` only;
known safe no-ops ("Mero trámite", "Previo a proveer", "No ha lugar desarchivo") leave state
unchanged. Anything unmatched into a recognizable state stays INDETERMINATE → GRIS.

## Plazos (Deadline) Table — CPC legal basis

`DeadlineType` enum carries `(días_hábiles, legal_basis)`; engine writes `legal_basis` into the row.

| DeadlineType | días háb. | Legal basis | Trigger |
|---|---|---|---|
| EXCEPCIONES_8D | 8 | art. 459 CPC | NOTIFICACION_EXITOSA |
| TRASLADO_EJECUTANTE_4D | 4 | art. 466 CPC | EXCEPCIONES_OPUESTAS |
| TERMINO_PROBATORIO_10D | 10 | art. 468 CPC | AUTO_PRUEBA |
| LISTA_TESTIGOS_2D | 2 | art. 468 CPC | AUTO_PRUEBA (sub) |
| OBSERVACIONES_PRUEBA_6D | 6 | art. 469 CPC | fin término probatorio |
| SENTENCIA_10D | 10 | art. 162/470 CPC | CITACION_SENTENCIA |
| APELACION_5D | 5 | art. 189/475 CPC | SENTENCIA |

Días hábiles = Mon–Fri minus `holidays.Chile`. **Counting starts the day AFTER the triggering
event** (movement_date = day 0). REBELDÍA is a computed transition (no PJUD movement fires it):
NOTIFICADO + EXCEPCIONES_8D expired with no excepciones rule seen → state REBELDE. Abandono /
prescripción are SEPARATE approaching-flags (not semáforo, not exact dates): `abandono_risk`
(art. 152 CPC, off `last_movement_at`: none / approaching >4.5m / presumible >6m) and
`prescripcion_risk` (art. 2515 CC, off `filed_at`: none / approaching >2.5y / at_risk >3y).

Semáforo from nearest active deadline: ROJO ≤1 día hábil or expired; AMARILLO 2–5; VERDE >5;
GRIS when INDETERMINATE / non-civil / no active deadline (safe-fail — never a false ROJO).

## Data Model & Migration 008

New `app/models/case_deadline.py` (`CaseDeadline`): `id, case_id FK, deadline_type, legal_basis,
due_date DATE, triggered_at DATE, status (active|met|expired|superseded), source_movement_id FK,
computed_at, created_at`. New `Case` columns: `procedural_state VARCHAR(30)`, `semaforo
VARCHAR(10)`, `next_deadline_at DATE`. `Case.deadlines` relationship added.

Migration `008_add_case_deadlines.py` (follows 007 style — `revision="008"`, `down_revision="007"`):
`create_table("case_deadlines", ...)`, `UNIQUE(case_id, deadline_type, triggered_at)` (prevents
duplicate upserts), `ix_case_deadlines_case_id`, `ix_case_deadlines_due_date`,
`ix_cases_next_deadline_at` + `ix_cases_semaforo` (support `sort_by=criticidad`), and three
`op.add_column` on `cases`. Downgrade drops indexes, table, then the three columns (no data
loss — all derived/recomputable). **Coordination flag**: the prospect-search tracker branch also
claims 008/009. This change targets `main` where 008 is free; whichever branch merges second MUST
renumber before merge.

## Dependency

Add `holidays = "^0.58"` to `pyproject.toml` `[tool.poetry.dependencies]`. Why: Chilean feriados
include moving dates (Viernes/Sábado Santo) and Ley 21.169 additions — hand-maintaining them is an
annual legal-correctness liability. Mitigation: exhaustive unit tests pin known feriados; a Slice B
`chile_feriados_override` table will let ops correct the lib without a deploy.

## Sync Hook Integration

In `detect_and_sync_movements._do_fetch` (app/services/sync_service.py), AFTER `sync_svc.sync_movements(...)`
(~line 1377–1387) and within the existing try/transaction, before the entity-sync loop:

```python
from app.services.deadline_engine import DeadlineEngine
if db_case and (db_case.competencia or "civil") == "civil":
    DeadlineEngine.recompute_case(db, db_case)  # flush-only; outer db.commit() persists
```

Rollback-safe: it only flushes; the existing `db.commit()` (~line 1423) persists it, and the
existing `except: db.rollback()` already discards partial state. `recompute_case` must never raise
on classification gaps — it logs and falls back to GRIS so a bad rule can never break movement sync.

## API (PR2)

`app/api/v1/deadlines.py` — `GET /cases/{id}/deadlines`, reuses `_resolve_lawyer_id` + `Case.lawyer_id`
scoping (404 on miss). Response: `case_id, procedural_state, semaforo, active_deadlines[] {type, label,
legal_basis, due_date, triggered_at, dias_habiles_remaining, status}, abandono_risk, prescripcion_risk,
disclaimer`. `disclaimer` is a module constant `DEADLINE_DISCLAIMER` ("Este cálculo es orientativo y no
reemplaza el criterio del abogado.") — present in EVERY response. Register router in `app/api/v1/router.py`.

Extend `app/api/v1/cases.py`: add `procedural_state, semaforo, next_deadline_at` to `CaseResponse`;
add `sort_by: Optional[Literal["criticidad","updated_at"]] = "updated_at"`; `criticidad` →
`ORDER BY next_deadline_at ASC NULLS LAST`. No auth change.

## Testing Strategy

| Layer | What | Approach |
|---|---|---|
| Unit | `business_days` per deadline type incl. feriado-crossing (Fiestas Patrias, Semana Santa, year boundary) | Parametrized; assert vs manually verified due dates; day-after-event start |
| Unit | Classifier state + triggers | Replay representative REAL movement sequences (the ~50 QA cases as fixtures) → assert final state + active deadlines; mis-history → INDETERMINATE |
| Unit | Semáforo thresholds + REBELDÍA + abandono/prescripción flags | Synthetic deadline sets across boundaries (≤1/2-5/>5, expired) |
| Integration | `recompute_case` persistence + UNIQUE upsert idempotency + non-civil GRIS | SQLite in-memory; recompute twice → no dup rows |
| Migration | 008 round-trip | upgrade→downgrade→upgrade on SQLite + Postgres |
| API | endpoint shape, disclaimer present, lawyer scoping, sort_by=criticidad ordering | FastAPI TestClient |

## PR Boundary (feature-branch-chain)

- **PR1** (~340–390 lines): `business_days.py`, `procedural_classifier.py`, `deadline_engine.py`,
  `case_deadline.py` model + `Case` columns, migration 008, sync hook, `holidays` dep, + unit/integration/migration tests.
- **PR2** (~180–230 lines): `deadlines.py` endpoint + router registration, `CaseResponse`/`sort_by`
  extension, + API tests.

Both within the 400-line review budget. PR1 → tracker branch; PR2 → PR1 branch.

## Open Questions

- [ ] Confirm `holidays.Chile` covers Ley 21.169 (27 Jun / 31 Oct / 1 Nov) for target years; pin exact version after verification.
- [ ] Final migration number once prospect-search merge order is decided.
