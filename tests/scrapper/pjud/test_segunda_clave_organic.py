"""Tests for organic reCAPTCHA v3 segunda-clave login (NO 2Captcha).

Covers the two new PJUDBaseScraper methods added to make segunda-clave
auto-loginable using a token generated ORGANICALLY in the same real browser
(grecaptcha.execute), replacing the removed 2Captcha integration:

1. ``_generate_recaptcha_token``: in-page JS eval that injects the reCAPTCHA
   script if missing, polls for readiness, and runs grecaptcha.ready +
   grecaptcha.execute — returns the token string or raises on failure.
2. ``login_with_segunda_clave``: orchestrates goto(home) -> generate token ->
   login_with_token, classifying failures (InvalidCredentialsError vs
   ShapeChallengeError vs generic propagation).

All Playwright interactions are mocked — no live PJUD connections.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.scrapper.pjud.civil import CivilScraper
from app.scrapper.pjud.exceptions import (
    InvalidCredentialsError,
    LoginError,
    ShapeChallengeError,
)
from app.services.pjud_session import PJUDSession


def _make_session() -> PJUDSession:
    return PJUDSession.create(
        rut="19643548-4",
        cookies=[{"name": "PHPSESSID", "value": "abc", "domain": ".pjud.cl"}],
        auth_method="captcha",
    )


class TestGenerateRecaptchaToken:
    """_generate_recaptcha_token: in-page JS eval to get an organic reCAPTCHA v3 token."""

    @pytest.mark.asyncio
    async def test_returns_token_when_evaluate_resolves(self):
        """A successful grecaptcha.execute() resolution returns the token string."""
        scraper = CivilScraper(headless=True)
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value="fake-organic-token-abc123")

        token = await scraper._generate_recaptcha_token(
            page, "SITEKEY123", "validate_captcha_seg_clave_hn"
        )

        assert token == "fake-organic-token-abc123"
        page.evaluate.assert_awaited_once()
        # sitekey/action must be forwarded as the eval argument
        _, call_kwargs_or_args = page.evaluate.call_args
        args = page.evaluate.call_args.args
        assert args[1] == {"sitekey": "SITEKEY123", "action": "validate_captcha_seg_clave_hn"}

    @pytest.mark.asyncio
    async def test_raises_when_grecaptcha_never_loads(self):
        """NO_GRECAPTCHA sentinel from the page -> raise a clear LoginError."""
        scraper = CivilScraper(headless=True)
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value="NO_GRECAPTCHA")

        with pytest.raises(LoginError, match="NO_GRECAPTCHA"):
            await scraper._generate_recaptcha_token(page, "SITEKEY123", "action_x")

    @pytest.mark.asyncio
    async def test_raises_when_execute_rejects(self):
        """ERR:<reason> sentinel (execute() promise rejected) -> raise LoginError."""
        scraper = CivilScraper(headless=True)
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value="ERR:timeout-6s")

        with pytest.raises(LoginError, match="ERR:timeout-6s"):
            await scraper._generate_recaptcha_token(page, "SITEKEY123", "action_x")


class TestLoginWithSegundaClave:
    """login_with_segunda_clave: goto(home) -> organic token -> login_with_token."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_session(self):
        """Token generation + login_with_token succeed -> returns the session."""
        scraper = CivilScraper(headless=True)
        page = AsyncMock()
        fake_session = _make_session()

        with (
            patch.object(scraper, "_get_page", AsyncMock(return_value=page)),
            patch.object(
                scraper, "_generate_recaptcha_token", AsyncMock(return_value="organic-token")
            ) as mock_gen,
            patch.object(
                scraper, "login_with_token", AsyncMock(return_value=fake_session)
            ) as mock_login,
        ):
            session = await scraper.login_with_segunda_clave("19643548-4", "mypassword")

        assert session is fake_session
        page.goto.assert_awaited_once()
        mock_gen.assert_awaited_once()
        mock_login.assert_awaited_once_with("19643548-4", "mypassword", "organic-token")

    @pytest.mark.asyncio
    async def test_invalid_credentials_propagates(self):
        """PJUD rejects the RUT/password -> InvalidCredentialsError propagates untouched."""
        scraper = CivilScraper(headless=True)
        page = AsyncMock()

        with (
            patch.object(scraper, "_get_page", AsyncMock(return_value=page)),
            patch.object(
                scraper, "_generate_recaptcha_token", AsyncMock(return_value="organic-token")
            ),
            patch.object(
                scraper,
                "login_with_token",
                AsyncMock(side_effect=InvalidCredentialsError("PJUD rejected credentials")),
            ),
        ):
            with pytest.raises(InvalidCredentialsError):
                await scraper.login_with_segunda_clave("19643548-4", "wrongpass")

    @pytest.mark.asyncio
    async def test_shape_page_raises_shape_challenge_error(self):
        """login_with_token fails generically and the page shows a Shape/TSPD
        challenge -> reclassified as ShapeChallengeError, not a generic LoginError."""
        scraper = CivilScraper(headless=True)
        page = AsyncMock()
        page.url = "https://oficinajudicialvirtual.pjud.cl/home/index.php"

        shape_html = (
            "<html>TSPD_101 failureConfig what code is in the image support id 12345</html>"
        )

        with (
            patch.object(scraper, "_get_page", AsyncMock(return_value=page)),
            patch.object(
                scraper, "_generate_recaptcha_token", AsyncMock(return_value="organic-token")
            ),
            patch.object(
                scraper,
                "login_with_token",
                AsyncMock(side_effect=LoginError("Session not established - still on login page")),
            ),
            patch.object(scraper, "_safe_page_content", AsyncMock(return_value=shape_html)),
        ):
            with pytest.raises(ShapeChallengeError):
                await scraper.login_with_segunda_clave("19643548-4", "mypassword")

    @pytest.mark.asyncio
    async def test_generic_login_error_without_shape_markers_propagates(self):
        """A non-Shape generic LoginError (e.g. a network hiccup) propagates as-is."""
        scraper = CivilScraper(headless=True)
        page = AsyncMock()
        page.url = "https://oficinajudicialvirtual.pjud.cl/home/index.php"

        with (
            patch.object(scraper, "_get_page", AsyncMock(return_value=page)),
            patch.object(
                scraper, "_generate_recaptcha_token", AsyncMock(return_value="organic-token")
            ),
            patch.object(
                scraper,
                "login_with_token",
                AsyncMock(side_effect=LoginError("Error during login: network blip")),
            ),
            patch.object(scraper, "_safe_page_content", AsyncMock(return_value="<html>ok</html>")),
        ):
            with pytest.raises(LoginError) as exc_info:
                await scraper.login_with_segunda_clave("19643548-4", "mypassword")

        assert not isinstance(exc_info.value, ShapeChallengeError)
        assert not isinstance(exc_info.value, InvalidCredentialsError)
