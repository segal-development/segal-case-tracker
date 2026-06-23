"""Tests for admin role + account management API.

Covers:
  Role 403/200 gate — PUT /api/v1/auth/password, PUT /api/v1/goals/{key},
                       GET /api/v1/lawyers/accounts, PUT /api/v1/lawyers/{id}/account
  GET  /api/v1/auth/me  — returns {rut, name, email, role}
  PUT  /api/v1/lawyers/{id}/account — full account update (email, role, password)
  GET  /api/v1/lawyers/accounts     — admin-only account list with has_password flag
"""

import pytest
from datetime import timedelta

from app.core.security import create_access_token, hash_password
from app.models.lawyer import Lawyer


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ADMIN_RUT = "11111111-1"
ADMIN_EMAIL = "admin@segal.cl"
ADMIN_PASSWORD = "adminpassword123"

LAWYER_RUT = "22222222-2"
LAWYER_EMAIL = "lawyer@segal.cl"
LAWYER_PASSWORD = "lawyerpassword123"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_lawyer(db):
    """Lawyer with role='admin', email, and password_hash."""
    lawyer = Lawyer(rut=ADMIN_RUT, name="Admin Lawyer", email=ADMIN_EMAIL, role="admin")
    db.add(lawyer)
    db.commit()
    db.refresh(lawyer)
    lawyer.password_hash = hash_password(ADMIN_PASSWORD)
    db.commit()
    db.refresh(lawyer)
    return lawyer


@pytest.fixture
def regular_lawyer(db):
    """Lawyer with role='lawyer' (default), email, and password_hash."""
    lawyer = Lawyer(rut=LAWYER_RUT, name="Regular Lawyer", email=LAWYER_EMAIL, role="lawyer")
    db.add(lawyer)
    db.commit()
    db.refresh(lawyer)
    lawyer.password_hash = hash_password(LAWYER_PASSWORD)
    db.commit()
    db.refresh(lawyer)
    return lawyer


