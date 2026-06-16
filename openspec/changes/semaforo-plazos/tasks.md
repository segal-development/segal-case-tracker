# Tasks: Semáforo de Plazos — Procedural Deadline Engine (Slice A)

## Open-Question Resolutions

| Question | Decision |
|---|---|
| REBELDÍA trigger | Sync-driven: recompute checks `today > EXCEPCIONES_8D.due_date` AND no EXCEPCIONES movement → REBELDE; flips on the next sync that touches the case (no separate daily job in Slice A) |
| OBSERVACIONES_PRUEBA_6D trigger | Computed off `TERMINO_PROBATORIO.due_date` (a computed date, not a PJUD movement event) |
| Pagaré prescripción | No `instrument_type` column in Slice A → use 3-year acción ejecutiva default (art. 2515 CC); add `# TODO: Slice B — differentiate pagaré (1y, art. 98 Ley 18.092) via instrument_type` |
| LISTA_TESTIGOS_2D | Stub in `DeadlineType` enum only; secondary deadline computation deferred to Slice B |
| DEADLINE_DISCLAIMER | Module constant: `"Este cálculo es orientativo y no reemplaza el criterio del abogado."` — mandatory field in every `GET /cases/{id}/deadlines` response |

---

## Review Workload Forecast

| Field | Value |
|---|---|
| Estimated changed lines | PR1: ~360–415 lines; PR2: ~190–235 lines |
| 400-line budget risk | PR1: Medium (edge — tests push toward upper bound); PR2: Low |
| Chained PRs recommended | Yes |
| Suggested split | PR1 (`feat/semaforo-plazos-s1` → tracker) then PR2 (`feat/semaforo-plazos-s2` → PR1 branch) |
| Delivery strategy | feature-branch-chain (resolved) |
| Chain strategy | feature-branch-chain |

Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: feature-branch-chain
400-line budget risk: Medium

### PR Boundary Names (feature-branch-chain)

| Branch | Targets | Scope |
|---|---|---|
| `feat/semaforo-plazos` | `main` | Tracker — only this merges to main |
| `feat/semaforo-plazos-s1` | `feat/semaforo-plazos` | PR1: engine + classifier + migration + sync hook |
| `feat/semaforo-plazos-s2` | `feat/semaforo-plazos-s1` | PR2: API — deadlines endpoint + cases list extension |

> Migration coordination: `008` is free on `main` (latest = `007`). If the `prospect-search` branch merges first, renumber this branch's migration to `009` and update `down_revision` accordingly.

### Suggested Work Units

| Unit | Goal | Base branch | Notes |
|---|---|---|---|
| PR1 | Engine + classifier + migration 008 + sync hook + all unit/integration/migration tests | `feat/semaforo-plazos` (tracker) | Verify gate: `pytest -m "not integration"` + migration round-trip + `mypy app/core` |
| PR2 | Deadlines endpoint + cases list extension + API tests | `feat/semaforo-plazos-s1` | Verify gate: `pytest -m "not integration"` + `mypy app/core` |

---

## Phase 1 — Infrastructure / Foundation (PR1)

Tasks T1.1, T1.2, T1.3 are **fully parallel** (no dependencies between them).

- [ ] **T1.1** Add `holidays = "^0.58"` to `pyproject.toml` and run `poetry lock`. [REQ-2]
  - Files: `pyproject.toml`, `poetry.lock`

- [ ] **T1.2** Create `app/core/deadlines_config.py`: `ProceduralState` enum (MANDAMIENTO, NOTIFICADO, EXCEPCIONES, TRASLADO_EJECUTANTE, ADMISIBILIDAD, AUTO_PRUEBA, CITACION_SENTENCIA, TERMINADA, REBELDE, INDETERMINATE), `ProcEvent` enum (8 events), `DeadlineType` enum with `(dias_habiles: int, legal_basis: str)` payload (all 6 + LISTA_TESTIGOS_2D stub), `ClassifierRule` dataclass (description_regex, stage_regex, event, next_state, starts_deadline_type, priority), ordered `CLASSIFIER_RULES` list (8 rules using real-data regexes from design), `DEADLINE_DISCLAIMER` constant. [REQ-1, REQ-2, REQ-7, REQ-10]
  - Files: `app/core/deadlines_config.py`
  - Note: LISTA_TESTIGOS_2D in enum only; exclude from engine computation in Slice A

