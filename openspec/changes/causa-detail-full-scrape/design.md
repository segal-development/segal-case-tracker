# Design: causa-detail-full-scrape

Architecture for parsing, storing, and change-detecting the four currently-discarded
civil case-detail tabs (Litigantes, Notificaciones, Escritos por Resolver, Exhortos)
from the SAME detail HTML the scraper already fetches.

This document is the HOW at the architectural level. Tasks (the step-by-step WHAT-to-do)
come in the next phase.

---

## 1. Architecture Approach

**Pattern:** mirror the existing vertical slice `Movement → Alert → SyncService → NotificationService`
once per new entity, but factor the repeated change-detection loop into a single
**generic entity-sync engine** driven by per-entity configuration.

**Layering (unchanged, additive):**

```
scrapper/pjud (civil.py + base.py)      ── parse HTML → typed dataclasses
        │  PJUDCaseDetail (+4 lists)
        ▼
services/sync_service.py                ── upsert + is_new + alert (generic engine)
        │  Alert rows
        ▼
services/notification_service.py        ── email (SendGrid) + HMAC webhook fan-out
        │
        ▼
models/ (4 new tables + Alert columns)  ── persistence + natural-key dedup
```

**Boundary rule (the load-bearing fix):** every tab parser MUST scope to its Bootstrap
pane `id` BEFORE matching the inner `table-bordered` table. All five panes
(`#historiaCiv`, `#litigantesCiv`, `#notificacionesCiv`, `#escritosCiv`, `#exhortosCiv`)
use the IDENTICAL table class `table table-bordered table-striped table-hover`. The
current `movements_table` selector matches the FIRST such table in the whole document and
only works by accident because Historia renders first. Pane-scoping converts an accidental
correctness into a structural guarantee and unlocks the four new parsers.

---

## 2. Component Map

### 2.1 Scraper dataclasses — `app/scrapper/pjud/base.py` (Modified)

Add four frozen-style dataclasses, sibling to `PJUDMovement`:

```python
@dataclass
class PJUDLitigante:
    participante: str          # "DTE.", "AB.DDO", ...
    rut: str                   # "81826800-9" (may be "")
    persona_type: str          # "JURIDICA" / "NATURAL"
    nombre: str

@dataclass
class PJUDNotificacion:
    rol: str
    estado_notif: str
    tipo_notif: str
    fecha_tramite: str
    tipo_participante: str
    nombre: str
    tramite: str
    obs_fallida: str           # may be ""

@dataclass
class PJUDEscrito:
    fecha_ingreso: str
    tipo_escrito: str
    solicitante: str
    tiene_documento: bool
    tiene_anexo: bool
    doc_token: Optional[str] = None

@dataclass
class PJUDExhorto:
    rol_origen: str
    tipo_exhorto: str
    rol_destino: str           # text inside the <label> onclick=detalleExhortosCivil(...)
    fecha_ordena: str
    fecha_ingreso: str
    tribunal_destino: str
    estado: str
    detalle_token: Optional[str] = None   # JWT from detalleExhortosCivil (stored, not fetched here)
```

Extend `PJUDCaseDetail` with four new lists (additive, defaulted):

```python
litigantes:     List[PJUDLitigante]   = field(default_factory=list)
notificaciones: List[PJUDNotificacion]= field(default_factory=list)
escritos:       List[PJUDEscrito]     = field(default_factory=list)
exhortos:       List[PJUDExhorto]     = field(default_factory=list)
```

`partes: Dict[str,str]` is left in place but superseded by `litigantes` (deprecate in a
later cleanup — keeping it avoids touching call sites that read it).

### 2.2 Parsers — `app/scrapper/pjud/civil.py` (Modified)

Introduce one private helper to remove duplication and enforce the boundary rule:

```python
def _extract_pane_tbody(self, html: str, pane_id: str) -> Optional[str]:
    """Return the <tbody> inner HTML of the table inside the pane div #pane_id, or None."""
    # 1. isolate the pane:  id="pane_id" ... up to the matching closing of the tab-pane
    # 2. inside it, match the first table-bordered <tbody>(...)</tbody>
```