@pytest.fixture
def admin_headers(admin_lawyer):
    """Authorization headers with a valid JWT for the admin lawyer."""
    token = create_access_token({"sub": ADMIN_RUT}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def lawyer_headers(regular_lawyer):
    """Authorization headers with a valid JWT for the regular lawyer."""
    token = create_access_token({"sub": LAWYER_RUT}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. Role 403 gate — role="lawyer" gets 403 on admin-only endpoints
# ---------------------------------------------------------------------------


def test_put_password_requires_admin_403(client, regular_lawyer, lawyer_headers):
    resp = client.put(
        "/api/v1/auth/password",
        json={"email": LAWYER_EMAIL, "new_password": "newpassword123"},
        headers=lawyer_headers,
    )
    assert resp.status_code == 403


def test_put_goals_requires_admin_403(client, regular_lawyer, lawyer_headers):
    resp = client.put(
        "/api/v1/goals/monthly_productivity",
        json={"value": 100},
        headers=lawyer_headers,
    )
    assert resp.status_code == 403


def test_get_accounts_requires_admin_403(client, regular_lawyer, lawyer_headers):
    resp = client.get("/api/v1/lawyers/accounts", headers=lawyer_headers)
    assert resp.status_code == 403


def test_put_account_requires_admin_403(client, admin_lawyer, regular_lawyer, lawyer_headers):
    resp = client.put(
        f"/api/v1/lawyers/{admin_lawyer.id}/account",
        json={"email": "new@segal.cl"},
        headers=lawyer_headers,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 2. Role 200 gate — role="admin" gets 200 on admin-only endpoints
# ---------------------------------------------------------------------------


def test_put_password_admin_allowed(client, admin_lawyer, admin_headers):
    resp = client.put(
        "/api/v1/auth/password",
        json={"email": ADMIN_EMAIL, "new_password": "newpassword456"},
        headers=admin_headers,
    )
    assert resp.status_code == 200


def test_put_goals_admin_allowed(client, admin_lawyer, admin_headers):
    resp = client.put(
        "/api/v1/goals/monthly_productivity",
        json={"value": 100},
        headers=admin_headers,
    )
    assert resp.status_code == 200


def test_get_accounts_admin_allowed(client, admin_lawyer, admin_headers):
    resp = client.get("/api/v1/lawyers/accounts", headers=admin_headers)
    assert resp.status_code == 200


def test_put_account_admin_allowed(client, admin_lawyer, regular_lawyer, admin_headers):
    resp = client.put(
        f"/api/v1/lawyers/{regular_lawyer.id}/account",
        json={"email": "updated@segal.cl"},
        headers=admin_headers,
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 3. GET /api/v1/auth/me
# ---------------------------------------------------------------------------


def test_get_me_admin(client, admin_lawyer, admin_headers):
    resp = client.get("/api/v1/auth/me", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["rut"] == ADMIN_RUT
    assert data["name"] == "Admin Lawyer"
    assert data["email"] == ADMIN_EMAIL
    assert data["role"] == "admin"


def test_get_me_regular_lawyer(client, regular_lawyer, lawyer_headers):
    resp = client.get("/api/v1/auth/me", headers=lawyer_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["rut"] == LAWYER_RUT
    assert data["role"] == "lawyer"


def test_get_me_unauthorized(client):
    """No token → 403 (HTTPBearer raises 403 when no Authorization header)."""
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 4. PUT /api/v1/lawyers/{id}/account scenarios
# ---------------------------------------------------------------------------


def test_update_account_sets_email_role_password(client, admin_lawyer, regular_lawyer, admin_headers):
    """Update email + role + password; verify login works and /me shows new role."""
    new_email = "updated_lawyer@segal.cl"
    new_password = "updatedpassword123"
    new_role = "admin"

    resp = client.put(
        f"/api/v1/lawyers/{regular_lawyer.id}/account",
        json={"email": new_email, "role": new_role, "new_password": new_password},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == new_email
    assert data["role"] == new_role
    assert data["has_password"] is True

    # New password must work on web-login
    login_resp = client.post(
        "/api/v1/auth/web-login",
        json={"email": new_email, "password": new_password},
    )
    assert login_resp.status_code == 200
    new_token = login_resp.json()["access_token"]

    # GET /me must reflect the new role
    me_resp = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {new_token}"},
    )
    assert me_resp.status_code == 200
    assert me_resp.json()["role"] == new_role


def test_update_account_invalid_role(client, admin_lawyer, regular_lawyer, admin_headers):
    """Invalid role string → 422."""
    resp = client.put(
        f"/api/v1/lawyers/{regular_lawyer.id}/account",
        json={"role": "superuser"},
        headers=admin_headers,
    )
    assert resp.status_code == 422


def test_update_account_password_too_short(client, admin_lawyer, regular_lawyer, admin_headers):
    """Password shorter than 8 chars → 422."""
    resp = client.put(
        f"/api/v1/lawyers/{regular_lawyer.id}/account",
        json={"new_password": "short"},
        headers=admin_headers,
    )
    assert resp.status_code == 422


def test_update_account_nonexistent_id(client, admin_lawyer, admin_headers):
    """Non-existent lawyer id → 404."""
    resp = client.put(
        "/api/v1/lawyers/99999/account",
        json={"email": "nobody@segal.cl"},
        headers=admin_headers,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 5. GET /api/v1/lawyers/accounts
# ---------------------------------------------------------------------------


def test_list_accounts(client, admin_lawyer, regular_lawyer, admin_headers):
    """Lists all lawyers with id, rut, name, email, role, has_password."""
    resp = client.get("/api/v1/lawyers/accounts", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2

    admin_entry = next(x for x in data if x["rut"] == ADMIN_RUT)
    assert admin_entry["role"] == "admin"
    assert admin_entry["has_password"] is True

    lawyer_entry = next(x for x in data if x["rut"] == LAWYER_RUT)
    assert lawyer_entry["role"] == "lawyer"
    assert lawyer_entry["has_password"] is True


def test_list_accounts_has_password_false(client, admin_lawyer, admin_headers, db):
    """has_password is False when password_hash is None."""
    no_pwd_lawyer = Lawyer(rut="33333333-3", name="No Password Lawyer", role="lawyer")
    db.add(no_pwd_lawyer)
    db.commit()
    db.refresh(no_pwd_lawyer)

    resp = client.get("/api/v1/lawyers/accounts", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    no_pwd_entry = next(x for x in data if x["rut"] == "33333333-3")
    assert no_pwd_entry["has_password"] is False
