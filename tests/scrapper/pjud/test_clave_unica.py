"""
Tests for ClaveUnicaAuth authentication module.

Tests:
1. ClaveUnicaCredentials validation
2. ClaveUnicaAuth.login() form filling
3. ClaveUnicaAuth.login() selector usage
4. Error handling for failed login
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime


class TestClaveUnicaCredentials:
    """Test ClaveUnicaCredentials dataclass."""
    
    def test_valid_credentials(self):
        """Valid credentials should pass validation."""
        from app.scrapper.pjud.clave_unica import ClaveUnicaCredentials
        
        creds = ClaveUnicaCredentials(
            rut="12345678-9",
            password="secret123",
        )
        
        assert creds.validate() is True
    
    def test_empty_rut_fails(self):
        """Empty RUT should fail validation."""
        from app.scrapper.pjud.clave_unica import ClaveUnicaCredentials
        
        creds = ClaveUnicaCredentials(
            rut="",
            password="secret123",
        )
        
        assert creds.validate() is False
    
    def test_empty_password_fails(self):
        """Empty password should fail validation."""
        from app.scrapper.pjud.clave_unica import ClaveUnicaCredentials
        
        creds = ClaveUnicaCredentials(
            rut="12345678-9",
            password="",
        )
        
        assert creds.validate() is False


class TestClaveUnicaAuthFormFilling:
    """Test ClaveUnicaAuth form filling with mocked page."""
    
    @pytest.fixture
    def mock_page(self):
        """Create a mock Playwright page."""
        page = AsyncMock()
        page.context = AsyncMock()
        page.context.cookies = AsyncMock(return_value=[
            {"name": "PHPSESSID", "value": "abc123", "domain": ".pjud.cl"},
            {"name": "other", "value": "value", "domain": ".pjud.cl"},
        ])
        page.evaluate = AsyncMock(return_value="{}")
        
        # Mock locators
        locator = AsyncMock()
        locator.is_visible = AsyncMock(return_value=True)
        locator.wait_for = AsyncMock()
        locator.click = AsyncMock()
        locator.fill = AsyncMock()
        locator.text_content = AsyncMock(return_value="")
        
        page.locator = MagicMock(return_value=locator)
        page.wait_for_load_state = AsyncMock()
        page.wait_for_url = AsyncMock()
        page.goto = AsyncMock()
        
        return page
    
    @pytest.fixture
    def mock_registry(self):
        """Create a mock selector registry."""
        with patch('app.scrapper.pjud.clave_unica.SelectorRegistry') as MockRegistry:
            registry = MagicMock()
            registry.load = MagicMock()
            registry.get = MagicMock(side_effect=lambda comp, sel: f"#{sel}")
            MockRegistry.return_value = registry
            yield registry
    
    @pytest.mark.asyncio
    async def test_login_fills_rut_and_password(self, mock_page, mock_registry):
        """Login should fill RUT and password fields."""
        from app.scrapper.pjud.clave_unica import ClaveUnicaAuth, ClaveUnicaCredentials
        
        auth = ClaveUnicaAuth()
        auth._registry = mock_registry
        
        credentials = ClaveUnicaCredentials(
            rut="12345678-9",
            password="secret123",
        )
        
        await auth.login(mock_page, credentials, lawyer_id=1)
        
        # Verify form filling was called
        locator = mock_page.locator.return_value
        
        # Check that fill was called for both fields
        fill_calls = locator.fill.call_args_list
        assert len(fill_calls) >= 2
        
        # Extract the values that were filled
        filled_values = [call.args[0] for call in fill_calls]
        assert "12345678-9" in filled_values
        assert "secret123" in filled_values
    
    @pytest.mark.asyncio
    async def test_login_uses_correct_selectors(self, mock_page, mock_registry):
        """Login should use selectors from registry."""
        from app.scrapper.pjud.clave_unica import ClaveUnicaAuth, ClaveUnicaCredentials
        
        auth = ClaveUnicaAuth()
        auth._registry = mock_registry
        
        credentials = ClaveUnicaCredentials(
            rut="12345678-9",
            password="secret123",
        )
        
        await auth.login(mock_page, credentials, lawyer_id=1)
        
        # Verify registry was queried for selectors
        get_calls = mock_registry.get.call_args_list
        selector_names = [call.args[1] for call in get_calls]
        
        assert "rut_input" in selector_names
        assert "password_input" in selector_names
        assert "submit_button" in selector_names
    
    @pytest.mark.asyncio
    async def test_login_returns_session(self, mock_page, mock_registry):
        """Successful login should return PJUDSession."""
        from app.scrapper.pjud.clave_unica import ClaveUnicaAuth, ClaveUnicaCredentials
        from app.services.pjud_session import PJUDSession
        
        auth = ClaveUnicaAuth()
        auth._registry = mock_registry
        
        credentials = ClaveUnicaCredentials(
            rut="12345678-9",
            password="secret123",
        )
        
        session = await auth.login(mock_page, credentials, lawyer_id=1)
        
        assert isinstance(session, PJUDSession)
        assert session.rut == "12345678-9"
        assert session.lawyer_id == 1
        assert session.auth_method == "clave_unica"
        assert len(session.cookies) == 2  # From mock


class TestClaveUnicaAuthErrors:
    """Test ClaveUnicaAuth error handling."""
    
    @pytest.mark.asyncio
    async def test_invalid_credentials_raises_error(self):
        """Login with invalid credentials should raise error."""
        from app.scrapper.pjud.clave_unica import (
            ClaveUnicaAuth,
            ClaveUnicaCredentials,
            ClaveUnicaAuthError,
        )
        
        auth = ClaveUnicaAuth()
        
        credentials = ClaveUnicaCredentials(
            rut="",
            password="secret123",
        )
        
        mock_page = AsyncMock()
        
        with pytest.raises(ClaveUnicaAuthError, match="Invalid credentials"):
            await auth.login(mock_page, credentials, lawyer_id=1)
    
    @pytest.mark.asyncio
    async def test_timeout_raises_error(self):
        """Timeout during login should raise error."""
        from app.scrapper.pjud.clave_unica import (
            ClaveUnicaAuth,
            ClaveUnicaCredentials,
            ClaveUnicaAuthError,
        )
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        
        auth = ClaveUnicaAuth()
        
        credentials = ClaveUnicaCredentials(
            rut="12345678-9",
            password="secret123",
        )
        
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock(side_effect=PlaywrightTimeout("Timeout"))
        
        with patch('app.scrapper.pjud.clave_unica.SelectorRegistry') as MockRegistry:
            registry = MagicMock()
            registry.load = MagicMock()
            MockRegistry.return_value = registry
            auth._registry = registry
            
            with pytest.raises(ClaveUnicaAuthError, match="timeout"):
                await auth.login(mock_page, credentials, lawyer_id=1)


class TestClaveUnicaAuthSessionExtraction:
    """Test session extraction after login."""
    
    @pytest.mark.asyncio
    async def test_extracts_pjud_cookies_only(self):
        """Should only extract cookies from PJUD domain."""
        from app.scrapper.pjud.clave_unica import ClaveUnicaAuth, ClaveUnicaCredentials
        
        mock_page = AsyncMock()
        mock_context = AsyncMock()
        mock_page.context = mock_context
        
        # Mix of PJUD and non-PJUD cookies
        mock_context.cookies = AsyncMock(return_value=[
            {"name": "PHPSESSID", "value": "abc", "domain": ".pjud.cl"},
            {"name": "session", "value": "xyz", "domain": "oficinajudicialvirtual.pjud.cl"},
            {"name": "external", "value": "123", "domain": ".google.com"},
        ])
        mock_page.evaluate = AsyncMock(return_value="{}")
        
        # Mock all the other page interactions
        locator = AsyncMock()
        locator.is_visible = AsyncMock(return_value=True)
        locator.wait_for = AsyncMock()
        locator.click = AsyncMock()
        locator.fill = AsyncMock()
        mock_page.locator = MagicMock(return_value=locator)
        mock_page.wait_for_load_state = AsyncMock()
        mock_page.wait_for_url = AsyncMock()
        mock_page.goto = AsyncMock()
        
        with patch('app.scrapper.pjud.clave_unica.SelectorRegistry') as MockRegistry:
            registry = MagicMock()
            registry.load = MagicMock()
            registry.get = MagicMock(return_value="#selector")
            MockRegistry.return_value = registry
            
            auth = ClaveUnicaAuth()
            auth._registry = registry
            
            credentials = ClaveUnicaCredentials(rut="12345678-9", password="secret")
            session = await auth.login(mock_page, credentials, lawyer_id=1)
            
            # Should only have PJUD cookies
            assert len(session.cookies) == 2
            for cookie in session.cookies:
                assert "pjud.cl" in cookie["domain"]
