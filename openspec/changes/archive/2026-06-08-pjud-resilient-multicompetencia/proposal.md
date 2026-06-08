# Proposal: PJUD Resilient Multi-Competencia

## Intent

Transform the single-competency PJUD Civil scraper into a production-grade multi-competency system supporting Laboral and Penal. Current state: hardcoded selectors, no retry logic, no observability. Target state: resilient, observable, dynamically configurable scraper serving 40-60 concurrent users with HIGH criticality and immediate failure alerts.

## Scope

### In Scope
- Base class extraction from `pjud_civil.py` → `PJUDBaseScraper`
- Selector registry (YAML) with hot-reload capability
- Resilience layer: circuit breaker, retry with backoff, health checks, rate limiter
- Laboral scraper subclass
- Penal scraper subclass
- Observability: structured logging, metrics, webhook alerts
- Structure change detection (HTML hash monitoring)

### Out of Scope
- Cobranza competency (not needed now)
- Familia competency (not needed now)
- UI for selector management
- Database storage of scrape results
- API endpoint changes (separate change)

## Capabilities

### New Capabilities
- `pjud-selector-registry`: YAML-based selector storage with hot-reload, fallback selectors
- `pjud-resilience`: Circuit breaker (5 failures/60s), exponential backoff (1-30s), health checks
- `pjud-laboral`: Laboral competency scraping with full case lifecycle
- `pjud-penal`: Penal competency scraping with full case lifecycle
- `pjud-observability`: Structured JSON logging, Prometheus metrics, webhook alerts

### Modified Capabilities
- `pjud-civil`: Refactor to subclass of `PJUDBaseScraper`, migrate to selector registry

## Approach

1. Extract abstract `PJUDBaseScraper` with common logic (login, session, pagination, documents)
2. Create `SelectorRegistry` class loading from YAML with file watcher for hot-reload
3. Implement resilience decorators/context managers (circuit breaker, retry)
4. Subclass for each competency (Civil refactor, then Laboral, Penal)
5. Add observability layer with configurable alert webhooks

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `app/scrapper/pjud_civil.py` | Modified | Refactor into base + subclass |
| `app/scrapper/pjud/` | New | New package structure |
| `app/scrapper/pjud/selectors/` | New | Selector registry + YAML files |
| `app/scrapper/pjud/resilience/` | New | Circuit breaker, retry, health |
| `app/scrapper/pjud/observability/` | New | Metrics, alerts, logging |

## Risks

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| PJUD structure changes | Medium | High | Health checks + fallback selectors + alerts |
| Rate limiting by PJUD | Medium | Medium | Configurable delays + circuit breaker |
| Concurrent user overload | Low | High | Rate limiter + connection pooling |
| YAML syntax errors in prod | Low | Medium | Validation on load + last-known-good fallback |

## Rollback Plan

1. Keep original `pjud_civil.py` as `pjud_civil_legacy.py` during transition
2. Feature flag to switch between legacy and new implementation
3. If critical issues: revert to legacy, alert via webhook, investigate

## Dependencies

- Playwright (existing)
- PyYAML for selector registry
- watchdog (optional) for file hot-reload

## Success Criteria

- [ ] Civil scraper works identically after refactor (existing tests pass)
- [ ] Laboral scraper fetches cases matching Civil's feature set
- [ ] Penal scraper fetches cases matching Civil's feature set
- [ ] Selector changes apply without code deploy
- [ ] Circuit breaker triggers after 5 failures, recovers after 60s
- [ ] Webhook alert fires within 30s of first failure
- [ ] Health check detects structure changes within 5 minutes
