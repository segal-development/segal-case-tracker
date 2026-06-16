# Procedural Deadlines Specification

## Purpose

Define behavior for the `procedural-deadlines` capability (Slice A, juicio ejecutivo): classifying PJUD movements into a `ProceduralState`, computing legal deadlines in días hábiles (excluding weekends and Chilean feriados), assigning a semáforo color, surfacing abandono/prescripción risk flags, and exposing an abogado-facing API. All output is advisory; a mandatory legal disclaimer MUST accompany every deadline response. Only cases where `competencia == "civil"` are processed.

---

## Requirements

### Requirement: REQ-1 — Movement→State Classification

The system MUST walk a case's movements in chronological order and apply a priority-ordered rule list to derive the current `ProceduralState`. When no rule matches, or the movement history is empty, the state MUST be `INDETERMINATE`.

**State table (juicio ejecutivo, CPC + Ley 21.394):**

| State | Trigger rule (description regex / stage regex) |
|---|---|
| `MANDAMIENTO` | description ≈ `[Oo]rdena despachar mandamiento` |
| `NOTIFICADO` | description ≈ `(NOTIFICACI[ÓO]N DE DEMANDA\|Notificaci[oó]n demanda).*(Exitosa\|exitosa)` |
| `EXCEPCIONES` | stage ≈ `Excepciones` |
| `TRASLADO_EJECUTANTE` | stage ≈ `Contestaci[oó]n Excepciones` |
| `ADMISIBILIDAD` | description ≈ `[Ss]e pronuncia sobre admisibilidad` |
| `AUTO_PRUEBA` | description ≈ `Notificaci[oó]n resoluci[oó]n que recibe.*(prueba\|Prueba)` |
| `CITACION_SENTENCIA` | description ≈ `Cita a Audiencia` |
| `TERMINADA` | stage ≈ `Terminada` |
| `INDETERMINATE` | no rule matched / empty history |

#### Scenario: Successful notification → NOTIFICADO

- GIVEN a case with a movement `description="NOTIFICACIÓN DE DEMANDA (Exitosa)"`, stage `"Gestión Preparatoria"`
- WHEN the classifier processes the movement list
- THEN `procedural_state == NOTIFICADO` and deadline `EXCEPCIONES_8D` is triggered

#### Scenario: Excepciones stage → EXCEPCIONES

- GIVEN a movement with `stage="Excepciones"` following NOTIFICADO
- WHEN classified
- THEN `procedural_state == EXCEPCIONES` and deadline `TRASLADO_EJECUTANTE_4D` is triggered

#### Scenario: Auto de prueba description → AUTO_PRUEBA

- GIVEN a movement `description="Notificación resolución que recibe la causa a prueba"` (any stage)
- WHEN classified
- THEN `procedural_state == AUTO_PRUEBA` and deadline `TERMINO_PROBATORIO_10D` is triggered

#### Scenario: Stage "Terminada" → TERMINADA

- GIVEN a movement with `stage="Terminada"`
- WHEN classified
- THEN `procedural_state == TERMINADA` and no new deadline is triggered

#### Scenario: Empty history → INDETERMINATE

- GIVEN a case with zero movements OR all movements matching no rule
- WHEN classified
- THEN `procedural_state == INDETERMINATE`

---

### Requirement: REQ-2 — Días Hábiles Deadline Computation

The system MUST compute `due_date = trigger_movement_date + N días hábiles`, where counting starts the day AFTER the trigger date and excludes weekends and Chilean feriados (via the `holidays` library, `holidays.Chile()`).

**Deadline table:**

