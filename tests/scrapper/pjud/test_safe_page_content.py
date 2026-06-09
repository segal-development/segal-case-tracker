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
        mock_page.wait_for_load_state.assert_awaited_once_with(
            "domcontentloaded", timeout=5000
        )

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

    # ------------------------------------------------------------------
    # Finding 1 — recovery failure must not mask the original error
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_reraises_original_error_when_recovery_fails(
        self, scraper, mock_page
    ):
        """If wait_for_load_state raises during recovery the ORIGINAL
        page.content error must propagate — not the recovery error."""
        nav_error = Exception(
            "Page.content: Unable to retrieve content because the page is navigating"
        )
        mock_page.content = AsyncMock(side_effect=[nav_error, nav_error, nav_error])
        mock_page.wait_for_load_state = AsyncMock(
            side_effect=RuntimeError("Target closed")
        )

        with patch("asyncio.sleep"):
            with pytest.raises(Exception, match="page is navigating"):
                await scraper._safe_page_content(mock_page, attempts=3)

    # ------------------------------------------------------------------
    # Finding 2 — non-transient errors must not be retried
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_does_not_retry_non_transient_errors(self, scraper, mock_page):
        """A non-navigation error (e.g. closed context) must propagate
        immediately with no retries — page.content awaited exactly once."""
        non_transient = RuntimeError("Target closed")
        mock_page.content = AsyncMock(side_effect=non_transient)

        with patch("asyncio.sleep"):
            with pytest.raises(RuntimeError, match="Target closed"):
                await scraper._safe_page_content(mock_page)

        assert mock_page.content.call_count == 1

    # ------------------------------------------------------------------
    # Finding 3 — attempts=0 must raise ValueError, not TypeError
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_attempts_zero_raises_value_error(self, scraper, mock_page):
        """attempts=0 must raise ValueError before any page interaction."""
        with pytest.raises(ValueError, match="attempts must be >= 1"):
            await scraper._safe_page_content(mock_page, attempts=0)

    # ------------------------------------------------------------------
    # Finding 4 — no wasted recovery wait after the final attempt
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_no_recovery_wait_on_final_attempt(self, scraper, mock_page):
        """wait_for_load_state must be skipped after the last failed attempt.
        With attempts=2 it should be called at most once (between the two
        attempts), never after the second failure."""
        nav_error = Exception(
            "Page.content: Unable to retrieve content because the page is navigating"
        )
        mock_page.content = AsyncMock(side_effect=[nav_error, nav_error])

        with patch("asyncio.sleep"):
            with pytest.raises(Exception, match="page is navigating"):
                await scraper._safe_page_content(mock_page, attempts=2)

        assert mock_page.wait_for_load_state.call_count <= 1
