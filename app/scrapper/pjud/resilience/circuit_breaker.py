"""
Circuit Breaker for PJUD scraper.

Implements the circuit breaker pattern to fail-fast when PJUD is unavailable,
preventing cascading failures and allowing recovery time.

States:
- CLOSED: Normal operation, requests pass through
- OPEN: Failing fast, requests rejected immediately
- HALF_OPEN: Testing recovery, limited requests allowed
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from typing import Any, Callable, Optional, TypeVar

from app.scrapper.pjud.exceptions import CircuitOpenError


logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Failing fast
    HALF_OPEN = "half_open" # Testing recovery


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker behavior.
    
    Attributes:
        failure_threshold: Number of failures before opening circuit
        recovery_timeout: Seconds to wait before trying half-open
        half_open_max_calls: Max calls allowed in half-open state
        success_threshold: Successes needed in half-open to close
    """
    failure_threshold: int = 5
    recovery_timeout: int = 60
    half_open_max_calls: int = 3
    success_threshold: int = 2


@dataclass
class CircuitBreaker:
    """Thread-safe circuit breaker implementation.
    
    Example:
        cb = CircuitBreaker(name="pjud-civil")
        
        try:
            result = await cb.call(scraper.get_cases, session)
        except CircuitOpenError as e:
            # Circuit is open, fail fast
            return cached_result
    """
    name: str
    config: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    
    # Internal state
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _success_count: int = field(default=0, init=False)
    _last_failure_time: float = field(default=0.0, init=False)
    _half_open_calls: int = field(default=0, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    
    @property
    def state(self) -> CircuitState:
        """Current circuit state."""
        return self._state
    
    @property
    def failure_count(self) -> int:
        """Current failure count."""
        return self._failure_count
    
    @property
    def is_closed(self) -> bool:
        """Check if circuit is closed (normal operation)."""
        return self._state == CircuitState.CLOSED
    
    @property
    def is_open(self) -> bool:
        """Check if circuit is open (failing fast)."""
        return self._state == CircuitState.OPEN
    
    @property
    def time_until_recovery(self) -> int:
        """Seconds until circuit may transition to half-open."""
        if not self.is_open:
            return 0
        elapsed = time.time() - self._last_failure_time
        remaining = self.config.recovery_timeout - elapsed
        return max(0, int(remaining))
    
    async def _check_state_transition(self) -> None:
        """Check if state should transition based on time."""
        if self._state == CircuitState.OPEN:
            elapsed = time.time() - self._last_failure_time
            if elapsed >= self.config.recovery_timeout:
                logger.info(
                    f"Circuit {self.name}: OPEN -> HALF_OPEN after {elapsed:.1f}s"
                )
                self._state = CircuitState.HALF_OPEN
                self._half_open_calls = 0
                self._success_count = 0
    
    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """Execute function through circuit breaker.
        
        Args:
            func: Async function to execute
            *args: Positional arguments
            **kwargs: Keyword arguments
        
        Returns:
            Function result if successful
        
        Raises:
            CircuitOpenError: If circuit is open
            Exception: Original exception from function
        """
        async with self._lock:
            await self._check_state_transition()
            
            # Fail fast if open
            if self._state == CircuitState.OPEN:
                raise CircuitOpenError(
                    competencia=self.name,
                    recovery_time=self.time_until_recovery,
                )
            
            # Check half-open limits
            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_calls >= self.config.half_open_max_calls:
                    raise CircuitOpenError(
                        competencia=self.name,
                        recovery_time=self.config.recovery_timeout,
                    )
                self._half_open_calls += 1
        
        try:
            # Execute the function
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)
            
            await self.record_success()
            return result
            
        except Exception as e:
            await self.record_failure()
            raise
    
    async def record_success(self) -> None:
        """Record a successful call."""
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.config.success_threshold:
                    logger.info(
                        f"Circuit {self.name}: HALF_OPEN -> CLOSED after "
                        f"{self._success_count} successes"
                    )
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    self._success_count = 0
            elif self._state == CircuitState.CLOSED:
                # Reset failure count on success in closed state
                self._failure_count = 0
    
    async def record_failure(self) -> None:
        """Record a failed call."""
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            
            if self._state == CircuitState.HALF_OPEN:
                # Any failure in half-open goes back to open
                logger.warning(
                    f"Circuit {self.name}: HALF_OPEN -> OPEN after failure"
                )
                self._state = CircuitState.OPEN
                self._half_open_calls = 0
                
            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.config.failure_threshold:
                    logger.warning(
                        f"Circuit {self.name}: CLOSED -> OPEN after "
                        f"{self._failure_count} failures"
                    )
                    self._state = CircuitState.OPEN
    
    async def reset(self) -> None:
        """Manually reset circuit to closed state."""
        async with self._lock:
            logger.info(f"Circuit {self.name}: Manual reset to CLOSED")
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._half_open_calls = 0
    
    def get_status(self) -> dict:
        """Get current circuit breaker status."""
        return {
            "name": self.name,
            "state": self._state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "half_open_calls": self._half_open_calls,
            "time_until_recovery": self.time_until_recovery,
            "config": {
                "failure_threshold": self.config.failure_threshold,
                "recovery_timeout": self.config.recovery_timeout,
                "half_open_max_calls": self.config.half_open_max_calls,
                "success_threshold": self.config.success_threshold,
            },
        }


# Global circuit breaker registry
_circuit_breakers: dict[str, CircuitBreaker] = {}


def get_circuit_breaker(
    name: str,
    config: Optional[CircuitBreakerConfig] = None,
) -> CircuitBreaker:
    """Get or create a circuit breaker by name.
    
    Args:
        name: Circuit breaker identifier (e.g., "pjud-civil")
        config: Optional configuration (used only on creation)
    
    Returns:
        CircuitBreaker instance
    """
    if name not in _circuit_breakers:
        _circuit_breakers[name] = CircuitBreaker(
            name=name,
            config=config or CircuitBreakerConfig(),
        )
    return _circuit_breakers[name]


def circuit_breaker(
    name: str,
    config: Optional[CircuitBreakerConfig] = None,
) -> Callable:
    """Decorator to wrap async functions with circuit breaker.
    
    Example:
        @circuit_breaker("pjud-civil")
        async def get_cases(session):
            ...
    
    Args:
        name: Circuit breaker name
        config: Optional configuration
    
    Returns:
        Decorated function
    """
    def decorator(func: Callable) -> Callable:
        cb = get_circuit_breaker(name, config)
        
        @wraps(func)
        async def wrapper(*args, **kwargs):
            return await cb.call(func, *args, **kwargs)
        
        # Expose circuit breaker for inspection
        wrapper.circuit_breaker = cb
        return wrapper
    
    return decorator
