"""A PJUD session must be bound to a real lawyer before it is persisted.

Regression for the captcha re-auth branch that saved sessions with the
``lawyer_id=0`` "unbound" default: every captcha lawyer overwrote
``pjud:session:lawyer:0`` and the secondary RUT/id keys pointed at "0", so
lookups by RUT or session id could resolve ANOTHER lawyer's session.
"""
import pytest

from app.services.pjud_session import PJUDSession
from app.services.session_store import (
    _ID_KEY,
    _LAWYER_KEY,
    _RUT_KEY,
    InvalidSessionBindingError,
    SessionStore,
)

RUT_A = "11111111-1"
RUT_B = "22222222-2"


def _session(rut: str, lawyer_id: int) -> PJUDSession:
    return PJUDSession.create(
        rut=rut,
        cookies=[{"name": "PHPSESSID", "value": f"c-{rut}", "domain": ".pjud.cl"}],
        lawyer_id=lawyer_id,
        auth_method="captcha",
    )


class TestSaveRequiresBoundLawyer:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_id", [0, -1])
    async def test_unbound_session_is_rejected_loudly(self, fake_redis, bad_id):
        store = SessionStore(redis_client=fake_redis)

        with pytest.raises(InvalidSessionBindingError):
            await store.asave_session(_session(RUT_A, bad_id))

        # Nothing may be written under any key.
        assert await fake_redis.keys("pjud:session:*") == []

    @pytest.mark.asyncio
    async def test_each_lawyer_finds_their_own_session(self, fake_redis):
        store = SessionStore(redis_client=fake_redis)
        sess_a = _session(RUT_A, 1)
        sess_b = _session(RUT_B, 7)

        await store.asave_session(sess_a)
        await store.asave_session(sess_b)

        found_a = await store.get_session_by_lawyer(1)
        found_b = await store.get_session_by_lawyer(7)
        assert found_a is not None and found_a.session_id == sess_a.session_id
        assert found_b is not None and found_b.session_id == sess_b.session_id


class TestLegacyUnboundKeysCannotLeakAcrossLawyers:
    """State left in Redis by the old bug: all secondary keys point at "0"."""

    @staticmethod
    async def _seed_legacy(fake_redis):
        sess_a = _session(RUT_A, 0)
        sess_b = _session(RUT_B, 0)  # written last: owns lawyer:0
        await fake_redis.setex(f"{_LAWYER_KEY}0", 600, sess_b.to_redis())
        for sess in (sess_a, sess_b):
            await fake_redis.setex(f"{_ID_KEY}{sess.session_id}", 600, "0")
            await fake_redis.setex(f"{_RUT_KEY}{sess.rut}", 600, "0")
        return sess_a, sess_b

    @pytest.mark.asyncio
    async def test_get_by_rut_never_returns_another_lawyers_session(self, fake_redis):
        store = SessionStore(redis_client=fake_redis)
        await self._seed_legacy(fake_redis)

        assert await store.aget_session_by_rut(RUT_A) is None

    @pytest.mark.asyncio
    async def test_get_by_id_never_returns_another_lawyers_session(self, fake_redis):
        store = SessionStore(redis_client=fake_redis)
        sess_a, _ = await self._seed_legacy(fake_redis)

        assert await store.aget_session_by_id(sess_a.session_id) is None

    @pytest.mark.asyncio
    async def test_logout_by_rut_does_not_delete_another_lawyers_session(self, fake_redis):
        store = SessionStore(redis_client=fake_redis)
        _, sess_b = await self._seed_legacy(fake_redis)

        await store.adelete_session_by_rut(RUT_A)

        assert await fake_redis.get(f"{_LAWYER_KEY}0") is not None
        assert await fake_redis.get(f"{_ID_KEY}{sess_b.session_id}") is not None

    @pytest.mark.asyncio
    async def test_delete_by_id_does_not_delete_another_lawyers_session(self, fake_redis):
        store = SessionStore(redis_client=fake_redis)
        sess_a, sess_b = await self._seed_legacy(fake_redis)

        await store.adelete_session(sess_a.session_id)

        assert await fake_redis.get(f"{_LAWYER_KEY}0") is not None
        assert await fake_redis.get(f"{_ID_KEY}{sess_b.session_id}") is not None

    @pytest.mark.asyncio
    async def test_primary_lookup_ignores_the_unbound_key(self, fake_redis):
        store = SessionStore(redis_client=fake_redis)
        await self._seed_legacy(fake_redis)

        assert await store.get_session_by_lawyer(0) is None


class TestSecondaryIndexMismatch:
    """A secondary key pointing at a real but DIFFERENT lawyer is never trusted."""

    @pytest.mark.asyncio
    async def test_rut_index_pointing_at_other_lawyer_returns_none(self, fake_redis):
        store = SessionStore(redis_client=fake_redis)
        await store.asave_session(_session(RUT_B, 7))
        await fake_redis.setex(f"{_RUT_KEY}{RUT_A}", 600, "7")

        assert await store.aget_session_by_rut(RUT_A) is None

    @pytest.mark.asyncio
    async def test_id_index_pointing_at_other_lawyer_returns_none(self, fake_redis):
        store = SessionStore(redis_client=fake_redis)
        await store.asave_session(_session(RUT_B, 7))
        await fake_redis.setex(f"{_ID_KEY}some-other-session-id", 600, "7")

        assert await store.aget_session_by_id("some-other-session-id") is None
