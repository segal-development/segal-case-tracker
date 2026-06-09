"""
PJUD Scraper Package.

Multi-competency scraper for Chile's PJUD (Poder Judicial) portal.
Supports Civil, Laboral, and Penal competencies.
"""

from app.scrapper.pjud.base import (
    PJUDBaseScraper,
    PJUDCase,
    PJUDDocument,
    PJUDMovement,
    PJUDCaseDetail,
    CompetencyConfig,
    PJUD_BASE_URL,
    PJUD_HOME_URL,
    PJUD_INDEX_URL,
)

from app.scrapper.pjud.civil import CivilScraper, reload_selectors, get_selector_registry
from app.scrapper.pjud.laboral import LaboralScraper
from app.scrapper.pjud.penal import PenalScraper
from app.scrapper.pjud.browser import BrowserFactory

from app.scrapper.pjud.selectors import SelectorRegistry, SelectorConfig

from app.scrapper.pjud.exceptions import (
    PJUDError,
    LoginError,
    SessionExpiredError,
    ScrapingError,
    SelectorNotFoundError,
    StructureChangeError,
    CircuitOpenError,
    RateLimitError,
)

from app.scrapper.pjud.resilience import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    circuit_breaker,
    RetryConfig,
    with_retry,
    TokenBucketLimiter,
    RateLimiterConfig,
    rate_limit,
    HealthChecker,
    HealthStatus,
    HealthCheckResult,
)

from app.scrapper.pjud.resilience.circuit_breaker import get_circuit_breaker
from app.scrapper.pjud.resilience.rate_limiter import get_rate_limiter
from app.scrapper.pjud.resilience.health import get_health_checker

from app.scrapper.pjud.observability import (
    # Logging
    configure_logging,
    get_logger,
    log_operation,
    LogContext,
    # Metrics
    PJUDMetrics,
    get_metrics,
    metrics_endpoint,
    # Alerts
    AlertManager,
    AlertSeverity,
    get_alert_manager,
    send_alert,
)


__all__ = [
    # Base classes
    "PJUDBaseScraper",
    "CompetencyConfig",
    
    # Data classes
    "PJUDCase",
    "PJUDDocument",
    "PJUDMovement",
    "PJUDCaseDetail",
    
    # Scrapers
    "CivilScraper",
    "LaboralScraper",
    "PenalScraper",
    
    # Browser Factory
    "BrowserFactory",
    
    # Selector Registry
    "SelectorRegistry",
    "SelectorConfig",
    "reload_selectors",
    "get_selector_registry",
    
    # Exceptions
    "PJUDError",
    "LoginError",
    "SessionExpiredError",
    "ScrapingError",
    "SelectorNotFoundError",
    "StructureChangeError",
    "CircuitOpenError",
    "RateLimitError",
    
    # Resilience
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitState",
    "circuit_breaker",
    "get_circuit_breaker",
    "RetryConfig",
    "with_retry",
    "TokenBucketLimiter",
    "RateLimiterConfig",
    "rate_limit",
    "get_rate_limiter",
    "HealthChecker",
    "HealthStatus",
    "HealthCheckResult",
    "get_health_checker",
    
    # Constants
    "PJUD_BASE_URL",
    "PJUD_HOME_URL",
    "PJUD_INDEX_URL",
    
    # Observability - Logging
    "configure_logging",
    "get_logger",
    "log_operation",
    "LogContext",
    
    # Observability - Metrics
    "PJUDMetrics",
    "get_metrics",
    "metrics_endpoint",
    
    # Observability - Alerts
    "AlertManager",
    "AlertSeverity",
    "get_alert_manager",
    "send_alert",
]
