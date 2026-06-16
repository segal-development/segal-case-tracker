# Proposal: Semáforo de Plazos — Procedural Deadline Engine (Juicio Ejecutivo)

## Intent

Turn raw scraped PJUD movements into actionable procedural intelligence: the current procedural state of each case, its active legal deadlines counted in días hábiles, and a traffic-light (semáforo) so abogados act before fatal deadlines — especially the 8-day excepciones plazo fatal — and spot abandono/prescripción risk early. Today movements are stored but a lawyer must read each history manually to know what is due. This is the highest-value, demo-able first slice of the broader judicial-workflow vision (the firm secured budget without a demo).

## Scope

### In Scope (Slice A)
- `ProceduralState` / `ProcEvent` enums (CPC + Ley 21.394, juicio ejecutivo).
- Rules-based, config-driven `MovementClassifier` grounded in real scraped data; INDETERMINATE → GRIS safe-fail.
- `DeadlineEngine.recompute_case` + `business_days` module using the `holidays` lib (Chile).
- Migration 008: `case_deadlines` table + `Case.procedural_state/semaforo/next_deadline_at`.
- Inject `recompute_case` into `detect_and_sync_movements` after movement sync (same transaction).
- `GET /cases/{id}/deadlines` (timeline + próxima acción + abandono/prescripción flags + mandatory legal disclaimer).
- Extend `GET /cases` with `semaforo`, `next_deadline_at`, and `sort_by=criticidad`.
- Validate classifier + engine against the ~50 real cases already scraped in QA.

### Out of Scope (Non-goals)
- Gerencia dashboard `GET /dashboard/cartera` (Slice B).
- `deadline_rojo` alert firing (Slice B).
- `chile_feriados_override` table / migration 009 (Slice B).
- Document generation, e-signature, non-ejecutivo procedures.
- Exact prescripción dates (only an "approaching" flag — interrupting acts need legal interpretation), auto-filing any escrito.

## Capabilities

### New Capabilities
- `procedural-deadlines`: procedural state machine, rules-based movement classifier, días-hábiles deadline engine, semáforo computation, and the abogado-facing deadlines API.

### Modified Capabilities
- None at the spec level. The `detect_and_sync_movements` hook injection is an implementation integration point, not a behavior change to existing sync specs.

## Approach

A stateful **rules-based classifier** walks each case's movements chronologically (config-driven `ClassifierRule` list — transparent and legally auditable, unlike ML). State transitions emit deadline triggers. The **DeadlineEngine** computes due dates via a `holidays`-backed `add_business_days`, persists `case_deadlines` rows (UNIQUE prevents dupes), and caches `procedural_state/semaforo/next_deadline_at` on the case for fast `ORDER BY criticidad`. The **semáforo** derives from the nearest active deadline (ROJO ≤1 biz day/expired, AMARILLO 2–5, VERDE >5, GRIS when INDETERMINATE or non-civil). Recompute runs synchronously inside the existing per-case sync loop — no new workers. Unknown/incomplete histories fail safe to GRIS, never a false ROJO.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `app/services/deadline_engine.py` | New | Classifier + recompute_case + semáforo |
| `app/services/business_days.py` | New | Días hábiles via `holidays` (Chile) |
| `app/models/case_deadline.py` | New | CaseDeadline ORM model |
| `app/models/case.py` | Modified | `procedural_state`, `semaforo`, `next_deadline_at` |
| `alembic/versions/008_*` | New | case_deadlines table + Case columns |
| `app/services/sync_service.py` | Modified | Hook recompute after movement sync |
| `app/api/v1/cases.py` | Modified | Semáforo fields + `sort_by=criticidad` |
| `app/api/v1/deadlines.py` | New | `GET /cases/{id}/deadlines` |
| `pyproject.toml` | Modified | Add `holidays` dependency |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Días-hábiles/feriados error = legal liability | High | Exhaustive unit tests per deadline type; mandatory disclaimer in every response; ADVISORY UI label; lawyer review before prod |
| Classifier accuracy on incomplete histories | High | INDETERMINATE→GRIS safe-fail; validate vs 50 real cases |
| Prescripción needs legal interpretation | Med | Only an "approaching" flag, never exact dates |
| Migration-number clash with prospect-search tracker (also uses 008/009) | Med | This change targets main where 008 is free; coordinate numbering before merging either branch |
| PJUD changes movement description text | Med | Config-driven rule list is cheap to update |

## Rollback Plan

Revert via `alembic downgrade` of migration 008 (drops `case_deadlines` + the three Case columns) and revert the code PRs. The sync hook is additive and guarded; removing it leaves movement sync unchanged. No data loss — deadlines are derived and recomputable on next sync.

## Dependencies

- PyPI `holidays` library (Chile calendar) added to `pyproject.toml`.
- Built on `main` (latest migration 007 → this adds 008).
- ~50 QA-scraped real cases for classifier/engine validation.

## Delivery (Slice A = 2 chained PRs, feature-branch-chain)

- **PR1**: engine + classifier + migration 008 + sync hook.
- **PR2**: API — `GET /cases/{id}/deadlines` + extended `GET /cases` (semáforo + `sort_by=criticidad`).

## Success Criteria

- [ ] Classifier states the correct current stage for every validated real case.
- [ ] Each active deadline matches a manually-verified due date.
- [ ] Semáforo sorts the abogado's worklist by real criticality.
- [ ] The 8-day excepciones plazo fatal is never silently missed.
- [ ] Mandatory advisory disclaimer present in every deadline response.
</content>
</invoke>
