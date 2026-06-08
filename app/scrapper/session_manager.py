"""PJUD session management with Redis caching."""

import json
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta

import redis

from app.config import settings


logger = logging.getLogger(__name__)


@dataclass
class PJUDSession:
    """PJUD session data for caching."""
    
    rut: str
    cookies: List[Dict]
    local_storage: str  # JSON string
    created_at: datetime
    expires_at: Optional[datetime] = None
    
    def __post_init__(self):
        if not self.expires_at:
            # Sessions expire after 25 minutes (PJUD timeout is ~30 min)
            self.expires_at = self.created_at + timedelta(minutes=25)
    
    def is_expired(self) -> bool:
        """Check if session has expired."""
        return datetime.now() > self.expires_at
    
    def to_dict(self) -> Dict:
        """Convert to dict for JSON serialization."""
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        data["expires_at"] = self.expires_at.isoformat() if self.expires_at else None
        return data
    
    @classmethod
    def from_dict(cls, data: Dict) -> "PJUDSession":
        """Create from dict."""
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        if data.get("expires_at"):
            data["expires_at"] = datetime.fromisoformat(data["expires_at"])
        return cls(**data)


class SessionManager:
    """
    Manage PJUD sessions in Redis.
    
    Sessions are keyed by RUT and cached for 25 minutes.
    This reduces reCAPTCHA solves and speeds up scraping.
    """
    
    SESSION_TTL_SECONDS = 25 * 60  # 25 minutes
    SESSION_PREFIX = "pjud:session:"
    
    def __init__(self, redis_client: Optional[redis.Redis] = None):
        """
        Initialize session manager.
        
        Args:
            redis_client: Redis client instance. If None, creates one from settings.
        """
        if redis_client:
            self.redis = redis_client
        else:
            try:
                self.redis = redis.from_url(settings.REDIS_URL)
            except Exception as e:
                logger.warning(f"Redis connection failed: {e}")
                self.redis = None
    
    def _get_key(self, rut: str) -> str:
        """Get Redis key for RUT's PJUD session."""
        # Normalize RUT: remove dots and hyphens
        rut_clean = rut.replace("-", "").replace(".", "")
        return f"{self.SESSION_PREFIX}{rut_clean}"
    
    async def get_session(self, rut: str) -> Optional[PJUDSession]:
        """
        Get cached PJUD session.
        
        Args:
            rut: Lawyer RUT (with or without verification digit)
        
        Returns:
            PJUDSession or None if not cached/expired
        """
        if not self.redis:
            logger.debug("Redis not available, no cached session")
            return None
        
        try:
            key = self._get_key(rut)
            data = self.redis.get(key)
            
            if not data:
                logger.debug(f"No cached session for RUT {rut}")
                return None
            
            session = PJUDSession.from_dict(json.loads(data))
            
            if session.is_expired():
                logger.debug(f"Cached session expired for RUT {rut}")
                await self.invalidate_session(rut)
                return None
            
            logger.debug(f"Found valid cached session for RUT {rut}")
            return session
            
        except Exception as e:
            logger.error(f"Error getting session: {e}")
            return None
    
    async def save_session(self, session: PJUDSession) -> None:
        """
        Cache PJUD session.
        
        Args:
            session: PJUDSession to cache
        """
        if not self.redis:
            logger.debug("Redis not available, session not cached")
            return
        
        try:
            key = self._get_key(session.rut)
            data = json.dumps(session.to_dict())
            self.redis.setex(key, self.SESSION_TTL_SECONDS, data)
            logger.debug(f"Session cached for RUT {session.rut}")
            
        except Exception as e:
            logger.error(f"Error saving session: {e}")
    
    async def invalidate_session(self, rut: str) -> None:
        """
        Invalidate cached session.
        
        Args:
            rut: Lawyer RUT
        """
        if not self.redis:
            return
        
        try:
            key = self._get_key(rut)
            self.redis.delete(key)
            logger.debug(f"Session invalidated for RUT {rut}")
            
        except Exception as e:
            logger.error(f"Error invalidating session: {e}")
    
    async def refresh_session(self, session: PJUDSession) -> None:
        """
        Refresh session TTL (extend expiration).
        
        Call this after successful PJUD requests to keep session alive.
        """
        session.expires_at = datetime.now() + timedelta(minutes=25)
        await self.save_session(session)
    
    async def get_active_sessions_count(self) -> int:
        """Get count of active (non-expired) sessions."""
        if not self.redis:
            return 0
        
        try:
            keys = self.redis.keys(f"{self.SESSION_PREFIX}*")
            return len(keys)
        except Exception as e:
            logger.error(f"Error counting sessions: {e}")
            return 0
