# Tasks: causa-detail-full-scrape

Ordered test-first task checklist derived from spec (obs#518) + design (obs#519).
Each task: **failing test → implement → green**, then included in its slice verification gate.
Strict TDD runner: `.venv/bin/python -m pytest`
Non-integration gate: `.venv/bin/python -m pytest -m "not integration"`
Mypy gate: `.venv/bin/python -m mypy app/core`

---

## Review Workload Forecast

```
Slice 1 (parsing + models + migration + scoping fix, no notifications)
  Code lines:         ~870  (base.py, civil.py, civil.yaml, 4 models, alert.py,
                             case.py, __init__.py, alembic migration, sync_service.py,
                             parser tests, idempotency tests)
  HTML fixture lines: ~700  (detail_rich_C-1253-2015.html + detail_synthetic_full.html)
  Slice 1 diff total: ~1570 lines

Slice 2 (change detection + notifications)
  Code lines:         ~380  (sync_service.py, notification_service.py, L2/L3 tests)
  Slice 2 diff total: ~380  lines

Total estimated diff: ~1950 lines (~1250 code + ~700 HTML fixtures)

Chained PRs recommended: Yes
400-line budget risk:    High (Slice 1 code alone ~870 lines; total ~1570 with fixtures)
Decision needed before apply: Yes — confirm chain strategy before starting apply:
  · stacked-to-main: Slice 1 PR → main; Slice 2 PR → main
  · feature-branch-chain: tracker PR (feat/notifications-pipeline) stays open;
    Slice 1 PR targets tracker; Slice 2 PR targets Slice 1 branch
Note: HTML fixture files (~700 lines) inflate the diff but are data, not code.
      Ask maintainer whether to accept size:exception on Slice 1 or split parsers
      from models/migration into two PRs if reviewer capacity is limited.
```

---

## Slice 1 — Parsing + Models + Migration + Scoping Fix

**Rollback boundary:** drop the 4 new tables + 2 alert columns via migration downgrade.
No existing behavior is altered; Alert/Movement paths are untouched.
**Shippable alone:** data is queryable; no notifications fire.

---

### S1-T01 — HTML test fixture files  *(no deps — can run parallel with S1-T02)*

**Spec:** Tab-Scoped Parsing, Litigante Parsing, Exhorto Parsing (test data prerequisite)
**Status:** DONE (Slice 1a, commit 0e0a0f3)
**Work:**
- [x] Create `tests/fixtures/` and `tests/fixtures/pjud/` directories with `__init__.py` files
- [x] Commit `/tmp/detail_rich.html` content as `tests/fixtures/pjud/detail_civil_rich.html`
      (real PJUD: 3 movements in #historiaCiv, 6 litigantes in #litigantesCiv,
       0 notificaciones, 0 escritos, 1 exhorto in #exhortosCiv)
- [x] Build `tests/fixtures/pjud/detail_civil_synthetic.html` by editing the rich fixture to add:
      - 2 notificacion `<tr>` rows inside `#notificacionesCiv tbody`
      - 2 escrito `<tr>` rows inside `#escritosCiv tbody` (row 1 has dtaDoc, row 2 does not)
      (used for L1 notif/escrito row-count tests and all L2 change-detection tests)
- [x] Add `tests/fixtures/pjud/conftest.py` with `@pytest.fixture` helpers
      `rich_html()` and `synthetic_html()` that read the fixture files by path

**Verification:** files exist and are readable; `synthetic_html` contains target pane content.

---

### S1-T02 — `base.py`: 4 scraper dataclasses + `PJUDCaseDetail` extension  *(parallel with S1-T01)*

**Spec:** Litigante Parsing, Notificacion Parsing, Escrito Parsing, Exhorto Parsing
**Status:** DONE (Slice 1a, commit 9e10f44)
**Work:**
- [x] Write a failing mypy-typed test in `tests/test_pjud_base.py` asserting the four new
      dataclasses (`PJUDLitigante`, `PJUDNotificacion`, `PJUDEscrito`, `PJUDExhorto`) exist
      and that `PJUDCaseDetail` has `.litigantes`, `.notificaciones`, `.escritos`, `.exhortos`
      defaulting to empty lists — **red**
- [x] Add to `app/scrapper/pjud/base.py`:
      - `PJUDLitigante(participante, rut, persona_type, nombre)`
      - `PJUDNotificacion(rol, estado_notif, tipo_notif, fecha_tramite, tipo_participante,
                          nombre, tramite, obs_fallida)`
      - `PJUDEscrito(fecha_ingreso, tipo_escrito, solicitante, tiene_documento, tiene_anexo,
                     doc_token=None)`
      - `PJUDExhorto(rol_origen, tipo_exhorto, rol_destino, fecha_ordena, fecha_ingreso,
                     tribunal_destino, estado, detalle_token=None)`
      - Extend `PJUDCaseDetail` with 4 additive fields (default_factory=list)
      (`partes` left in place, not removed)
- [x] Run test — **green**
- [x] `mypy app/core` — clean

---

### S1-T03 — Scoping regression test + `_extract_pane_tbody` + movements re-scope  *(needs S1-T01, S1-T02)*

**Status:** DONE (Slice 1a, commit bd8b316)
**Spec:** "Movements Parser Scoped to #historiaCiv"; "Tab-Scoped Parsing" (regression guard)
**Work:**
- [x] Write failing test in `tests/scrapper/pjud/test_civil_parsers.py`:
      Craft an HTML string where `#litigantesCiv` appears BEFORE `#historiaCiv`;
      assert `_parse_movements_table` returns ONLY rows from `#historiaCiv` — **red**
      (this FAILS against the old unscoped selector, proving the bug)
- [x] Add `_extract_pane_tbody(self, html: str, pane_id: str) -> Optional[str]` to
      `app/scrapper/pjud/civil.py`; scopes to `id="pane_id"` before matching
      the first `table-bordered tbody`
- [x] Refactor `_parse_movements_table` to call `_extract_pane_tbody(html, "historiaCiv")`
      instead of the current full-doc table match
- [x] Update `app/scrapper/pjud/selectors/civil.yaml`:
      - Add `movements_pane_id: primary: "historiaCiv"` entry
      - Add `pane_table_pattern` (parametrizable by pane id) for future parsers
      - Add `entity_row_pattern` and `exhorto_detail_token_pattern`
      - Keep old `movements_table` unscoped pattern as `fallback` under the existing key
        (zero-regression: if PJUD drops the pane id, code degrades to today's behavior)
- [x] Run regression test — **green**
- [x] Existing movement tests still pass (zero regression)

---

### S1-T04 — Litigante parser  *(needs S1-T03)*

**Status:** DONE (Slice 1a, commit bd8b316)
**Spec:** "Litigante Parsing" — all BDD scenarios
**Work:**
- [ ] In `tests/scrapper/pjud/test_civil_parsers.py` add:
      - `test_parse_litigantes_returns_6_rows`: parse rich fixture → 6 `PJUDLitigante`
      - `test_parse_litigantes_first_row_fields`: assert DTE. / 81826800-9 / JURIDICA /
        CAJA DE COMPENSACION... (with whitespace stripped)
      - `test_parse_litigantes_empty_tbody_returns_empty_list`: hand-crafted empty-tbody HTML
      — **red** for all three
- [ ] Implement `_parse_litigantes_table(self, html: str) -> List[PJUDLitigante]`
      in `civil.py` using `_extract_pane_tbody(html, "litigantesCiv")`
- [ ] Run tests — **green**

---

### S1-T05 — Exhorto parser  *(needs S1-T03, parallel with S1-T04)*

**Status:** DONE (Slice 1a, commit bd8b316)
**Spec:** "Exhorto Parsing" — all BDD scenarios
**Work:**
- [ ] In `tests/scrapper/pjud/test_civil_parsers.py` add:
      - `test_parse_exhortos_returns_1_row`: parse rich fixture → 1 `PJUDExhorto`
      - `test_parse_exhortos_rol_destino_label_stripped`: assert `rol_destino == "E-355-2026"`
        (text extracted from `<label onclick=detalleExhortosCivil(...)>E-355-2026</label>`)
      - `test_parse_exhortos_detalle_token_captured`: assert `detalle_token` is not None/empty
      - `test_parse_exhortos_tribunal_trimmed`: trailing spaces stripped
      - `test_parse_exhortos_empty_tbody_returns_empty_list`
      — **red** for all
- [ ] Implement `_parse_exhortos_table(self, html: str) -> List[PJUDExhorto]`
      using `_extract_pane_tbody(html, "exhortosCiv")`; extract label text via tag-strip;
      capture JWT via `detalleExhortosCivil\('([^']+)'\)` sub-match (stored, NOT fetched)
- [ ] Run tests — **green**

---

### S1-T06 — Notificacion + Escrito parsers  *(needs S1-T03, parallel with S1-T04 and S1-T05)*

**Status:** DONE (Slice 1a, commit bd8b316)
**Spec:** "Notificacion Parsing"; "Escrito Parsing"
**Work:**
- [ ] Add to `tests/scrapper/pjud/test_civil_parsers.py`:
      - `test_parse_notificaciones_rich_fixture_empty`: rich fixture has empty pane → `[]`
      - `test_parse_notificaciones_synthetic_fixture_2_rows`: synthetic fixture → 2 rows
      - `test_parse_escritos_rich_fixture_empty`: rich fixture → `[]`
      - `test_parse_escritos_synthetic_fixture_2_rows`: synthetic fixture → 2 rows,
        assert `tiene_documento` and `tiene_anexo` are booleans
      — **red** for all
- [ ] Implement `_parse_notificaciones_table(self, html) -> List[PJUDNotificacion]`
      using `_extract_pane_tbody(html, "notificacionesCiv")`
      Add `# TODO(validate-real-rich-case): column mapping unconfirmed on real data`
- [ ] Implement `_parse_escritos_table(self, html) -> List[PJUDEscrito]`
      using `_extract_pane_tbody(html, "escritosCiv")`
      Add `# TODO(validate-real-rich-case): column mapping unconfirmed on real data`
- [ ] Run tests — **green**

---

### S1-T07 — `_parse_case_detail_html` extension  *(needs S1-T04, S1-T05, S1-T06)*

**Status:** DONE (Slice 1a, commit bd8b316)
**Spec:** "All five tabs populated from single detail HTML" (pjud-civil delta spec)
**Work:**
- [ ] Write failing test `test_parse_case_detail_html_populates_all_five_lists`:
      parse rich fixture → `PJUDCaseDetail` with `len(movements)==3`,
      `len(litigantes)==6`, `len(exhortos)==1`, `len(notificaciones)==0`,
      `len(escritos)==0` (not None) — **red**
- [ ] Update `_parse_case_detail_html` in `civil.py` to call all five parsers and
      assign results to the four new `PJUDCaseDetail` fields
- [ ] Run test — **green**
- [ ] Existing callers still compile (additive fields with defaults)

---

### S1-T08 — SQLAlchemy models: 4 entity tables + Alert columns + Case back-refs  *(parallel with S1-T03 onwards)*

**Spec:** "Natural Key Upsert — Idempotency" (natural_key dedup spine); ADR-002; ADR-003
**Status:** DONE (Slice 1b, commit bb29f3b)
**Work:**
- [x] Write failing import/attribute test in `tests/test_models_case_detail.py` — **red**
- [x] Create `app/models/case_litigante.py` (`CaseLitigante`)
- [x] Create `app/models/case_notificacion.py` (`CaseNotificacion`)
- [x] Create `app/models/case_escrito.py` (`CaseEscrito`)
- [x] Create `app/models/case_exhorto.py` (`CaseExhorto`)
- [ ] Update `app/models/alert.py`: entity_type + entity_id (DEFERRED to Slice 2)
- [x] Update `app/models/case.py`: 4 back-ref relationships
- [x] Update `app/models/__init__.py`: export 4 new model classes
- [x] Run attribute test — **green** (12 tests)
- [x] `mypy app/core` — clean

---

### S1-T09 — Single Alembic migration  *(needs S1-T08)*

**Spec:** "Natural Key Upsert — Idempotency" (persistence prerequisite)
**Status:** DONE (Slice 1b, commit 0afd12e)
**Work:**
- [x] Write failing test `test_migration_importable` — **red** then **green**
- [x] Write migration `alembic/versions/004_add_case_detail_entities.py` (down_revision='003')
- [x] Verify upgrade creates 4 tables with UNIQUE(case_id, natural_key)
- [x] Verify downgrade drops all 4 tables cleanly (Alert columns deferred to Slice 2)
- [x] Run import test — **green** (5 tests)
- [x] Run `alembic upgrade head` against local Postgres — no errors
- [x] `alembic downgrade -1 && alembic upgrade head` — reversibility verified

---

### S1-T10 — Scraped DTOs + converters + `natural_key` functions  *(needs S1-T02, parallel with S1-T08/T09)*

**Spec:** "Natural Key Upsert — Idempotency"; ADR-002 (natural_key recipe)
**Status:** DONE (Slice 1b, commit a2cfa42)
**Work:**
- [x] Write failing tests in `tests/services/test_case_detail_sync.py` — **red** then **green**
- [x] Add to `app/services/sync_service.py`:
      - `normalize_cell(s: str) -> str`
      - `litigante_natural_key`, `exhorto_natural_key`, `notificacion_natural_key`, `escrito_natural_key`
      (Note: Scraped* dataclasses omitted per ADR-001 — natural_key fns take PJUDLitigante/etc directly)
- [x] Run tests — **green** (43 total in test_case_detail_sync.py)

---

### S1-T11 — `EntitySyncSpec` + `_sync_entities` engine (storage-only)  *(needs S1-T09, S1-T10)*

**Spec:** "Natural Key Upsert — Idempotency" — all BDD scenarios
**Status:** DONE (Slice 1b, commit a2cfa42)
**Work:**
- [x] Add L2 tests (idempotency, 0 alerts, mock NotificationService) — **red** then **green**
- [x] Add `EntitySyncSpec` dataclass + 4 spec instances (creates_alert=False, notify=False)
- [x] Add `_sync_entities(db, case_id, scraped_list, spec, ...)` generic engine
- [x] Add `upsert_litigantes/notificaciones/escritos/exhortos` convenience wrappers
- [x] End-to-end tests: rich fixture → 6 litigantes + 1 exhorto in DB; synthetic → 2 notif + 2 escritos
- [x] Run tests — **green** (43 tests, 0 Alert rows, NotificationService never called)

---

### S1-T12 — Extend `detect_and_sync_movements` with 4 entity sync calls  *(needs S1-T11, S1-T07)*

**Spec:** "All five tabs populated from single detail HTML" (end-to-end storage)
**Work:**
- [ ] Add failing test `test_detect_and_sync_stores_all_entity_types`:
      mock scraper returning `PJUDCaseDetail` with rich fixture data;
      after `detect_and_sync_movements` assert DB has 6 litigantes + 1 exhorto
      + 0 notificaciones + 0 escritos — **red**
- [ ] Update `detect_and_sync_movements` in `sync_service.py`:
      after existing `sync_movements` call, call `_sync_entities` for each of the 4 specs
      using the `detail.{litigantes,notificaciones,escritos,exhortos}` lists
      (no extra network fetch — same `detail` object)
- [ ] Run test — **green**

---

### S1-VER — Slice 1 verification gate

- [ ] `pytest -m "not integration" -v` — all new + existing tests green
- [ ] `mypy app/core` — no new errors
- [ ] Manual spot-check: run migration up/down on local DB without errors
- [ ] Confirm 0 Alert rows generated in any test scenario
- [ ] Confirm TODO(validate-real-rich-case) markers present on notif/escrito parsers

**Rollback:** `alembic downgrade -1` drops 4 tables + 2 alert columns.
**PR boundary:** open Slice 1 PR targeting `feat/notifications-pipeline` (or main per chain strategy).

---

## Slice 2 — Change Detection + Notifications

**Dependency:** Slice 1 fully merged and green.
**Rollback boundary:** flip spec flags back to `creates_alert=False, notify=False`;
no schema change required (columns land in Slice 1).

---

### S2-T01 — Flip spec flags + Alert entity wiring in `_sync_entities`  *(first Slice 2 task)*

**Spec:** "Change Detection" — all BDD scenarios; ADR-003; ADR-004
**Work:**
- [ ] Write failing L2 tests in `tests/services/test_case_detail_sync.py`:
      - `test_new_exhorto_creates_alert_with_entity_type`: run `_sync_entities` on 1 exhorto
        → 1 Alert with `type="new_exhorto"`, `entity_type="exhorto"`, `entity_id=<row.id>`
      - `test_existing_exhorto_no_alert`: seed CaseExhorto first; re-run → 0 new Alerts
      - `test_new_notificacion_creates_alert`: synthetic fixture, 2 notificaciones → 2 Alerts
      - `test_new_escrito_creates_alert`: synthetic fixture, 2 escritos → 2 Alerts
      - `test_litigante_creates_no_alert`: 6 litigantes → 0 Alerts (policy: litigantes silent)
      — **red** for all
- [ ] In `app/services/sync_service.py`:
      - Flip `creates_alert=True` on SPEC_NOTIFICACION, SPEC_ESCRITO, SPEC_EXHORTO
        (SPEC_LITIGANTE stays `creates_alert=False`)
      - Wire alert creation in `_sync_entities` when `is_new and spec.creates_alert`:
        `Alert(type="new_{entity_type}", entity_type=spec.entity_type, entity_id=row.id,
               lawyer_id=lawyer.id, case_id=case_id, title=spec.alert_title_fn(...),
               message=spec.alert_message_fn(...))`
- [ ] Run tests — **green**

---

### S2-T02 — `NotificationService`: new methods + `_dispatch` helper + webhook events  *(parallel with S2-T01)*

**Spec:** "Change Detection" (notify_new_* methods); ADR-004 (event strings); ADR-003
**Work:**
- [ ] Write failing L3 tests in `tests/services/test_notification_service.py`:
      - `test_notify_new_notificacion_sends_email`: mock SendGrid → assert `send_email_alert`
        called with correct subject and body
      - `test_notify_new_escrito_sends_webhook`: mock httpx → assert webhook body is
        canonical JSON `{event: "escrito.created", version: "1", data: {...}}` with HMAC header
      - `test_notify_new_exhorto_sends_webhook`: event `"exhorto.created"`, HMAC present
      - `test_dispatch_helper_calls_email_and_all_webhooks`: 2 webhooks → 2 webhook calls + 1 email
      — **red** for all
- [ ] Add to `app/services/notification_service.py`:
      - Private `_dispatch(self, alert, lawyer, webhooks, payload: dict)` absorbing
        `send_email_alert` + HMAC webhook fan-out (eliminates copy-paste from next 3 methods)
      - `notify_new_notificacion(self, db, case, notificacion_row, lawyer, webhooks, alert)`
      - `notify_new_escrito(self, db, case, escrito_row, lawyer, webhooks, alert)`
      - `notify_new_exhorto(self, db, case, exhorto_row, lawyer, webhooks, alert)`
      - Each builds v1 envelope: `{event, version:"1", data:{lawyer_id, case:{...}, <entity>:{...}}}`
      - Event strings: `"notificacion.created"`, `"escrito.created"`, `"exhorto.created"`
- [ ] Run tests — **green**

---

### S2-T03 — Notify dispatch wiring in `_sync_entities` + shared budget refactor  *(needs S2-T01 + S2-T02)*

**Spec:** "Notification Cap Across All Entity Types"; ADR-005; "Change Detection" (dispatch path)
**Work:**
- [ ] Write failing tests:
      - `test_sync_entities_calls_notify_fn_per_new_row`: mock `NotificationService`;
        3 new exhortos → `notify_new_exhorto` called 3 times
      - `test_litigante_notify_never_called_even_when_new`:
        6 new litigantes → `notify_new_litigante` (or any notify method) never called
      - `test_budget_cap_across_entity_types`:
        `settings.NOTIFY_MAX_PER_SYNC = 2`; 1 new notificacion + 2 new escritos (3 total);
        only 2 dispatches; all 3 Alert rows persisted; cap-warning logged
      - `test_sync_movements_still_works_with_shared_budget`:
        existing `sync_movements` callers work unchanged (additive default signature)
      — **red** for all
- [ ] In `app/services/sync_service.py`:
      - Introduce `class NotifyBudget` (mutable counter) or `list[int]` wrapper
      - Refactor `sync_movements` to accept optional `budget: NotifyBudget = None`
        (if None, creates own local budget — backward compat)
      - Flip `notify=True` on SPEC_NOTIFICACION, SPEC_ESCRITO, SPEC_EXHORTO
        (SPEC_LITIGANTE stays `notify=False`)
      - Wire dispatch in `_sync_entities`: when `is_new and spec.notify and budget.remaining > 0`:
        call `getattr(notification_svc, spec.notify_fn_name)(...)`, decrement budget
        else log cap-reached warning
      - Update `detect_and_sync_movements` to create ONE shared `NotifyBudget` and pass it
        into `sync_movements` + all 4 `_sync_entities` calls in priority order
        (movements → notificaciones → escritos → exhortos)
- [ ] Run tests — **green**

---

### S2-VER — Slice 2 verification gate

- [ ] `pytest -m "not integration" -v` — all tests green (including Slice 1 suite)
- [ ] `mypy app/core` — no new errors
- [ ] Assert L2 budget test: cap=2 → exactly 2 dispatches, 3 alerts persisted
- [ ] Assert L3 payload: HMAC header present, event strings correct per entity
- [ ] Confirm litigante path: 0 alerts, 0 notify calls regardless of `creates_alert`/`notify` flags
- [ ] Confirm TODO(validate-real-rich-case) markers still present
      (Slice 2 acceptance gate for notificaciones/escritos: do NOT remove markers until
       a real PJUD case with populated notificacion/escrito rows is parsed and verified)
- [ ] Manual smoke: run full sync against test case C-1253-2015 → 1 exhorto alert generated,
      no duplicate on re-run, no litigante alert

**PR boundary:** open Slice 2 PR targeting Slice 1 branch (feature-branch-chain)
or main (stacked-to-main) per cached chain strategy.

---

## Task Dependency Graph

```
S1-T01 (fixtures) ─┐
                   ├──▶ S1-T03 (pane_tbody + scope) ──▶ S1-T04 (litigantes) ─┐
S1-T02 (base.py)  ─┘                                                           │
                                    S1-T05 (exhortos) ─────────────────────────┤
                                    S1-T06 (notif+escrito) ────────────────────┤
                                                                               ▼
S1-T08 (models) ──▶ S1-T09 (migration) ─┐       S1-T07 (detail html) ──┐
S1-T10 (DTOs)   ─────────────────────────┴──▶ S1-T11 (_sync_entities) ──┤
                                                                          ▼
                                                         S1-T12 (detect_and_sync) ──▶ S1-VER
                                                              │
S2-T01 (flags + alert wiring) ─┐                             │
S2-T02 (NotifService methods) ─┼──▶ S2-T03 (dispatch+budget) ──▶ S2-VER
                                │       (needs S2-T01 + S2-T02)
```

**Parallel pairs within Slice 1:**
- S1-T01 ∥ S1-T02
- S1-T04 ∥ S1-T05 ∥ S1-T06 (all need S1-T03, all modify civil.py — serialize in one PR)
- S1-T08 ∥ S1-T10 (models vs DTOs, different files)

**Parallel within Slice 2:**
- S2-T01 ∥ S2-T02 (no code dependency between each other)

---

## Spec → Task Coverage Map

| Spec Requirement | Tasks |
|---|---|
| Tab-Scoped Parsing | S1-T03 (scoping regression), S1-T04, S1-T05, S1-T06 |
| Litigante Parsing | S1-T04 |
| Exhorto Parsing | S1-T05 |
| Notificacion Parsing | S1-T06 |
| Escrito Parsing | S1-T06 |
| All five tabs populated | S1-T07, S1-T12 |
| Natural Key Upsert / Idempotency | S1-T08, S1-T09, S1-T10, S1-T11 |
| Change Detection (alerts) | S2-T01 |
| Change Detection (notifications) | S2-T02, S2-T03 |
| Litigantes silent (ADR-004) | S2-T01 (creates_alert=False), S2-T03 (notify=False) |
| Notification Cap Across All Types | S2-T03 |
| Civil selectors from registry | S1-T03 (civil.yaml) |
| Movements parser re-scope (regression) | S1-T03 |
| Backward compatible API | S1-T02 (additive defaults only) |
| Polymorphic Alert target (ADR-003) | S1-T08 (model), S2-T01 (wiring) |
