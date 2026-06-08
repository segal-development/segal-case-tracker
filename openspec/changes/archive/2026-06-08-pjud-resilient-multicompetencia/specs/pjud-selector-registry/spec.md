# Selector Registry Specification

## Purpose

YAML-based selector storage with hot-reload, fallback chains, and validation for dynamic PJUD structure adaptation.

## Requirements

### Requirement: YAML Selector Loading

The system MUST load CSS selectors from YAML files per competency.

| Field | Required | Description |
|-------|----------|-------------|
| `element_name` | Yes | Logical name (e.g., `case_table`) |
| `primary` | Yes | Primary CSS selector |
| `fallback` | No | Fallback selector if primary fails |
| `description` | No | Human-readable purpose |

#### Scenario: Load selectors on startup

- GIVEN `selectors/civil.yaml` exists with valid content
- WHEN `SelectorRegistry.load("civil")` is called
- THEN selectors MUST be accessible via `registry.get("civil", "case_table")`

#### Scenario: Fallback selector on primary failure

- GIVEN primary selector `#oldTable` no longer exists in DOM
- AND fallback selector `#verDetalleMisCauCiv` is defined
- WHEN the scraper queries the element
- THEN it MUST use the fallback selector automatically

### Requirement: Hot Reload

The system SHOULD support hot-reload without restart.

#### Scenario: Detect YAML change via file watcher

- GIVEN `watchdog` is available and enabled
- WHEN `selectors/civil.yaml` is modified
- THEN the registry MUST reload within 5 seconds
- AND active scrapers MUST use updated selectors

#### Scenario: Reload via explicit endpoint

- GIVEN hot-reload is triggered via `registry.reload("civil")`
- WHEN the YAML is valid
- THEN new selectors MUST be active immediately

### Requirement: Validation and Fallback

The system MUST validate YAML on load and retain last-known-good on error.

#### Scenario: Invalid YAML retains previous

- GIVEN registry has valid selectors loaded
- WHEN a reload encounters invalid YAML syntax
- THEN the previous selectors MUST remain active
- AND an error MUST be logged with file path and line number

#### Scenario: Startup with invalid YAML fails

- GIVEN no previous selectors exist
- WHEN initial load encounters invalid YAML
- THEN startup MUST fail with clear error message