- [ ] **T1.3** Create `app/models/case_deadline.py`: `CaseDeadline` SQLAlchemy model — columns `id`, `case_id` (FK→cases), `deadline_type VARCHAR(50)`, `legal_basis VARCHAR(100)`, `due_date DATE`, `triggered_at DATE`, `status VARCHAR(20) DEFAULT 'active'` (active|met|expired|superseded), `source_movement_id` (FK→movements nullable), `computed_at DATETIME`, `created_at DATETIME`; unique constraint `(case_id, deadline_type, triggered_at)`; indexes on `case_id` and `due_date`. [REQ-9]
  - Files: `app/models/case_deadline.py`

- [ ] **T1.4** [depends: T1.3] Modify `app/models/case.py`: add `procedural_state = Column(String(30), nullable=True)`, `semaforo = Column(String(10), nullable=True)`, `next_deadline_at = Column(Date, nullable=True)` + `deadlines` relationship to `CaseDeadline`. [REQ-9]
  - Files: `app/models/case.py`

- [ ] **T1.5** [depends: T1.3] Register `CaseDeadline` in `app/models/__init__.py` import block so Alembic autogenerate sees it. [REQ-9]
  - Files: `app/models/__init__.py`

- [ ] **T1.6** [depends: T1.4, T1.5] Create `alembic/versions/008_add_case_deadlines.py` (`revision="008"`, `down_revision="007"`): upgrade — `create_table("case_deadlines", …)` + UNIQUE constraint + `ix_case_deadlines_case_id` + `ix_case_deadlines_due_date` + `add_column("cases", procedural_state)` + `add_column("cases", semaforo)` + `add_column("cases", next_deadline_at)` + `create_index("ix_cases_next_deadline_at")` + `create_index("ix_cases_semaforo")`; downgrade drops indexes → drops `case_deadlines` → drops 3 Case columns. [REQ-9]
  - Files: `alembic/versions/008_add_case_deadlines.py`
  - Coordination note: renumber to `009` / update `down_revision` if `prospect-search` merges first

---

## Phase 2 — Core Logic, Test-First (PR1)

T2.1 and T2.3 are **parallel** (both depend only on Phase 1 tasks).

- [ ] **T2.1 [RED — CRITICAL]** [depends: T1.1, T1.2] Write `tests/unit/test_business_days.py`: parametrized `add_business_days` covering all 6 `DeadlineType` day counts; feriado-crossing scenarios: Sept 18-19 Fiestas Patrias (→ REQ-2 spec scenario: trigger 2026-09-10, expected 2026-09-23), Viernes Santo / Semana Santa, Jan 1 year boundary; day-after-event start confirmed; `count_business_days_remaining` across ROJO/AMARILLO/VERDE thresholds. **CRITICAL test area — días-hábiles error = legal liability.** [REQ-2]
  - Asserting tests: `test_excepciones_8d_crosses_fiestas_patrias`, `test_apelacion_5d_standard`, `test_traslado_4d_crosses_weekend`, `test_termino_probatorio_10d_crosses_semana_santa`, `test_count_remaining_rojo_due_today`, `test_count_remaining_amarillo_3d`, `test_day_after_trigger_is_day_one`

- [ ] **T2.2 [GREEN]** [depends: T2.1] Create `app/services/business_days.py`: `add_business_days(start: date, n: int, country: str = "CL") -> date` (skip weekends + `holidays.Chile()` entries); `count_business_days_remaining(due_date: date, today: date) -> int` (negative when past due). [REQ-2]
  - Files: `app/services/business_days.py`

