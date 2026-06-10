"""
Integration tests for stateless PJUD endpoints.

S1-T8 updates:
- PJUDSession imported from pjud_session (canonical)
- store.aget_session_by_id replaces store.get_session
- store.adelete_session replaces store.delete_session (async)
- /pjud/login uses uuid4 session_id (from PJUDSession.create)
- store.asave_session is awaited
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient


class TestStatelessEndpoints:
    """Test that endpoints retrieve session from Redis and don't share state."""

    @pytest.fixture
    def mock_session(self):
        """Create canonical PJUDSession for testing."""
        from app.services.pjud_session import PJUDSession
        return PJUDSession.create(
            rut="12345678-9",
            cookies=[{"name": "test", "value": "cookie"}],
            lawyer_id=1,
            auth_method="captcha",
        )

    def test_session_retrieved_from_redis(self, client, mock_session):
        """Sessions should be retrieved from Redis, not in-memory."""
        with patch("app.api.v1.pjud.get_session_store") as mock_store_fn:
            mock_store = MagicMock()
            mock_store.aget_session_by_id = AsyncMock(return_value=mock_session)
            mock_store_fn.return_value = mock_store

            with patch("app.api.v1.pjud.BrowserFactory") as mock_factory_cls:
                mock_factory = AsyncMock()
                mock_factory.__aenter__ = AsyncMock(return_value=mock_factory)
                mock_factory.__aexit__ = AsyncMock(return_value=None)
                mock_factory.new_page = AsyncMock()
                mock_factory._browser = MagicMock()
                mock_factory._context = MagicMock()
                mock_factory_cls.return_value = mock_factory

                with patch("app.api.v1.pjud.get_scraper") as mock_scraper_fn:
                    mock_scraper = MagicMock()
                    mock_scraper.get_cases_count = AsyncMock(return_value=(10, 1))
                    mock_scraper_fn.return_value = mock_scraper

                    response = client.get(
                        "/api/v1/pjud/cases/count",
                        params={"session_id": mock_session.session_id}
                    )

                    mock_store.aget_session_by_id.assert_called_once_with(mock_session.session_id)

    def test_invalid_session_returns_401(self, client):
        """Invalid session should return 401."""
        with patch("app.api.v1.pjud.get_session_store") as mock_store_fn:
            mock_store = MagicMock()
            mock_store.aget_session_by_id = AsyncMock(return_value=None)
            mock_store_fn.return_value = mock_store

            response = client.get(
                "/api/v1/pjud/cases/count",
                params={"session_id": "invalid-session"}
            )

            assert response.status_code == 401
            assert "Session not found" in response.json()["detail"]

    def test_logout_deletes_from_redis(self, client):
        """Logout should delete session from Redis (async)."""
        with patch("app.api.v1.pjud.get_session_store") as mock_store_fn:
            mock_store = MagicMock()
            mock_store.adelete_session = AsyncMock(return_value=True)
            mock_store_fn.return_value = mock_store

            response = client.delete(
                "/api/v1/pjud/logout",
                params={"session_id": "test-session-123"}
            )

            assert response.status_code == 200
            assert response.json()["success"] is True
            mock_store.adelete_session.assert_called_once_with("test-session-123")


