"""Redis connection management."""

from typing import Optional
import redis

from app.config import settings

_redis_client: Optional[redis.Redis] = None


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
        _redis_client = redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
        )
        # Test connection
        _redis_client.ping()
        return _redis_client
    except redis.ConnectionError:
        return None


def close_redis() -> None:
    """Close Redis connection."""
    global _redis_client
    
    if _redis_client:
        _redis_client.close()
        _redis_client = None
