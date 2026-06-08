# PJUDBaseScraper Specification

## Purpose

Abstract base class providing shared login, session, pagination, and document handling for all PJUD competency scrapers.

## Requirements

### Requirement: Base Class Abstraction

The system MUST provide an abstract `PJUDBaseScraper` class that encapsulates common PJUD functionality.

| Method | Type | Description |
|--------|------|-------------|
| `login_with_token()` | Concrete | Authenticate via reCAPTCHA token |
| `get_session()` | Concrete | Retrieve cached session |
| `_get_page()` | Concrete | Get Playwright page with session |
| `_ensure_panel_loaded()` | Abstract | Load competency-specific panel |
| `_get_table_id()` | Abstract | Return competency's table ID |
| `_get_modal_id()` | Abstract | Return competency's modal ID |
| `_get_detail_function()` | Abstract | Return JS function name for detail |

#### Scenario: Subclass implements abstract methods

- GIVEN a subclass `PJUDLaboralScraper`
- WHEN it inherits from `PJUDBaseScraper`
- THEN it MUST implement `_get_table_id()` returning `verDetalleMisCauLab`
- AND it MUST implement `_get_modal_id()` returning `modalDetalleMisCauLaboral`

#### Scenario: Base class rejects direct instantiation

- GIVEN `PJUDBaseScraper` is abstract
- WHEN a client attempts to instantiate it directly
- THEN a `TypeError` MUST be raised

### Requirement: Session Lifecycle

The system MUST manage browser session lifecycle with proper cleanup.

#### Scenario: Session restored from cache

- GIVEN a valid cached `PJUDSession` for RUT "12345678-9"
- WHEN `_get_page(session)` is called
- THEN cookies MUST be restored to the browser context
- AND localStorage MUST be restored to the page

#### Scenario: Session cleanup on stop

- GIVEN an active browser context
- WHEN `stop()` is called
- THEN page, context, browser, and playwright MUST be closed
- AND no resources MUST leak

### Requirement: API Compatibility

The base class MUST NOT break existing `PJUDCivilScraper` public API.

#### Scenario: Civil API unchanged after refactor

- GIVEN the refactored `PJUDCivilScraper` extends `PJUDBaseScraper`
- WHEN existing tests call `get_my_cases()`, `get_case_detail()`, `download_document()`
- THEN all tests MUST pass without modification
