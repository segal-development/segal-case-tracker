"""
Retry with exponential backoff for PJUD scraper.

Handles transient failures by retrying with increasing delays.
Skips retry when circuit breaker is open.
"""

import asyncio
import logging
import random
from dataclasses import dataclass
from functools import wraps
from typing import Callable, Optional, Tuple, Type

from app.scrapper.pjud.exceptions import CircuitOpenError


logger = logging.getLogger(__name__)


@dataclass
class RetryConfig:
    """Configuration for retry behavior.
    
    Attributes:
        max_retries: Maximum retry attempts
        base_delay: Initial delay in seconds
        max_delay: Maximum delay cap in seconds
        exponential_base: Multiplier for exponential backoff
        jitter: Add randomness to delays (0.0-1.0)
        retryable_exceptions: Exception types that trigger retry
    """
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    exponential_base: float = 2.0
    jitter: float = 0.1
    retryable_exceptions: Tuple[Type[Exception], ...] = (
        TimeoutError,
        ConnectionError,
        asyncio.TimeoutError,
    )


def calculate_delay(
    attempt: int,
    config: RetryConfig,
) -> float:
    """Calculate delay for a retry attempt.
    
    Uses exponential backoff with optional jitter:
        delay = base_delay * (exponential_base ** attempt)
        delay = min(delay, max_delay)
        delay = delay * (1 + random(0, jitter))
    
    Args:
        attempt: Current attempt number (0-indexed)
        config: Retry configuration
    
    Returns:
        Delay in seconds
    """
    delay = config.base_delay * (config.exponential_base ** attempt)
    delay = min(delay, config.max_delay)
    
    if config.jitter > 0:
        jitter_factor = 1 + (random.random() * config.jitter)
        delay *= jitter_factor
    
    return delay


def with_retry(
    config: Optional[RetryConfig] = None,
    max_retries: Optional[int] = None,
    base_delay: Optional[float] = None,
    max_delay: Optional[float] = None,
) -> Callable:
    """Decorator to add retry logic with exponential backoff.
    
    Example:
        @with_retry(max_retries=3)
        async def fetch_data():
            ...
        
        @with_retry(config=RetryConfig(base_delay=2.0))
        async def slow_operation():
            ...
    
    Args:
        config: Full retry configuration
        max_retries: Override max retries (shorthand)
        base_delay: Override base delay (shorthand)
        max_delay: Override max delay (shorthand)
    
    Returns:
        Decorated function with retry logic
    """
    # Build config from parameters
    if config is None:
        config = RetryConfig()
    
    if max_retries is not None:
        config = RetryConfig(
            max_retries=max_retries,
            base_delay=config.base_delay,
            max_delay=config.max_delay,
            exponential_base=config.exponential_base,
            jitter=config.jitter,
            retryable_exceptions=config.retryable_exceptions,
        )
    
    if base_delay is not None:
        config = RetryConfig(
            max_retries=config.max_retries,
            base_delay=base_delay,
            max_delay=config.max_delay,
            exponential_base=config.exponential_base,
            jitter=config.jitter,
            retryable_exceptions=config.retryable_exceptions,
        )
    
    if max_delay is not None:
        config = RetryConfig(
            max_retries=config.max_retries,
            base_delay=config.base_delay,
            max_delay=max_delay,
            exponential_base=config.exponential_base,
            jitter=config.jitter,
            retryable_exceptions=config.retryable_exceptions,
        )
    
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            
            for attempt in range(config.max_retries + 1):
                try:
                    if asyncio.iscoroutinefunction(func):
                        return await func(*args, **kwargs)
                    else:
                        return func(*args, **kwargs)
                    
                except CircuitOpenError:
                    # Never retry circuit breaker errors
                    logger.debug(
                        f"Retry skipped for {func.__name__}: circuit open"
                    )
                    raise
                
                except config.retryable_exceptions as e:
                    last_exception = e
                    
                    if attempt >= config.max_retries:
                        logger.warning(
                            f"Retry exhausted for {func.__name__} after "
                            f"{attempt + 1} attempts: {e}"
                        )
                        raise
                    
                    delay = calculate_delay(attempt, config)
                    logger.info(
                        f"Retry {attempt + 1}/{config.max_retries} for "
                        f"{func.__name__} in {delay:.1f}s: {e}"
                    )
                    await asyncio.sleep(delay)
                
                except Exception:
                    # Non-retryable exceptions are raised immediately
                    raise
            
            # Should not reach here, but just in case
            if last_exception:
                raise last_exception
        
        # Expose config for inspection
        wrapper.retry_config = config
        return wrapper
    
    return decorator


async def retry_async(
    func: Callable,
    config: Optional[RetryConfig] = None,
    *args,
    **kwargs,
):
    """Execute an async function with retry logic.
    
    Functional alternative to the decorator for one-off retries.
    
    Example:
        result = await retry_async(
            fetch_data,
            RetryConfig(max_retries=5),
            session=session,
        )
    
    Args:
        func: Async function to execute
        config: Retry configuration
        *args: Positional arguments for func
        **kwargs: Keyword arguments for func
    
    Returns:
        Function result
    """
    if config is None:
        config = RetryConfig()
    
    @with_retry(config=config)
    async def wrapper():
        return await func(*args, **kwargs)
    
    return await wrapper()
