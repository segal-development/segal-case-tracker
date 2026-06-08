# Laboral Scraper Specification

## Purpose

PJUD Laboral competency scraper with identical API to Civil, using base class and selector registry.

## Requirements

### Requirement: Laboral API Parity

The system MUST provide `PJUDLaboralScraper` with same public API as `PJUDCivilScraper`.

| Method | Description |
|--------|-------------|
| `get_my_cases()` | Fetch lawyer's Laboral cases with pagination |
| `get_my_cases_recent()` | Fetch last N years of cases |
| `get_case_detail()` | Fetch full case detail with movements |
| `download_document()` | Download document PDF |

#### Scenario: Fetch Laboral cases

- GIVEN authenticated session for RUT "12345678-9"
- WHEN `laboral_scraper.get_my_cases(session)` is called
- THEN cases from `consultaMisCausasLaboral.php` MUST be returned
- AND each case MUST have `rol`, `tribunal`, `caratulado`, `fecha_ingreso`

#### Scenario: Laboral case detail

- GIVEN a valid `case_token` from Laboral search
- WHEN `get_case_detail(session, case_token)` is called
- THEN native `detalleMisCausasLaboral(token)` MUST be invoked
- AND modal `#modalDetalleMisCauLaboral` content MUST be parsed

### Requirement: Laboral Selectors

The system MUST use `selectors/laboral.yaml` for all DOM queries.

| Element | Expected Selector |
|---------|-------------------|
| `table_id` | `verDetalleMisCauLab` |
| `modal_id` | `modalDetalleMisCauLaboral` |
| `detail_function` | `detalleMisCausasLaboral` |

#### Scenario: Selector from registry

- GIVEN `laboral.yaml` defines `table_id: "#verDetalleMisCauLab"`
- WHEN scraper queries cases table
- THEN selector MUST come from registry, not hardcoded

### Requirement: Laboral Data Classes

The system MUST reuse `PJUDCase`, `PJUDMovement`, `PJUDCaseDetail` data classes.

#### Scenario: Laboral case type

- GIVEN a Laboral case with ROL "O-1234-2026"
- WHEN `case.tipo` is accessed
- THEN it MUST return "O"
