"""Tests for POST /pjud/connect and GET /pjud/connect/{id}/status.

TDD RED → GREEN cycle (Task 2.1 / 2.2).

Security invariant tested: the Redis queue payload MUST NOT contain any
credential or password field (INV-1 / INV-4 from spec).

Auth is mocked via app.dependency_overrides[get_current_lawyer].
Redis is injected via app.dependency_overrides[get_redis_dep].
"""

import json
import pytest
import fakeredis.aioredis

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.lawyer import Lawyer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_redis():
    """In-memory async Redis backed by fakeredis — no live connection needed."""
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def lawyer_with_credential(db):
    """A lawyer that has an encrypted PJUD password stored (segunda_clave)."""
    l = Lawyer(
        rut="12345678-9",
        name="Test Lawyer",
        encrypted_pjud_password="fernet_encrypted_token_here",
    )
    db.add(l)
    db.commit()
    db.refresh(l)
    return l


@pytest.fixture
def lawyer_cu_credential(db):
    """A lawyer that has an encrypted CU password stored."""
    l = Lawyer(
        rut="98765432-1",
        name="CU Lawyer",
        encrypted_clave_unica_password="fernet_encrypted_cu_token",
    )
    db.add(l)
    db.commit()
    db.refresh(l)
    return l


@pytest.fixture
def lawyer_no_credential(db):
    """A lawyer with no stored credential."""
    l = Lawyer(rut="11111111-1", name="No Cred Lawyer")
    db.add(l)
    db.commit()
    db.refresh(l)
    return l


@pytest.fixture
def authed_client(client, lawyer_with_credential, fake_redis):
    """Test client with auth mock and FakeRedis injection."""
    from app.api.v1.pjud import get_redis_dep

    async def _mock_lawyer():
        return {"sub": str(lawyer_with_credential.id)}

    async def _mock_redis():
        return fake_redis

    app.dependency_overrides[get_current_lawyer] = _mock_lawyer
    app.dependency_overrides[get_redis_dep] = _mock_redis
    yield client, fake_redis, lawyer_with_credential
    app.dependency_overrides.pop(get_current_lawyer, None)
    app.dependency_overrides.pop(get_redis_dep, None)


@pytest.fixture
def authed_client_cu(client, lawyer_cu_credential, fake_redis):
    """Test client with auth mock pointing to the CU-credentialed lawyer."""
    from app.api.v1.pjud import get_redis_dep

    async def _mock_lawyer():
        return {"sub": str(lawyer_cu_credential.id)}

    async def _mock_redis():
        return fake_redis

    app.dependency_overrides[get_current_lawyer] = _mock_lawyer
    app.dependency_overrides[get_redis_dep] = _mock_redis
    yield client, fake_redis, lawyer_cu_credential
    app.dependency_overrides.pop(get_current_lawyer, None)
    app.dependency_overrides.pop(get_redis_dep, None)


@pytest.fixture
def authed_client_no_cred(client, lawyer_no_credential, fake_redis):
    """Test client with auth mock pointing to a lawyer with no credential."""
    from app.api.v1.pjud import get_redis_dep

    async def _mock_lawyer():
        return {"sub": str(lawyer_no_credential.id)}

    async def _mock_redis():
        return fake_redis

    app.dependency_overrides[get_current_lawyer] = _mock_lawyer
    app.dependency_overrides[get_redis_dep] = _mock_redis
    yield client, fake_redis, lawyer_no_credential
    app.dependency_overrides.pop(get_current_lawyer, None)
    app.dependency_overrides.pop(get_redis_dep, None)


# ---------------------------------------------------------------------------
# POST /pjud/connect tests
# ---------------------------------------------------------------------------


