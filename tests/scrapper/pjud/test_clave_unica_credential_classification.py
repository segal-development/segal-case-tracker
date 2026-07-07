"""Tests for ClaveUnicaAuth.login credential-failure classification.

TDD: written BEFORE implementation. All tests must fail initially.

When the post-login verification fails, ClaveUnicaAuth.login must distinguish:
1. Invalid credentials (PJUD/Clave Única rejected the password) -> raises
   InvalidCredentialsError (the lawyer must update their clave; retry is
   pointless). Detected via classify_login_failure on the page content.
2. A Shape/TSPD challenge -> raises ShapeChallengeError (a PJUD block, NOT a
   credential problem). Detected via detect_shape_challenge.
3. Neither marker present -> falls back to the existing generic
   ClaveUnicaAuthError (unclassified/transient failure).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_mock_page(*, content: str = "", url: str = "https://oficinajudicialvirtual.pjud.cl/home/index.php"):
    page = AsyncMock()
    page.url = url
    page.content = AsyncMock(return_value=content)
    page.evaluate = AsyncMock(return_value={"misCausas": False, "logout": False, "welcome": False})

    locator = AsyncMock()
    locator.is_visible = AsyncMock(return_value=False)
    locator.wait_for = AsyncMock()
    locator.click = AsyncMock()
    locator.fill = AsyncMock()
    locator.press = AsyncMock()
    locator.text_content = AsyncMock(return_value="")
    locator.first = locator

    page.locator = MagicMock(return_value=locator)
    page.wait_for_load_state = AsyncMock()
    page.wait_for_url = AsyncMock()
    page.goto = AsyncMock()

    return page


@pytest.fixture
def mock_registry():
    with patch("app.scrapper.pjud.clave_unica.SelectorRegistry") as MockRegistry:
        registry = MagicMock()
        registry.load = MagicMock()
        registry.get = MagicMock(side_effect=lambda comp, sel: f"#{sel}")
        MockRegistry.return_value = registry
        yield registry


@pytest.fixture(autouse=True)
def _bypass_login_rate_limiter():
    """Bypass the real PJUD login token bucket (shared module-level singleton,
    burst=3 / ~1 refill per 30s) so these unit tests never block on real wall
    time waiting for a rate-limit token."""
    mock_limiter = MagicMock()
    mock_limiter.acquire = AsyncMock(return_value=True)
    with patch(
        "app.scrapper.pjud.resilience.rate_limiter.pjud_action_limiter",
        return_value=mock_limiter,
    ):
        yield


class TestClaveUnicaCredentialClassification:
    @pytest.mark.asyncio
    async def test_invalid_credentials_page_raises_invalid_credentials_error(
        self, mock_registry
    ):
        from app.scrapper.pjud.clave_unica import ClaveUnicaAuth, ClaveUnicaCredentials
        from app.scrapper.pjud.exceptions import InvalidCredentialsError

        auth = ClaveUnicaAuth()
        auth._registry = mock_registry
        credentials = ClaveUnicaCredentials(rut="12345678-9", password="wrongpass")

        page = _make_mock_page(
            content="<html>El RUT o clave ingresada es incorrecta, intente nuevamente.</html>",
            url="https://oficinajudicialvirtual.pjud.cl/home/index.php",
        )

        with pytest.raises(InvalidCredentialsError):
            await auth.login(page, credentials, lawyer_id=1)

    @pytest.mark.asyncio
    async def test_shape_challenge_page_raises_shape_challenge_error(self, mock_registry):
        from app.scrapper.pjud.clave_unica import ClaveUnicaAuth, ClaveUnicaCredentials
        from app.scrapper.pjud.exceptions import ShapeChallengeError

        auth = ClaveUnicaAuth()
        auth._registry = mock_registry
        credentials = ClaveUnicaCredentials(rut="12345678-9", password="secret123")

        page = _make_mock_page(
            content="<html>failureConfig TSPD_101 what code is in the image support id 12345</html>",
            url="https://oficinajudicialvirtual.pjud.cl/home/index.php",
        )

        with pytest.raises(ShapeChallengeError):
            await auth.login(page, credentials, lawyer_id=1)

    @pytest.mark.asyncio
    async def test_unclassified_failure_raises_generic_error(self, mock_registry):
        from app.scrapper.pjud.clave_unica import ClaveUnicaAuth, ClaveUnicaCredentials, ClaveUnicaAuthError
        from app.scrapper.pjud.exceptions import InvalidCredentialsError, ShapeChallengeError

        auth = ClaveUnicaAuth()
        auth._registry = mock_registry
        credentials = ClaveUnicaCredentials(rut="12345678-9", password="secret123")

        page = _make_mock_page(
            content="<html>Something unrelated went wrong.</html>",
            url="https://oficinajudicialvirtual.pjud.cl/home/index.php",
        )

        with pytest.raises(ClaveUnicaAuthError) as exc_info:
            await auth.login(page, credentials, lawyer_id=1)

        assert not isinstance(exc_info.value, InvalidCredentialsError)
        assert not isinstance(exc_info.value, ShapeChallengeError)
