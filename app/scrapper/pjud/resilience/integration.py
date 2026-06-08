"""
Resilience integration for PJUD scrapers.

This module provides pre-configured resilience wrappers that integrate
circuit breaker, retry, rate limiting, and observability.

Usage:
    from app.scrapper.pjud.resilience.integration import resilient_scrape

    @resilient_scrape(competency="civil")
    async def my_scrape_function():
        ...
"""

import functools
import time
from typing import Callable, Optional, Any

from app.scrapper.pjud.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    get_circuit_breaker,
)
from app.scrapper.pjud.resilience.retry import RetryConfig, with_retry
from app.scrapper.pjud.resilience.rate_limiter import (
    TokenBucketLimiter,
    RateLimiterConfig,
    get_rate_limiter,
)
from app.scrapper.pjud.observability.logging import get_logger, LogContext
from app.scrapper.pjud.observability.metrics import get_metrics
from app.scrapper.pjud.observability.alerts import send_alert, AlertSeverity
from app.scrapper.pjud.exceptions import CircuitOpenError, RateLimitError

from app.config import settings


# Pre-configured circuit breakers per competency
_circuit_breakers: dict[str, CircuitBreaker] = {}

# Pre-configured rate limiters
_rate_limiters: dict[str, TokenBucketLimiter] = {}


def get_competency_circuit_breaker(competency: str) -> CircuitBreaker:
    """Get or create circuit breaker for a competency."""
    name = f"pjud-{competency}"
    if name not in _circuit_breakers:
        config = CircuitBreakerConfig(
            failure_threshold=getattr(settings, 'PJUD_CB_FAILURE_THRESHOLD', 5),
            recovery_timeout=getattr(settings, 'PJUD_CB_RECOVERY_TIMEOUT', 60),
            half_open_max_calls=3,
        )
        _circuit_breakers[name] = CircuitBreaker(name=name, config=config)
    return _circuit_breakers[name]


def get_competency_rate_limiter(competency: str) -> TokenBucketLimiter:
    """Get or create rate limiter for a competency."""
    name = f"pjud-{competency}"
    if name not in _rate_limiters:
        config = RateLimiterConfig(
            rate=getattr(settings, 'PJUD_RATE_LIMIT', 10),
            burst=getattr(settings, 'PJUD_RATE_LIMIT', 10) * 2,
        )
        _rate_limiters[name] = TokenBucketLimiter(name=name, config=config)
    return _rate_limiters[name]


