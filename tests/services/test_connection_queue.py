"""Tests for connection_queue.py — PJUD Redis job queue helpers.

TDD RED → GREEN cycle (Task 1.1 / 1.2).

Security invariant tested: the Redis queue payload MUST NOT contain any
credential or password field (INV-1 / INV-4 from spec).
"""

import json
import pytest
import fakeredis.aioredis


@pytest.fixture
def fake_redis():
    """In-memory async Redis backed by fakeredis — no live connection required."""
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


class TestEnqueueConnection:
    """enqueue_connection pushes a credential-free job and returns a UUID."""

    @pytest.mark.asyncio
    async def test_enqueue_returns_uuid_connection_id(self, fake_redis):
        """enqueue_connection returns a non-empty UUID4 string."""
        from app.services.connection_queue import enqueue_connection

        cid = await enqueue_connection(
            fake_redis,
            lawyer_id=1,
            rut="12345678-9",
            auth_method="segunda_clave",
            captcha_token="tok123",
        )
        assert isinstance(cid, str)
        assert len(cid) == 36  # UUID4 is "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

    @pytest.mark.asyncio
    async def test_blpop_returns_full_job_dict(self, fake_redis):
        """blpop_connection returns the complete job dict enqueued by enqueue_connection."""
        from app.services.connection_queue import enqueue_connection, blpop_connection

        cid = await enqueue_connection(
            fake_redis,
            lawyer_id=2,
            rut="87654321-0",
            auth_method="segunda_clave",
            captcha_token="abc",
        )
        job = await blpop_connection(fake_redis, timeout=1)

        assert job is not None
        assert job["connection_id"] == cid
        assert job["lawyer_id"] == 2
        assert job["rut"] == "87654321-0"
        assert job["auth_method"] == "segunda_clave"
        assert job["captcha_token"] == "abc"
        assert "enqueued_at" in job


class TestConnectionStatus:
    """set_status / get_status round-trip with correct TTL."""

    @pytest.mark.asyncio
    async def test_set_status_writes_with_exactly_300s_ttl(self, fake_redis):
        """set_status writes the status key with a TTL of 300 seconds (within 1s tolerance)."""
        from app.services.connection_queue import set_status

        await set_status(
            fake_redis,
            "conn-123",
            {"status": "pending", "updated_at": "2026-01-01T00:00:00+00:00"},
        )
        ttl = await fake_redis.ttl("pjud:connect:status:conn-123")
        # Allow ±1 s tolerance: fakeredis may tick between setex and ttl
        assert 299 <= ttl <= 300, f"Expected TTL ~300s, got {ttl}s"

    @pytest.mark.asyncio
    async def test_get_status_parses_json(self, fake_redis):
        """get_status returns a parsed dict matching the payload passed to set_status."""
        from app.services.connection_queue import set_status, get_status

        payload = {"status": "pending", "updated_at": "2026-01-01T00:00:00+00:00"}
        await set_status(fake_redis, "conn-456", payload)

        result = await get_status(fake_redis, "conn-456")
        assert result is not None
        assert result["status"] == "pending"
        assert result["updated_at"] == "2026-01-01T00:00:00+00:00"


class TestSecurityInvariants:
    """INV-1 / INV-4: no credential field may appear in the queue payload."""

    @pytest.mark.asyncio
    async def test_enqueued_payload_never_contains_credential_fields(self, fake_redis):
        """The LRANGE payload must not contain any password or credential-bearing key."""
        from app.services.connection_queue import enqueue_connection

        await enqueue_connection(
            fake_redis,
            lawyer_id=3,
            rut="11111111-1",
            auth_method="segunda_clave",
            captcha_token="tok",
        )

        raw_items = await fake_redis.lrange("pjud:connect:queue", 0, -1)
        assert len(raw_items) == 1

        payload = json.loads(raw_items[0])
        forbidden_fields = {
            "password",
            "encrypted_password",
            "pwd",
            "secret",
            "plaintext",
            "encrypted_pjud_password",
            "encrypted_cu_password",
        }
        found = forbidden_fields & set(payload.keys())
        assert not found, f"Credential field(s) {found!r} found in queue payload"
