# Proposal: causa-detail-full-scrape

## Intent

The product promise is: notify lawyers when ANYTHING relevant changes in their PJUD cases. Today the scraper only parses the **Historia (movements)** tab of the civil case-detail modal and silently discards the other four tabs that are already present in the SAME HTML response: **Litigantes, Notificaciones, Escritos por Resolver, Exhortos**. Lawyers therefore miss high-value events — a notificación served, a pending escrito to resolve, a new exhorto, a change in parties — even though that data is already in hand. This change extends parsing and change-detection to all five tabs so new items in any of them trigger notifications, with no extra network cost (the data is in the modal HTML we already fetch).

## Scope

### In Scope
- Parse the 4 new tabs from the existing detail HTML (`litigantesCiv`, `notificacionesCiv`, `escritosCiv`, `exhortosCiv`), each scraper scoped to its pane `id`.
- 4 new SQLAlchemy models + one Alembic migration, linked to `Case`, following the existing `Movement` model pattern.
- 4 scraper dataclasses (`PJUDLitigante/Notificacion/Escrito/Exhorto`) and extension of `PJUDCaseDetail`.
- Extend change detection: clone the `sync_movements` upsert + `is_new` + notify loop per entity type → notifications + new webhook event types; extend `detect_and_sync_movements`.
- **Fix the latent `movements_table` selector bug**: it matches the first `table-bordered` table, not scoped to `#historiaCiv`. All parsers (including movements) MUST scope to their pane by `id` to prevent cross-tab collisions.

### Out of Scope (Non-Goals / Phase 2)
- Multi-cuaderno movement scraping (the `selCuaderno` selector fires a separate `historiaCausaCuaderno(token)` AJAX call; only the default cuaderno is in the static HTML).
- AJAX-loaded secondary content (`anexoCausaCivil`, `receptorCivil`/`receptorCivilReserva`, `detalleExhortosCivil` — all JWT-token calls for documents / receptor / exhorto detail).
- Laboral and penal competencias — civil only for now.

## Capabilities

### New Capabilities
- `case-detail-entities`: parsing, storage, and change-detection of litigantes, notificaciones, escritos por resolver, and exhortos from the civil case-detail modal.

### Modified Capabilities
- `pjud-civil`: `_parse_case_detail_html` extended to populate four new entity lists; movements parser scoped to `#historiaCiv`.

## Approach

Approach A from exploration (4 models + 4 upsert methods), mirroring the existing `Movement`/`Alert`/`SyncService` pipeline so each entity is independently queryable and notifiable. Each new parser follows `_parse_movements_table`: extract pane HTML by `id`, iterate `<tr>` in its `<tbody>`, return a typed dataclass list. Change detection generalizes the existing upsert→`is_new`→notify loop; a shared `_sync_entities(case_id, scraped, upsert_fn, notify_fn)` helper is a design-phase decision. Alert model gains sparse nullable FKs (`litigante_id`, `notificacion_id`, `escrito_id`, `exhorto_id`), consistent with the existing nullable `movement_id`.

**Confirmed real-data natural keys** (from `/tmp/detail_rich.html`, case C-1253-2015):
- Litigantes: `(case_id, rut, participante)` — fallback `(case_id, participante, nombre)` when RUT absent.
- Exhortos: `(case_id, rol_origen, rol_destino, tipo_exhorto)`.
- Notificaciones / Escritos: `(case_id, row_hash)` — SHA256 of row cells (no stable PJUD id).

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `app/scrapper/pjud/base.py` | Modified | 4 new dataclasses; extend `PJUDCaseDetail` |
| `app/scrapper/pjud/civil.py` | Modified | 4 parser methods; extend `_parse_case_detail_html`; scope movements to `#historiaCiv` |
| `app/scrapper/pjud/selectors/civil.yaml` | Modified | Selectors for 4 panes; scope movements selector |
| `app/models/` | New | `CaseLitigante`, `CaseNotificacion`, `CaseEscrito`, `CaseExhorto` |
| `app/models/alert.py` | Modified | 4 nullable entity FKs; new `type` values |
| `app/services/sync_service.py` | Modified | 4 scraped dataclasses + upsert/sync methods; extend `detect_and_sync_movements` |
| `app/services/notification_service.py` | Modified | `notify_new_{litigante,notificacion,escrito,exhorto}` |
| `alembic/versions/` | New | Migration: 4 tables + Alert FK columns |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Empty-tab validation gap (notificaciones/escritos have no sample rows yet) | High | Design from headers; validate against a richer case during apply before shipping Slice 2 notifications |
| `row_hash` instability for notificaciones/escritos (whitespace/format drift in PJUD cells) | Med | Normalize/strip cells before hashing; revisit once real rows are seen |
| Notification cap (`NOTIFY_MAX_PER_SYNC`) currently movement-only | Med | Extend cap to span ALL entity types per sync call, or document a per-entity cap |
| `movements_table` selector bug (matches first `table-bordered`) | High | Scope every parser to its pane `id`; covered in Slice 1 |
| First sync floods alerts (all existing items are "new") | Med | Reuse existing first-sync behavior; cap applies; optionally suppress litigante notifications |

## Rollback Plan

Revert the migration (`alembic downgrade` drops the 4 tables + Alert FK columns) and revert the code commits. The new parsers and sync methods are additive; reverting restores movement-only behavior with no data loss to existing tables.

## Dependencies

- A richer civil case with populated notificaciones/escritos tabs for Slice 2 integration validation (only exhortos/litigantes have sample rows today).

## Suggested First Slice

**Slice 1 — Parsing + Models (no notifications, ~400 lines):** 4 parser methods, 4 models + migration, scraped dataclasses + upserts, and the `movements_table` scoping fix. Data becomes stored and queryable.
**Slice 2 — Change Detection + Notifications (~250 lines):** Alert FK extension, sync→notify wiring, NotificationService methods, new webhook event types (`litigante.created`, `notificacion.created`, `escrito.created`, `exhorto.created`), extend `detect_and_sync_movements`.

## Success Criteria

- [ ] All 5 tabs parsed from a single detail HTML; movements parser scoped to `#historiaCiv` (bug fixed).
- [ ] 4 new tables created via migration; litigantes + exhortos correctly upserted from real C-1253-2015 data.
- [ ] Re-running sync on unchanged data produces zero new rows / zero alerts (idempotent natural keys).
- [ ] Slice 2: a new notificación/escrito/exhorto/litigante triggers an Alert + email + signed webhook with the correct `event` type, respecting the per-sync notification cap.