class TestPjudConnect:
    """POST /api/v1/pjud/connect endpoint."""

    def test_valid_segunda_clave_enqueues_and_returns_pending(self, authed_client):
        """Valid segunda_clave request → 200 with {connection_id, status: pending}."""
        test_client, _, lawyer = authed_client
        response = test_client.post(
            "/api/v1/pjud/connect",
            json={
                "lawyer_id": lawyer.id,
                "rut": lawyer.rut,
                "auth_method": "segunda_clave",
                "captcha_token": "valid_token_abc",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert "connection_id" in body
        assert body["status"] == "pending"
        # connection_id is a UUID4 (36 chars)
        assert len(body["connection_id"]) == 36

    def test_missing_captcha_token_segunda_clave_returns_422(self, authed_client):
        """segunda_clave without captcha_token → 422 Unprocessable Entity."""
        test_client, _, lawyer = authed_client
        response = test_client.post(
            "/api/v1/pjud/connect",
            json={
                "lawyer_id": lawyer.id,
                "rut": lawyer.rut,
                "auth_method": "segunda_clave",
                # captcha_token intentionally omitted
            },
        )
        assert response.status_code == 422, response.text

    def test_clave_unica_connect_no_captcha_token_ok(self, authed_client_cu):
        """clave_unica connect without captcha_token → 200 with pending status."""
        test_client, _, lawyer = authed_client_cu
        response = test_client.post(
            "/api/v1/pjud/connect",
            json={
                "lawyer_id": lawyer.id,
                "rut": lawyer.rut,
                "auth_method": "clave_unica",
                # no captcha_token — not required for CU
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "pending"

    def test_unknown_lawyer_returns_404(self, authed_client):
        """A lawyer_id that does not exist in the DB → 404."""
        test_client, _, _ = authed_client
        response = test_client.post(
            "/api/v1/pjud/connect",
            json={
                "lawyer_id": 999999,
                "rut": "00000000-0",
                "auth_method": "segunda_clave",
                "captcha_token": "tok",
            },
        )
        assert response.status_code == 404, response.text

    def test_no_stored_credential_segunda_clave_returns_409(self, authed_client_no_cred):
        """Lawyer exists but has no encrypted_pjud_password → 409 Conflict."""
        test_client, _, lawyer = authed_client_no_cred
        response = test_client.post(
            "/api/v1/pjud/connect",
            json={
                "lawyer_id": lawyer.id,
                "rut": lawyer.rut,
                "auth_method": "segunda_clave",
                "captcha_token": "tok",
            },
        )
        assert response.status_code == 409, response.text

    def test_security_invariant_no_password_in_queue_payload(self, authed_client):
        """INV-1 / INV-4: LRANGE after POST must not contain any password field."""
        import asyncio
        test_client, fake_redis, lawyer = authed_client
        response = test_client.post(
            "/api/v1/pjud/connect",
            json={
                "lawyer_id": lawyer.id,
                "rut": lawyer.rut,
                "auth_method": "segunda_clave",
                "captcha_token": "tok_inv",
            },
        )
        assert response.status_code == 200, response.text

        # Inspect the Redis payload directly
        raw_items = asyncio.get_event_loop().run_until_complete(
            fake_redis.lrange("pjud:connect:queue", 0, -1)
        )
        assert len(raw_items) >= 1
        payload = json.loads(raw_items[-1])

        forbidden_fields = {
            "password",
            "encrypted_password",
            "pwd",
            "secret",
            "plaintext",
            "encrypted_pjud_password",
            "encrypted_cu_password",
            "encrypted_clave_unica_password",
        }
        found = forbidden_fields & set(payload.keys())
        assert not found, f"Credential field(s) {found!r} found in queue payload"


# ---------------------------------------------------------------------------
# GET /pjud/connect/{connection_id}/status tests
# ---------------------------------------------------------------------------


class TestPjudConnectStatus:
    """GET /api/v1/pjud/connect/{connection_id}/status endpoint."""

    def _seed_status(self, fake_redis, connection_id: str, payload: dict):
        """Write a status blob directly into FakeRedis (bypasses the endpoint)."""
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            fake_redis.setex(
                f"pjud:connect:status:{connection_id}",
                300,
                json.dumps(payload),
            )
        )

    def test_status_returns_pending(self, authed_client):
        """GET status for a recently enqueued connection → {status: pending}."""
        test_client, fake_redis, _ = authed_client
        cid = "test-pending-uuid-1234"
        self._seed_status(fake_redis, cid, {"status": "pending", "updated_at": "2026-01-01T00:00:00+00:00"})

        response = test_client.get(f"/api/v1/pjud/connect/{cid}/status")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "pending"

    def test_status_returns_connected_with_cases_synced(self, authed_client):
        """GET status for a completed connection → {status: connected, cases_synced: N}."""
        test_client, fake_redis, _ = authed_client
        cid = "test-connected-uuid-5678"
        self._seed_status(
            fake_redis,
            cid,
            {"status": "connected", "cases_synced": 42, "session_id": "sess-abc"},
        )

        response = test_client.get(f"/api/v1/pjud/connect/{cid}/status")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "connected"
        assert body["cases_synced"] == 42

    def test_status_returns_failed_with_reason(self, authed_client):
        """GET status for a failed connection → {status: failed, reason: ...}."""
        test_client, fake_redis, _ = authed_client
        cid = "test-failed-uuid-9999"
        self._seed_status(
            fake_redis,
            cid,
            {"status": "failed", "reason": "token_expired"},
        )

        response = test_client.get(f"/api/v1/pjud/connect/{cid}/status")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "failed"
        assert body["reason"] == "token_expired"

    def test_status_unknown_connection_id_returns_404(self, authed_client):
        """GET status for an unknown connection_id → 404."""
        test_client, _, _ = authed_client
        response = test_client.get("/api/v1/pjud/connect/nonexistent-id-xyz/status")
        assert response.status_code == 404, response.text
