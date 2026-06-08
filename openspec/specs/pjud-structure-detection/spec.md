# Structure Change Detection Specification

## Purpose

Detect PJUD HTML structure changes early via hash monitoring to prevent silent scraper failures.

## Requirements

### Requirement: Hash Baseline

The system MUST capture baseline hashes of key HTML elements per competency.

| Element | Description |
|---------|-------------|
| Table structure | Case list table headers and row structure |
| Modal structure | Detail modal container and key fields |
| Form fields | Search form input names and IDs |

#### Scenario: Baseline captured on first success

- GIVEN Civil scraper has never run
- WHEN first successful `get_my_cases()` completes
- THEN baseline hash MUST be stored with timestamp
- AND hash MUST cover table ID, column headers, and row structure

### Requirement: Drift Detection

The system MUST compare current structure against baseline on each health check.

#### Scenario: Structure unchanged

- GIVEN baseline hash is `abc123`
- WHEN health check computes hash `abc123`
- THEN no alert MUST fire
- AND health status MUST remain HEALTHY

#### Scenario: Structure changed

- GIVEN baseline hash is `abc123`
- WHEN health check computes hash `xyz789`
- THEN alert webhook MUST fire immediately
- AND health status MUST become WARNING
- AND log MUST include old hash, new hash, and elements that changed

### Requirement: Baseline Update

The system MUST allow manual baseline update after verified structure change.

#### Scenario: Admin updates baseline

- GIVEN structure change was verified as intentional PJUD update
- WHEN admin calls `registry.update_baseline("civil")`
- THEN new hash MUST become baseline
- AND previous baseline MUST be archived with timestamp

### Requirement: Hash Stability

The hash MUST be stable across irrelevant changes (whitespace, dynamic IDs).

#### Scenario: Ignore dynamic content

- GIVEN table contains dynamic session tokens
- WHEN computing structure hash
- THEN dynamic values MUST be normalized or excluded
- AND hash MUST depend only on structural elements