def resilient_scrape(
    competency: str,
    operation: str = "scrape",
    use_circuit_breaker: bool = True,
    use_retry: bool = True,
    use_rate_limit: bool = True,
    use_logging: bool = True,
    use_metrics: bool = True,
    use_alerts: bool = True,
):
    """
    Decorator that wraps a scraping function with full resilience stack.
    
    Applies (in order):
    1. Rate limiting (before call)
    2. Circuit breaker check (before call)
    3. Retry with exponential backoff (wraps call)
    4. Logging (entry/exit)
    5. Metrics (success/failure counters, duration)
    6. Alerts (on circuit open)
    
    Args:
        competency: "civil", "laboral", or "penal"
        operation: Name of the operation for logging/metrics
        use_circuit_breaker: Enable circuit breaker
        use_retry: Enable retry with backoff
        use_rate_limit: Enable rate limiting
        use_logging: Enable structured logging
        use_metrics: Enable metrics collection
        use_alerts: Enable alerts on circuit open
    
    Example:
        @resilient_scrape(competency="civil", operation="get_cases")
        async def get_cases(session, year):
            ...
    """
    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            logger = get_logger(__name__) if use_logging else None
            metrics = get_metrics() if use_metrics else None
            cb = get_competency_circuit_breaker(competency) if use_circuit_breaker else None
            rl = get_competency_rate_limiter(competency) if use_rate_limit else None
            
            start_time = time.time()
            context = LogContext(
                competency=competency,
                operation=operation,
            )
            
            # 1. Rate limiting
            if rl:
                acquired = await rl.acquire(timeout=5.0)
                if not acquired:
                    if logger:
                        logger.warning(f"Rate limit exceeded for {competency}", extra=context.to_dict())
                    if metrics:
                        metrics.increment("pjud_rate_limit_exceeded", {"competency": competency})
                    raise RateLimitError(f"Rate limit exceeded for {competency}")
            
            # 2. Circuit breaker check
            if cb and cb.state == CircuitState.OPEN:
                if logger:
                    logger.warning(f"Circuit open for {competency}", extra=context.to_dict())
                if metrics:
                    metrics.increment("pjud_circuit_rejected", {"competency": competency})
                raise CircuitOpenError(f"Circuit breaker open for {competency}")
            
            # 3. Execute with retry
            if logger:
                logger.info(f"Starting {operation} for {competency}", extra=context.to_dict())
            
            try:
                if use_retry:
                    # Apply retry logic
                    max_attempts = getattr(settings, 'PJUD_RETRY_MAX_ATTEMPTS', 3)
                    result = await _execute_with_retry(func, args, kwargs, max_attempts, logger, context)
                else:
                    result = await func(*args, **kwargs)
                
                # Success
                duration = time.time() - start_time
                
                if cb:
                    cb.record_success()
                
                if metrics:
                    metrics.increment("pjud_requests_total", {"competency": competency, "status": "success"})
                    metrics.observe("pjud_request_duration_seconds", duration, {"competency": competency})
                
                if logger:
                    logger.info(
                        f"Completed {operation} for {competency} in {duration:.2f}s",
                        extra={**context.to_dict(), "duration_seconds": duration}
                    )
                
                return result
                
            except Exception as e:
                duration = time.time() - start_time
                
                if cb:
                    cb.record_failure()
                    # Check if circuit just opened
                    if use_alerts and cb.state == CircuitState.OPEN:
                        await send_alert(
                            severity=AlertSeverity.CRITICAL,
                            title=f"Circuit Breaker Open: {competency}",
                            message=f"PJUD {competency} circuit breaker opened after repeated failures",
                            context={
                                "competency": competency,
                                "operation": operation,
                                "error": str(e),
                                "failure_count": cb.failure_count,
                            }
                        )
                
                if metrics:
                    metrics.increment("pjud_requests_total", {"competency": competency, "status": "error"})
                    metrics.increment("pjud_errors_total", {"competency": competency, "error_type": type(e).__name__})
                
                if logger:
                    logger.error(
                        f"Failed {operation} for {competency}: {e}",
                        extra={**context.to_dict(), "error": str(e), "duration_seconds": duration}
                    )
                
                raise
        
        return wrapper
    return decorator


async def _execute_with_retry(
    func: Callable,
    args: tuple,
    kwargs: dict,
    max_attempts: int,
    logger: Optional[Any],
    context: LogContext,
) -> Any:
    """Execute function with exponential backoff retry."""
    import asyncio
    
    last_error = None
    base_delay = 1.0
    max_delay = 30.0
    
    for attempt in range(1, max_attempts + 1):
        try:
            return await func(*args, **kwargs)
        except CircuitOpenError:
            # Don't retry if circuit is open
            raise
        except RateLimitError:
            # Don't retry if rate limited
            raise
        except Exception as e:
            last_error = e
            
            if attempt < max_attempts:
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                if logger:
                    logger.warning(
                        f"Attempt {attempt}/{max_attempts} failed, retrying in {delay:.1f}s: {e}",
                        extra=context.to_dict()
                    )
                await asyncio.sleep(delay)
            else:
                if logger:
                    logger.error(
                        f"All {max_attempts} attempts failed",
                        extra=context.to_dict()
                    )
    
    raise last_error


# Convenience functions for manual resilience control

def record_scrape_success(competency: str, cases_count: int = 0):
    """Record a successful scrape operation."""
    metrics = get_metrics()
    if cases_count > 0:
        metrics.cases_scraped.inc(cases_count, competency=competency)


def record_scrape_error(competency: str, error_type: str):
    """Record a scrape error."""
    metrics = get_metrics()
    metrics.record_error(competency=competency, error_type=error_type)


def get_circuit_state(competency: str) -> str:
    """Get current circuit breaker state."""
    cb = get_competency_circuit_breaker(competency)
    return cb.state.value


async def reset_circuit(competency: str):
    """Manually reset circuit breaker."""
    cb = get_competency_circuit_breaker(competency)
    await cb.reset()


def reset_circuit_sync(competency: str):
    """Manually reset circuit breaker (sync version for tests)."""
    cb = get_competency_circuit_breaker(competency)
    cb._state = CircuitState.CLOSED
    cb._failure_count = 0
    cb._success_count = 0


__all__ = [
    "resilient_scrape",
    "get_competency_circuit_breaker",
    "get_competency_rate_limiter",
    "record_scrape_success",
    "record_scrape_error",
    "get_circuit_state",
    "reset_circuit",
    "reset_circuit_sync",
]
