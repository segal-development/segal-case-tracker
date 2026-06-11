"""
Tests for authentication endpoints — dual login paths (captcha + Clave Única).

S1-T6 additions:
- login_with_token is awaited (not login_with_user_captcha)
- store.asave_session is awaited
- /auth/refresh returns 404 (endpoint removed per ADR-4)
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from datetime import datetime, timedelta, timezone


class TestDualAuthEndpoints:
    """Test that both auth endpoints exist and validate input."""

    def test_captcha_login_endpoint_exists(self, client: TestClient):
        """POST /login should exist and require captcha_token."""
        response = client.post(
            "/api/v1/auth/login",
            json={
                "rut": "12345678-9",
                "password": "testpass",
                # Missing captcha_token
            }
        )
        assert response.status_code == 422
        assert "captcha_token" in response.text

    def test_clave_unica_login_endpoint_exists(self, client: TestClient):
        """POST /login/clave-unica should exist."""
        response = client.post(
            "/api/v1/auth/login/clave-unica",
            json={
                "rut": "12345678-9",
                "password": "testpass",
            }
        )
        assert response.status_code != 404

    def test_clave_unica_requires_rut(self, client: TestClient):
        response = client.post(
            "/api/v1/auth/login/clave-unica",
            json={"password": "testpass"}
        )
        assert response.status_code == 422
        assert "rut" in response.text

    def test_clave_unica_requires_password(self, client: TestClient):
        response = client.post(
            "/api/v1/auth/login/clave-unica",
            json={"rut": "12345678-9"}
        )
        assert response.status_code == 422
        assert "password" in response.text


class TestRefreshEndpointRemoved:
    """AUTH-03: No dedicated session-refresh endpoint (S1-T6)."""

    def test_refresh_endpoint_returns_404(self, client: TestClient):
        """POST /auth/refresh must not exist after ADR-4 removal."""
        response = client.post(
            "/api/v1/auth/refresh",
            json={"captcha_token": "test-token"},
            headers={"Authorization": "Bearer fake"},
        )
        assert response.status_code == 404


class TestCaptchaLoginCallSites:
    """S1-T6: login endpoint calls login_with_token (not login_with_user_captcha)."""

    def test_login_with_token_is_called(self, client: TestClient):
        """POST /auth/login must call login_with_token on the scraper."""
        from app.services.pjud_session import PJUDSession
        fake_session = PJUDSession.create(
            rut="12345678-9",
            cookies=[],
            auth_method="captcha",
        )

        with patch("app.api.v1.auth.PJUDCivilScraper") as MockScraper, \
             patch("app.api.v1.auth.get_session_store") as mock_store_fn:

            mock_scraper_instance = MagicMock()
            mock_scraper_instance.start = AsyncMock()
            mock_scraper_instance.stop = AsyncMock()
            mock_scraper_instance.login_with_token = AsyncMock(return_value=fake_session)
            MockScraper.return_value = mock_scraper_instance

            mock_store = MagicMock()
            mock_store.asave_session = AsyncMock(return_value=True)
            mock_store_fn.return_value = mock_store

            response = client.post(
                "/api/v1/auth/login",
                json={
                    "rut": "12345678-9",
                    "password": "testpass",
                    "captcha_token": "captcha-123",
                }
            )

            assert response.status_code == 200
            mock_scraper_instance.login_with_token.assert_awaited_once()
            mock_store.asave_session.assert_awaited_once()

    def test_login_no_login_with_user_captcha_symbol_in_module(self):
        """login_with_user_captcha must not be imported in auth.py."""
        import app.api.v1.auth as auth_module
        assert not hasattr(auth_module, "login_with_user_captcha"), (
            "login_with_user_captcha symbol should not exist in auth.py after rework"
        )

    def test_store_asave_session_is_awaited(self, client: TestClient):
        """asave_session must be awaited (async store) on captcha login."""
        from app.services.pjud_session import PJUDSession
        fake_session = PJUDSession.create(rut="12345678-9", cookies=[], auth_method="captcha")

        with patch("app.api.v1.auth.PJUDCivilScraper") as MockScraper, \
             patch("app.api.v1.auth.get_session_store") as mock_store_fn:

            mock_inst = MagicMock()
            mock_inst.start = AsyncMock()
            mock_inst.stop = AsyncMock()
            mock_inst.login_with_token = AsyncMock(return_value=fake_session)
            MockScraper.return_value = mock_inst

            mock_store = MagicMock()
            mock_store.asave_session = AsyncMock(return_value=True)
            mock_store_fn.return_value = mock_store

            client.post(
                "/api/v1/auth/login",
                json={"rut": "12345678-9", "password": "p", "captcha_token": "c"},
            )

            # asave_session must be an AsyncMock that was actually awaited
            mock_store.asave_session.assert_awaited()


class TestClaveUnicaLoginMocked:
    """Test Clave Unica login with mocked browser."""

    @pytest.mark.asyncio
    async def test_successful_clave_unica_login(self, client: TestClient):
        """Successful Clave Unica login should return token and session_id."""
        from app.services.pjud_session import PJUDSession
        mock_session = PJUDSession.create(
            rut="12345678-9",
            cookies=[],
            lawyer_id=1,
            auth_method="clave_unica",
        )

        with patch("app.api.v1.auth.BrowserFactory") as MockBrowserFactory, \
             patch("app.api.v1.auth.ClaveUnicaAuth") as MockAuth, \
             patch("app.api.v1.auth.get_session_store") as mock_store_fn:

            mock_factory = AsyncMock()
            mock_page = AsyncMock()
            mock_factory.__aenter__ = AsyncMock(return_value=mock_factory)
            mock_factory.__aexit__ = AsyncMock()
            mock_factory.new_page = AsyncMock(return_value=mock_page)
            MockBrowserFactory.return_value = mock_factory

            mock_auth = MagicMock()
            mock_auth.login = AsyncMock(return_value=mock_session)
            MockAuth.return_value = mock_auth

            mock_store = MagicMock()
            mock_store.asave_session = AsyncMock(return_value=True)
            mock_store_fn.return_value = mock_store

            response = client.post(
                "/api/v1/auth/login/clave-unica",
                json={"rut": "12345678-9", "password": "testpass"}
            )

            assert response.status_code == 200
            data = response.json()
            assert "access_token" in data
            assert data["token_type"] == "bearer"
            assert data["auth_method"] == "clave_unica"
            assert data["session_id"] == mock_session.session_id

    @pytest.mark.asyncio
    async def test_clave_unica_auth_error_handling(self):
        from app.scrapper.pjud.clave_unica import ClaveUnicaAuth, ClaveUnicaAuthError, ClaveUnicaCredentials

        error = ClaveUnicaAuthError("Invalid credentials")
        assert str(error) == "Invalid credentials"

        creds = ClaveUnicaCredentials(rut="", password="pass")
        assert creds.validate() is False


class TestSessionStatusWithAuthMethod:
    """Test session status includes auth_method."""

    def test_session_status_endpoint_exists(self, client: TestClient):
        response = client.get("/api/v1/auth/session-status")
        assert response.status_code in [401, 403]
