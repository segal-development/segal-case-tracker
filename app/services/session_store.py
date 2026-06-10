"""
Session Store — persist PJUD sessions in Redis (async).

Key strategy (ADR-2):
  PRIMARY   pjud:session:lawyer:{lawyer_id}  → full JSON  (worker hot path)
  SECONDARY pjud:session:id:{session_id}     → lawyer_id  (pjud.py lookup)
  SECONDARY pjud:session:rut:{rut_clean}     → lawyer_id  (auth session-status/logout)

TTL for all three keys equals ``session.time_until_expiry()`` so they
expire together with the session.

Sync validation-cache methods (``is_session_valid_cached`` etc.) are kept
for backward compatibility with existing callers; they use the sync
``get_redis_client()``.
"""

import logging
from typing import Any, Dict, List, Optional

from app.core.redis import get_redis_client, get_async_redis_client
from app.services.pjud_session import PJUDSession  # canonical model; re-exported below

# Sentinel — distinguishes "auto-detect Redis" from "explicitly disabled"
_UNSET: Any = object()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants kept for backward compatibility with existing test imports
# ---------------------------------------------------------------------------

SESSION_VALIDATION_CACHE_TTL = 5 * 60  # 300 seconds
SESSION_VALID_CACHE_PREFIX = "pjud:session_valid:"

# Kept for tests that import these names from this module
SESSION_PREFIX = "pjud:session:"
LAWYER_SESSIONS_PREFIX = "pjud:lawyer_sessions:"

# New canonical key prefixes (ADR-2)
_LAWYER_KEY = "pjud:session:lawyer:"
_ID_KEY = "pjud:session:id:"
_RUT_KEY = "pjud:session:rut:"


def _rut_clean(rut: str) -> str:
    return rut.replace("-", "").replace(".", "")


