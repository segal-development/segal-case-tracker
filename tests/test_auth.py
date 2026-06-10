"""Tests for authentication endpoints."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient


def test_login_requires_credentials(client: TestClient):
    """Login should require RUT and password."""
    response = client.post("/api/v1/auth/login", json={})
    assert response.status_code == 422


@pytest.mark.xfail(
    reason="/login calls scraper.login_with_user_captcha which does not exist — auth rework deferred (see #9)",
    strict=True,
)
def test_login_with_credentials(client: TestClient):
    """Login should return token with valid credentials."""
    response = client.post(
        "/api/v1/auth/login",
        json={
            "rut": "12345678-9",
            "password": "testpassword",
            "captcha_token": "test-captcha-token",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


def test_logout(client: TestClient, auth_headers: dict):
    """Logout should invalidate session."""
    with patch("app.api.v1.auth.SessionManager") as mock_sm_class:
        mock_sm = MagicMock()
        mock_sm.invalidate_session = AsyncMock(return_value=None)
        mock_sm_class.return_value = mock_sm
        response = client.post("/api/v1/auth/logout", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert "message" in data
