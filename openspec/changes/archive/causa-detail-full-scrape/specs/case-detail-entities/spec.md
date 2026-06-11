# case-detail-entities Specification

## Purpose

Defines parsing, storage, and change-detection for the four non-Historia tabs in the PJUD civil case-detail modal: Litigantes, Notificaciones, Escritos por Resolver, and Exhortos. All tab data is present in the single HTML response already fetched; no additional network calls are required.

## Requirements

### Requirement: Tab-Scoped Parsing

Each parser MUST scope to its pane `id` before extracting the inner table. Parsers MUST NOT match rows across pane boundaries.

| Parser method | Pane id |
|---------------|---------|
| `_parse_litigantes_table` | `#litigantesCiv` |
| `_parse_notificaciones_table` | `#notificacionesCiv` |
| `_parse_escritos_table` | `#escritosCiv` |
| `_parse_exhortos_table` | `#exhortosCiv` |

#### Scenario: Parser scopes to correct pane

- GIVEN a detail HTML with data in multiple tabs
- WHEN each tab parser runs
- THEN each parser returns rows ONLY from its own pane id
- AND no rows from any other pane appear in the result

### Requirement: Litigante Parsing

The system MUST parse litigantes from `#litigantesCiv` and return a list of `PJUDLitigante` with fields `participante`, `rut`, `persona`, `nombre`. Whitespace in each cell MUST be stripped.

#### Scenario: Litigantes extracted from rich HTML

- GIVEN the C-1253-2015 detail HTML containing 6 litigante rows
- WHEN `_parse_litigantes_table` is called
- THEN 6 `PJUDLitigante` objects are returned
- AND the first object has `participante="DTE."`, `rut="81826800-9"`, `persona="JURIDICA"`, `nombre="CAJA DE COMPENSACION DE ASIGNACION FAMILIAR DE LOS ANDES"`

#### Scenario: Empty litigantes tab yields zero rows without error

- GIVEN a detail HTML with an empty `#litigantesCiv tbody`
- WHEN `_parse_litigantes_table` is called
- THEN an empty list is returned
- AND no exception is raised

### Requirement: Exhorto Parsing

The system MUST parse exhortos from `#exhortosCiv` and return a list of `PJUDExhorto` with fields `rol_origen`, `tipo_exhorto`, `rol_destino`, `fecha_ordena`, `fecha_ingreso`, `tribunal_destino`, `estado`. The `rol_destino` cell MAY contain an HTML label element; the text content MUST be extracted.

#### Scenario: Exhortos extracted from rich HTML

- GIVEN the C-1253-2015 detail HTML containing 1 exhorto row
- WHEN `_parse_exhortos_table` is called
- THEN 1 `PJUDExhorto` is returned
- AND `rol_origen="C-1253-2015"`, `tipo_exhorto="Exhorto"`, `rol_destino="E-355-2026"`, `fecha_ordena="05/05/2026"`, `tribunal_destino="8º Juzgado Civil de Santiago"`, `estado="Generado"`

#### Scenario: Empty exhortos tab yields zero rows without error

- GIVEN a detail HTML with an empty `#exhortosCiv tbody`
- WHEN `_parse_exhortos_table` is called
- THEN an empty list is returned
- AND no exception is raised

### Requirement: Notificacion Parsing

The system MUST parse notificaciones from `#notificacionesCiv` with columns `rol`, `estado_notif`, `tipo_notif`, `fecha_tramite`, `tipo_participante`, `nombre`, `tramite`, `obs_fallida`. An empty tab MUST yield zero rows without error.

#### Scenario: Empty notificaciones tab yields zero rows without error

- GIVEN a detail HTML with an empty `#notificacionesCiv tbody`
- WHEN `_parse_notificaciones_table` is called
- THEN an empty list is returned
- AND no exception is raised

### Requirement: Escrito Parsing

The system MUST parse escritos from `#escritosCiv` with columns `fecha_ingreso`, `tipo_escrito`, `solicitante`, and boolean flags `tiene_documento` and `tiene_anexo`. An empty tab MUST yield zero rows without error.

#### Scenario: Empty escritos tab yields zero rows without error

- GIVEN a detail HTML with an empty `#escritosCiv tbody`
- WHEN `_parse_escritos_table` is called
- THEN an empty list is returned
- AND no exception is raised

### Requirement: Natural Key Upsert — Idempotency

Each entity type MUST upsert by its natural key. Re-running a sync with identical data MUST NOT insert duplicate rows or create new alerts.

| Entity | Natural Key |
|--------|-------------|
| `CaseLitigante` | `(case_id, rut, participante)`; fallback `(case_id, participante, nombre)` when RUT is absent or blank |
| `CaseExhorto` | `(case_id, rol_origen, rol_destino, tipo_exhorto)` |
| `CaseNotificacion` | `(case_id, row_hash)` — SHA256 of stripped, concatenated row cells |
| `CaseEscrito` | `(case_id, row_hash)` — SHA256 of stripped, concatenated row cells |

#### Scenario: Re-sync with unchanged data produces zero new rows

- GIVEN a case previously synced with 6 litigantes and 1 exhorto
- WHEN the identical detail HTML is synced again
- THEN no new rows are inserted in any entity table
- AND no new Alert records are created

### Requirement: Change Detection

The system MUST detect new entities on each sync. An entity not matched by its natural key MUST be flagged `is_new=True`, MUST generate an Alert of the corresponding type, and MUST invoke the entity-specific notification method. Litigantes are stored-only (no alerts, no notifications) per ADR-004.

| New entity | Alert type | Notification method |
|------------|------------|---------------------|
| `CaseLitigante` | (stored only, no alert) | (not invoked) |
| `CaseNotificacion` | `new_notificacion` | `notify_new_notificacion` |
| `CaseEscrito` | `new_escrito` | `notify_new_escrito` |
| `CaseExhorto` | `new_exhorto` | `notify_new_exhorto` |

#### Scenario: New exhorto triggers alert and notification

- GIVEN a case with no previously stored exhortos
- WHEN a sync parses 1 exhorto from the detail HTML
- THEN 1 `CaseExhorto` row is inserted
- AND 1 Alert of type `new_exhorto` is created linked to that row
- AND `notify_new_exhorto` is called for the case's lawyer

#### Scenario: Existing entity does not re-alert

- GIVEN a `CaseExhorto` row already stored matching the natural key `(case_id, rol_origen, rol_destino, tipo_exhorto)`
- WHEN the same exhorto appears in the next sync
- THEN no new Alert is created
- AND no notification is dispatched

### Requirement: Notification Cap Across All Entity Types

`NOTIFY_MAX_PER_SYNC` MUST limit the total notification count across ALL entity types (movements, litigantes, notificaciones, escritos, exhortos) in a single sync. Once the cap is reached no further notification calls MUST be made for any entity type in that sync.

#### Scenario: Cap spans mixed entity types

- GIVEN `NOTIFY_MAX_PER_SYNC = 3` and a sync that produces 2 new movements and 2 new notificaciones
- WHEN the sync runs
- THEN exactly 3 notifications are dispatched
- AND the 4th new entity (2nd notificacion) produces no notification call
