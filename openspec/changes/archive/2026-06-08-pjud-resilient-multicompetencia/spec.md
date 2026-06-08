# Specifications: pjud-resilient-multicompetencia

## Overview

Delta and new specifications for transforming single-competency Civil scraper into production-grade multi-competency system.

## Spec Index

| Domain | Type | Path |
|--------|------|------|
| pjud-base | New | `specs/pjud-base/spec.md` |
| pjud-selector-registry | New | `specs/pjud-selector-registry/spec.md` |
| pjud-resilience | New | `specs/pjud-resilience/spec.md` |
| pjud-laboral | New | `specs/pjud-laboral/spec.md` |
| pjud-penal | New | `specs/pjud-penal/spec.md` |
| pjud-observability | New | `specs/pjud-observability/spec.md` |
| pjud-structure-detection | New | `specs/pjud-structure-detection/spec.md` |
| pjud-civil | Delta | `specs/pjud-civil/spec.md` |

## Summary by Capability

### SPEC-001: PJUDBaseScraper
- Abstract base with login, session, pagination
- Template methods for competency-specific parsing
- Maintains backward compatibility

### SPEC-002: SelectorRegistry  
- YAML loading with primary + fallback selectors
- Hot-reload via file watcher or endpoint
- Validation with last-known-good fallback

### SPEC-003: Resilience
- Circuit breaker: 5 failures/60s, 30s recovery
- Retry: exponential backoff 1s→30s, max 3 retries
- Health check: 5 min interval
- Rate limiter: configurable, default 10 req/sec

### SPEC-004: LaboralScraper
- Extends PJUDBaseScraper
- Uses laboral.yaml selectors
- Same API as CivilScraper

### SPEC-005: PenalScraper
- Extends PJUDBaseScraper
- Uses penal.yaml selectors
- Same API as CivilScraper

### SPEC-006: Observability
- Structured JSON logging with context
- Prometheus metrics: cases_scraped, request_duration, errors, circuit_state
- Webhook alerts on circuit open, health fail, structure change

### SPEC-007: StructureChangeDetection
- Hash key HTML elements per competency
- Baseline on first success
- Compare on health check, alert on drift
