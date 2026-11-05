"""The DNS bypass argument reaches every Chromium we launch — and nothing else does.

Two launch sites exist (``BrowserFactory._start`` and ``PJUDBaseScraper.start``),
each with a persistent-profile branch and an ephemeral one. All four must gain
the ``--host-resolver-rules`` flag when the setting is on, and must be
byte-for-byte what they are today when it is off or when our own resolution
fails.

The argument list is the Shape-sensitive surface of this change, so these tests
assert the COMPLETE list, not just the presence of the new flag.

No browser is launched and no name is resolved for real.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings

HOST = "oficinajudicialvirtual.pjud.cl"
BYPASS_ARG = f"--host-resolver-rules=MAP {HOST} 203.0.113.7"
BASE_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
]
SYSTEM_CHROME_EXTRA_ARGS = [
    "--disable-sync",
    "--no-first-run",
    "--hide-scrollbars",
    "--mute-audio",
]


@pytest.fixture(autouse=True)
def ephemeral_profile(monkeypatch):
    """Default both launch sites to the ephemeral, bundled-Chromium branch."""
    monkeypatch.setattr(settings, "PJUD_CHROME_PATH", "")
    monkeypatch.setattr(settings, "PJUD_USER_DATA_DIR", "")


@pytest.fixture
def resolved(monkeypatch):
    """Our own resolver answers; the setting decides whether it is consulted."""
    monkeypatch.setattr(
        "app.scrapper.pjud.dns_resolver.resolve_via_public_dns",
        lambda host, **kw: ["203.0.113.7"],
    )


@pytest.fixture
def bypass_on(monkeypatch):
    monkeypatch.setattr(settings, "PJUD_BYPASS_SYSTEM_DNS", True)


@pytest.fixture
def bypass_off(monkeypatch):
    monkeypatch.setattr(settings, "PJUD_BYPASS_SYSTEM_DNS", False)


def _playwright_mock(mock_async_playwright):
    playwright = AsyncMock()
    mock_async_playwright.return_value.start = AsyncMock(return_value=playwright)
    playwright.chromium.launch = AsyncMock(return_value=AsyncMock())
    playwright.chromium.launch_persistent_context = AsyncMock(return_value=AsyncMock())
    return playwright


async def _browser_factory_args(persistent: bool, monkeypatch):
    if persistent:
        monkeypatch.setattr(settings, "PJUD_USER_DATA_DIR", "/tmp/does-not-exist-profile")
    with patch("app.scrapper.pjud.browser.async_playwright") as mock_pw:
        playwright = _playwright_mock(mock_pw)
        from app.scrapper.pjud.browser import BrowserFactory

        factory = BrowserFactory()
        await factory._start()
    call = (
        playwright.chromium.launch_persistent_context
        if persistent
        else playwright.chromium.launch
    )
    call.assert_called_once()
    return call.call_args.kwargs["args"]


async def _scraper_args(persistent: bool, monkeypatch):
    if persistent:
        monkeypatch.setattr(settings, "PJUD_USER_DATA_DIR", "/tmp/does-not-exist-profile")
    with patch("app.scrapper.pjud.base.async_playwright") as mock_pw:
        playwright = _playwright_mock(mock_pw)
        from app.scrapper.pjud.civil import CivilScraper

        scraper = CivilScraper(headless=True)
        await scraper.start()
    call = (
        playwright.chromium.launch_persistent_context
        if persistent
        else playwright.chromium.launch
    )
    call.assert_called_once()
    return call.call_args.kwargs["args"]


@pytest.mark.parametrize("persistent", [False, True], ids=["ephemeral", "persistent"])
class TestBrowserFactory:
    async def test_args_unchanged_when_bypass_is_off(
        self, persistent, monkeypatch, bypass_off, resolved
    ):
        assert await _browser_factory_args(persistent, monkeypatch) == BASE_ARGS

    async def test_bypass_argument_appended_when_on(
        self, persistent, monkeypatch, bypass_on, resolved
    ):
        assert await _browser_factory_args(persistent, monkeypatch) == BASE_ARGS + [
            BYPASS_ARG
        ]

    async def test_falls_back_to_todays_args_when_resolution_fails(
        self, persistent, monkeypatch, bypass_on
    ):
        def boom(host, **kwargs):
            raise OSError("no answer")

        monkeypatch.setattr(
            "app.scrapper.pjud.dns_resolver.resolve_via_public_dns", boom
        )
        assert await _browser_factory_args(persistent, monkeypatch) == BASE_ARGS


@pytest.mark.parametrize("persistent", [False, True], ids=["ephemeral", "persistent"])
class TestBaseScraper:
    async def test_args_unchanged_when_bypass_is_off(
        self, persistent, monkeypatch, bypass_off, resolved
    ):
        assert await _scraper_args(persistent, monkeypatch) == BASE_ARGS

    async def test_bypass_argument_appended_when_on(
        self, persistent, monkeypatch, bypass_on, resolved
    ):
        assert await _scraper_args(persistent, monkeypatch) == BASE_ARGS + [BYPASS_ARG]

    async def test_falls_back_to_todays_args_when_resolution_fails(
        self, persistent, monkeypatch, bypass_on
    ):
        def boom(host, **kwargs):
            raise OSError("no answer")

        monkeypatch.setattr(
            "app.scrapper.pjud.dns_resolver.resolve_via_public_dns", boom
        )
        assert await _scraper_args(persistent, monkeypatch) == BASE_ARGS


class TestSystemChromeExtrasSurvive:
    """The stealth extras added for system Chrome must keep their order and company."""

    async def test_system_chrome_args_with_bypass_on(
        self, monkeypatch, bypass_on, resolved
    ):
        monkeypatch.setattr(settings, "PJUD_CHROME_PATH", "/Applications/Chrome")
        assert await _scraper_args(False, monkeypatch) == (
            BASE_ARGS + SYSTEM_CHROME_EXTRA_ARGS + [BYPASS_ARG]
        )

    async def test_system_chrome_args_with_bypass_off(
        self, monkeypatch, bypass_off, resolved
    ):
        monkeypatch.setattr(settings, "PJUD_CHROME_PATH", "/Applications/Chrome")
        assert await _scraper_args(False, monkeypatch) == (
            BASE_ARGS + SYSTEM_CHROME_EXTRA_ARGS
        )
