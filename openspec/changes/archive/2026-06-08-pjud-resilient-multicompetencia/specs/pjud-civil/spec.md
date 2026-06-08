# Delta for pjud-civil

## MODIFIED Requirements

### Requirement: Civil Scraper Architecture

The `PJUDCivilScraper` MUST extend `PJUDBaseScraper` and use selector registry instead of hardcoded selectors.
(Previously: Standalone class with hardcoded CSS selectors and table IDs)

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