| DeadlineType | días hábiles | CPC article | Trigger |
|---|---|---|---|
| `EXCEPCIONES_8D` | 8 | art. 459 (FATAL) | `NOTIFICACION_EXITOSA` |
| `TRASLADO_EJECUTANTE_4D` | 4 | art. 466 | `EXCEPCIONES_OPUESTAS` |
| `TERMINO_PROBATORIO_10D` | 10 | art. 468 | `AUTO_PRUEBA` |
| `OBSERVACIONES_PRUEBA_6D` | 6 | art. 469 | end of `TERMINO_PROBATORIO` |
| `SENTENCIA_10D` | 10 | arts. 162 / 470 | `CITACION_SENTENCIA` |
| `APELACION_5D` | 5 | arts. 187 / 475 | `SENTENCIA` |

#### Scenario: EXCEPCIONES_8D from Sept 10 2026 (crosses Sept 18-19 feriados)

- GIVEN trigger_date = 2026-09-10 (Thursday), `EXCEPCIONES_8D` rule
- WHEN `add_business_days(date(2026, 9, 10), 8)` is called
- THEN `due_date == date(2026, 9, 23)` (Sept 18 Fiestas Patrias and Sept 19 Glorias del Ejército excluded)

#### Scenario: APELACION_5D standard computation

- GIVEN trigger_date = 2026-06-16 (Tuesday), `APELACION_5D` rule
- WHEN computed
- THEN `due_date == date(2026, 6, 23)` (5 business days, no intervening holiday)

#### Scenario: TRASLADO_EJECUTANTE_4D

- GIVEN trigger_date = 2026-07-01, `TRASLADO_EJECUTANTE_4D` rule
- WHEN computed
- THEN `due_date == date(2026, 7, 7)` (4 business days after July 1, skipping weekend July 4-5)

---

### Requirement: REQ-3 — REBELDÍA Computed Transition

The system MUST mark a case `REBELDE` when: `procedural_state == NOTIFICADO` AND ≥ 8 días hábiles have elapsed since the triggering `NOTIFICACION_EXITOSA` movement AND no movement matching the `EXCEPCIONES` rule has been recorded. When REBELDE, the mandamiento hace las veces de sentencia and the abandono window extends to 3 years.

#### Scenario: REBELDE fires after 8 days with no excepciones

- GIVEN `procedural_state == NOTIFICADO`, `EXCEPCIONES_8D.due_date` is in the past, and no EXCEPCIONES movement exists
- WHEN `recompute_case` runs
- THEN `procedural_state == REBELDE` and `EXCEPCIONES_8D.status == "expired"`

#### Scenario: REBELDE does NOT fire when excepciones exist

- GIVEN `procedural_state == NOTIFICADO` and a movement with `stage="Excepciones"` is present
- WHEN `recompute_case` runs
- THEN `procedural_state == EXCEPCIONES` (not REBELDE)

---

### Requirement: REQ-4 — Abandono and Prescripción Risk Flags

The system MUST compute `abandono_risk` and `prescripcion_risk` as advisory flags. It MUST NOT compute exact prescripción dates (interrupting acts require legal interpretation).

| Flag | Value | Condition |
|---|---|---|
| `abandono_risk` | `none` | `last_movement_at` < 4.5 months ago |
| `abandono_risk` | `approaching` | 4.5 months ≤ elapsed < 6 months |
| `abandono_risk` | `presumible` | elapsed ≥ 6 months (art. 152 CPC) |
| `abandono_risk` | `approaching` (post-REBELDE) | 2.5 years ≤ elapsed < 3 years (art. 153 inc. 2 CPC) |
| `prescripcion_risk` | `approaching` | `filed_at` age ≥ 2.5 years (3y acción ejecutiva, art. 2515 CC / 1y pagaré) |
| `prescripcion_risk` | `at_risk` | `filed_at` age ≥ 3 years |

#### Scenario: Abandono approaching (general)

- GIVEN `last_movement_at` = 5 months ago, case not REBELDE
- WHEN flags are computed
- THEN `abandono_risk == "approaching"`

#### Scenario: No prescripción flag on recent case

- GIVEN `filed_at` = 1 year ago
- WHEN flags computed
- THEN `prescripcion_risk == "none"`

---

### Requirement: REQ-5 — Semáforo Color

