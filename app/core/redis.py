"""Redis connection management."""

import logging
from typing import Optional

import redis
import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)

_redis_client: Optional[redis.Redis] = None
_async_redis_client: Optional[aioredis.Redis] = None


def get_redis_client() -> Optional[redis.Redis]:
    """
    Get Redis client singleton.
    
    Returns None if Redis is not configured or unavailable.
    """
    global _redis_client
    
    if _redis_client is not None:
        return _redis_client
    
    if not settings.REDIS_URL:
        return None
    
    try:
        client = redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
        )
        # Test connection — only cache the client if ping succeeds so that a
        # failed connection attempt does not poison the global singleton with a
        # stale object (which would prevent subsequent tests from skipping).
        client.ping()
        _redis_client = client
        return _redis_client
    except redis.ConnectionError:
        return None


async def get_async_redis_client() -> Optional[aioredis.Redis]:
    """Get async Redis client singleton.

    Returns None if Redis is not configured or unavailable.
    """
    global _async_redis_client

    if _async_redis_client is not None:
        return _async_redis_client

    if not settings.REDIS_URL:
        return None

    try:
        client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await client.ping()
        _async_redis_client = client
        return _async_redis_client
    except Exception as exc:
        logger.warning("Async Redis connection failed: %s", exc)
        return None


def close_redis() -> None:
    """Close Redis connections."""
    global _redis_client, _async_redis_client

    if _redis_client:
        _redis_client.close()
        _redis_client = None

    if _async_redis_client:
        # fire-and-forget: aclose() is a coroutine; callers who need
        # clean shutdown should await it directly.
        _async_redis_client = None
