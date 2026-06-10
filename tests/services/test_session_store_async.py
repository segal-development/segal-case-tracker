"""S1-T4: Async SessionStore tests using fakeredis.

All tests inject a FakeRedis instance — no live Redis required.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock


class TestSessionStoreRoundTrip:
    """asave_session → get_session_by_lawyer round-trip (SESS-03)."""

    @pytest.mark.asyncio
    async def test_save_and_retrieve_by_lawyer(self, session_store, sample_session):
        """Session saved via asave_session is findable by lawyer_id."""
        saved = await session_store.asave_session(sample_session)
        assert saved is True

        retrieved = await session_store.get_session_by_lawyer(sample_session.lawyer_id)
        assert retrieved is not None
        assert retrieved.session_id == sample_session.session_id

    @pytest.mark.asyncio
    async def test_retrieve_by_session_id(self, session_store, sample_session):
        """Session saved is findable by session_id secondary key."""
        await session_store.asave_session(sample_session)
        retrieved = await session_store.aget_session_by_id(sample_session.session_id)
        assert retrieved is not None
        assert retrieved.lawyer_id == sample_session.lawyer_id

    @pytest.mark.asyncio
    async def test_retrieve_by_rut(self, session_store, sample_session):
        """Session saved is findable by rut secondary key."""
        await session_store.asave_session(sample_session)
        retrieved = await session_store.aget_session_by_rut(sample_session.rut)
        assert retrieved is not None
        assert retrieved.session_id == sample_session.session_id

    @pytest.mark.asyncio
    async def test_none_returned_for_unknown_lawyer(self, session_store):
        """get_session_by_lawyer returns None when no session exists."""
        result = await session_store.get_session_by_lawyer(9999)
        assert result is None

    @pytest.mark.asyncio
    async def test_auth_method_preserved_in_round_trip(self, session_store, sample_session):
        """auth_method is preserved through save→retrieve."""
        await session_store.asave_session(sample_session)
        retrieved = await session_store.get_session_by_lawyer(sample_session.lawyer_id)
        assert retrieved.auth_method == sample_session.auth_method


class TestSessionStoreDeletion:
    """Session deletion clears all keys."""

    @pytest.mark.asyncio
    async def test_delete_removes_session(self, session_store, sample_session):
        await session_store.asave_session(sample_session)
        await session_store.adelete_session(sample_session.session_id)
        assert await session_store.get_session_by_lawyer(sample_session.lawyer_id) is None

    @pytest.mark.asyncio
    async def test_delete_by_rut(self, session_store, sample_session):
        await session_store.asave_session(sample_session)
        await session_store.adelete_session_by_rut(sample_session.rut)
        assert await session_store.get_session_by_lawyer(sample_session.lawyer_id) is None


class TestSessionStoreGracefulDegradation:
    """SESS-05: graceful failure when Redis is unavailable."""

    @pytest.mark.asyncio
    async def test_save_returns_false_when_redis_none(self, sample_session):
        """asave_session returns False (not exception) when Redis unavailable."""
        from app.services.session_store import SessionStore
        store = SessionStore(redis_client=None)
        result = await store.asave_session(sample_session)
        assert result is False

    @pytest.mark.asyncio
    async def test_get_by_lawyer_returns_none_when_redis_none(self):
        """get_session_by_lawyer returns None when Redis unavailable."""
        from app.services.session_store import SessionStore
        store = SessionStore(redis_client=None)
        result = await store.get_session_by_lawyer(1)
        assert result is None

    @pytest.mark.asyncio
    async def test_redis_exception_returns_graceful_result(self, sample_session):
        """Exception during async op returns graceful result (no unhandled exc)."""
        broken_redis = AsyncMock()
        broken_redis.setex = AsyncMock(side_effect=Exception("connection refused"))
        from app.services.session_store import SessionStore
        store = SessionStore(redis_client=broken_redis)
        result = await store.asave_session(sample_session)
        assert result is False
