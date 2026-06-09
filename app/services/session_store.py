"""
Session Store - Persist PJUD sessions in Redis.

Sessions are stored with a 2-hour TTL matching PJUD session duration.
The worker reads these sessions to perform background sync.

Flow:
1. Lawyer solves captcha and logs in via API
2. Session stored in Redis with lawyer_id as key
3. Worker checks Redis for active sessions
4. Worker uses session to sync cases
5. If session expired, notify lawyer to re-login
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from app.core.redis import get_redis_client

logger = logging.getLogger(__name__)

# Session TTL in seconds (2 hours = PJUD session duration)
SESSION_TTL_SECONDS = 2 * 60 * 60  # 7200 seconds

# Session validation cache TTL (5 minutes)
SESSION_VALIDATION_CACHE_TTL = 5 * 60  # 300 seconds

# Redis key prefix
SESSION_PREFIX = "pjud:session:"
LAWYER_SESSIONS_PREFIX = "pjud:lawyer_sessions:"
SESSION_VALID_CACHE_PREFIX = "pjud:session_valid:"


@dataclass
class PJUDSession:
    """
    Stored PJUD session data.
    
    Attributes:
        session_id: Unique session identifier
        lawyer_id: Database ID of the lawyer
        rut: Lawyer's RUT (Chilean tax ID)
        cookies: Playwright cookies for session restoration
        created_at: ISO timestamp when session was created
        expires_at: ISO timestamp when session expires
        local_storage: localStorage JSON string for restoration
        last_used_at: ISO timestamp of last use (optional)
        auth_method: Authentication method used ("captcha" or "clave_unica")
    """
    session_id: str
    lawyer_id: int
    rut: str
    cookies: List[Dict[str, Any]]  # Playwright cookies (List of dicts)
    created_at: str  # ISO format
    expires_at: str  # ISO format
    local_storage: str = "{}"  # localStorage JSON string
    last_used_at: Optional[str] = None
    auth_method: str = "captcha"  # "captcha" or "clave_unica"
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> "PJUDSession":
        # Handle missing auth_method for backward compatibility
        if "auth_method" not in data:
            data["auth_method"] = "captcha"
        return cls(**data)
    
    def is_expired(self) -> bool:
        """Check if session is expired."""
        expires = datetime.fromisoformat(self.expires_at)
        return datetime.utcnow() > expires
    
    def time_until_expiry(self) -> timedelta:
        """Get time until session expires."""
        expires = datetime.fromisoformat(self.expires_at)
        return expires - datetime.utcnow()


class SessionStore:
    """
    Redis-backed session store for PJUD sessions.
    
    Usage:
        store = SessionStore()
        
        # Save session after login
        store.save_session(session)
        
        # Get session for sync
        session = store.get_session_by_lawyer(lawyer_id)
        
        # Check active sessions for worker
        sessions = store.get_all_active_sessions()
    """
    
    def __init__(self):
        self.redis = get_redis_client()
    
    def _is_available(self) -> bool:
        """Check if Redis is available."""
        return self.redis is not None
    
    def save_session(self, session: PJUDSession) -> bool:
        """
        Save a PJUD session to Redis.
        
        Args:
            session: Session data to store
            
        Returns:
            True if saved successfully
        """
        if not self._is_available():
            logger.warning("Redis not available, session not persisted")
            return False
        
        try:
            key = f"{SESSION_PREFIX}{session.session_id}"
            data = json.dumps(session.to_dict())
            
            # Calculate TTL based on expiry time
            ttl = int(session.time_until_expiry().total_seconds())
            if ttl <= 0:
                logger.warning(f"Session {session.session_id} already expired")
                return False
            
            # Store session
            self.redis.setex(key, ttl, data)
            
            # Also index by lawyer_id for quick lookup
            lawyer_key = f"{LAWYER_SESSIONS_PREFIX}{session.lawyer_id}"
            self.redis.setex(lawyer_key, ttl, session.session_id)
            
            logger.info(
                f"Saved session {session.session_id} for lawyer {session.lawyer_id}, "
                f"TTL: {ttl}s, auth_method: {session.auth_method}"
            )
            return True
            
        except Exception as e:
            logger.error(f"Failed to save session: {e}")
            return False
    
    def get_session(self, session_id: str) -> Optional[PJUDSession]:
        """
        Get a session by ID.
        
        Args:
            session_id: Session ID
            
        Returns:
            Session if found and not expired, None otherwise
        """
        if not self._is_available():
            return None
        
        try:
            key = f"{SESSION_PREFIX}{session_id}"
            data = self.redis.get(key)
            
            if not data:
                return None
            
            session = PJUDSession.from_dict(json.loads(data))
            
            # Double-check expiry (Redis TTL should handle this, but be safe)
            if session.is_expired():
                self.delete_session(session_id)
                return None
            
            return session
            
        except Exception as e:
            logger.error(f"Failed to get session {session_id}: {e}")
            return None
    
    def get_session_by_lawyer(self, lawyer_id: int) -> Optional[PJUDSession]:
        """
        Get active session for a lawyer.
        
        Args:
            lawyer_id: Lawyer ID
            
        Returns:
            Active session if exists, None otherwise
        """
        if not self._is_available():
            return None
        
        try:
            lawyer_key = f"{LAWYER_SESSIONS_PREFIX}{lawyer_id}"
            session_id = self.redis.get(lawyer_key)
            
            if not session_id:
                return None
            
            return self.get_session(session_id)
            
        except Exception as e:
            logger.error(f"Failed to get session for lawyer {lawyer_id}: {e}")
            return None
    
    def delete_session(self, session_id: str) -> bool:
        """
        Delete a session.
        
        Args:
            session_id: Session ID to delete
            
        Returns:
            True if deleted
        """
        if not self._is_available():
            return False
        
        try:
            # Get session first to find lawyer_id
            session = self.get_session(session_id)
            
            key = f"{SESSION_PREFIX}{session_id}"
            self.redis.delete(key)
            
            # Also remove lawyer index
            if session:
                lawyer_key = f"{LAWYER_SESSIONS_PREFIX}{session.lawyer_id}"
                self.redis.delete(lawyer_key)
            
            logger.info(f"Deleted session {session_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to delete session {session_id}: {e}")
            return False
    
    def update_last_used(self, session_id: str) -> bool:
        """
        Update last_used_at timestamp for a session.
        
        Args:
            session_id: Session ID
            
        Returns:
            True if updated
        """
        if not self._is_available():
            return False
        
        try:
            session = self.get_session(session_id)
            if not session:
                return False
            
            session.last_used_at = datetime.utcnow().isoformat()
            return self.save_session(session)
            
        except Exception as e:
            logger.error(f"Failed to update last_used for {session_id}: {e}")
            return False
    
    def get_all_active_sessions(self) -> List[PJUDSession]:
        """
        Get all active (non-expired) sessions.
        
        Used by the worker to find sessions for sync.
        
        Returns:
            List of active sessions
        """
        if not self._is_available():
            return []
        
        try:
            # Find all session keys
            pattern = f"{SESSION_PREFIX}*"
            keys = self.redis.keys(pattern)
            
            sessions = []
            for key in keys:
                data = self.redis.get(key)
                if data:
                    try:
                        session = PJUDSession.from_dict(json.loads(data))
                        if not session.is_expired():
                            sessions.append(session)
                    except Exception:
                        continue
            
            logger.info(f"Found {len(sessions)} active sessions")
            return sessions
            
        except Exception as e:
            logger.error(f"Failed to get active sessions: {e}")
            return []
    
    def get_lawyers_needing_login(self, all_lawyer_ids: List[int]) -> List[int]:
        """
        Get list of lawyers who don't have an active session.
        
        Used to notify lawyers they need to re-login.
        
        Args:
            all_lawyer_ids: List of all active lawyer IDs
            
        Returns:
            List of lawyer IDs without active sessions
        """
        if not self._is_available():
            return all_lawyer_ids
        
        lawyers_with_sessions = set()
        
        for session in self.get_all_active_sessions():
            lawyers_with_sessions.add(session.lawyer_id)
        
        return [lid for lid in all_lawyer_ids if lid not in lawyers_with_sessions]
    
    def get_session_status(self, lawyer_id: int) -> Dict[str, Any]:
        """
        Get session status for a lawyer.
        
        Args:
            lawyer_id: Lawyer ID
            
        Returns:
            Status dict with session info
        """
        session = self.get_session_by_lawyer(lawyer_id)
        
        if not session:
            return {
                "has_session": False,
                "needs_login": True,
                "message": "No active session. Please login to PJUD.",
            }
        
        time_left = session.time_until_expiry()
        minutes_left = int(time_left.total_seconds() / 60)
        
        return {
            "has_session": True,
            "needs_login": False,
            "session_id": session.session_id,
            "created_at": session.created_at,
            "expires_at": session.expires_at,
            "minutes_remaining": minutes_left,
            "last_used_at": session.last_used_at,
            "auth_method": session.auth_method,
        }
    
    def is_session_valid_cached(self, session_id: str) -> Optional[bool]:
        """
        Check if session validity is cached.
        
        Reduces PJUD roundtrips by caching validity for 5 minutes.
        
        Args:
            session_id: Session ID to check
            
        Returns:
            True if cached as valid, False if cached as invalid, None if not cached
        """
        if not self._is_available():
            return None
        
        try:
            cache_key = f"{SESSION_VALID_CACHE_PREFIX}{session_id}"
            cached = self.redis.get(cache_key)
            
            if cached is None:
                return None
            
            return cached == "valid"
            
        except Exception as e:
            logger.debug(f"Cache check failed for {session_id}: {e}")
            return None
    
    def cache_session_validity(self, session_id: str, is_valid: bool) -> bool:
        """
        Cache session validity status.
        
        Args:
            session_id: Session ID
            is_valid: Whether session is valid
            
        Returns:
            True if cached successfully
        """
        if not self._is_available():
            return False
        
        try:
            cache_key = f"{SESSION_VALID_CACHE_PREFIX}{session_id}"
            value = "valid" if is_valid else "invalid"
            self.redis.setex(cache_key, SESSION_VALIDATION_CACHE_TTL, value)
            
            logger.debug(
                f"Cached session validity: {session_id} = {value} "
                f"(TTL: {SESSION_VALIDATION_CACHE_TTL}s)"
            )
            return True
            
        except Exception as e:
            logger.debug(f"Failed to cache session validity: {e}")
            return False
    
    def invalidate_session_cache(self, session_id: str) -> bool:
        """
        Invalidate cached session validity.
        
        Call this when a session is known to be expired or invalid.
        
        Args:
            session_id: Session ID
            
        Returns:
            True if invalidated successfully
        """
        if not self._is_available():
            return False
        
        try:
            cache_key = f"{SESSION_VALID_CACHE_PREFIX}{session_id}"
            self.redis.delete(cache_key)
            logger.debug(f"Invalidated session cache: {session_id}")
            return True
            
        except Exception as e:
            logger.debug(f"Failed to invalidate session cache: {e}")
            return False


# Global instance
_session_store: Optional[SessionStore] = None


def get_session_store() -> SessionStore:
    """Get or create session store singleton."""
    global _session_store
    
    if _session_store is None:
        _session_store = SessionStore()
    
    return _session_store