Then four thin parsers + the movements refactor:

| Method | Pane id | Columns (cell index) |
|---|---|---|
| `_parse_movements_table` (refactor) | `historiaCiv` | unchanged mapping, now pane-scoped |
| `_parse_litigantes_table` | `litigantesCiv` | participante(0) rut(1) persona(2) nombre(3) |
| `_parse_notificaciones_table` | `notificacionesCiv` | rol(0) estado(1) tipo(2) fecha(3) tipoPart(4) nombre(5) tramite(6) obs(7) |
| `_parse_escritos_table` | `escritosCiv` | doc(0) anexo(1) fechaIngreso(2) tipoEscrito(3) solicitante(4) |
| `_parse_exhortos_table` | `exhortosCiv` | rolOrigen(0) tipo(1) rolDestino(2) fOrdena(3) fIngreso(4) tribunal(5) estado(6) |

Each parser: `tbody = _extract_pane_tbody(...)`; if None → `[]`; iterate `<tr>`; split by
`</td>`; `clean()` (strip tags + collapse whitespace) each cell. Tag-stripping naturally
extracts `E-355-2026` from the exhorto `<label … onclick=…>E-355-2026</label>`; the JWT
token is captured separately via a `detalleExhortosCivil\('([^']+)'\)` sub-match for
storage only (no network call — that is Phase 2).

`_parse_case_detail_html` calls all five parsers and passes the four new lists into
`PJUDCaseDetail`. Empty tbody (notificaciones/escritos in both samples) yields `[]` — never
an error.

### 2.3 Selectors — `app/scrapper/pjud/selectors/civil.yaml` (Modified)

