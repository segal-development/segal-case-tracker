# Penal Scraper Specification

## Purpose

PJUD Penal competency scraper with identical API to Civil, using base class and selector registry.

## Requirements

### Requirement: Penal API Parity

The system MUST provide `PJUDPenalScraper` with same public API as `PJUDCivilScraper`.

| Method | Description |
|--------|-------------|
| `get_my_cases()` | Fetch lawyer's Penal cases with pagination |
| `get_my_cases_recent()` | Fetch last N years of cases |
| `get_case_detail()` | Fetch full case detail with movements |
| `download_document()` | Download document PDF |

#### Scenario: Fetch Penal cases

- GIVEN authenticated session for RUT "12345678-9"
- WHEN `penal_scraper.get_my_cases(session)` is called
- THEN cases from `consultaMisCausasPenal.php` MUST be returned
- AND each case MUST have `rol`, `tribunal`, `caratulado`, `fecha_ingreso`

#### Scenario: Penal case detail

- GIVEN a valid `case_token` from Penal search
- WHEN `get_case_detail(session, case_token)` is called
- THEN native `detalleMisCausasPenal(token)` MUST be invoked
- AND modal `#modalDetalleMisCauPenal` content MUST be parsed

### Requirement: Penal Selectors

The system MUST use `selectors/penal.yaml` for all DOM queries.

| Element | Expected Selector |
|---------|-------------------|
| `table_id` | `verDetalleMisCauPen` |
| `modal_id` | `modalDetalleMisCauPenal` |
| `detail_function` | `detalleMisCausasPenal` |

#### Scenario: Selector from registry

- GIVEN `penal.yaml` defines `modal_id: "#modalDetalleMisCauPenal"`
- WHEN scraper opens case detail modal
- THEN selector MUST come from registry, not hardcoded

### Requirement: Penal Data Classes

The system MUST reuse `PJUDCase`, `PJUDMovement`, `PJUDCaseDetail` data classes.

#### Scenario: Penal case type

- GIVEN a Penal case with ROL "RIT-5678-2026"
- WHEN `case.tipo` is accessed
- THEN it MUST return "RIT"
