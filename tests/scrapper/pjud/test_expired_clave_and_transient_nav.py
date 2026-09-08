"""Tests for two scraper misclassifications observed in production (2026-09-08).

Defect A — an expired PJUD clave (login POST redirected to
``home/includes/expiraPass.php``) was logged as "Login successful" and an
unusable session was saved; every later navigation then failed with
``misCausas not found`` and the credential vault never learned the clave was dead.

Defect B — a page that simply did not load (DNS, connection, timeout) was
raised as ``SessionNotAuthenticatedError`` even though the detector itself
computed ``looks_like_login=False``. The scheduler then re-authenticated,
retried the same case into the same blip and aborted the whole batch.

All tests mock Playwright — no live connections.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ===========================================================================
# Exception hierarchy
# ===========================================================================

class TestExceptionHierarchy:
    def test_credential_expired_is_invalid_credentials_and_login_error(self):
        from app.scrapper.pjud.exceptions import (
            CredentialExpiredError,
            InvalidCredentialsError,
            LoginError,
        )

        exc = CredentialExpiredError("clave expirada")
        assert isinstance(exc, InvalidCredentialsError)
        assert isinstance(exc, LoginError)

    def test_transient_navigation_is_pjud_error_but_not_session_error(self):
        from app.scrapper.pjud.exceptions import (
            PJUDError,
            SessionExpiredError,
            SessionNotAuthenticatedError,
            TransientNavigationError,
        )

        exc = TransientNavigationError(
            url="https://oficinajudicialvirtual.pjud.cl/indexN.php",
            reason="net::ERR_NAME_NOT_RESOLVED",
        )
        assert isinstance(exc, PJUDError)
        assert not isinstance(exc, SessionNotAuthenticatedError)
        assert not isinstance(exc, SessionExpiredError)
        assert exc.url == "https://oficinajudicialvirtual.pjud.cl/indexN.php"
        assert exc.reason == "net::ERR_NAME_NOT_RESOLVED"
        # The message is what ends up in sync_history.error_message — it must
        # name the real cause, not authentication.
        assert "no disponible" in str(exc)
        assert "net::ERR_NAME_NOT_RESOLVED" in str(exc)
        assert "authenticated" not in str(exc).lower()


# ===========================================================================
# classify_post_login — pure helper
# ===========================================================================

class TestClassifyPostLogin:
    def test_expira_pass_url_is_expired(self):
        from app.scrapper.pjud.base import classify_post_login

        url = "https://oficinajudicialvirtual.pjud.cl/home/includes/expiraPass.php?usu=eyJhbGci.abc.def"
        assert classify_post_login(url, "<html></html>") == "expired"

    def test_expira_pass_url_is_case_insensitive(self):
        from app.scrapper.pjud.base import classify_post_login

        url = "https://oficinajudicialvirtual.pjud.cl/home/includes/EXPIRAPASS.PHP?usu=x"
        assert classify_post_login(url, "") == "expired"

    @pytest.mark.parametrize(
        "content",
        [
            "<p>Su clave ha expirado, debe renovarla</p>",
            "<p>SU CLAVE HA EXPIRADO</p>",
            "<p>Su contraseña ha expirado</p>",
            "<p>Su contrasena ha expirado</p>",
            "<p>Usted debe cambiar su clave para continuar</p>",
            "<p>Debe Cambiar Su Clave</p>",
        ],
    )
    def test_expiry_markers_in_content_are_expired(self, content):
        from app.scrapper.pjud.base import classify_post_login

        url = "https://oficinajudicialvirtual.pjud.cl/indexN.php"
        assert classify_post_login(url, content) == "expired"

    def test_script_only_reference_to_expira_pass_is_not_expired(self):
        """A JS reference to expiraPass.php on the login page is not a verdict —
        a rejected token must stay retry-able (login_page), not become 'expired'."""
        from app.scrapper.pjud.base import classify_post_login

        url = "https://oficinajudicialvirtual.pjud.cl/home/index.php"
        content = (
            "<script>if (r.expired) location.href='includes/expiraPass.php';</script>"
            "<div>Error de validación. Intente nuevamente.</div>"
        )
        assert classify_post_login(url, content) == "login_page"

    def test_home_index_is_login_page(self):
        from app.scrapper.pjud.base import classify_post_login

        url = "https://oficinajudicialvirtual.pjud.cl/home/index.php"
        assert classify_post_login(url, "<html>Ingrese su RUT y clave</html>") == "login_page"

    def test_index_n_normal_content_is_ok(self):
        from app.scrapper.pjud.base import classify_post_login

        url = "https://oficinajudicialvirtual.pjud.cl/indexN.php"
        content = "<html><body><a onclick='misCausas()'>Mis Causas</a> Cerrar sesion</body></html>"
        assert classify_post_login(url, content) == "ok"

    def test_none_content_is_safe(self):
        from app.scrapper.pjud.base import classify_post_login

        assert classify_post_login("https://oficinajudicialvirtual.pjud.cl/indexN.php", None) == "ok"


# ===========================================================================
# classify_missing_miscausas — pure helper
# ===========================================================================

class TestClassifyMissingMisCausas:
    INDEX = "https://oficinajudicialvirtual.pjud.cl/indexN.php"

    def test_login_url_is_auth(self):
        from app.scrapper.pjud.base import classify_missing_miscausas

        url = "https://oficinajudicialvirtual.pjud.cl/home/index.php"
        assert classify_missing_miscausas(url, 50_000, "") == "auth"

    def test_login_n_url_is_auth(self):
        from app.scrapper.pjud.base import classify_missing_miscausas

        url = "https://oficinajudicialvirtual.pjud.cl/loginN.php"
        assert classify_missing_miscausas(url, 50_000, "") == "auth"

    def test_login_url_wins_even_with_nav_error(self):
        from app.scrapper.pjud.base import classify_missing_miscausas

        url = "https://oficinajudicialvirtual.pjud.cl/home/index.php"
        assert classify_missing_miscausas(url, 10, "Timeout 20000ms exceeded") == "auth"

    def test_not_login_short_content_is_transient(self):
        from app.scrapper.pjud.base import classify_missing_miscausas

        assert classify_missing_miscausas(self.INDEX, 0, "") == "transient"
        assert classify_missing_miscausas(self.INDEX, 1999, "") == "transient"

    @pytest.mark.parametrize(
        "nav_error",
        [
            "Page.goto: net::ERR_NAME_NOT_RESOLVED at https://oficinajudicialvirtual.pjud.cl/indexN.php",
            "Page.goto: Timeout 20000ms exceeded.",
            "Target page, context or browser has been closed",
            "Execution context was destroyed, most likely because of a navigation",
        ],
    )
    def test_not_login_nav_error_is_transient(self, nav_error):
        from app.scrapper.pjud.base import classify_missing_miscausas

        # Even with a long body (stale content from a previous page) a navigation
        # error on the last attempt means the page did not load.
        assert classify_missing_miscausas(self.INDEX, 50_000, nav_error) == "transient"

    def test_not_login_long_content_no_error_is_auth(self):
        from app.scrapper.pjud.base import classify_missing_miscausas

        assert classify_missing_miscausas(self.INDEX, 2000, "") == "auth"
        assert classify_missing_miscausas(self.INDEX, 50_000, "") == "auth"


# ===========================================================================
# _ensure_panel_loaded — retry + classification branch
# ===========================================================================

INDEX_URL = "https://oficinajudicialvirtual.pjud.cl/indexN.php"
LOGIN_URL = "https://oficinajudicialvirtual.pjud.cl/home/index.php"


def _make_page(url: str, miscausas_answers, content: str = "") -> AsyncMock:
    """AsyncMock page whose ``typeof misCausas`` probe answers from *miscausas_answers*
    (one entry per probe; the last entry repeats)."""
    answers = list(miscausas_answers)

    async def evaluate(expr, *args, **kwargs):
        if "typeof misCausas" in expr:
            return answers.pop(0) if len(answers) > 1 else answers[0]
        if "typeof window.jQuery" in expr:
            return False
        if "contMain" in expr:
            return True
        return None

    page = AsyncMock()
    page.url = url
    page.evaluate = AsyncMock(side_effect=evaluate)
    page.content = AsyncMock(return_value=content)
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    return page


class TestEnsurePanelLoadedTransientRecovery:
    @pytest.mark.asyncio
    async def test_miscausas_appears_on_retry_proceeds_normally(self):
        """misCausas absent on first probe, present after a reload → no raise."""
        from app.scrapper.pjud.civil import CivilScraper

        scraper = CivilScraper(headless=True)
        page = _make_page(INDEX_URL, [False, True], content="x" * 100)

        with patch("app.scrapper.pjud.base.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await scraper._ensure_panel_loaded(page)

        assert scraper._panel_loaded is True
        # One reload to recover.
        assert page.goto.await_count == 1
        assert mock_sleep.await_count >= 1

    @pytest.mark.asyncio
    async def test_short_content_after_retries_raises_transient(self):
        """Not a login page, body never loads (short) → TransientNavigationError, not auth."""
        from app.scrapper.pjud.civil import CivilScraper
        from app.scrapper.pjud.exceptions import (
            SessionNotAuthenticatedError,
            TransientNavigationError,
        )

        scraper = CivilScraper(headless=True)
        page = _make_page(INDEX_URL, [False], content="")

        with patch("app.scrapper.pjud.base.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(TransientNavigationError) as exc_info:
                await scraper._ensure_panel_loaded(page)

        assert not isinstance(exc_info.value, SessionNotAuthenticatedError)
        # Initial probe + 2 reload retries.
        assert page.goto.await_count == 2
        assert INDEX_URL in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_goto_dns_failure_after_retries_raises_transient(self):
        """page.goto keeps failing with a Playwright network error → TransientNavigationError."""
        from playwright.async_api import Error as PlaywrightError

        from app.scrapper.pjud.civil import CivilScraper
        from app.scrapper.pjud.exceptions import TransientNavigationError

        scraper = CivilScraper(headless=True)
        # Page is NOT on indexN.php so the initial goto runs (and fails) too.
        page = _make_page("about:blank", [False], content="x" * 50_000)
        page.goto = AsyncMock(
            side_effect=PlaywrightError(
                "Page.goto: net::ERR_NAME_NOT_RESOLVED at https://oficinajudicialvirtual.pjud.cl/indexN.php"
            )
        )

        with patch("app.scrapper.pjud.base.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(TransientNavigationError) as exc_info:
                await scraper._ensure_panel_loaded(page)

        assert "net::ERR_NAME_NOT_RESOLVED" in str(exc_info.value)
        # Initial goto + 2 retries.
        assert page.goto.await_count == 3

    @pytest.mark.asyncio
    async def test_login_page_raises_auth_immediately_without_retry(self):
        """Genuine auth failure (login URL) → SessionNotAuthenticatedError, no reload retries."""
        from app.scrapper.pjud.civil import CivilScraper
        from app.scrapper.pjud.exceptions import SessionNotAuthenticatedError

        scraper = CivilScraper(headless=True)
        page = _make_page(LOGIN_URL, [False], content="")

        with patch("app.scrapper.pjud.base.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(SessionNotAuthenticatedError) as exc_info:
                await scraper._ensure_panel_loaded(page)

        assert exc_info.value.looks_like_login is True
        # Only the initial goto (page was not on indexN.php); no reload retries.
        assert page.goto.await_count == 1

    @pytest.mark.asyncio
    async def test_full_page_without_miscausas_still_raises_auth(self):
        """Fully loaded non-login page without misCausas → unknown state → keep the
        SessionNotAuthenticatedError diagnostics (unchanged behaviour)."""
        from app.scrapper.pjud.civil import CivilScraper
        from app.scrapper.pjud.exceptions import (
            SessionNotAuthenticatedError,
            TransientNavigationError,
        )

        scraper = CivilScraper(headless=True)
        page = _make_page(INDEX_URL, [False], content="x" * 50_000)

        with patch("app.scrapper.pjud.base.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(SessionNotAuthenticatedError) as exc_info:
                await scraper._ensure_panel_loaded(page)

        assert not isinstance(exc_info.value, TransientNavigationError)
        assert exc_info.value.looks_like_login is False


# ===========================================================================
# login_with_token — expired clave path
# ===========================================================================

EXPIRA_PASS_URL = (
    "https://oficinajudicialvirtual.pjud.cl/home/includes/expiraPass.php?usu=eyJhbGciOiJIUzI1NiJ9.e30.sig"
)


def _make_login_page(post_login_url: str, post_login_content: str) -> AsyncMock:
    async def evaluate(expr, *args, **kwargs):
        if 'input[type="hidden"]' in expr:
            return {"name": "ACCESO", "value": "eyJhbGciOiJIUzI1NiJ9.e30.sig"}
        # The login-POST builder is called with a dict argument.
        if args:
            return {
                "ok": True,
                "endpoint": "https://oficinajudicialvirtual.pjud.cl/home/includes/login.php",
                "fields": {"a": "b"},
                "roleMap": {"a": "rut"},
            }
        if "localStorage" in expr:
            return "{}"
        return None

    page = AsyncMock()
    page.url = post_login_url
    page.evaluate = AsyncMock(side_effect=evaluate)
    page.content = AsyncMock(return_value=post_login_content)
    page.goto = AsyncMock()
    page.wait_for_url = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    return page


class TestLoginWithTokenExpiredClave:
    @pytest.mark.asyncio
    async def test_expira_pass_redirect_raises_credential_expired(self, caplog):
        from app.scrapper.pjud.civil import CivilScraper
        from app.scrapper.pjud.exceptions import CredentialExpiredError, InvalidCredentialsError

        scraper = CivilScraper(headless=True)
        page = _make_login_page(EXPIRA_PASS_URL, "<html>Su clave ha expirado</html>")
        scraper._get_page = AsyncMock(return_value=page)
        scraper._context = MagicMock()
        scraper._context.cookies = AsyncMock(return_value=[])

        limiter = MagicMock()
        limiter.acquire = AsyncMock()

        with patch(
            "app.scrapper.pjud.resilience.rate_limiter.pjud_action_limiter",
            return_value=limiter,
        ), patch("app.scrapper.pjud.base.asyncio.sleep", new_callable=AsyncMock):
            with caplog.at_level("INFO", logger="app.scrapper.pjud.base"):
                with pytest.raises(CredentialExpiredError) as exc_info:
                    await scraper.login_with_token("19586894-5", "secret", "tok")

        assert isinstance(exc_info.value, InvalidCredentialsError)
        assert "19586894" in str(exc_info.value)
        assert "renovar" in str(exc_info.value)
        assert "Login successful" not in caplog.text
        # No session must be built from an unusable page.
        scraper._context.cookies.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_normal_index_still_logs_in(self):
        """Regression guard: a normal indexN.php landing still returns a session."""
        from app.scrapper.pjud.civil import CivilScraper

        scraper = CivilScraper(headless=True)
        page = _make_login_page(
            "https://oficinajudicialvirtual.pjud.cl/indexN.php",
            "<html><a onclick='misCausas()'>Mis Causas</a> Cerrar sesion</html>",
        )
        scraper._get_page = AsyncMock(return_value=page)
        scraper._context = MagicMock()
        scraper._context.cookies = AsyncMock(
            return_value=[{"name": "PHPSESSID", "value": "abc", "domain": ".pjud.cl"}]
        )

        limiter = MagicMock()
        limiter.acquire = AsyncMock()

        with patch(
            "app.scrapper.pjud.resilience.rate_limiter.pjud_action_limiter",
            return_value=limiter,
        ), patch("app.scrapper.pjud.base.asyncio.sleep", new_callable=AsyncMock):
            session = await scraper.login_with_token("19586894-5", "secret", "tok")

        assert session is not None
        assert session.cookies