- Re-scope `movements_table` so the primary pattern anchors on `id="historiaCiv"` before the
  table match; keep the old un-scoped pattern as a **fallback** (zero-regression: if PJUD
  drops the id, behaviour degrades to today's).
- Add `pane_table_pattern` (parametrizable by id) + `entity_row_pattern` (reuse
  `<tr[^>]*>(.*?)</tr>`) + the `exhorto_detail_token_pattern`.

### 2.4 Models — `app/models/` (New: 4 files)

All four mirror `Movement`: `id` PK, `case_id` FK→`cases.id`, descriptive columns,
`created_at`, and a `case` relationship. **Key architectural addition: every table carries a
single `natural_key VARCHAR(64)` column with a `UniqueConstraint(case_id, natural_key)`.**
This is the dedup spine that lets ONE generic upsert serve all entities (see §4).

```
case_litigantes
  participante VARCHAR(50), rut VARCHAR(20), persona_type VARCHAR(50),
  nombre VARCHAR(500), natural_key VARCHAR(64)   UNIQUE(case_id, natural_key)

case_notificaciones
  rol VARCHAR(50), estado_notif VARCHAR(100), tipo_notif VARCHAR(100),
  fecha_tramite DATETIME NULL, tipo_participante VARCHAR(50), nombre VARCHAR(500),
  tramite VARCHAR(255), obs_fallida TEXT NULL, natural_key VARCHAR(64)
                                                 UNIQUE(case_id, natural_key)

case_escritos
  fecha_ingreso DATETIME NULL, tipo_escrito VARCHAR(255), solicitante VARCHAR(500),
  tiene_documento BOOLEAN, tiene_anexo BOOLEAN, doc_token VARCHAR(1024) NULL,
  natural_key VARCHAR(64)                        UNIQUE(case_id, natural_key)

case_exhortos
  rol_origen VARCHAR(50), tipo_exhorto VARCHAR(100), rol_destino VARCHAR(50),
  fecha_ordena DATETIME NULL, fecha_ingreso DATETIME NULL,
  tribunal_destino VARCHAR(255), estado VARCHAR(100), natural_key VARCHAR(64)
                                                 UNIQUE(case_id, natural_key)
```

Add `relationship(...)` back-refs on `Case` for symmetry (`litigantes`, `notificaciones`,
`escritos`, `exhortos`).

### 2.5 Alert — `app/models/alert.py` (Modified)

Add a **generic polymorphic target** (see §5), not four more FK columns:

```python
entity_type = Column(String(30), nullable=True)  # "notificacion" | "escrito" | "exhorto"
entity_id   = Column(Integer,    nullable=True)   # PK in the corresponding table
```

`movement_id` stays untouched; the existing movement-alert path is not modified (Slice
isolation, zero regression).

### 2.6 SyncService — `app/services/sync_service.py` (Modified)

- 4 `@dataclass Scraped{Litigante,Notificacion,Escrito,Exhorto}` (sync-layer DTOs, decoupled
  from scraper dataclasses exactly like `ScrapedMovement`).
- `convert_*_to_scraped` helpers, mirroring `convert_api_movements_to_scraped`.
- One generic engine `_sync_entities(case_id, scraped_list, spec, lawyer, webhooks, budget)`
  replacing four near-identical copies of the `sync_movements` body (see §4).
- Extend `detect_and_sync_movements` to also run the four entity types from the same
  `detail` object (no extra fetch).

### 2.7 NotificationService — `app/services/notification_service.py` (Modified)

Add `notify_new_notificacion / _escrito / _exhorto`, each building a v1 payload with its own
`event` string, then reusing the existing `send_email_alert` + webhook fan-out (identical
structure to `notify_new_movement`). A small private `_dispatch(alert, lawyer, webhooks,
payload)` can absorb the shared email+webhook loop to avoid copy-paste.

### 2.8 Migration — `alembic/versions/` (New: 1 file)

Single revision: `create_table` ×4 (+ unique constraints + `case_id` index) and
`add_column` ×2 on `alerts` (`entity_type`, `entity_id`). `downgrade` drops the four tables
and the two columns. One migration keeps the change atomic and the rollback trivial.

---

## 3. Data Flow

```
get_case_detail(token) ──▶ detail HTML (one response, all 5 panes)
        │
        ▼ _parse_case_detail_html
PJUDCaseDetail{ movements, litigantes, notificaciones, escritos, exhortos }
        │
        ▼ detect_and_sync_movements   (one DB case row resolved once)
        ├─ sync_movements(case_id, movements)                 [existing path, untouched]
        ├─ _sync_entities(case_id, litigantes,    SPEC_LITIGANTE)
        ├─ _sync_entities(case_id, notificaciones,SPEC_NOTIFICACION)
        ├─ _sync_entities(case_id, escritos,      SPEC_ESCRITO)
        └─ _sync_entities(case_id, exhortos,      SPEC_EXHORTO)
                 │  per row: compute natural_key → query (case_id, natural_key)
                 │           exists? → is_new=False ; else insert → is_new=True
                 ▼ is_new and spec.creates_alert
            Alert(entity_type, entity_id) ──▶ (spec.notify) NotificationService.notify_*
                                                     │
                                                     ├─ email (SendGrid)
                                                     └─ webhook fan-out (HMAC, event=…)
```

**Idempotency contract:** re-running sync on unchanged HTML produces zero inserts and zero
alerts because the `(case_id, natural_key)` unique key matches every existing row. This is
the success criterion "zero new rows / zero alerts".

---

## 4. ADR-001 — Generic `_sync_entities` engine over per-entity methods

**Decision:** a single configurable engine, not four hand-written `sync_*` methods.

```python
@dataclass
class EntitySyncSpec:
    model: type                      # CaseNotificacion, ...
    entity_type: str                 # "notificacion"
    natural_key_fn: Callable         # scraped → str (see ADR-002)
    to_model_fields: Callable        # scraped → dict(column→value)
    creates_alert: bool              # False for litigantes
    notify: bool                     # False for litigantes
    alert_title_fn: Callable         # (case, model) → str
    alert_message_fn: Callable       # (case, model) → str
    notify_fn_name: str              # "notify_new_notificacion"
    event: str                       # "notificacion.created"
    priority: int                    # drains the shared notify budget in priority order
```

The engine reproduces the proven `sync_movements` loop EXACTLY: hoist `lawyer` + active
`webhooks` once, upsert-by-natural-key, create alert when `is_new and spec.creates_alert`,
dispatch when `spec.notify` and budget remains, log a cap-reached warning.

**Why:** the four loops are byte-for-byte identical except (model, key, alert text, event).
Encoding the differences as data (the spec) removes ~4× duplication, gives ONE place to fix
bugs / change cap behaviour, and makes adding laboral/penal entities later a config entry,
not a new method. Type-specific behaviour stays explicit and testable through the spec
callables.

**Rejected — four copy-pasted `sync_*` methods (the literal proposal sketch):** simpler to
read row-by-row but quadruples the surface for the next bug (e.g. the notify-cap fix would
need re-applying four times) and tempts drift between entities. The generic engine is the
senior call; the per-entity sketch in the proposal was illustrative, not prescriptive.

**Rejected — fully generic single EAV table:** loses per-entity queryability, typing, and
FK integrity (exploration Approach C). Not revisited.

---

## 5. ADR-002 — Single `natural_key` column per table (uniform dedup)

**Decision:** every entity table dedups on one derived `natural_key VARCHAR(64)` with
`UNIQUE(case_id, natural_key)`, instead of per-entity compound DB unique constraints.

`natural_key` is computed deterministically in the sync layer, NOT a hash of raw HTML:

| Entity | natural_key recipe |
|---|---|
| Litigante | `normalize(rut)` when rut present; else `sha256(norm(participante)|norm(nombre))[:64]` |
| Exhorto | `sha256(norm(rol_origen)|norm(rol_destino)|norm(tipo_exhorto))[:64]` |
| Notificacion | `row_hash` = `sha256` of `|`-joined normalized cells (no stable PJUD id) |
| Escrito | `row_hash` = `sha256` of `|`-joined normalized cells (no stable PJUD id) |

`normalize` = `strip()` + collapse internal whitespace + casefold for the hashed inputs
(PJUD pads cells with trailing spaces — see `JURIDICA` followed by many spaces in the
fixture). Hashing normalized values, not raw cells, defuses the documented `row_hash`
instability risk.

**Why one column:** it is the ENABLER for the §4 generic engine — the engine queries every
entity the same way: `model.query.filter(case_id==, natural_key==spec.natural_key_fn(row))`.
Descriptive columns (`rut`, `rol_origen`, …) are still stored verbatim for querying and
display; `natural_key` is purely the dedup spine. One uniform index pattern across four
tables.

**Rejected — per-entity compound UNIQUE constraints** (e.g. `UNIQUE(case_id, rol_origen,
rol_destino, tipo_exhorto)` for exhortos, `UNIQUE(case_id, rut)` for litigantes): more
"natural" at the schema level but forces a different query shape per entity, which breaks
the generic engine and reintroduces four code paths. The derived-key approach gets the same
correctness with one mechanism. Litigante's RUT-or-fallback ambiguity (the proposal's two
candidate keys) is also resolved cleanly inside `natural_key_fn`.