- [ ] **T2.3 [FIXTURE — CRITICAL]** [depends: T1.2] Create `tests/fixtures/real_case_movements.py`: encode ~50 QA-scraped movement sequences as `RealCaseFixture(case_id, movements, expected_state, expected_active_deadline_type)` named tuples or dataclasses; cover all deterministic states + mixed/incomplete histories → INDETERMINATE. **CRITICAL test area — classifier accuracy = legal liability.** [REQ-11]
  - Files: `tests/fixtures/real_case_movements.py`

- [ ] **T2.4 [RED — CRITICAL]** [depends: T2.3] Write `tests/unit/test_procedural_classifier.py`: all REQ-1 state scenarios (all 8 ProceduralStates); REBELDÍA fires: NOTIFICADO + 8d elapsed + no excepciones → REBELDE (REQ-3); REBELDÍA NOT fired when excepciones movement present (REQ-3); competencia guard: non-civil input → INDETERMINATE (REQ-10); unknown sequence → INDETERMINATE, no exception (REQ-10, REQ-11); parametrized real-cases fixture from T2.3 (REQ-11). **CRITICAL test area.** [REQ-1, REQ-3, REQ-10, REQ-11]
  - Asserting tests: `test_notificado_triggers_excepciones_8d`, `test_excepciones_stage_triggers_traslado_4d`, `test_auto_prueba_triggers_termino_probatorio_10d`, `test_terminada_no_deadlines`, `test_empty_history_is_indeterminate`, `test_rebeldia_fires_after_8d_no_excepciones`, `test_rebeldia_not_fired_when_excepciones_present`, `test_unknown_sequence_is_indeterminate_not_crash`, `test_real_case_movements[case_N]` (parametrized)

- [ ] **T2.5 [GREEN]** [depends: T2.4, T1.2] Create `app/services/procedural_classifier.py`: `MovementClassifier.classify(movements: list[Movement], today: date) -> tuple[ProceduralState, dict[DeadlineType, Movement | date]]` — compile `CLASSIFIER_RULES` once, walk movements ASC, apply highest-priority matching rule per movement (last wins on tie), REBELDÍA computed transition post-walk. [REQ-1, REQ-3, REQ-10]
  - Files: `app/services/procedural_classifier.py`
  - Note: OBSERVACIONES_PRUEBA_6D trigger = `TERMINO_PROBATORIO.due_date` stored as `date` in triggers dict (computed date, no movement source)
  - Note: REBELDÍA = after walk, if state==NOTIFICADO AND today > computed EXCEPCIONES_8D due_date AND no EXCEPCIONES movement seen → state=REBELDE
  - Note: any exception in classify → log + return (INDETERMINATE, {}) — fail-safe, no partial state

- [ ] **T2.6 [RED]** [depends: T1.2, T1.3, T1.4, T2.2, T2.5] Write `tests/unit/test_deadline_engine.py`: semáforo thresholds (ROJO ≤1 biz day, ROJO expired, AMARILLO 2–5d, VERDE >5d, GRIS no active deadlines, GRIS INDETERMINATE — REQ-5); abandono_risk: none <4.5m, approaching 5m, presumible 7m, post-REBELDE window 2.5–3y (REQ-4); prescripcion_risk: none 1y, approaching 2.5y, at_risk 3y (3-year acción default, REQ-4); GRIS for non-civil (REQ-10); INDETERMINATE never ROJO (REQ-10). [REQ-3, REQ-4, REQ-5, REQ-10]
  - Asserting tests: `test_semaforo_rojo_due_today`, `test_semaforo_rojo_expired_yesterday`, `test_semaforo_rojo_1d_remaining`, `test_semaforo_amarillo_3d`, `test_semaforo_verde_10d`, `test_semaforo_gris_no_deadlines`, `test_semaforo_gris_indeterminate_ignores_stale_deadline`, `test_abandono_approaching_5_months`, `test_abandono_presumible_7_months`, `test_abandono_post_rebeldia_approaching`, `test_prescripcion_approaching_2_5_years`, `test_prescripcion_at_risk_3_years`, `test_non_civil_yields_gris`, `test_indeterminate_never_produces_rojo`