The system MUST assign `semaforo` based on the most critical (earliest `due_date`) active deadline's business days remaining from today.

| Color | Condition |
|---|---|
| `GRIS` | `procedural_state == INDETERMINATE` OR no active deadlines |
| `ROJO` | `biz_days_remaining <= 1` OR deadline status `expired` |
| `AMARILLO` | `2 <= biz_days_remaining <= 5` |
| `VERDE` | `biz_days_remaining > 5` |

#### Scenario: ROJO — expired deadline

- GIVEN most critical deadline `due_date` is yesterday, `status == "active"`
- WHEN semáforo computed today
- THEN `semaforo == ROJO`

#### Scenario: ROJO — due today (0 remaining biz days)

- GIVEN `due_date == today` → `biz_days_remaining == 0`
- WHEN computed
- THEN `semaforo == ROJO`

#### Scenario: AMARILLO — 3 días hábiles remaining

- GIVEN `biz_days_remaining == 3`
- WHEN computed
- THEN `semaforo == AMARILLO`

#### Scenario: VERDE — 10 días hábiles remaining

- GIVEN `biz_days_remaining == 10`
- WHEN computed
- THEN `semaforo == VERDE`

#### Scenario: GRIS — INDETERMINATE state

- GIVEN `procedural_state == INDETERMINATE`
- WHEN semáforo computed
- THEN `semaforo == GRIS` regardless of `case_deadlines` content

---

### Requirement: REQ-6 — Recompute on Sync

The system MUST invoke `DeadlineEngine.recompute_case(db, case)` after `sync_movements` completes in `detect_and_sync_movements`, within the same database transaction. `recompute_case` MUST update `case.procedural_state`, `case.semaforo`, and `case.next_deadline_at`.

#### Scenario: New movements trigger recompute

- GIVEN a sync run detects 1 or more new movements for a case
- WHEN `detect_and_sync_movements` completes
- THEN `case.procedural_state`, `case.semaforo`, and `case.next_deadline_at` reflect the post-sync movement history

#### Scenario: No new movements — fields unchanged

- GIVEN a sync run detects zero new movements
- WHEN `detect_and_sync_movements` completes
- THEN `case.semaforo` and `case.next_deadline_at` are unchanged

---

### Requirement: REQ-7 — GET /cases/{id}/deadlines

The system MUST expose `GET /cases/{id}/deadlines` returning: `procedural_state`, `semaforo`, `active_deadlines` (list with `type`, `label`, `due_date`, `triggered_at`, `dias_habiles_remaining`, `status`, and `legal_basis`), `proxima_accion`, `abandono_risk`, `prescripcion_risk`, and a `disclaimer` string. Authentication is required; the case MUST be owned by the requesting lawyer (404 if not).

#### Scenario: Happy path — NOTIFICADO with active EXCEPCIONES_8D

- GIVEN an authenticated lawyer owns case 42, `procedural_state == NOTIFICADO`, 3 biz days remain on `EXCEPCIONES_8D`
- WHEN `GET /cases/42/deadlines`
- THEN status 200, `semaforo == "amarillo"`, `active_deadlines[0].type == "excepciones_8d"`, `active_deadlines[0].legal_basis == "art. 459 CPC"`, `disclaimer` is non-empty

#### Scenario: Disclaimer is always present

- GIVEN any case state
- WHEN `GET /cases/{id}/deadlines` returns 200
- THEN `response.disclaimer` contains the string "no reemplaza el criterio del abogado"

#### Scenario: 404 for unowned case

- GIVEN lawyer A is authenticated, case 99 belongs to lawyer B
- WHEN `GET /cases/99/deadlines`
- THEN status 404

---

### Requirement: REQ-8 — GET /cases List Extension

The system MUST include `procedural_state`, `semaforo`, and `next_deadline_at` in each item of `GET /cases`. It MUST support query parameter `sort_by=criticidad`, which orders results by `next_deadline_at ASC NULLS LAST`.

