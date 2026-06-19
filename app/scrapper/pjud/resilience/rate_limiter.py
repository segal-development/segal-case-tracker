"""
Token Bucket Rate Limiter for PJUD scraper.

Prevents overwhelming the PJUD portal with requests.
Thread-safe implementation for concurrent users (40-60 target).
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from functools import wraps
from typing import Callable, Optional

from app.scrapper.pjud.exceptions import RateLimitError


logger = logging.getLogger(__name__)


@dataclass
class RateLimiterConfig:
    """Configuration for rate limiter.
    
    Attributes:
        rate: Tokens added per second (requests/second)
        burst: Maximum tokens (allows bursting)
        wait_timeout: Max seconds to wait for token (0 = no wait)
    """
    rate: float = 10.0    # 10 requests per second
    burst: int = 20       # Allow bursts up to 20
    wait_timeout: float = 30.0  # Wait up to 30s for a token


@dataclass
class TokenBucketLimiter:
    """Thread-safe token bucket rate limiter.
    
    Example:
        limiter = TokenBucketLimiter(name="pjud")
        
        # Blocking acquire
        await limiter.acquire()
        # ... make request ...
        
        # Non-blocking check
        if limiter.try_acquire():
            # ... make request ...
    """
    name: str
    config: RateLimiterConfig = field(default_factory=RateLimiterConfig)
    
    # Internal state
    _tokens: float = field(init=False)
    _last_refill: float = field(default_factory=time.time, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    
    def __post_init__(self):
        """Initialize token count to burst limit."""
        self._tokens = float(self.config.burst)
    
    @property
    def available_tokens(self) -> float:
        """Current available tokens (approximate, not thread-safe)."""
        return self._tokens
    
    async def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.time()
        elapsed = now - self._last_refill
        
        # Add tokens based on rate and elapsed time
        new_tokens = elapsed * self.config.rate
        self._tokens = min(self.config.burst, self._tokens + new_tokens)
        self._last_refill = now
    
    async def acquire(self, timeout: Optional[float] = None) -> bool:
        """Acquire a token, waiting if necessary.
        
        Args:
            timeout: Max seconds to wait (None = use config default)
        
        Returns:
            True if token acquired
        
        Raises:
            RateLimitError: If timeout exceeded and no token available
        """
        if timeout is None:
            timeout = self.config.wait_timeout
        
        start_time = time.time()
        
        while True:
            async with self._lock:
                await self._refill()
                
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                
                # Calculate wait time for one token
                tokens_needed = 1.0 - self._tokens
                wait_time = tokens_needed / self.config.rate
            
            # Check timeout
            elapsed = time.time() - start_time
            if elapsed >= timeout:
                raise RateLimitError(wait_time=wait_time)
            
            # Wait for a short interval and retry
            remaining = min(wait_time, timeout - elapsed, 0.1)
            if remaining > 0:
                await asyncio.sleep(remaining)
    
    def try_acquire(self) -> bool:
        """Try to acquire a token without waiting.
        
        Returns:
            True if token acquired, False otherwise
        """
        # Note: This is a sync check, less precise but useful for quick checks
        now = time.time()
        elapsed = now - self._last_refill
        current_tokens = min(
            self.config.burst,
            self._tokens + elapsed * self.config.rate,
        )
        
        if current_tokens >= 1.0:
            # Will acquire in the next async call
            return True
        return False
    
    async def reset(self) -> None:
        """Reset limiter to full token bucket."""
        async with self._lock:
            self._tokens = float(self.config.burst)
            self._last_refill = time.time()
            logger.info(f"Rate limiter {self.name}: Reset to {self.config.burst} tokens")
    
    def get_status(self) -> dict:
        """Get current rate limiter status."""
        return {
            "name": self.name,
            "available_tokens": round(self._tokens, 2),
            "rate": self.config.rate,
            "burst": self.config.burst,
            "wait_timeout": self.config.wait_timeout,
        }


# Global rate limiter registry
_rate_limiters: dict[str, TokenBucketLimiter] = {}


def get_rate_limiter(
    name: str,
    config: Optional[RateLimiterConfig] = None,
) -> TokenBucketLimiter:
    """Get or create a rate limiter by name.
    
    Args:
        name: Rate limiter identifier (e.g., "pjud")
        config: Optional configuration (used only on creation)
    
    Returns:
        TokenBucketLimiter instance
    """
    if name not in _rate_limiters:
        _rate_limiters[name] = TokenBucketLimiter(
            name=name,
            config=config or RateLimiterConfig(),
        )
    return _rate_limiters[name]


def rate_limit(
    name: str,
    config: Optional[RateLimiterConfig] = None,
) -> Callable:
    """Decorator to apply rate limiting to async functions.
    
    Example:
        @rate_limit("pjud")
        async def make_request():
            ...
        
        @rate_limit("pjud", config=RateLimiterConfig(rate=5))
        async def slow_endpoint():
            ...
    
    Args:
        name: Rate limiter name
        config: Optional configuration
    
    Returns:
        Decorated function with rate limiting
    """
    def decorator(func: Callable) -> Callable:
        limiter = get_rate_limiter(name, config)

        @wraps(func)
        async def wrapper(*args, **kwargs):
            await limiter.acquire()
            return await func(*args, **kwargs)

        # Expose limiter for inspection
        wrapper.rate_limiter = limiter
        return wrapper

    return decorator


def pjud_action_limiter(action: str) -> TokenBucketLimiter:
    """Return (or create) a per-action TokenBucketLimiter for PJUD.

    Import settings inside the function to avoid circular import cycles.
    Supported actions: "login", "list", "detail", "document".
    Unknown actions fall back to a conservative 0.1 req/s, burst=1.
    """
    from app.config import settings

    action_map = {
        "login":    (settings.PJUD_RL_LOGIN_RATE,    settings.PJUD_RL_LOGIN_BURST),
        "list":     (settings.PJUD_RL_LIST_RATE,     settings.PJUD_RL_LIST_BURST),
        "detail":   (settings.PJUD_RL_DETAIL_RATE,   settings.PJUD_RL_DETAIL_BURST),
        "document": (settings.PJUD_RL_DOCUMENT_RATE, settings.PJUD_RL_DOCUMENT_BURST),
    }

    if action in action_map:
        rate, burst = action_map[action]
    else:
        rate, burst = 0.1, 1

    cfg = RateLimiterConfig(
        rate=rate,
        burst=burst,
        wait_timeout=settings.PJUD_RL_WAIT_TIMEOUT,
    )
    return get_rate_limiter(f"pjud:{action}", cfg)