---

## 6. ADR-003 — Polymorphic Alert target over four nullable FKs

**Decision:** add `entity_type VARCHAR(30)` + `entity_id INTEGER` (nullable) to `alerts`;
keep `movement_id` as-is.

**Why:** the proposal sketched four new nullable FK columns (`litigante_id`, …). That is
two columns of schema growth PER future entity type and a sparse, mostly-NULL table. The
polymorphic pair is fixed-width regardless of how many entity types exist (laboral, penal,
cuadernos later), and it keeps the §4 engine uniform — the engine just writes
`(spec.entity_type, model.id)`. No DB-level FK on `entity_id` (the trade-off): integrity is
enforced in code, matching the already-nullable, loosely-coupled `movement_id` style. The
existing movement path is deliberately NOT migrated to the polymorphic pair in this change
(keeps Slice 1/2 isolated and the movement regression surface at zero); a future cleanup can
unify it.

**Rejected — four sparse nullable FKs (proposal sketch):** schema bloat + per-entity join
columns + a fifth/sixth column every time a new entity appears. The polymorphic pair is the
extensible choice.

---

## 7. ADR-004 — Notification policy per entity (which tabs notify)

**Decision:**

| Entity | Stored | Creates Alert | Email + Webhook | Event |
|---|---|---|---|---|
| Movement | yes | yes | yes | `movement.created` (existing) |
| Notificacion | yes | yes | yes | `notificacion.created` |
| Escrito | yes | yes | yes | `escrito.created` |
| Exhorto | yes | yes | yes | `exhorto.created` |
| **Litigante** | **yes** | **no** | **no** | (`litigante.created` reserved, not emitted) |