#### Scenario: Fields present in list response

- GIVEN authenticated lawyer with 3 cases
- WHEN `GET /cases`
- THEN each item contains `procedural_state`, `semaforo`, `next_deadline_at` (may be null)

#### Scenario: sort_by=criticidad orders by urgency

- GIVEN cases with `next_deadline_at`: [null, 2026-06-20, 2026-06-18]
- WHEN `GET /cases?sort_by=criticidad`
- THEN order is [2026-06-18, 2026-06-20, null]

---

### Requirement: REQ-9 — Migration 008 (Round-Trip)

The system MUST create migration `008_*` that:

1. Adds columns `procedural_state VARCHAR(30)`, `semaforo VARCHAR(10)`, `next_deadline_at DATE` (all nullable) to `cases`.
2. Creates table `case_deadlines` with columns: `id SERIAL PK`, `case_id FK→cases(id)`, `deadline_type VARCHAR(50) NOT NULL`, `due_date DATE NOT NULL`, `status VARCHAR(20) NOT NULL DEFAULT 'active'`, `source_movement_id FK→movements(id) nullable`, `triggered_at DATE NOT NULL`, `computed_at DATETIME`, `created_at DATETIME`; UNIQUE constraint on `(case_id, deadline_type, triggered_at)`; indexes on `case_id` and `due_date`.

The migration MUST be reversible: `alembic downgrade` MUST drop `case_deadlines` and remove the three added columns from `cases` without data loss to pre-existing rows.

#### Scenario: Upgrade creates table and columns

- GIVEN a DB at revision 007
- WHEN `alembic upgrade 008`
- THEN `case_deadlines` table exists, `cases` has `procedural_state`, `semaforo`, `next_deadline_at`

#### Scenario: Downgrade restores DB to 007

- GIVEN a DB at revision 008
- WHEN `alembic downgrade 007`
- THEN `case_deadlines` table is gone, three columns removed from `cases`, pre-existing case rows intact

---

### Requirement: REQ-10 — Safety and Legal Guards

The system MUST enforce:

1. `competencia` guard: only cases where `case.competencia == "civil"` are processed by the classifier and engine. All other competencias MUST yield `procedural_state = INDETERMINATE` and `semaforo = GRIS` without raising an error.
2. `INDETERMINATE` → no `ROJO`: a case in state `INDETERMINATE` MUST NEVER receive `semaforo = ROJO`.
3. Every `GET /cases/{id}/deadlines` response MUST carry a non-empty `disclaimer` field.
4. The classifier MUST default to `INDETERMINATE` on any exception or unrecognized pattern (fail-safe, no partial state).

#### Scenario: Non-civil case → GRIS

- GIVEN `case.competencia == "laboral"`
- WHEN `recompute_case` runs
- THEN `procedural_state == INDETERMINATE` and `semaforo == GRIS`

#### Scenario: INDETERMINATE never produces ROJO

- GIVEN `procedural_state == INDETERMINATE` with a stale `case_deadline` row
- WHEN semáforo computed
- THEN `semaforo == GRIS` (not ROJO)

---

### Requirement: REQ-11 — Classifier Validation Against Real Cases

The system MUST include a pytest fixture (`tests/fixtures/real_case_movements.py` or equivalent) encoding representative movement sequences from the ~50 QA-scraped real cases. A parametrized test MUST assert expected `procedural_state` and, where deterministic, expected active `DeadlineType` for each fixture entry.

#### Scenario: Fixture test passes on all known real cases

- GIVEN the fixture list of (movement_sequence, expected_state, expected_deadline_type_or_None) tuples
- WHEN the classifier processes each sequence
- THEN all assertions pass (0 unexpected INDETERMINATE on cases with deterministic histories)

#### Scenario: Unknown movement sequence → INDETERMINATE (not a crash)

- GIVEN a movement sequence with no matching classifier rule
- WHEN classified
- THEN result is `INDETERMINATE`, no exception raised