- [ ] **T2.7 [GREEN]** [depends: T2.6, T1.6] Create `app/services/deadline_engine.py`: `DeadlineEngine.recompute_case(db: Session, case: Case) -> None` — 8-step pipeline: (1) load movements ASC by movement_date; (2) guard competencia=="civil" else write GRIS+return; (3) classify via MovementClassifier; (4) per trigger: compute `due_date=add_business_days(trigger_date, n)`, UPSERT `case_deadlines` on `(case_id, deadline_type, triggered_at)`; (5) mark superseded rows; (6) REBELDÍA post-transition: set `case_deadline.status="expired"` for EXCEPCIONES_8D; (7) compute semáforo from nearest active deadline + abandono/prescripción flags; (8) write `case.procedural_state`, `case.semaforo`, `case.next_deadline_at`; flush (do not commit). Never raises — wrap step 3–7 in try/except, log, set GRIS fallback. [REQ-3, REQ-4, REQ-5, REQ-6, REQ-10]
  - Files: `app/services/deadline_engine.py`
  - Note: add `# TODO: Slice B — differentiate pagaré (1y prescripción, art. 98 Ley 18.092) via Case.instrument_type`

---

## Phase 3 — Integration Tests (PR1) [T3.1 ∥ T3.2 — parallel]

- [ ] **T3.1 [RED+GREEN]** [depends: T2.7, T1.6] Write `tests/integration/test_deadline_engine_integration.py`: SQLite in-memory session; `recompute_case` persists CaseDeadline rows and updates Case columns; UNIQUE upsert idempotency — call recompute twice, assert no duplicate `case_deadlines` rows; non-civil case → `semaforo="gris"` and `procedural_state="indeterminate"` persisted. [REQ-6, REQ-9]
  - Files: `tests/integration/test_deadline_engine_integration.py`
  - Markers: `@pytest.mark.integration`

- [ ] **T3.2 [RED+GREEN]** [depends: T1.6] Write `tests/integration/test_migration_008.py`: upgrade 007→008 (assert `case_deadlines` table exists, assert 3 Case columns exist); downgrade 008→007 (assert table gone, columns removed, pre-existing case rows intact); re-upgrade succeeds. [REQ-9]
  - Files: `tests/integration/test_migration_008.py`
  - Markers: `@pytest.mark.integration`

---

## Phase 4 — Sync Hook Wiring (PR1) [sequential: T4.1 → T4.2; T4.1 ∥ T3.x]

- [ ] **T4.1 [RED]** [depends: T2.7] Write `tests/unit/test_sync_hook.py`: mock `DeadlineEngine`; assert `recompute_case` called once per civil case after `sync_movements` completes; assert NOT called when `competencia != "civil"`; assert sync continues (no exception propagated) if `recompute_case` raises `RuntimeError`. [REQ-6]
  - Files: `tests/unit/test_sync_hook.py`

- [ ] **T4.2 [GREEN]** [depends: T4.1] Modify `app/services/sync_service.py`: in `detect_and_sync_movements._do_fetch`, after `sync_svc.sync_movements(...)` call (~line 1387), within existing try/transaction block, add: `from app.services.deadline_engine import DeadlineEngine; if db_case and getattr(db_case, "competencia", "civil") == "civil": try: DeadlineEngine.recompute_case(db, db_case) except Exception: logger.exception("deadline recompute failed for case %s", db_case.id)`. Flush-only; outer `db.commit()` persists; existing `except: db.rollback()` discards on failure. [REQ-6]
  - Files: `app/services/sync_service.py`

---

## Phase 5 — API Endpoints (PR2) [T5.1 ∥ T5.3 — parallel]

> Branch base: `feat/semaforo-plazos-s1`. Do NOT target tracker or main.

- [ ] **T5.1 [RED]** Write `tests/api/test_deadlines_endpoint.py`: happy path GET 200 with full schema (procedural_state, semaforo, active_deadlines[], abandono_risk, prescripcion_risk, disclaimer); disclaimer contains literal `"no reemplaza el criterio del abogado"`; AMARILLO scenario (NOTIFICADO + 3d remaining → semaforo=="amarillo"); 404 for unowned case (lawyer B owns it); 401 unauthenticated. [REQ-7, REQ-10]
  - Asserting tests: `test_deadlines_happy_path_notificado_amarillo`, `test_disclaimer_always_present_and_non_empty`, `test_deadlines_404_unowned_case`, `test_deadlines_401_unauthenticated`, `test_active_deadlines_legal_basis_field`

