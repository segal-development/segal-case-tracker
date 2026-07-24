"""Tests for _fetch_cases_page_resilient — the list-page fetch that must survive
a mid-fetch navigation destroying the JS execution context.

Regression guard for the production incident where a ~2000-causa lawyer aborted
its entire sync ("Failed to sync civil for lawyer 7: ... Execution context was
destroyed") because a single transient navigation on page 4 propagated out of
get_all_cases. The wrapper must retry the page (reloading the panel), not discard
the whole lawyer, yet must NOT retry unrelated errors.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.scrapper.pjud.civil import CivilScraper
from app.scrapper.pjud.exceptions import ScrapingError

CTX_DESTROYED = Exception(
    "Page.evaluate: Execution context was destroyed, most likely because of a navigation."
)
ARGS = ("19456852", "0", "M", "", "", "", 4)  # rut_num, dv, tipo, year, desde, hasta, page


@pytest.fixture
def scraper():
    s = CivilScraper(headless=True)
    s._ensure_panel_loaded = AsyncMock()  # no real browser in this unit test
    s._panel_loaded = True
    return s


@pytest.mark.asyncio
async def test_retries_after_context_destroyed_then_succeeds(scraper):
    page = MagicMock()
    scraper._fetch_cases_page = AsyncMock(side_effect=[CTX_DESTROYED, "<html>page4</html>"])
    result = await scraper._fetch_cases_page_resilient(page, *ARGS)
    assert result == "<html>page4</html>"
    assert scraper._fetch_cases_page.await_count == 2
    # Panel was invalidated and reloaded between the failed and successful fetch.
    assert scraper._panel_loaded is False
    scraper._ensure_panel_loaded.assert_awaited()


@pytest.mark.asyncio
async def test_raises_scraping_error_after_exhausting_retries(scraper):
    page = MagicMock()
    scraper._fetch_cases_page = AsyncMock(side_effect=CTX_DESTROYED)
    with pytest.raises(ScrapingError):
        await scraper._fetch_cases_page_resilient(page, *ARGS)
    assert scraper._fetch_cases_page.await_count == 3  # bounded, not infinite


@pytest.mark.asyncio
async def test_unrelated_error_propagates_without_retry(scraper):
    page = MagicMock()
    scraper._fetch_cases_page = AsyncMock(side_effect=RuntimeError("AJAX 500"))
    with pytest.raises(RuntimeError):
        await scraper._fetch_cases_page_resilient(page, *ARGS)
    # A non-navigation error must not be retried (no wasted panel reloads).
    assert scraper._fetch_cases_page.await_count == 1
    scraper._ensure_panel_loaded.assert_not_awaited()


# --- PJUD "ERROR:" body: a transient AJAX failure, retried like a destroyed
#     context (regression guard for 2026-07-23, lawyers 14 & 16 lost at
#     "AJAX error: ERROR:0" mid-pagination). ---


@pytest.mark.asyncio
async def test_retries_after_ajax_error_body_then_succeeds(scraper):
    page = MagicMock()
    # First fetch returns PJUD's transient error payload, second returns real HTML.
    scraper._fetch_cases_page = AsyncMock(side_effect=["ERROR:0", "<html>page4</html>"])
    result = await scraper._fetch_cases_page_resilient(page, *ARGS)
    assert result == "<html>page4</html>"
    assert scraper._fetch_cases_page.await_count == 2
    # Panel was invalidated + reloaded before the retry (same recovery path).
    assert scraper._panel_loaded is False
    scraper._ensure_panel_loaded.assert_awaited()


@pytest.mark.asyncio
async def test_raises_scraping_error_when_ajax_error_persists(scraper):
    page = MagicMock()
    scraper._fetch_cases_page = AsyncMock(side_effect=["ERROR:0", "ERROR:0", "ERROR:0"])
    with pytest.raises(ScrapingError):
        await scraper._fetch_cases_page_resilient(page, *ARGS)
    assert scraper._fetch_cases_page.await_count == 3  # bounded, never returns the ERROR body


@pytest.mark.asyncio
async def test_good_body_returns_immediately_without_reload(scraper):
    page = MagicMock()
    scraper._fetch_cases_page = AsyncMock(return_value="<html>ok</html>")
    result = await scraper._fetch_cases_page_resilient(page, *ARGS)
    assert result == "<html>ok</html>"
    assert scraper._fetch_cases_page.await_count == 1
    # A healthy first fetch must not touch the panel.
    scraper._ensure_panel_loaded.assert_not_awaited()
    assert scraper._panel_loaded is True