**Why litigantes are silent:** the party roster is reference data that is fully populated at
case creation and changes rarely. On first sync it would emit 6+ alerts per case (the
fixture has 6 litigantes) — pure noise drowning the high-value notificación/escrito/exhorto
signals. We still STORE litigantes (queryable, and a future "party changed" diff can light
up `creates_alert`/`notify` by flipping two spec flags). This narrows the proposal's "…or
litigante triggers a notification" success criterion to a deliberate non-goal; flagged as a
product-confirmation risk in §10.

**Webhook events** follow the existing `movement.created` v1 envelope shape exactly
(`event`, `version:"1"`, `data:{lawyer_id, case:{rol,tribunal,caratulado}, <entity>:{…}}`),
so consumers parse a familiar structure.

---

## 8. ADR-005 — Shared notification budget across all entity types

**Decision:** `NOTIFY_MAX_PER_SYNC` becomes a per-case-sync budget shared across movements +
all entity types, threaded as a mutable counter through `detect_and_sync_movements`, drained
in priority order (movements → notificaciones → escritos → exhortos).

**Why:** the cap exists to prevent a notification flood on first sync or a large catch-up.
Today it is enforced only inside `sync_movements`; if each entity type kept its own
independent cap, a first sync could emit `4 × NOTIFY_MAX_PER_SYNC` notifications and defeat
the protection. A single shared budget keeps the guarantee. Alerts are still ALWAYS persisted
(only the email/webhook DISPATCH is gated), preserving the existing movement semantics. When
the budget is exhausted, log the same cap-reached warning that `sync_movements` already
emits.

**Implementation note:** `sync_movements` currently owns its local cap. The engine and
`sync_movements` must read/decrement a passed-in budget object rather than re-reading
`settings.NOTIFY_MAX_PER_SYNC` independently. This is a small refactor of `sync_movements`'s
signature (Slice 2) — additive default keeps existing callers working.

---

## 9. Test Strategy (mock-based, pytest)

**Fixtures (committed):**
- `tests/fixtures/pjud/detail_rich_C-1253-2015.html` — verbatim copy of `/tmp/detail_rich.html`
  (real PJUD: 3 movements, 6 litigantes, 0 notificaciones, 0 escritos, 1 exhorto).
- `tests/fixtures/pjud/detail_synthetic_full.html` — hand-built variant that ADDS 2
  notificacion rows + 2 escrito rows into the empty panes, so change-detection can be
  exercised end-to-end despite no real rich case being available yet.

**Layer 1 — parser unit tests** (no DB, pure functions):
- `_parse_litigantes_table` → 6 rows; assert participante/rut/persona/nombre for DTE +
  AB.DDO rows; assert padded `JURIDICA` is trimmed.
- `_parse_exhortos_table` → 1 row; assert `rol_destino == "E-355-2026"` (label tag stripped)
  and `detalle_token` captured; assert tribunal trailing spaces trimmed.
- `_parse_notificaciones_table` / `_parse_escritos_table` on the rich fixture → `[]`
  (empty-tbody path), and on the synthetic fixture → 2 rows each.
