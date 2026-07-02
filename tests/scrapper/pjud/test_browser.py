"""
Tests for BrowserFactory lifecycle.

Tests:
1. BrowserFactory context manager creates and closes browser
2. new_page() raises error when browser not started
3. new_page() creates page without session
4. new_page() restores cookies from session
5. Multiple new_page() calls close previous context
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass
from typing import List, Dict, Any


@dataclass
class MockSession:
    """Mock session for testing."""
    cookies: List[Dict[str, Any]]
    local_storage: str = "{}"


class TestBrowserFactoryLifecycle:
    """Test BrowserFactory __aenter__ and __aexit__."""
    
    @pytest.mark.asyncio
    async def test_context_manager_starts_browser(self):
        """BrowserFactory should start browser on __aenter__."""
        with patch('app.scrapper.pjud.browser.async_playwright') as mock_pw:
            # Setup mocks
            mock_playwright = AsyncMock()
            mock_browser = AsyncMock()
            mock_pw.return_value.start = AsyncMock(return_value=mock_playwright)
            mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
            
            from app.scrapper.pjud.browser import BrowserFactory
            
            async with BrowserFactory() as factory:
                # Verify browser started
                assert factory._playwright is not None
                assert factory._browser is not None
                mock_playwright.chromium.launch.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_context_manager_closes_browser(self):
        """BrowserFactory should close browser on __aexit__."""
        with patch('app.scrapper.pjud.browser.async_playwright') as mock_pw:
            # Setup mocks
            mock_playwright = AsyncMock()
            mock_browser = AsyncMock()
            mock_pw.return_value.start = AsyncMock(return_value=mock_playwright)
            mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
            
            from app.scrapper.pjud.browser import BrowserFactory
            
            async with BrowserFactory() as factory:
                pass
            
            # Verify cleanup called
            mock_browser.close.assert_called_once()
            mock_playwright.stop.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_cleanup_handles_errors_gracefully(self):
        """BrowserFactory should handle cleanup errors gracefully."""
        with patch('app.scrapper.pjud.browser.async_playwright') as mock_pw:
            # Setup mocks that raise on close
            mock_playwright = AsyncMock()
            mock_browser = AsyncMock()
            mock_browser.close.side_effect = Exception("Close failed")
            mock_pw.return_value.start = AsyncMock(return_value=mock_playwright)
            mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
            
            from app.scrapper.pjud.browser import BrowserFactory
            
            # Should not raise
            async with BrowserFactory() as factory:
                pass
            
            # Verify cleanup attempted despite error
            mock_browser.close.assert_called_once()


class TestBrowserFactoryNewPage:
    """Test BrowserFactory.new_page() method."""
    
    @pytest.mark.asyncio
    async def test_new_page_raises_without_browser(self):
        """new_page() should raise when browser not started."""
        from app.scrapper.pjud.browser import BrowserFactory
        
        factory = BrowserFactory()
        
        with pytest.raises(RuntimeError, match="Browser not started"):
            await factory.new_page()
    
    @pytest.mark.asyncio
    async def test_new_page_creates_context_and_page(self):
        """new_page() should create browser context and page."""
        with patch('app.scrapper.pjud.browser.async_playwright') as mock_pw:
            # Setup mocks
            mock_playwright = AsyncMock()
            mock_browser = AsyncMock()
            mock_context = AsyncMock()
            mock_page = AsyncMock()
            
            mock_pw.return_value.start = AsyncMock(return_value=mock_playwright)
            mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
            mock_browser.new_context = AsyncMock(return_value=mock_context)
            mock_context.new_page = AsyncMock(return_value=mock_page)
            
            from app.scrapper.pjud.browser import BrowserFactory
            
            async with BrowserFactory() as factory:
                page = await factory.new_page()
                
                # Verify context and page created
                mock_browser.new_context.assert_called_once()
                mock_context.new_page.assert_called_once()
                assert page is mock_page
    
    @pytest.mark.asyncio
    async def test_new_page_restores_cookies(self):
        """new_page() should pass cookies via storage_state to new_context (not add_cookies)."""
        with patch('app.scrapper.pjud.browser.async_playwright') as mock_pw:
            # Setup mocks
            mock_playwright = AsyncMock()
            mock_browser = AsyncMock()
            mock_context = AsyncMock()
            mock_page = AsyncMock()

            mock_pw.return_value.start = AsyncMock(return_value=mock_playwright)
            mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
            mock_browser.new_context = AsyncMock(return_value=mock_context)
            mock_context.new_page = AsyncMock(return_value=mock_page)

            from app.scrapper.pjud.browser import BrowserFactory

            # Create mock session with cookies
            session = MockSession(
                cookies=[{"name": "test", "value": "cookie"}],
                local_storage="{}",
            )

            async with BrowserFactory() as factory:
                await factory.new_page(session)

                # Cookies are now passed via storage_state, NOT add_cookies
                call_kwargs = mock_browser.new_context.call_args.kwargs
                storage_state = call_kwargs.get("storage_state") or {}
                assert storage_state.get("cookies") == session.cookies
                mock_context.add_cookies.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_new_page_closes_previous_context(self):
        """Multiple new_page() calls should close previous context."""
        with patch('app.scrapper.pjud.browser.async_playwright') as mock_pw:
            # Setup mocks
            mock_playwright = AsyncMock()
            mock_browser = AsyncMock()
            mock_context1 = AsyncMock()
            mock_context2 = AsyncMock()
            mock_page = AsyncMock()
            
            mock_pw.return_value.start = AsyncMock(return_value=mock_playwright)
            mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
            mock_browser.new_context = AsyncMock(side_effect=[mock_context1, mock_context2])
            mock_context1.new_page = AsyncMock(return_value=mock_page)
            mock_context2.new_page = AsyncMock(return_value=mock_page)
            
            from app.scrapper.pjud.browser import BrowserFactory
            
            async with BrowserFactory() as factory:
                await factory.new_page()
                await factory.new_page()  # Second call
                
                # Verify first context closed
                mock_context1.close.assert_called_once()


class TestBrowserFactoryWithSession:
    """Test BrowserFactory session restoration."""
    
    @pytest.mark.asyncio
    async def test_session_without_cookies(self):
        """new_page() should handle session without cookies."""
        with patch('app.scrapper.pjud.browser.async_playwright') as mock_pw:
            # Setup mocks
            mock_playwright = AsyncMock()
            mock_browser = AsyncMock()
            mock_context = AsyncMock()
            mock_page = AsyncMock()
            
            mock_pw.return_value.start = AsyncMock(return_value=mock_playwright)
            mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
            mock_browser.new_context = AsyncMock(return_value=mock_context)
            mock_context.new_page = AsyncMock(return_value=mock_page)
            
            from app.scrapper.pjud.browser import BrowserFactory
            
            # Create mock session without cookies
            session = MockSession(cookies=[])
            
            async with BrowserFactory() as factory:
                await factory.new_page(session)
                
                # Verify add_cookies not called with empty list
                mock_context.add_cookies.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_none_session(self):
        """new_page() should handle None session."""
        with patch('app.scrapper.pjud.browser.async_playwright') as mock_pw:
            # Setup mocks
            mock_playwright = AsyncMock()
            mock_browser = AsyncMock()
            mock_context = AsyncMock()
            mock_page = AsyncMock()
            
            mock_pw.return_value.start = AsyncMock(return_value=mock_playwright)
            mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
            mock_browser.new_context = AsyncMock(return_value=mock_context)
            mock_context.new_page = AsyncMock(return_value=mock_page)
            
            from app.scrapper.pjud.browser import BrowserFactory
            
            async with BrowserFactory() as factory:
                page = await factory.new_page(None)
                
                # Should still create page
                assert page is mock_page
                mock_context.add_cookies.assert_not_called()

    @pytest.mark.asyncio
    async def test_persistent_context_uses_user_data_dir(self):
        """Persistent mode should launch a context directly and skip browser.new_context."""
        with (
            patch('app.scrapper.pjud.browser.settings.PJUD_USER_DATA_DIR', '/tmp/pjud-profile'),
            patch('app.scrapper.pjud.browser.async_playwright') as mock_pw,
        ):
            mock_playwright = AsyncMock()
            mock_context = AsyncMock()
            mock_page = AsyncMock()

            mock_pw.return_value.start = AsyncMock(return_value=mock_playwright)
            mock_playwright.chromium.launch_persistent_context = AsyncMock(
                return_value=mock_context
            )
            mock_playwright.chromium.launch = AsyncMock()
            mock_context.new_page = AsyncMock(return_value=mock_page)

            from app.scrapper.pjud.browser import BrowserFactory

            async with BrowserFactory() as factory:
                page = await factory.new_page()

                mock_playwright.chromium.launch_persistent_context.assert_called_once()
                mock_playwright.chromium.launch.assert_not_called()
                mock_context.new_page.assert_called_once()
                assert page is mock_page

    @pytest.mark.asyncio
    async def test_persistent_context_restores_session_without_new_context(self):
        """Persistent mode should restore cookies into the existing context."""
        with (
            patch('app.scrapper.pjud.browser.settings.PJUD_USER_DATA_DIR', '/tmp/pjud-profile'),
            patch('app.scrapper.pjud.browser.async_playwright') as mock_pw,
        ):
            mock_playwright = AsyncMock()
            mock_context = AsyncMock()
            mock_page = AsyncMock()

            mock_pw.return_value.start = AsyncMock(return_value=mock_playwright)
            mock_playwright.chromium.launch_persistent_context = AsyncMock(
                return_value=mock_context
            )
            mock_playwright.chromium.launch = AsyncMock()
            mock_context.new_page = AsyncMock(return_value=mock_page)

            from app.scrapper.pjud.browser import BrowserFactory

            session = MockSession(
                cookies=[
                    {
                        "name": "test",
                        "value": "cookie",
                        "domain": ".pjud.cl",
                        "path": "/",
                    }
                ],
                local_storage='{"token": "abc"}',
            )

            async with BrowserFactory() as factory:
                await factory.new_page(session)

                mock_context.add_cookies.assert_called_once_with(session.cookies)
                assert mock_context.add_init_script.call_count >= 2
