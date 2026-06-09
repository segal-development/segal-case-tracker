"""
Tests for PJUD session restoration fix.

TDD: tests written BEFORE implementation.

Covers:
1. build_storage_state() pure helper — happy path, empty variants, invalid JSON
2. _ensure_panel_loaded() raises SessionNotAuthenticatedError on auth failure
3. _ensure_panel_loaded() happy path still works
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass
from typing import List, Dict, Any


# ---------------------------------------------------------------------------
# Minimal session stub (matches the shape used in existing tests)
# ---------------------------------------------------------------------------

@dataclass
class MockSession:
    cookies: List[Dict[str, Any]]
    local_storage: str = "{}"


# ===========================================================================
# build_storage_state — pure helper
# ===========================================================================

class TestBuildStorageState:
    """Unit tests for the build_storage_state helper."""

    def test_cookies_and_valid_local_storage(self):
        """Cookies pass through; localStorage items map to name/value pairs."""
        from app.scrapper.pjud.browser import build_storage_state
        from app.scrapper.pjud.base import PJUD_BASE_URL

        cookies = [{"name": "PHPSESSID", "value": "abc123", "domain": "pjud.cl"}]
        local_storage_json = '{"token": "tok_xyz", "user": "lawyer1"}'

        result = build_storage_state(MockSession(cookies=cookies, local_storage=local_storage_json))

        assert result is not None
        assert result["cookies"] == cookies
        assert len(result["origins"]) == 1
        origin = result["origins"][0]
        assert origin["origin"] == PJUD_BASE_URL
        ls_items = {item["name"]: item["value"] for item in origin["localStorage"]}
        assert ls_items == {"token": "tok_xyz", "user": "lawyer1"}

    def test_empty_local_storage_string(self):
        """local_storage='{}' produces an empty localStorage list — no raise."""
        from app.scrapper.pjud.browser import build_storage_state

        result = build_storage_state(MockSession(cookies=[], local_storage="{}"))

        assert result is not None
        origin = result["origins"][0]
        assert origin["localStorage"] == []

    def test_blank_local_storage_string(self):
        """local_storage='' produces an empty localStorage list — no raise."""
        from app.scrapper.pjud.browser import build_storage_state

        result = build_storage_state(MockSession(cookies=[], local_storage=""))

        assert result is not None
        origin = result["origins"][0]
        assert origin["localStorage"] == []

    def test_none_local_storage(self):
        """local_storage=None produces an empty localStorage list — no raise."""
        from app.scrapper.pjud.browser import build_storage_state

        result = build_storage_state(MockSession(cookies=[], local_storage=None))

        assert result is not None
        origin = result["origins"][0]
        assert origin["localStorage"] == []

    def test_invalid_json_local_storage_does_not_raise(self):
        """Invalid JSON in local_storage falls back to empty list — no raise."""
        from app.scrapper.pjud.browser import build_storage_state

        result = build_storage_state(MockSession(cookies=[], local_storage="not-valid-json{{{"))

        assert result is not None
        origin = result["origins"][0]
        assert origin["localStorage"] == []

    def test_none_session_returns_none(self):
        """None session → None (no storage_state)."""
        from app.scrapper.pjud.browser import build_storage_state

        result = build_storage_state(None)

        assert result is None


# ===========================================================================
# _ensure_panel_loaded — auth guard
# ===========================================================================

class TestEnsurePanelLoaded:
    """Tests for the auth-guard logic in _ensure_panel_loaded."""

    def _make_page(self, url: str, evaluate_side_effect=None, evaluate_return=None):
        """Build an AsyncMock page with controlled evaluate/url."""
        page = AsyncMock()
        page.url = url

        if evaluate_side_effect is not None:
            page.evaluate = AsyncMock(side_effect=evaluate_side_effect)
        elif evaluate_return is not None:
            page.evaluate = AsyncMock(side_effect=evaluate_return)
        else:
            page.evaluate = AsyncMock(return_value=False)

        page.goto = AsyncMock()
        page.wait_for_selector = AsyncMock()
        page.wait_for_load_state = AsyncMock()
        return page

    @pytest.mark.asyncio
    async def test_raises_session_not_authenticated_when_miscausas_absent(self):
        """_ensure_panel_loaded raises SessionNotAuthenticatedError when misCausas is not a function."""
        from app.scrapper.pjud.civil import CivilScraper
        from app.scrapper.pjud.exceptions import SessionNotAuthenticatedError

        scraper = CivilScraper(headless=True)

        # Simulate page already on indexN.php so we skip the goto
        login_url = "https://oficinajudicialvirtual.pjud.cl/home/index.php"

        async def evaluate_sequence(expr, *args, **kwargs):
            # misCausas check → False (not a function)
            if "typeof misCausas" in expr:
                return False
            # jQuery check (diagnostic)
            if "typeof window.jQuery" in expr:
                return False
            return False

        page = self._make_page(
            url="https://oficinajudicialvirtual.pjud.cl/indexN.php",
            evaluate_return=evaluate_sequence,
        )

        with pytest.raises(SessionNotAuthenticatedError) as exc_info:
            await scraper._ensure_panel_loaded(page)

        error_msg = str(exc_info.value)
        # Must contain the page URL
        assert "indexN.php" in error_msg or "pjud.cl" in error_msg

    @pytest.mark.asyncio
    async def test_raises_includes_login_url_in_message(self):
        """Exception message contains the current page URL."""
        from app.scrapper.pjud.civil import CivilScraper
        from app.scrapper.pjud.exceptions import SessionNotAuthenticatedError

        scraper = CivilScraper(headless=True)
        page_url = "https://oficinajudicialvirtual.pjud.cl/home/index.php"

        async def evaluate_sequence(expr, *args, **kwargs):
            if "typeof misCausas" in expr:
                return False
            if "typeof window.jQuery" in expr:
                return False
            return False

        page = self._make_page(url=page_url, evaluate_return=evaluate_sequence)

        with pytest.raises(SessionNotAuthenticatedError) as exc_info:
            await scraper._ensure_panel_loaded(page)

        assert page_url in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_no_raise_when_miscausas_exists_and_content_loads(self):
        """Happy path: misCausas present + content loaded → no exception, _panel_loaded=True."""
        from app.scrapper.pjud.civil import CivilScraper

        scraper = CivilScraper(headless=True)

        call_count = 0

        async def evaluate_sequence(expr, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if "typeof misCausas" in expr:
                return True
            # content check (innerHTML.length > 1000)
            if "#contMain" in expr or "contMain" in expr:
                return True
            return True

        page = AsyncMock()
        page.url = "https://oficinajudicialvirtual.pjud.cl/indexN.php"
        page.evaluate = AsyncMock(side_effect=evaluate_sequence)
        page.goto = AsyncMock()
        page.wait_for_selector = AsyncMock()
        page.wait_for_load_state = AsyncMock()

        # Should not raise
        await scraper._ensure_panel_loaded(page)

        assert scraper._panel_loaded is True