- [ ] **T5.2 [GREEN]** [depends: T5.1] Create `app/api/v1/deadlines.py`: router `GET /cases/{id}/deadlines`; reuse `_resolve_lawyer_id` + `Case.lawyer_id` ownership check (raise HTTPException 404 on miss); load case_deadlines where status=="active"; compute `dias_habiles_remaining` per deadline; inject `DEADLINE_DISCLAIMER` from `app.core.deadlines_config` in every response body. [REQ-7, REQ-10]
  - Files: `app/api/v1/deadlines.py`

- [ ] **T5.3 [RED]** Write `tests/api/test_cases_deadlines_extension.py`: `GET /cases` response items contain `procedural_state`, `semaforo`, `next_deadline_at` (may be null); `GET /cases?sort_by=criticidad` returns order [earliest next_deadline_at, …, null LAST]; default sort (`?sort_by=updated_at`) unchanged. [REQ-8]
  - Asserting tests: `test_cases_list_contains_deadline_fields`, `test_sort_by_criticidad_nulls_last`, `test_default_sort_unchanged`

- [ ] **T5.4 [GREEN]** [depends: T5.3] Extend `app/api/v1/cases.py` and `app/schemas/case.py`: add `procedural_state: str | None`, `semaforo: str | None`, `next_deadline_at: date | None` to `CaseResponse`; add query param `sort_by: Literal["criticidad", "updated_at"] = "updated_at"`; when `sort_by=="criticidad"` → `ORDER BY cases.next_deadline_at ASC NULLS LAST`. [REQ-8]
  - Files: `app/api/v1/cases.py`, `app/schemas/case.py`

- [ ] **T5.5** [depends: T5.2] Register deadlines router in `app/api/v1/router.py`: `from app.api.v1.deadlines import router as deadlines_router` + `api_router.include_router(deadlines_router, prefix="/cases", tags=["deadlines"])`. [REQ-7]
  - Files: `app/api/v1/router.py`

---

## Dependency Graph Summary

```
T1.1 ──────────────────────────┐
T1.2 ─────────────────────┐    ├─ T2.1 RED ─ T2.2 GREEN
T1.3 ──┬─ T1.4 ──┬─ T1.6 ─┤    │
        │         │         └─ T2.3 FIXTURE ─ T2.4 RED ─ T2.5 GREEN ─┐
        └─ T1.5 ──┘                                                    ├─ T2.6 RED ─ T2.7 GREEN
                                                      T2.2 GREEN ──────┘
T2.7 GREEN ──┬─ T3.1 RED+GREEN (∥)                   T1.6 ──┘
              └─ T4.1 RED ─ T4.2 GREEN (∥ with T3.x)
T1.6 ──────── T3.2 RED+GREEN (∥ with T3.1)

── PR1 complete ──────────────────────────────────────────────

T5.1 RED (∥)─ T5.2 GREEN ─ T5.5
T5.3 RED (∥)─ T5.4 GREEN
```

## Task Summary

| Phase | Tasks | PR | Focus |
|---|---|---|---|
| 1 | T1.1–T1.6 (6) | PR1 | Infrastructure: deps, models, migration, config |
| 2 | T2.1–T2.7 (7) | PR1 | Core logic: días-hábiles, classifier, engine (TDD) |
| 3 | T3.1–T3.2 (2) | PR1 | Integration: persistence + migration round-trip |
| 4 | T4.1–T4.2 (2) | PR1 | Sync hook wiring |
| 5 | T5.1–T5.5 (5) | PR2 | API: deadlines endpoint + cases list extension |
| **Total** | **22** | — | — |

## Verify Gate (per PR, before push)

```bash
.venv/bin/python -m pytest -m "not integration" -x
mypy app/core
# For PR1 only:
alembic upgrade 008 && alembic downgrade 007 && alembic upgrade 008
```
