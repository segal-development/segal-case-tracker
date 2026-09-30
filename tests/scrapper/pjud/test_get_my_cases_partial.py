"""get_my_cases must not throw away the causas it already fetched when a later
list page fails for good: it raises PartialListingError carrying them."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.scrapper.pjud.civil import CivilScraper
from app.scrapper.pjud.exceptions import (
    PartialListingError,
    PJUDError,
    ScrapingError,
    SessionNotAuthenticatedError,
)


@pytest.fixture
def scraper():
    s = CivilScraper(headless=True)
    s._page_for = AsyncMock(return_value=MagicMock())
    s._ensure_panel_loaded = AsyncMock()
    s._parse_pagination_info = MagicMock(return_value=(139, 5))
    # Each page "HTML" is just its number; the parser maps it to 2 fake cases.
    s._parse_cases_html = MagicMock(side_effect=lambda html: [f"{html}-a", f"{html}-b"])
    return s


def _session():
    sess = MagicMock()
    sess.rut = "19456852-0"
    return sess


async def _run(scraper):
    with patch(
        "app.scrapper.pjud.resilience.rate_limiter.pjud_action_limiter"
    ) as limiter:
        limiter.return_value.acquire = AsyncMock()
        return await scraper.get_my_cases(_session())


@pytest.mark.asyncio
async def test_failure_on_page_n_raises_partial_with_cases_so_far(scraper):
    scraper._fetch_cases_page_resilient = AsyncMock(
        side_effect=["p1", "p2", "p3", ScrapingError("list page 4: failed after 3 retries")]
    )
    with pytest.raises(PartialListingError) as exc:
        await _run(scraper)
    err = exc.value
    assert err.cases == ["p1-a", "p1-b", "p2-a", "p2-b", "p3-a", "p3-b"]
    assert err.failed_page == 4
    assert err.total_pages == 5
    assert "4" in str(err)


@pytest.mark.asyncio
async def test_failure_on_first_page_propagates_generic_error(scraper):
    scraper._fetch_cases_page_resilient = AsyncMock(
        side_effect=ScrapingError("list page 1: failed after 3 retries")
    )
    with pytest.raises(ScrapingError) as exc:
        await _run(scraper)
    assert not isinstance(exc.value, PartialListingError)


@pytest.mark.asyncio
async def test_existing_except_scraping_error_still_catches_partial(scraper):
    scraper._fetch_cases_page_resilient = AsyncMock(
        side_effect=["p1", ScrapingError("boom")]
    )
    caught = None
    try:
        await _run(scraper)
    except ScrapingError as e:  # what legacy callers do
        caught = e
    assert isinstance(caught, PartialListingError)
    assert isinstance(caught, PJUDError)


@pytest.mark.asyncio
async def test_session_errors_mid_pagination_are_not_converted(scraper):
    """Auth/Shape problems keep their own handling (re-auth / block), not 'partial'."""
    scraper._fetch_cases_page_resilient = AsyncMock(
        side_effect=[
            "p1",
            SessionNotAuthenticatedError(
                url="u", jquery_present=True, looks_like_login=True
            ),
        ]
    )
    with pytest.raises(SessionNotAuthenticatedError):
        await _run(scraper)


@pytest.mark.asyncio
async def test_happy_path_returns_all_cases(scraper):
    scraper._parse_pagination_info = MagicMock(return_value=(6, 3))
    scraper._fetch_cases_page_resilient = AsyncMock(side_effect=["p1", "p2", "p3"])
    cases = await _run(scraper)
    assert cases == ["p1-a", "p1-b", "p2-a", "p2-b", "p3-a", "p3-b"]
