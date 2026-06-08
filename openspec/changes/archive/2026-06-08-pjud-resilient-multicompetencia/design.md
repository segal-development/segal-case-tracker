# Design: PJUD Resilient Multi-Competencia

## Technical Approach

Template Method pattern for scraper hierarchy with composition for cross-cutting concerns (resilience, observability). Selector registry provides runtime-configurable selectors with hot-reload. Decorators wrap scraper methods for retry/circuit-breaker without coupling to base class.

## Architecture Decisions

| Decision | Options Considered | Choice | Rationale |
|----------|-------------------|--------|-----------|
| Base class vs Protocol | ABC / Protocol | ABC with Template Method | Enforce scraping flow, allow override hooks |
| Selector storage | YAML / JSON / DB | YAML files | Human-readable, git-trackable, no DB dependency |
| Hot-reload trigger | File watcher / Endpoint / Manual | Reload endpoint | Simpler than watchdog, explicit over implicit |
| Circuit breaker impl | tenacity / custom / circuitbreaker | Custom decorator | Async support, fits existing patterns |
| Rate limiter algorithm | Token bucket / Sliding window | Token bucket | Simple, proven, configurable |
| Resilience composition | Inheritance / Decorators / Middleware | Decorators | Composable, testable, doesn't pollute base class |
| Observability | structlog / standard logging | structlog | Structured JSON, context propagation |

## Data Flow

```
Request
    │
    ▼
┌─────────────────┐
│  Rate Limiter   │ ── blocks if quota exhausted
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Circuit Breaker │ ── fast-fail if open
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Retry w/Backoff│ ── retries on transient errors
└────────┬────────┘
         │
         ▼
┌─────────────────┐    ┌─────────────────┐
│  PJUDBaseScraper│───▶│ SelectorRegistry│
│  (Civil/Laboral)│    │   (YAML files)  │
└────────┬────────┘    └─────────────────┘
         │
         ▼
    PJUD Portal
         │
         ▼
┌─────────────────┐
│   Observability │ ── logs, metrics, alerts
└─────────────────┘
```

## File Changes

| File | Action | Description |
|------|--------|-------------|
| `app/scrapper/pjud/__init__.py` | Create | Package exports |
| `app/scrapper/pjud/base.py` | Create | `PJUDBaseScraper` ABC |
| `app/scrapper/pjud/civil.py` | Create | `CivilScraper` subclass |
| `app/scrapper/pjud/laboral.py` | Create | `LaboralScraper` subclass |
| `app/scrapper/pjud/penal.py` | Create | `PenalScraper` subclass |
| `app/scrapper/pjud/selectors/registry.py` | Create | `SelectorRegistry` class |
| `app/scrapper/pjud/selectors/civil.yaml` | Create | Civil selectors |
| `app/scrapper/pjud/selectors/laboral.yaml` | Create | Laboral selectors |
| `app/scrapper/pjud/selectors/penal.yaml` | Create | Penal selectors |
| `app/scrapper/pjud/resilience/circuit_breaker.py` | Create | `@circuit_breaker` decorator |
| `app/scrapper/pjud/resilience/retry.py` | Create | `@with_retry` decorator |
| `app/scrapper/pjud/resilience/rate_limiter.py` | Create | `TokenBucketLimiter` |
| `app/scrapper/pjud/resilience/health.py` | Create | `HealthChecker` background task |
| `app/scrapper/pjud/observability/logging.py` | Create | Structured logging setup |
| `app/scrapper/pjud/observability/metrics.py` | Create | Prometheus metrics |
| `app/scrapper/pjud/observability/alerts.py` | Create | Webhook alert dispatcher |
| `app/scrapper/pjud_civil.py` | Modify | Import forwarding to new module |
| `app/config.py` | Modify | Add PJUD resilience settings |

## Interfaces / Contracts

```python
# base.py - Template Method pattern
class PJUDBaseScraper(ABC):
    def __init__(self, selectors: SelectorRegistry, competencia: str): ...
    
    @abstractmethod
    def get_search_url(self) -> str: ...
    
    @abstractmethod
    def get_detail_function_name(self) -> str: ...
    
    @abstractmethod
    def parse_case_row(self, row: str) -> Optional[PJUDCase]: ...
    
    # Template method - shared flow
    async def get_cases(self, session: PJUDSession, **filters) -> List[PJUDCase]:
        await self._ensure_loaded(page)
        html = await self._fetch_list(page, filters)
        return self._parse_results(html)

# selectors/registry.py
class SelectorRegistry:
    def __init__(self, selectors_dir: Path): ...
    def get(self, competencia: str, key: str) -> str: ...
    def reload(self, competencia: Optional[str] = None) -> None: ...

# resilience/circuit_breaker.py
@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5
    recovery_timeout: int = 60
    half_open_max_calls: int = 3

def circuit_breaker(config: CircuitBreakerConfig):
    def decorator(func): ...

# resilience/health.py
class HealthChecker:
    async def start(self, interval: int = 300): ...
    async def check_competencia(self, competencia: str) -> HealthStatus: ...
```

## Testing Strategy

| Layer | What to Test | Approach |
|-------|-------------|----------|
| Unit | `SelectorRegistry` loading, fallback | pytest with temp YAML files |
| Unit | Circuit breaker state transitions | pytest, mock time |
| Unit | Retry backoff timing | pytest, mock asyncio.sleep |
| Unit | HTML parsers for each competencia | pytest with fixture HTML |
| Integration | Full scrape flow with mocked PJUD | pytest-playwright with route mocking |
| E2E | Health check detection | Staging environment with altered HTML |

## Migration / Rollout

1. **Phase 1**: Create new `pjud/` package alongside existing `pjud_civil.py`
2. **Phase 2**: Feature flag `PJUD_USE_NEW_SCRAPER` (default: False)
3. **Phase 3**: Forward imports from `pjud_civil.py` to new module
4. **Phase 4**: Enable flag for single user, monitor
5. **Phase 5**: Enable for all, keep legacy 30 days
6. **Rollback**: Set flag to False, restart workers

## Open Questions

- [ ] Which webhook endpoint for alerts? (Slack, Teams, custom?)
- [ ] Prometheus push gateway or pull metrics?