class TestBrowserIsolation:
    """Test that each request uses a fresh browser."""

    @pytest.fixture
    def mock_session(self):
        from app.services.pjud_session import PJUDSession
        return PJUDSession.create(
            rut="12345678-9",
            cookies=[{"name": "PHPSESSID", "value": "abc123", "domain": ".pjud.cl"}],
            lawyer_id=1,
            auth_method="captcha",
        )

    def test_browser_factory_used_as_context_manager(self, client, mock_session):
        """Each request should use BrowserFactory as context manager."""
        with patch("app.api.v1.pjud.get_session_store") as mock_store_fn:
            mock_store = MagicMock()
            mock_store.aget_session_by_id = AsyncMock(return_value=mock_session)
            mock_store_fn.return_value = mock_store

            browser_factory_calls = []

            with patch("app.api.v1.pjud.BrowserFactory") as mock_factory_cls:
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

                with patch("app.api.v1.pjud.get_scraper") as mock_scraper_fn:
                    mock_scraper = MagicMock()
                    mock_scraper.get_cases_count = AsyncMock(return_value=(10, 1))
                    mock_scraper_fn.return_value = mock_scraper

                    client.get("/api/v1/pjud/cases/count",
                               params={"session_id": mock_session.session_id})
                    client.get("/api/v1/pjud/cases/count",
                               params={"session_id": mock_session.session_id})

                    assert len(browser_factory_calls) == 2

    def test_session_cookies_passed_to_new_page(self, client, mock_session):
        """Session cookies should be passed to new_page()."""
        with patch("app.api.v1.pjud.get_session_store") as mock_store_fn:
            mock_store = MagicMock()
            mock_store.aget_session_by_id = AsyncMock(return_value=mock_session)
            mock_store_fn.return_value = mock_store

            with patch("app.api.v1.pjud.BrowserFactory") as mock_factory_cls:
                mock_factory = AsyncMock()
                mock_factory.__aenter__ = AsyncMock(return_value=mock_factory)
                mock_factory.__aexit__ = AsyncMock(return_value=None)
                mock_factory.new_page = AsyncMock()
                mock_factory._browser = MagicMock()
                mock_factory._context = MagicMock()
                mock_factory_cls.return_value = mock_factory

                with patch("app.api.v1.pjud.get_scraper") as mock_scraper_fn:
                    mock_scraper = MagicMock()
                    mock_scraper.get_cases_count = AsyncMock(return_value=(10, 1))
                    mock_scraper_fn.return_value = mock_scraper

                    client.get("/api/v1/pjud/cases/count",
                               params={"session_id": mock_session.session_id})

                    mock_factory.new_page.assert_called_once()
                    call_args = mock_factory.new_page.call_args
                    session_arg = (call_args[0][0] if call_args[0]
                                   else call_args[1].get("session"))
                    assert session_arg.session_id == mock_session.session_id


class TestPjudLoginEndpoint:
    """S1-T8: /pjud/login uses PJUDSession.create and awaits asave_session."""

    def test_login_uses_uuid4_session_id(self, client):
        """Login endpoint must produce a uuid4 session_id (not timestamp-based)."""
        from app.services.pjud_session import PJUDSession
        fake_session = PJUDSession.create(
            rut="12345678-9",
            cookies=[],
            auth_method="captcha",
        )

        with patch("app.api.v1.pjud.get_scraper") as mock_scraper_fn, \
             patch("app.api.v1.pjud.get_session_store") as mock_store_fn:

            mock_scraper = MagicMock()
            mock_scraper.login_with_token = AsyncMock(return_value=fake_session)
            mock_scraper.close = AsyncMock()
            mock_scraper_fn.return_value = mock_scraper

            mock_store = MagicMock()
            mock_store.asave_session = AsyncMock(return_value=True)
            mock_store_fn.return_value = mock_store

            response = client.post(
                "/api/v1/pjud/login",
                json={
                    "rut": "12345678-9",
                    "password": "pass",
                    "captcha_token": "tok",
                },
            )

            assert response.status_code == 200
            data = response.json()
            # session_id must be a valid uuid4 (36-char hyphenated format)
            import re
            assert re.match(
                r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                data["session_id"],
            )

    def test_login_awaits_asave_session(self, client):
        """login must await store.asave_session (async store)."""
        from app.services.pjud_session import PJUDSession
        fake_session = PJUDSession.create(rut="12345678-9", cookies=[], auth_method="captcha")

        with patch("app.api.v1.pjud.get_scraper") as mock_scraper_fn, \
             patch("app.api.v1.pjud.get_session_store") as mock_store_fn:

            mock_scraper = MagicMock()
            mock_scraper.login_with_token = AsyncMock(return_value=fake_session)
            mock_scraper.close = AsyncMock()
            mock_scraper_fn.return_value = mock_scraper

            mock_store = MagicMock()
            mock_store.asave_session = AsyncMock(return_value=True)
            mock_store_fn.return_value = mock_store

            client.post(
                "/api/v1/pjud/login",
                json={"rut": "12345678-9", "password": "pass", "captcha_token": "tok"},
            )

            mock_store.asave_session.assert_awaited()
