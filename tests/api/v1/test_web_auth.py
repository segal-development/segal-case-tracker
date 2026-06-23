"""Tests for email+password web authentication endpoints.

Covers:
  POST /api/v1/auth/web-login  — email/password login returning JWT
  PUT  /api/v1/auth/password   — update password (requires valid JWT)
"""

import pytest

from app.core.security import hash_password, decode_access_token
from app.models.lawyer import Lawyer


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_RUT = "55555555-5"
TEST_EMAIL = "lawyer@segal.cl"
TEST_PASSWORD = "password123"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def lawyer_with_password(db):
    """Create a Lawyer with email and bcrypt password_hash."""
    lawyer = Lawyer(rut=TEST_RUT, name="Web Auth Lawyer", email=TEST_EMAIL)
    db.add(lawyer)
    db.commit()
    db.refresh(lawyer)
    lawyer.password_hash = hash_password(TEST_PASSWORD)
    db.commit()
    db.refresh(lawyer)
    return lawyer


# ---------------------------------------------------------------------------
# Tests: POST /api/v1/auth/web-login
# ---------------------------------------------------------------------------


def test_web_login_success(client, lawyer_with_password):
    response = client.post(
        "/api/v1/auth/web-login",
        json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    payload = decode_access_token(data["access_token"])
    assert payload is not None
    assert payload["sub"] == lawyer_with_password.rut


def test_web_login_wrong_password(client, lawyer_with_password):
    response = client.post(
        "/api/v1/auth/web-login",
        json={"email": TEST_EMAIL, "password": "wrong_password"},
    )
    assert response.status_code == 401


def test_web_login_unknown_email(client, db):
    response = client.post(
        "/api/v1/auth/web-login",
        json={"email": "nobody@example.com", "password": "whatever"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Credenciales inválidas"


def test_web_login_no_password_hash(client, db):
    """Lawyer exists but has no password_hash — should be 401."""
    lawyer = Lawyer(rut="66666666-6", name="No Password Lawyer", email="nopwd@segal.cl")
    db.add(lawyer)
    db.commit()

    response = client.post(
        "/api/v1/auth/web-login",
        json={"email": "nopwd@segal.cl", "password": TEST_PASSWORD},
    )
    assert response.status_code == 401


def test_web_login_token_works_on_protected_endpoint(client, lawyer_with_password):
    """Token from web-login must be accepted by a protected endpoint."""
    login_resp = client.post(
        "/api/v1/auth/web-login",
        json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
    )
    assert login_resp.status_code == 200
    token = login_resp.json()["access_token"]

    stats_resp = client.get(
        "/api/v1/stats/firm",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert stats_resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests: PUT /api/v1/auth/password
# ---------------------------------------------------------------------------


def test_set_password_endpoint(client, lawyer_with_password):
    """Authenticated user can change their password; new password works on next login."""
    # 1. Login to get a valid token
    login_resp = client.post(
        "/api/v1/auth/web-login",
        json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
    )
    assert login_resp.status_code == 200
    token = login_resp.json()["access_token"]

    # 2. Change the password
    new_password = "newpassword123"
    set_resp = client.put(
        "/api/v1/auth/password",
        json={"email": TEST_EMAIL, "new_password": new_password},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert set_resp.status_code == 200

    # 3. Login with the new password succeeds
    login_again = client.post(
        "/api/v1/auth/web-login",
        json={"email": TEST_EMAIL, "password": new_password},
    )
    assert login_again.status_code == 200


def test_set_password_too_short(client, lawyer_with_password):
    """new_password shorter than 8 chars must be rejected with 422."""
    login_resp = client.post(
        "/api/v1/auth/web-login",
        json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
    )
    assert login_resp.status_code == 200
    token = login_resp.json()["access_token"]

    set_resp = client.put(
        "/api/v1/auth/password",
        json={"email": TEST_EMAIL, "new_password": "short"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert set_resp.status_code == 422
