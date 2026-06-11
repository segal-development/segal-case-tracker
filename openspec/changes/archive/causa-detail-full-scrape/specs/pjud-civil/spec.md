# Delta for pjud-civil

## MODIFIED Requirements

### Requirement: Civil Scraper Architecture

The `PJUDCivilScraper` MUST extend `PJUDBaseScraper` and use the selector registry instead of hardcoded selectors. `_parse_case_detail_html` MUST populate all five entity lists — movements, litigantes, notificaciones, escritos, and exhortos — from the single detail HTML response.
(Previously: `_parse_case_detail_html` populated only movements; the movements parser matched the first `table-bordered` table in the full HTML rather than scoping to `#historiaCiv`)

#### Scenario: Civil inherits from base

- GIVEN `PJUDCivilScraper` class
- WHEN inspecting its inheritance
- THEN it MUST extend `PJUDBaseScraper`
- AND it MUST implement all abstract methods

#### Scenario: Civil selectors from registry

- GIVEN Civil scraper queries case table
- WHEN selector is needed for `#verDetalleMisCauCiv`
- THEN selector MUST come from `selectors/civil.yaml`
- AND NOT be hardcoded in the class

#### Scenario: Backward compatible API

- GIVEN existing code calls `civil_scraper.get_my_cases(session)`
- WHEN the refactored scraper executes
- THEN return type MUST be `List[PJUDCase]`
- AND all existing fields MUST be present
- AND no breaking changes to method signatures

#### Scenario: All five tabs populated from single detail HTML

- GIVEN a detail HTML where #historiaCiv has movements, #litigantesCiv has litigante rows, and #exhortosCiv has an exhorto row
- WHEN `_parse_case_detail_html` runs
- THEN the returned `PJUDCaseDetail` has non-empty `movements`, `litigantes`, and `exhortos` lists
- AND `notificaciones` and `escritos` are empty lists (not None)

## ADDED Requirements

### Requirement: Civil YAML Selectors

The system MUST have `selectors/civil.yaml` with all Civil-specific selectors.

| Element | Selector |
|---------|----------|
| `table_id` | `verDetalleMisCauCiv` |
| `modal_id` | `modalDetalleMisCauCivil` |
| `detail_function` | `detalleMisCausaCivil` |
| `search_endpoint` | `misCausas/civil/consultaMisCausasCivil.php` |

#### Scenario: Civil YAML exists and validates

- GIVEN application startup
- WHEN `selectors/civil.yaml` is loaded
- THEN all required elements MUST be present
- AND YAML MUST pass schema validation

### Requirement: Civil Resilience Integration

The system MUST wrap Civil operations with resilience decorators.

#### Scenario: Civil with circuit breaker

- GIVEN Civil circuit breaker is OPEN
- WHEN `get_my_cases()` is called
- THEN `CircuitOpenError` MUST be raised immediately
- AND no PJUD request MUST be made

### Requirement: Movements Parser Scoped to #historiaCiv

The `_parse_movements_table` method MUST scope to the `#historiaCiv` pane before matching `table-bordered`. Rows from any other pane MUST NOT appear in the returned movements list.

#### Scenario: Multi-tab detail does not bleed rows into movements (regression)

- GIVEN a detail HTML where #historiaCiv has 40 rows, #litigantesCiv has 6 rows, and #exhortosCiv has 1 row
- WHEN `_parse_movements_table` runs
- THEN exactly 40 movement objects are returned
- AND no litigante or exhorto rows appear in the movements result
