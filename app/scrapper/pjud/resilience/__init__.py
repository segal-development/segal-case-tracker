"""
PJUD Resilience Package.

Production-grade resilience patterns for PJUD scraping:
- Circuit breaker for fail-fast behavior
- Retry with exponential backoff
- Rate limiting with token bucket
- Health checking with structure detection
"""

from app.scrapper.pjud.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    circuit_breaker,
    get_circuit_breaker,
)

from app.scrapper.pjud.resilience.retry import (
    RetryConfig,
    with_retry,
)

from app.scrapper.pjud.resilience.rate_limiter import (
    TokenBucketLimiter,
    RateLimiterConfig,
    rate_limit,
)

from app.scrapper.pjud.resilience.health import (
    HealthChecker,
    HealthStatus,
    HealthCheckResult,
    get_health_checker,
)

from app.scrapper.pjud.resilience.integration import (
    resilient_scrape,
    get_competency_circuit_breaker,
    get_competency_rate_limiter,
    record_scrape_success,
    record_scrape_error,
    get_circuit_state,
    reset_circuit,
)


__all__ = [
    # Circuit Breaker
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitState",
    "circuit_breaker",
    "get_circuit_breaker",
    
    # Retry
    "RetryConfig",
    "with_retry",
    
    # Rate Limiter
    "TokenBucketLimiter",
    "RateLimiterConfig",
    "rate_limit",
    
    # Health Check
    "HealthChecker",
    "HealthStatus",
    "HealthCheckResult",
    "get_health_checker",
    
    # Integration
    "resilient_scrape",
    "get_competency_circuit_breaker",
    "get_competency_rate_limiter",
    "record_scrape_success",
    "record_scrape_error",
    "get_circuit_state",
    "reset_circuit",
]
