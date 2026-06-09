"""
Integration tests for stateless PJUD endpoints.

Tests:
1. Two requests don't share browser state
2. Session is retrieved from Redis, not in-memory
3. Logout removes session from Redis
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient


class TestStatelessEndpoints:
    """Test that endpoints don't share browser state."""
    
    @pytest.fixture
    def mock_session(self):
        """Create mock session data."""
        from app.services.session_store import PJUDSession
        return PJUDSession(
            session_id="test-session-123",
            lawyer_id=1,
            rut="12345678-9",
            cookies=[{"name": "test", "value": "cookie"}],
            created_at="2026-06-08T10:00:00",
            expires_at="2026-06-08T12:00:00",
            local_storage="{}",
            auth_method="captcha",
        )
    
    def test_session_retrieved_from_redis(self, client, mock_session):
        """Sessions should be retrieved from Redis, not in-memory."""
        with patch('app.api.v1.pjud.get_session_store') as mock_store_fn:
            mock_store = MagicMock()
            mock_store.get_session.return_value = mock_session
            mock_store_fn.return_value = mock_store
            
            with patch('app.api.v1.pjud.BrowserFactory') as mock_factory_cls:
                mock_factory = AsyncMock()
                mock_factory.__aenter__ = AsyncMock(return_value=mock_factory)
                mock_factory.__aexit__ = AsyncMock(return_value=None)
                mock_factory.new_page = AsyncMock()
                mock_factory._browser = MagicMock()
                mock_factory._context = MagicMock()
                mock_factory_cls.return_value = mock_factory
                
                with patch('app.api.v1.pjud.get_scraper') as mock_scraper_fn:
                    mock_scraper = MagicMock()
                    mock_scraper.get_cases_count = AsyncMock(return_value=(10, 1))
                    mock_scraper_fn.return_value = mock_scraper
                    
                    response = client.get(
                        "/api/v1/pjud/cases/count",
                        params={"session_id": "test-session-123"}
                    )
                    
                    # Verify session was fetched from Redis
                    mock_store.get_session.assert_called_once_with("test-session-123")
    
    def test_invalid_session_returns_401(self, client):
        """Invalid session should return 401."""
        with patch('app.api.v1.pjud.get_session_store') as mock_store_fn:
            mock_store = MagicMock()
            mock_store.get_session.return_value = None  # Session not found
            mock_store_fn.return_value = mock_store
            
            response = client.get(
                "/api/v1/pjud/cases/count",
                params={"session_id": "invalid-session"}
            )
            
            assert response.status_code == 401
            assert "Session not found" in response.json()["detail"]
    
    def test_logout_deletes_from_redis(self, client):
        """Logout should delete session from Redis."""
        with patch('app.api.v1.pjud.get_session_store') as mock_store_fn:
            mock_store = MagicMock()
            mock_store.delete_session.return_value = True
            mock_store_fn.return_value = mock_store
            
            response = client.delete(
                "/api/v1/pjud/logout",
                params={"session_id": "test-session-123"}
            )
            
            assert response.status_code == 200
            assert response.json()["success"] is True
            mock_store.delete_session.assert_called_once_with("test-session-123")


class TestBrowserIsolation:
    """Test that each request uses a fresh browser."""
    
    @pytest.fixture
    def mock_session(self):
        """Create mock session data."""
        from app.services.session_store import PJUDSession
        return PJUDSession(
            session_id="test-session-123",
            lawyer_id=1,
            rut="12345678-9",
            cookies=[{"name": "PHPSESSID", "value": "abc123"}],
            created_at="2026-06-08T10:00:00",
            expires_at="2026-06-08T12:00:00",
            local_storage="{}",
            auth_method="captcha",
        )
    
    def test_browser_factory_used_as_context_manager(self, client, mock_session):
        """Each request should use BrowserFactory as context manager."""
        with patch('app.api.v1.pjud.get_session_store') as mock_store_fn:
            mock_store = MagicMock()
            mock_store.get_session.return_value = mock_session
            mock_store_fn.return_value = mock_store
            
            browser_factory_calls = []
            
            with patch('app.api.v1.pjud.BrowserFactory') as mock_factory_cls:
                mock_factory = AsyncMock()
                mock_factory.__aenter__ = AsyncMock(return_value=mock_factory)
                mock_factory.__aexit__ = AsyncMock(return_value=None)
                mock_factory.new_page = AsyncMock()
                mock_factory._browser = MagicMock()
                mock_factory._context = MagicMock()
                
                def track_factory_call(*args, **kwargs):
                    browser_factory_calls.append(1)
                    return mock_factory
                
                mock_factory_cls.side_effect = track_factory_call
                
                with patch('app.api.v1.pjud.get_scraper') as mock_scraper_fn:
                    mock_scraper = MagicMock()
                    mock_scraper.get_cases_count = AsyncMock(return_value=(10, 1))
                    mock_scraper_fn.return_value = mock_scraper
                    
                    # First request
                    client.get(
                        "/api/v1/pjud/cases/count",
                        params={"session_id": "test-session-123"}
                    )
                    
                    # Second request
                    client.get(
                        "/api/v1/pjud/cases/count",
                        params={"session_id": "test-session-123"}
                    )
                    
                    # Each request should create its own BrowserFactory
                    assert len(browser_factory_calls) == 2
    
    def test_session_cookies_passed_to_new_page(self, client, mock_session):
        """Session cookies should be passed to new_page()."""
        with patch('app.api.v1.pjud.get_session_store') as mock_store_fn:
            mock_store = MagicMock()
            mock_store.get_session.return_value = mock_session
            mock_store_fn.return_value = mock_store
            
            with patch('app.api.v1.pjud.BrowserFactory') as mock_factory_cls:
                mock_factory = AsyncMock()
                mock_factory.__aenter__ = AsyncMock(return_value=mock_factory)
                mock_factory.__aexit__ = AsyncMock(return_value=None)
                mock_factory.new_page = AsyncMock()
                mock_factory._browser = MagicMock()
                mock_factory._context = MagicMock()
                mock_factory_cls.return_value = mock_factory
                
                with patch('app.api.v1.pjud.get_scraper') as mock_scraper_fn:
                    mock_scraper = MagicMock()
                    mock_scraper.get_cases_count = AsyncMock(return_value=(10, 1))
                    mock_scraper_fn.return_value = mock_scraper
                    
                    client.get(
                        "/api/v1/pjud/cases/count",
                        params={"session_id": "test-session-123"}
                    )
                    
                    # Verify session was passed to new_page
                    mock_factory.new_page.assert_called_once()
                    call_args = mock_factory.new_page.call_args
                    session_arg = call_args[0][0] if call_args[0] else call_args[1].get('session')
                    assert session_arg.session_id == "test-session-123"
