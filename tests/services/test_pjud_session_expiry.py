"""Tests for configurable PJUD session expiry and sliding TTL in SessionStore.

TDD RED → GREEN cycle (Task 1.3 / 1.4).

Spec scenarios:
- PJUD_SESSION_EXPIRY_MINUTES env/setting is honored in PJUDSession.create().
- asave_session applies sliding expiry: calling it a second time refreshes
  the TTL on ALL THREE Redis keys from now + EXPIRY_MINUTES (not the original
  expires_at).
"""

import pytest
import fakeredis.aioredis
from datetime import timedelta


class TestSessionExpiryConfig:
    """PJUD_SESSION_EXPIRY_MINUTES setting controls PJUDSession lifetime."""

    def test_pjud_session_expiry_minutes_honored(self, monkeypatch):
        """PJUDSession.create() uses PJUD_SESSION_EXPIRY_MINUTES, not a hardcoded literal."""
        from app.config import settings
        from app.services.pjud_session import PJUDSession

        monkeypatch.setattr(settings, "PJUD_SESSION_EXPIRY_MINUTES", 5)

        session = PJUDSession.create(rut="12345678-9", cookies=[], lawyer_id=1)
        delta = (session.expires_at - session.created_at).total_seconds()

        assert abs(delta - 5 * 60) < 2, (
            f"Expected expires_at ~5 min from created_at (300s), got {delta:.1f}s. "
            "PJUDSession.create() must read PJUD_SESSION_EXPIRY_MINUTES from settings."
        )


class TestSlidingSessionTTL:
    """asave_session refreshes TTL from now + EXPIRY_MINUTES on every call."""

    @pytest.mark.asyncio
    async def test_resave_refreshes_all_three_redis_key_ttls(self, monkeypatch):
        """Sliding expiry: asave_session with a near-expired session resets ALL 3 key TTLs.

        Without sliding: a session with 2-min remaining would produce ~120s TTL.
        With sliding:    asave_session recomputes from now + EXPIRY_MINUTES → ~600s TTL.
        """
        from app.config import settings
        from app.services.session_store import SessionStore
        from app.services.pjud_session import PJUDSession

        monkeypatch.setattr(settings, "PJUD_SESSION_EXPIRY_MINUTES", 10)

        fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = SessionStore(redis_client=fake_redis)
        session = PJUDSession.create(rut="12345678-9", cookies=[], lawyer_id=7)

        # Simulate a near-expired session: only 2 minutes remaining.
        # Without sliding expiry, asave_session would use time_until_expiry() ≈ 120s.
        session.expires_at = session.created_at + timedelta(minutes=2)

        await store.asave_session(session)

        lawyer_ttl = await fake_redis.ttl(f"pjud:session:lawyer:{session.lawyer_id}")
        id_ttl = await fake_redis.ttl(f"pjud:session:id:{session.session_id}")
        rut_keys = await fake_redis.keys("pjud:session:rut:*")
        rut_ttl = await fake_redis.ttl(rut_keys[0]) if rut_keys else -1

        # Sliding expiry must reset all 3 keys to ≈ 10 * 60 = 600s, not ~120s.
        assert lawyer_ttl > 550, (
            f"lawyer key TTL should be ~600s after sliding save, got {lawyer_ttl}s"
        )
        assert id_ttl > 550, (
            f"id key TTL should be ~600s after sliding save, got {id_ttl}s"
        )
        assert rut_ttl > 550, (
            f"rut key TTL should be ~600s after sliding save, got {rut_ttl}s"
        )
