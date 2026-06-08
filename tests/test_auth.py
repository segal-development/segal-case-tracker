"""Tests for authentication endpoints."""

import pytest
from fastapi.testclient import TestClient


def test_login_requires_credentials(client: TestClient):
    """Login should require RUT and password."""
    response = client.post("/api/v1/auth/login", json={})
    assert response.status_code == 422


def test_login_with_credentials(client: TestClient):
    """Login should return token with valid credentials."""
    response = client.post(
        "/api/v1/auth/login",
        json={
            "rut": "12345678-9",
            "password": "testpassword",
        },
    )
    # Currently returns placeholder, should be 200
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


def test_logout(client: TestClient):
    """Logout should invalidate session."""
    response = client.post("/api/v1/auth/logout")
    assert response.status_code == 200
    data = response.json()
    assert "message" in data
