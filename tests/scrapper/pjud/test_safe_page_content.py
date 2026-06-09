"""
Tests for PJUDBaseScraper._safe_page_content() retry helper.

Tests:
1. Returns HTML immediately when page.content() succeeds on first call.
2. Retries after a navigation-in-progress error and returns HTML on the second
   call; assert wait_for_load_state is awaited between attempts.
3. Re-raises the last exception when page.content() fails on all attempts.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, patch


class TestSafePageContent:
    """Test PJUDBaseScraper._safe_page_content() retry helper."""

    @pytest.fixture
    def scraper(self):
        """Create a concrete CivilScraper instance without starting a browser."""
        from app.scrapper.pjud import CivilScraper
        return CivilScraper()

    @pytest.fixture
    def mock_page(self):
        """Return an AsyncMock Playwright page."""
        return AsyncMock()

    @pytest.mark.asyncio
    async def test_returns_html_on_first_success(self, scraper, mock_page):
        """_safe_page_content returns HTML immediately when page.content() succeeds."""
        mock_page.content.return_value = "<html>success</html>"

        result = await scraper._safe_page_content(mock_page)

        assert result == "<html>success</html>"
        mock_page.content.assert_called_once()

    @pytest.mark.asyncio
    async def test_retries_after_navigation_error_and_succeeds(self, scraper, mock_page):
        """_safe_page_content retries after a 'page is navigating' error and returns HTML."""
        nav_error = Exception(
            "Page.content: Unable to retrieve content because the page is navigating"
        )
        mock_page.content = AsyncMock(side_effect=[nav_error, "<html>retried</html>"])

        with patch("asyncio.sleep"):
            result = await scraper._safe_page_content(mock_page)

        assert result == "<html>retried</html>"
        assert mock_page.content.call_count == 2
        # wait_for_load_state must be awaited exactly once between the two attempts
        mock_page.wait_for_load_state.assert_awaited_once_with("domcontentloaded")

    @pytest.mark.asyncio
    async def test_reraises_last_exception_when_all_attempts_fail(self, scraper, mock_page):
        """_safe_page_content re-raises the last exception once all attempts are exhausted."""
        nav_error = Exception(
            "Page.content: Unable to retrieve content because the page is navigating"
        )
        mock_page.content = AsyncMock(side_effect=[nav_error, nav_error, nav_error])

        with patch("asyncio.sleep"):
            with pytest.raises(Exception, match="page is navigating"):
                await scraper._safe_page_content(mock_page, attempts=3)

        assert mock_page.content.call_count == 3