class SessionStore:
    """
    Single async-native session store.

    ``redis_client`` — inject a ``fakeredis.aioredis.FakeRedis`` (or any
    ``redis.asyncio.Redis``-compatible client) in tests; pass ``None`` to
    fall back to the module-level ``get_async_redis_client()`` singleton.
    """

    def __init__(self, redis_client: Any = _UNSET) -> None:
        # _UNSET → auto-detect via get_async_redis_client() on first use
        # None   → explicitly disabled (no Redis available)
        # other  → injected client (e.g. fakeredis in tests)
        self._async_redis = redis_client
        # Sync client: used only by legacy validation-cache methods
        self._sync_redis = get_redis_client()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _redis(self) -> Optional[Any]:
        """Return the async Redis client.

        - Injected client (including FakeRedis) → returned as-is.
        - ``None`` → caller explicitly disabled Redis; return None.
        - ``_UNSET`` (default) → auto-detect via ``get_async_redis_client()``.
        """
        if self._async_redis is None:
            return None
        if self._async_redis is not _UNSET:
            return self._async_redis
        return await get_async_redis_client()

    def _sync_available(self) -> bool:
        return self._sync_redis is not None

    # ------------------------------------------------------------------
    # Primary async session operations
    # ------------------------------------------------------------------

    async def asave_session(self, session: PJUDSession) -> bool:
        """Persist a session under all three keys.

        Returns True on success, False on error or expired session.
        """
        redis = await self._redis()
        if redis is None:
            logger.warning("Redis not available — session not persisted")
            return False

        try:
            ttl = int(session.time_until_expiry().total_seconds())
            if ttl <= 0:
                logger.warning("Session %s is already expired, not saving", session.session_id)
                return False

            payload = session.to_redis()
            rut = _rut_clean(session.rut)

            await redis.setex(f"{_LAWYER_KEY}{session.lawyer_id}", ttl, payload)
            await redis.setex(f"{_ID_KEY}{session.session_id}", ttl, str(session.lawyer_id))
            await redis.setex(f"{_RUT_KEY}{rut}", ttl, str(session.lawyer_id))

            logger.info(
                "Saved session %s for lawyer %d, TTL: %ds, method: %s",
                session.session_id,
                session.lawyer_id,
                ttl,
                session.auth_method,
            )
            return True

        except Exception as exc:
            logger.error("Failed to save session: %s", exc)
            return False

    async def get_session_by_lawyer(self, lawyer_id: int) -> Optional[PJUDSession]:
        """Retrieve the active session for a lawyer (primary key)."""
        redis = await self._redis()
        if redis is None:
            return None

        try:
            data = await redis.get(f"{_LAWYER_KEY}{lawyer_id}")
            if not data:
                return None
            session = PJUDSession.from_redis(data)
            if session.is_expired():
                await redis.delete(
                    f"{_LAWYER_KEY}{lawyer_id}",
                    f"{_ID_KEY}{session.session_id}",
                    f"{_RUT_KEY}{_rut_clean(session.rut)}",
                )
                return None
            return session
        except Exception as exc:
            logger.error("Failed to get session for lawyer %d: %s", lawyer_id, exc)
            return None

    async def aget_session_by_id(self, session_id: str) -> Optional[PJUDSession]:
        """Retrieve the active session by session_id (secondary key)."""
        redis = await self._redis()
        if redis is None:
            return None

        try:
            lawyer_id_str = await redis.get(f"{_ID_KEY}{session_id}")
            if not lawyer_id_str:
                return None
            return await self.get_session_by_lawyer(int(lawyer_id_str))
        except Exception as exc:
            logger.error("Failed to get session %s: %s", session_id, exc)
            return None

    async def aget_session_by_rut(self, rut: str) -> Optional[PJUDSession]:
        """Retrieve the active session by RUT (secondary key)."""
        redis = await self._redis()
        if redis is None:
            return None

        try:
            rut = _rut_clean(rut)
            lawyer_id_str = await redis.get(f"{_RUT_KEY}{rut}")
            if not lawyer_id_str:
                return None
            return await self.get_session_by_lawyer(int(lawyer_id_str))
        except Exception as exc:
            logger.error("Failed to get session for rut %s: %s", rut, exc)
            return None

    async def adelete_session(self, session_id: str) -> bool:
        """Delete a session and all its secondary keys by session_id."""
        redis = await self._redis()
        if redis is None:
            return False

        try:
            session = await self.aget_session_by_id(session_id)
            if not session:
                # Try to clean up the session_id key even if primary is gone
                await redis.delete(f"{_ID_KEY}{session_id}")
                return True

            keys = [
                f"{_LAWYER_KEY}{session.lawyer_id}",
                f"{_ID_KEY}{session_id}",
                f"{_RUT_KEY}{_rut_clean(session.rut)}",
            ]
            await redis.delete(*keys)
            return True
        except Exception as exc:
            logger.error("Failed to delete session %s: %s", session_id, exc)
            return False

    async def adelete_session_by_rut(self, rut: str) -> bool:
        """Delete a session and all its keys using the rut secondary key."""
        redis = await self._redis()
        if redis is None:
            return False

        try:
            rut_clean = _rut_clean(rut)
            lawyer_id_str = await redis.get(f"{_RUT_KEY}{rut_clean}")
            if lawyer_id_str:
                session = await self.get_session_by_lawyer(int(lawyer_id_str))
                if session:
                    keys = [
                        f"{_LAWYER_KEY}{session.lawyer_id}",
                        f"{_ID_KEY}{session.session_id}",
                        f"{_RUT_KEY}{rut_clean}",
                    ]
                    await redis.delete(*keys)
                    return True
            # Clean up the rut key regardless
            await redis.delete(f"{_RUT_KEY}{rut_clean}")
            return True
        except Exception as exc:
            logger.error("Failed to delete session for rut %s: %s", rut, exc)
            return False

    # ------------------------------------------------------------------
    # Legacy sync methods — kept for backward compatibility
    # (used by existing auth.py session-status and test_session_store.py)
    # ------------------------------------------------------------------

    def is_session_valid_cached(self, session_id: str) -> Optional[bool]:
        """Check cached session validity (sync, uses sync redis client)."""
        redis = self._sync_redis
        if redis is None:
            return None
        try:
            cache_key = f"{SESSION_VALID_CACHE_PREFIX}{session_id}"
            cached = redis.get(cache_key)
            if cached is None:
                return None
            return cached == "valid"
        except Exception as exc:
            logger.debug("Cache check failed for %s: %s", session_id, exc)
            return None

    def cache_session_validity(self, session_id: str, is_valid: bool) -> bool:
        """Store session validity in sync cache."""
        redis = self._sync_redis
        if redis is None:
            return False
        try:
            cache_key = f"{SESSION_VALID_CACHE_PREFIX}{session_id}"
            value = "valid" if is_valid else "invalid"
            redis.setex(cache_key, SESSION_VALIDATION_CACHE_TTL, value)
            return True
        except Exception as exc:
            logger.debug("Failed to cache session validity: %s", exc)
            return False

    def invalidate_session_cache(self, session_id: str) -> bool:
        """Delete session validity from sync cache."""
        redis = self._sync_redis
        if redis is None:
            return False
        try:
            cache_key = f"{SESSION_VALID_CACHE_PREFIX}{session_id}"
            redis.delete(cache_key)
            return True
        except Exception as exc:
            logger.debug("Failed to invalidate session cache: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Worker helper
    # ------------------------------------------------------------------

    def get_session_status(self, lawyer_id: int) -> Dict[str, Any]:
        """Synchronous session status snapshot — for non-async callers."""
        # This is intentionally kept sync-safe; async callers should use
        # get_session_by_lawyer instead.
        return {"has_session": False, "needs_login": True}


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_session_store: Optional[SessionStore] = None


def get_session_store() -> SessionStore:
    """Get or create the global SessionStore singleton."""
    global _session_store
    if _session_store is None:
        _session_store = SessionStore()
    return _session_store
