"""Tests for POST /pjud/connect and GET /pjud/connect/{id}/status (DB-backed).

The connection queue lives in Cloud SQL (pending_connections table), NOT Redis —
Carla and the cloud share the DB, not Redis. Auth is mocked via get_current_lawyer;
the endpoint uses the test sqlite via the get_db override (conftest).

Security invariant (INV-1 / INV-4): no credential/password field is ever stored in
the pending_connections row — only the ephemeral captcha_token.
"""

import pytest

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.lawyer import Lawyer
from app.models.pending_connection import PendingConnection


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def lawyer_with_credential(db):
    """A lawyer with an encrypted PJUD password stored (segunda_clave)."""
    l = Lawyer(rut="12345678-9", name="Test Lawyer", encrypted_pjud_password="fernet_token")
    db.add(l)
    db.commit()
    db.refresh(l)
    return l


@pytest.fixture
def lawyer_cu_credential(db):
    """A lawyer with an encrypted CU password stored."""
    l = Lawyer(rut="98765432-1", name="CU Lawyer", encrypted_clave_unica_password="fernet_cu")
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


def _auth(lawyer):
    async def _mock_lawyer():
        return {"sub": str(lawyer.id)}
    app.dependency_overrides[get_current_lawyer] = _mock_lawyer


@pytest.fixture
def authed_client(client, db, lawyer_with_credential):
    _auth(lawyer_with_credential)
    yield client, db, lawyer_with_credential
    app.dependency_overrides.pop(get_current_lawyer, None)


@pytest.fixture
def authed_client_cu(client, db, lawyer_cu_credential):
    _auth(lawyer_cu_credential)
    yield client, db, lawyer_cu_credential
    app.dependency_overrides.pop(get_current_lawyer, None)


@pytest.fixture
def authed_client_no_cred(client, db, lawyer_no_credential):
    _auth(lawyer_no_credential)
    yield client, db, lawyer_no_credential
    app.dependency_overrides.pop(get_current_lawyer, None)


# ---------------------------------------------------------------------------
# POST /pjud/connect
# ---------------------------------------------------------------------------


class TestPjudConnect:
    def test_valid_segunda_clave_enqueues_and_returns_pending(self, authed_client):
        tc, db, lawyer = authed_client
        r = tc.post("/api/v1/pjud/connect", json={
            "lawyer_id": lawyer.id, "rut": lawyer.rut,
            "auth_method": "segunda_clave", "captcha_token": "valid_token_abc",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert "connection_id" in body and body["status"] == "pending"
        assert len(body["connection_id"]) == 36  # str(uuid4())
        row = db.query(PendingConnection).filter_by(connection_id=body["connection_id"]).first()
        assert row is not None and row.status == "pending"

    def test_missing_captcha_token_segunda_clave_returns_422(self, authed_client):
        tc, _, lawyer = authed_client
        r = tc.post("/api/v1/pjud/connect", json={
            "lawyer_id": lawyer.id, "rut": lawyer.rut, "auth_method": "segunda_clave",
        })
        assert r.status_code == 422, r.text

    def test_clave_unica_connect_no_captcha_token_ok(self, authed_client_cu):
        tc, _, lawyer = authed_client_cu
        r = tc.post("/api/v1/pjud/connect", json={
            "lawyer_id": lawyer.id, "rut": lawyer.rut, "auth_method": "clave_unica",
        })
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "pending"

    def test_unknown_lawyer_returns_404(self, authed_client):
        tc, _, _ = authed_client
        r = tc.post("/api/v1/pjud/connect", json={
            "lawyer_id": 999999, "rut": "00000000-0",
            "auth_method": "segunda_clave", "captcha_token": "tok",
        })
        assert r.status_code == 404, r.text

    def test_no_stored_credential_segunda_clave_returns_409(self, authed_client_no_cred):
        tc, _, lawyer = authed_client_no_cred
        r = tc.post("/api/v1/pjud/connect", json={
            "lawyer_id": lawyer.id, "rut": lawyer.rut,
            "auth_method": "segunda_clave", "captcha_token": "tok",
        })
        assert r.status_code == 409, r.text

    def test_security_invariant_no_password_in_queue_row(self, authed_client):
        """INV-1/INV-4: the pending_connections row carries no credential — only the token."""
        tc, db, lawyer = authed_client
        r = tc.post("/api/v1/pjud/connect", json={
            "lawyer_id": lawyer.id, "rut": lawyer.rut,
            "auth_method": "segunda_clave", "captcha_token": "tok_inv",
        })
        assert r.status_code == 200, r.text
        cols = set(PendingConnection.__table__.columns.keys())
        forbidden = {
            "password", "encrypted_password", "pwd", "secret", "plaintext",
            "encrypted_pjud_password", "encrypted_clave_unica_password",
        }
        assert not (forbidden & cols), f"credential column(s) in pending_connections: {forbidden & cols}"
        row = db.query(PendingConnection).filter_by(connection_id=r.json()["connection_id"]).first()
        assert row.captcha_token == "tok_inv"  # only the ephemeral token, no password


# ---------------------------------------------------------------------------
# GET /pjud/connect/{connection_id}/status
# ---------------------------------------------------------------------------


class TestPjudConnectStatus:
    def _seed(self, db, lawyer, connection_id, **fields):
        row = PendingConnection(
            connection_id=connection_id, lawyer_id=lawyer.id, rut=lawyer.rut,
            auth_method="segunda_clave", **fields,
        )
        db.add(row)
        db.commit()

    def test_status_returns_pending(self, authed_client):
        tc, db, lawyer = authed_client
        self._seed(db, lawyer, "cid-pending", status="pending")
        r = tc.get("/api/v1/pjud/connect/cid-pending/status")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "pending"

    def test_status_returns_connected_with_cases_synced(self, authed_client):
        tc, db, lawyer = authed_client
        self._seed(db, lawyer, "cid-connected", status="connected", cases_synced=42, session_id="sess-abc")
        r = tc.get("/api/v1/pjud/connect/cid-connected/status")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "connected" and body["cases_synced"] == 42

    def test_status_returns_failed_with_error(self, authed_client):
        tc, db, lawyer = authed_client
        self._seed(db, lawyer, "cid-failed", status="failed", error="token_expired")
        r = tc.get("/api/v1/pjud/connect/cid-failed/status")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "failed" and body["error"] == "token_expired"

    def test_status_unknown_connection_id_returns_404(self, authed_client):
        tc, _, _ = authed_client
        r = tc.get("/api/v1/pjud/connect/nonexistent-id-xyz/status")
        assert r.status_code == 404, r.text
