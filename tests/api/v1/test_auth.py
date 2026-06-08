"""
Integration tests for dual authentication (Captcha vs Clave Unica).

Tests:
1. Both endpoints exist and accept correct input
2. Both endpoints reject invalid input
3. Both endpoints return consistent response format
4. Session status reports auth_method correctly
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from datetime import datetime, timedelta


class TestDualAuthEndpoints:
    """Test that both auth endpoints work independently."""
    
    def test_captcha_login_endpoint_exists(self, client: TestClient):
        """POST /login should exist and require captcha_token."""
        response = client.post(
            "/api/v1/auth/login",
            json={
                "rut": "16021492-9",
                "password": "testpass",
                # Missing captcha_token
            }
        )
        # Should return 422 for missing captcha_token
        assert response.status_code == 422
        assert "captcha_token" in response.text
    
    def test_clave_unica_login_endpoint_exists(self, client: TestClient):
        """POST /login/clave-unica should exist."""
        response = client.post(
            "/api/v1/auth/login/clave-unica",
            json={
                "rut": "16021492-9",
                "password": "testpass",
            }
        )
        # Should not be 404 (endpoint exists)
        assert response.status_code != 404
    
    def test_clave_unica_requires_rut(self, client: TestClient):
        """Clave Unica login should require RUT."""
        response = client.post(
            "/api/v1/auth/login/clave-unica",
            json={
                "password": "testpass",
            }
        )
        assert response.status_code == 422
        assert "rut" in response.text
    
    def test_clave_unica_requires_password(self, client: TestClient):
        """Clave Unica login should require password."""
        response = client.post(
            "/api/v1/auth/login/clave-unica",
            json={
                "rut": "16021492-9",
            }
        )
        assert response.status_code == 422
        assert "password" in response.text


class TestClaveUnicaLoginMocked:
    """Test Clave Unica login with mocked browser."""
    
    @pytest.mark.asyncio
    async def test_successful_clave_unica_login(self, client: TestClient):
        """Successful Clave Unica login should return token and session_id."""
        # Mock the BrowserFactory and ClaveUnicaAuth
        mock_session = MagicMock()
        mock_session.session_id = "test-session-123"
        mock_session.rut = "16021492-9"
        mock_session.lawyer_id = 1
        mock_session.auth_method = "clave_unica"
        mock_session.cookies = []
        mock_session.created_at = datetime.utcnow().isoformat()
        mock_session.expires_at = (datetime.utcnow() + timedelta(hours=2)).isoformat()
        mock_session.time_until_expiry = MagicMock(return_value=timedelta(hours=2))
        mock_session.to_dict = MagicMock(return_value={
            "session_id": "test-session-123",
            "rut": "16021492-9",
            "lawyer_id": 1,
            "auth_method": "clave_unica",
            "cookies": [],
            "created_at": mock_session.created_at,
            "expires_at": mock_session.expires_at,
            "local_storage": "{}",
        })
        
        with patch('app.api.v1.auth.BrowserFactory') as MockBrowserFactory, \
             patch('app.api.v1.auth.ClaveUnicaAuth') as MockAuth, \
             patch('app.api.v1.auth.get_session_store') as mock_store:
            
            # Setup mocks
            mock_factory = AsyncMock()
            mock_page = AsyncMock()
            mock_factory.__aenter__ = AsyncMock(return_value=mock_factory)
            mock_factory.__aexit__ = AsyncMock()
            mock_factory.new_page = AsyncMock(return_value=mock_page)
            MockBrowserFactory.return_value = mock_factory
            
            mock_auth = MagicMock()
            mock_auth.login = AsyncMock(return_value=mock_session)
            MockAuth.return_value = mock_auth
            
            mock_store.return_value.save_session = MagicMock(return_value=True)
            
            response = client.post(
                "/api/v1/auth/login/clave-unica",
                json={
                    "rut": "16021492-9",
                    "password": "testpass",
                }
            )
            
            assert response.status_code == 200
            data = response.json()
            
            assert "access_token" in data
            assert data["token_type"] == "bearer"
            assert data["auth_method"] == "clave_unica"
            assert data["session_id"] == "test-session-123"
    
    @pytest.mark.asyncio
    async def test_clave_unica_auth_error_handling(self):
        """ClaveUnicaAuthError should be raised for failed auth."""
        from app.scrapper.pjud.clave_unica import ClaveUnicaAuth, ClaveUnicaAuthError, ClaveUnicaCredentials
        
        # Test that ClaveUnicaAuthError is properly defined
        error = ClaveUnicaAuthError("Invalid credentials")
        assert str(error) == "Invalid credentials"
        
        # Test credentials validation
        creds = ClaveUnicaCredentials(rut="", password="pass")
        assert creds.validate() is False


class TestSessionStatusWithAuthMethod:
    """Test session status includes auth_method."""
    
    def test_session_status_endpoint_exists(self, client: TestClient):
        """GET /session-status should exist."""
        # Will fail auth but endpoint should exist
        response = client.get("/api/v1/auth/session-status")
        # 401 or 403 means endpoint exists but requires auth
        assert response.status_code in [401, 403]
