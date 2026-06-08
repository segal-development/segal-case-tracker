# Tasks: PJUD Resilient Multi-Competencia

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~1250 lines |
| 400-line budget risk | High |
| Chained PRs recommended | Yes |
| Suggested split | PR1 → PR2 → PR3 → PR4 → PR5 |
| Delivery strategy | auto-chain |
| Chain strategy | stacked-to-main |

Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: High

### Suggested Work Units

| Unit | Goal | Likely PR | Notes |
|------|------|-----------|-------|
| 1 | Base class + Civil refactor + exceptions | PR1 | Base: main; ~300 lines |
| 2 | Selector registry + YAML files | PR2 | Base: main (after PR1); ~250 lines |
| 3 | Resilience layer (CB, retry, health, rate) | PR3 | Base: main (after PR2); ~300 lines |
| 4 | Laboral + Penal scrapers | PR4 | Base: main (after PR3); ~200 lines |
| 5 | Observability (logging, metrics, alerts) | PR5 | Base: main (after PR4); ~200 lines |

---

## PR1: Base Class + Civil Refactor (~300 lines)

### Phase 1: Foundation

- [ ] 1.1 Create `app/scrapper/pjud/__init__.py` — package exports
- [ ] 1.2 Create `app/scrapper/pjud/exceptions.py` — `PJUDError`, `CircuitOpenError`, `SelectorNotFoundError`, `StructureChangeError`
- [ ] 1.3 Create `app/scrapper/pjud/base.py` — `PJUDBaseScraper` ABC with template methods

### Phase 2: Civil Refactor

- [ ] 2.1 Create `app/scrapper/pjud/civil.py` — `CivilScraper(PJUDBaseScraper)` with abstract method implementations
- [ ] 2.2 Update `app/scrapper/pjud_civil.py` — import forwarding to new module (backward compat)
- [ ] 2.3 Update `app/api/v1/pjud.py` imports if needed

### Phase 3: Tests

- [ ] 3.1 Test `PJUDBaseScraper` rejects direct instantiation
- [ ] 3.2 Test `CivilScraper` implements all abstract methods
- [ ] 3.3 Test existing Civil API unchanged (regression)

---

## PR2: Selector Registry (~250 lines)

### Phase 1: Registry Core

- [ ] 1.1 Create `app/scrapper/pjud/selectors/__init__.py`
- [ ] 1.2 Create `app/scrapper/pjud/selectors/registry.py` — `SelectorRegistry` with `get()`, `reload()`, fallback chain
- [ ] 1.3 Create `app/scrapper/pjud/selectors/civil.yaml` — Civil selectors (table_id, modal_id, detail_function)

### Phase 2: Integration

- [ ] 2.1 Integrate `SelectorRegistry` into `CivilScraper.__init__`
- [ ] 2.2 Replace hardcoded selectors in `CivilScraper` with registry calls
- [ ] 2.3 Add `/api/v1/pjud/selectors/reload` endpoint (optional, if API exists)

### Phase 3: Tests

- [ ] 3.1 Test YAML loading and selector retrieval
- [ ] 3.2 Test fallback when primary selector missing
- [ ] 3.3 Test `reload()` updates in-memory selectors
- [ ] 3.4 Test invalid YAML retains previous config

---

## PR3: Resilience Layer (~300 lines)

### Phase 1: Components

- [ ] 1.1 Create `app/scrapper/pjud/resilience/__init__.py`
- [ ] 1.2 Create `app/scrapper/pjud/resilience/circuit_breaker.py` — `@circuit_breaker` decorator, `CircuitBreakerConfig`
- [ ] 1.3 Create `app/scrapper/pjud/resilience/retry.py` — `@with_retry` decorator with exponential backoff
- [ ] 1.4 Create `app/scrapper/pjud/resilience/rate_limiter.py` — `TokenBucketLimiter` class
- [ ] 1.5 Create `app/scrapper/pjud/resilience/health.py` — `HealthChecker` background task

### Phase 2: Integration

- [ ] 2.1 Add resilience config to `app/config.py` (CB threshold, retry delays, rate limit)
- [ ] 2.2 Decorate `PJUDBaseScraper.get_cases` with `@circuit_breaker`, `@with_retry`
- [ ] 2.3 Integrate rate limiter into base scraper request flow

### Phase 3: Tests

- [ ] 3.1 Test circuit opens after 5 failures, closes after timeout
- [ ] 3.2 Test retry with exponential backoff (mock asyncio.sleep)
- [ ] 3.3 Test rate limiter blocks when quota exhausted
- [ ] 3.4 Test health checker detects PJUD unavailable

---

## PR4: Laboral + Penal Scrapers (~200 lines)

### Phase 1: Selectors

- [ ] 1.1 Create `app/scrapper/pjud/selectors/laboral.yaml`
- [ ] 1.2 Create `app/scrapper/pjud/selectors/penal.yaml`

### Phase 2: Scrapers

- [ ] 2.1 Create `app/scrapper/pjud/laboral.py` — `LaboralScraper(PJUDBaseScraper)`
- [ ] 2.2 Create `app/scrapper/pjud/penal.py` — `PenalScraper(PJUDBaseScraper)`
- [ ] 2.3 Export new scrapers from `app/scrapper/pjud/__init__.py`

### Phase 3: API (if needed)

- [ ] 3.1 Add Laboral endpoints to `app/api/v1/pjud.py`
- [ ] 3.2 Add Penal endpoints to `app/api/v1/pjud.py`

### Phase 4: Tests

- [ ] 4.1 Test `LaboralScraper` fetches cases (mocked)
- [ ] 4.2 Test `PenalScraper` fetches cases (mocked)
- [ ] 4.3 Test selector YAML loads correctly for each competency

---

## PR5: Observability (~200 lines)

### Phase 1: Components

- [ ] 1.1 Create `app/scrapper/pjud/observability/__init__.py`
- [ ] 1.2 Create `app/scrapper/pjud/observability/logging.py` — structlog setup, context propagation
- [ ] 1.3 Create `app/scrapper/pjud/observability/metrics.py` — Prometheus metrics (`pjud_cases_scraped_total`, etc.)
- [ ] 1.4 Create `app/scrapper/pjud/observability/alerts.py` — webhook dispatcher

### Phase 2: Integration

- [ ] 2.1 Add `/metrics` endpoint in Prometheus format
- [ ] 2.2 Integrate structured logging into `PJUDBaseScraper` operations
- [ ] 2.3 Wire circuit breaker state changes to alert dispatcher
- [ ] 2.4 Add alert webhook URL to `app/config.py`

### Phase 3: Tests

- [ ] 3.1 Test metrics increment on scrape
- [ ] 3.2 Test webhook fires on circuit open (mock HTTP)
- [ ] 3.3 Test structured log output format
