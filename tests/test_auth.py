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


def test_login_session_bound_to_real_lawyer_id(client: TestClient):
    """Session saved to Redis must carry the DB-assigned lawyer_id (LID-02/03)."""
    from app.services.pjud_session import PJUDSession

    captured_sessions = []

    async def capture_save(session: PJUDSession) -> None:
        captured_sessions.append(session)

    fake_session = PJUDSession.create(
        rut="12345678-9",
        cookies=[],
        auth_method="captcha",
        lawyer_id=0,  # scraper returns 0 — endpoint must replace it
    )

    with patch("app.api.v1.auth.PJUDCivilScraper") as MockScraper, \
         patch("app.api.v1.auth.get_session_store") as mock_store_fn:

        mock_inst = MagicMock()
        mock_inst.start = AsyncMock()
        mock_inst.stop = AsyncMock()
        mock_inst.login_with_token = AsyncMock(return_value=fake_session)
        MockScraper.return_value = mock_inst

        mock_store = MagicMock()
        mock_store.asave_session = AsyncMock(side_effect=capture_save)
        mock_store_fn.return_value = mock_store

        response = client.post(
            "/api/v1/auth/login",
            json={
                "rut": "12345678-9",
                "password": "p",
                "captcha_token": "c",
            },
        )

    assert response.status_code == 200
    assert len(captured_sessions) == 1
    persisted = captured_sessions[0]
    # The lawyer_id must be a real DB-assigned integer >= 1, never the hardcoded 0
    assert persisted.lawyer_id != 0, (
        f"Session was saved with hardcoded lawyer_id=0; "
        f"expected the DB-resolved id"
    )
    assert isinstance(persisted.lawyer_id, int)
    assert persisted.lawyer_id >= 1


def test_logout(client: TestClient, auth_headers: dict):
    """Logout should delete session via the async store."""
    with patch("app.api.v1.auth.get_session_store") as mock_store_fn:
        mock_store = MagicMock()
        mock_store.adelete_session_by_rut = AsyncMock(return_value=True)
        mock_store_fn.return_value = mock_store
        response = client.post("/api/v1/auth/logout", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert "message" in data