- **Scoping regression test:** a crafted HTML where `litigantesCiv` is emitted BEFORE
  `historiaCiv` — assert `_parse_movements_table` still returns ONLY historia rows (proves
  the bug is fixed, would FAIL against the old un-scoped selector).

**Layer 2 — change-detection tests** (in-memory SQLite fixture + mocked
`NotificationService`):
- Seed a `Case`; run `_sync_entities` twice on the synthetic fixture → first run inserts N
  rows + creates alerts; second run inserts 0, creates 0 (idempotency via natural_key).
- Assert `notify_new_notificacion` / `_escrito` / `_exhorto` called once per NEW row with the
  correct `event`; assert litigantes create NEITHER alert NOR notify.
- Budget test: set `NOTIFY_MAX_PER_SYNC=2`, feed >2 new notifiable rows across entity types →
  exactly 2 dispatches, alerts still all persisted, cap-warning logged.

**Layer 3 — notification payload tests:** mock SendGrid + `httpx`; assert webhook body is
canonical JSON, HMAC header present, `event` correct per entity.

**Validation gap (explicit):** notificaciones/escritos parsers are proven against SYNTHETIC
rows only. Carry a `# TODO(validate-real-rich-case)` marker on those two parsers and a Slice
2 acceptance gate: do not ship Slice 2 notifications for notificaciones/escritos until parsed
against a real case with populated rows. Exhortos + litigantes are proven on real data now.

---

## 10. Slice Plan (sliceable decisions)

**Slice 1 — Parsing + Models + Scoping fix (no alerts/notifications):**
- base.py dataclasses + PJUDCaseDetail extension.
- civil.py: `_extract_pane_tbody`, 4 parsers, movements re-scope; selectors.yaml update.
- 4 models WITH `natural_key` + unique constraints; Case back-refs.
- 1 migration: 4 tables (Alert columns can land here too, unused until Slice 2 — or defer to
  Slice 2; **decision: include both column adds in the single migration** so there is exactly
  one migration for the whole change).
- SyncService: Scraped DTOs + converters + `_sync_entities` writing rows with
  `creates_alert=False, notify=False` for ALL specs (storage only).
- Layer 1 + idempotency tests.

**Slice 2 — Change Detection + Notifications:**
- Flip `creates_alert`/`notify` in the notifiable specs; wire Alert `entity_type/entity_id`.
- NotificationService methods + new webhook events.
- Shared notify budget refactor of `sync_movements` + `detect_and_sync_movements` extension.
- Layer 2 + Layer 3 tests; real-rich-case validation gate for notificaciones/escritos.

The `natural_key` design makes Slice 1 fully idempotent on its own, so Slice 1 is shippable
and valuable (data queryable) without Slice 2.

---

## 11. Architectural Risks / Assumptions

1. **Empty-tab validation gap (HIGH):** notificaciones/escritos parsers validated on
   synthetic rows only; real column-to-cell mapping unconfirmed. Mitigated by the §9
   validation gate before shipping Slice 2 for those two entities.
2. **`natural_key` drift (MED):** if PJUD reformats a cell (date format, added marker), the
   hash changes and a row re-alerts as "new". Mitigated by normalizing before hashing; revisit
   once real notificacion/escrito rows are seen.
3. **Litigante-silent decision (MED, product):** narrows the proposal success criterion.
   Needs product confirmation that party-roster changes are out of scope for notifications in
   this change. Flag-flip ready if rejected.
4. **Shared-budget refactor of `sync_movements` (MED):** changes a working hot path. Covered
   by the budget test + additive default signature to avoid breaking existing callers.
5. **Pane-scoping selector (LOW):** old un-scoped pattern retained as fallback, so a PJUD id
   change degrades to today's behaviour rather than breaking.
6. **Exhorto/receptor/anexo JWT detail calls remain Phase 2:** tokens are stored, not
   fetched. No network behaviour change in this slice.
