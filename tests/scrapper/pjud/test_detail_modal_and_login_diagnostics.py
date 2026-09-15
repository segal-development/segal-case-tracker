"""Tests for two scraper defects observed in production (2026-09-14).

Defect A — PJUD started answering the detail call with a tiny (~276 chars)
modal fragment about an hour into each lawyer's batch. The scraper accepted
anything >= 100 chars and handed it to the parser, which blew up with
``ValueError("Invalid ROL format: '')``: the ``pjud-detail`` breaker opened,
every remaining detail of that lawyer was lost and — because a ``ValueError``
is not a session error — no re-auth ever happened. A modal shorter than
``DETAIL_MODAL_MIN_CHARS`` must now be classified (session vs transient),
logged (visible text) and never reach the parser.

Defect B — when the login POST lands back on ``home/index.php`` without a
recognised credential message we raised a bare retryable ``LoginError`` and
never logged what PJUD actually said. The stuck page's visible text is now
logged and carried in a ``LoginPageError``.

All tests mock Playwright — no live connections.
"""

import uuid

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


INDEX_URL = "https://oficinajudicialvirtual.pjud.cl/indexN.php"
LOGIN_URL = "https://oficinajudicialvirtual.pjud.cl/home/index.php"


def _warning_messages(mock_logger: MagicMock) -> list[str]:
    """Formatted messages of every ``logger.warning(fmt, *args)`` call on a
    patched module logger. Asserting on the module logger (not caplog) keeps
    these checks independent of whatever logging configuration other tests in
    the suite leave behind."""
    out = []
    for call in mock_logger.warning.call_args_list:
        fmt, *args = call.args
        out.append(fmt % tuple(args) if args else str(fmt))
    return out


def _padded(html: str, size: int) -> str:
    """Pad *html* with empty spans up to exactly *size* chars (mimics the constant
    276-char fragment seen in production)."""
    filler = "<span></span>"
    while len(html) + len(filler) <= size:
        html += filler
    return html + " " * (size - len(html))


SESSION_FRAGMENT = _padded(
    "<div class='alert'>Su sesión ha expirado. Por favor inicie sesión nuevamente.</div>",
    276,
)
UNRELATED_FRAGMENT = _padded("<div class='cargando'>Cargando datos de la causa...</div>", 276)
# Production shape (2026-09-15 10:11): 276 chars of markup with NO visible text.
# Retrying returned the same shell every time; the next lawyer's fresh login got
# 66 full modals in a row → an empty shell means the session is dead.
EMPTY_SHELL_FRAGMENT = _padded("<div class='modal-body'><table><tbody></tbody></table></div>", 276)
SHAPE_FRAGMENT = _padded(
    "<html>failureConfig TSPD_101 what code is in the image support id 12345</html>",
    276,
)
REAL_MODAL = "<table><tr><td>" + ("Detalle de la causa " * 1500) + "</td></tr></table>"


# ===========================================================================
# Exception hierarchy
# ===========================================================================

class TestLoginPageError:
    def test_is_login_error_but_not_invalid_credentials(self):
        from app.scrapper.pjud.exceptions import (
            InvalidCredentialsError,
            LoginError,
            LoginPageError,
        )

        exc = LoginPageError("sigue en la página de login")
        assert isinstance(exc, LoginError)
        assert not isinstance(exc, InvalidCredentialsError)


# ===========================================================================
# classify_small_detail_modal — pure helper
# ===========================================================================

class TestClassifySmallDetailModal:
    def test_threshold_is_well_above_the_production_fragment(self):
        from app.scrapper.pjud.base import DETAIL_MODAL_MIN_CHARS

        assert DETAIL_MODAL_MIN_CHARS == 2000
        assert len(SESSION_FRAGMENT) == 276 < DETAIL_MODAL_MIN_CHARS
        assert len(REAL_MODAL) > DETAIL_MODAL_MIN_CHARS

    @pytest.mark.parametrize(
        "html",
        [
            "<div>Su sesión ha expirado</div>",
            "<div>SU SESION HA EXPIRADO</div>",
            "<p>La sesión expiró, vuelva a ingresar</p>",
            "<p>Debe iniciar sesión nuevamente</p>",
            "<p>Ingrese su RUT y clave</p>",
            "<div class='msg'>Login requerido</div>",
            "<div>Acceso denegado</div>",
            "<div>ACCESO DENEGADO</div>",
            "<div>No autorizado</div>",
            "<div>Usuario no identificado</div>",
            "<script>location.href='https://oficinajudicialvirtual.pjud.cl/home/index.php'</script><div>...</div>",
            "<a href='../loginN.php'>volver</a>",
        ],
    )
    def test_session_markers_are_session(self, html):
        from app.scrapper.pjud.base import classify_small_detail_modal

        assert classify_small_detail_modal(html) == "session"

    @pytest.mark.parametrize(
        "html",
        [
            "<div>Cargando datos de la causa...</div>",
            "<div class='cargando'>Espere un momento</div>",
            "   ",
            "",
        ],
    )
    def test_unrelated_short_text_is_transient(self, html):
        from app.scrapper.pjud.base import classify_small_detail_modal

        assert classify_small_detail_modal(html) == "transient"

    @pytest.mark.parametrize(
        "html",
        [
            "<table><tr><td></td></tr></table>",
            "<div class='modal-body'><table><tbody></tbody></table></div>",
            EMPTY_SHELL_FRAGMENT,
            "<div><span></span><span></span></div>",
        ],
    )
    def test_markup_with_no_visible_text_is_session(self, html):
        """A modal shell with NO visible text is what a dead PJUD session returns
        (verified in production: same 276-char shell on every retry, fixed only by
        a fresh login). It must trigger re-auth, not backoff retries."""
        from app.scrapper.pjud.base import classify_small_detail_modal

        assert classify_small_detail_modal(html) == "session"

    def test_marker_only_inside_script_or_style_is_not_visible_text(self):
        """A JS reference to 'sesion' in a script block is not PJUD telling the
        user anything — only visible text (plus login URLs) counts."""
        from app.scrapper.pjud.base import classify_small_detail_modal

        html = "<script>var sesion = checkSesion();</script><style>.usuario{}</style><div>Cargando</div>"
        assert classify_small_detail_modal(html) == "transient"


# ===========================================================================
# get_case_detail — small modal never reaches the parser
# ===========================================================================

def _make_detail_page(modal_html: str, url: str = INDEX_URL) -> AsyncMock:
    async def evaluate(expr, *args, **kwargs):
        if "=== 'function'" in expr:
            return True
        if "modal.innerHTML" in expr:
            return modal_html
        return None

    page = AsyncMock()
    page.url = url
    page.is_closed = MagicMock(return_value=False)
    page.evaluate = AsyncMock(side_effect=evaluate)
    return page


def _make_detail_scraper(modal_html: str):
    from app.scrapper.pjud.civil import CivilScraper

    scraper = CivilScraper(headless=True)
    scraper._page = _make_detail_page(modal_html)
    scraper._ensure_panel_loaded = AsyncMock()
    scraper._parse_case_detail_html = MagicMock(return_value="parsed")
    return scraper


def _detail_patches():
    limiter = MagicMock()
    limiter.acquire = AsyncMock()
    return (
        patch(
            "app.scrapper.pjud.resilience.rate_limiter.pjud_action_limiter",
            return_value=limiter,
        ),
        patch("app.scrapper.pjud.base.asyncio.sleep", new_callable=AsyncMock),
    )


class TestGetCaseDetailSmallModal:
    @pytest.mark.asyncio
    async def test_session_fragment_raises_session_expired_and_skips_parser(self):
        from app.scrapper.pjud.exceptions import SessionExpiredError
        from app.services.pjud_session import PJUDSession

        scraper = _make_detail_scraper(SESSION_FRAGMENT)
        session = PJUDSession.create(rut="16021492-9", cookies=[], lawyer_id=1)
        p1, p2 = _detail_patches()

        with p1, p2, patch("app.scrapper.pjud.base.logger") as mock_logger:
            with pytest.raises(SessionExpiredError) as exc_info:
                await scraper.get_case_detail(session=session, case_token="tok")

        scraper._parse_case_detail_html.assert_not_called()
        assert "276" in str(exc_info.value)
        assert "vencida" in str(exc_info.value)
        # The diagnostic we never had: PJUD's visible text, at WARNING.
        warnings = _warning_messages(mock_logger)
        assert any("Su sesión ha expirado" in m and "276" in m for m in warnings)

    @pytest.mark.asyncio
    async def test_empty_shell_fragment_raises_session_expired(self):
        """The production case: 276-char shell, visible text '' → session expired
        → the caller re-authenticates (never the parser, never backoff)."""
        from app.scrapper.pjud.exceptions import SessionExpiredError
        from app.services.pjud_session import PJUDSession

        scraper = _make_detail_scraper(EMPTY_SHELL_FRAGMENT)
        session = PJUDSession.create(rut="16021492-9", cookies=[], lawyer_id=1)
        p1, p2 = _detail_patches()

        with p1, p2, patch("app.scrapper.pjud.base.logger") as mock_logger:
            with pytest.raises(SessionExpiredError) as exc_info:
                await scraper.get_case_detail(session=session, case_token="tok")

        scraper._parse_case_detail_html.assert_not_called()
        assert "276" in str(exc_info.value)
        assert any("classified as session" in m for m in _warning_messages(mock_logger))

    @pytest.mark.asyncio
    async def test_unrelated_fragment_raises_transient_navigation(self):
        from app.scrapper.pjud.exceptions import (
            SessionExpiredError,
            TransientNavigationError,
        )
        from app.services.pjud_session import PJUDSession

        scraper = _make_detail_scraper(UNRELATED_FRAGMENT)
        session = PJUDSession.create(rut="16021492-9", cookies=[], lawyer_id=1)
        p1, p2 = _detail_patches()

        with p1, p2:
            with pytest.raises(TransientNavigationError) as exc_info:
                await scraper.get_case_detail(session=session, case_token="tok")

        scraper._parse_case_detail_html.assert_not_called()
        assert not isinstance(exc_info.value, SessionExpiredError)
        assert exc_info.value.url == INDEX_URL
        assert "276" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_shape_fragment_raises_shape_challenge(self):
        from app.scrapper.pjud.exceptions import ShapeChallengeError
        from app.services.pjud_session import PJUDSession

        scraper = _make_detail_scraper(SHAPE_FRAGMENT)
        session = PJUDSession.create(rut="16021492-9", cookies=[], lawyer_id=1)
        p1, p2 = _detail_patches()

        with p1, p2:
            with pytest.raises(ShapeChallengeError):
                await scraper.get_case_detail(session=session, case_token="tok")

        scraper._parse_case_detail_html.assert_not_called()

    @pytest.mark.asyncio
    async def test_normal_modal_is_parsed_as_before(self):
        from app.services.pjud_session import PJUDSession

        scraper = _make_detail_scraper(REAL_MODAL)
        session = PJUDSession.create(rut="16021492-9", cookies=[], lawyer_id=1)
        p1, p2 = _detail_patches()

        with p1, p2:
            result = await scraper.get_case_detail(session=session, case_token="tok")

        assert result == "parsed"
        scraper._parse_case_detail_html.assert_called_once_with(REAL_MODAL, "tok")


# ===========================================================================
# pjud-detail breaker — an expired session is not a PJUD outage
# ===========================================================================

@pytest.fixture
def cb_settings():
    settings = MagicMock()
    settings.PJUD_RETRY_MAX_ATTEMPTS = 1
    settings.PJUD_RETRY_BASE_DELAY = 0.0
    settings.PJUD_RETRY_MAX_DELAY = 0.1
    settings.PJUD_CB_FAILURE_THRESHOLD = 2
    settings.PJUD_CB_RECOVERY_TIMEOUT = 60
    with patch("app.config.settings", settings):
        yield settings


class TestPageFollowsTheSession:
    """After a re-auth the caller passes a NEW PJUDSession. The page built for the
    old session still carries the dead cookies, so reusing it replays the failure
    (production 2026-09-15 12:57: re-auth OK, retry <1s later got the same empty
    modal, batch stopped). The page must be rebuilt when the session changed."""

    @pytest.mark.asyncio
    async def test_rebuilds_page_when_session_changed(self):
        from app.services.pjud_session import PJUDSession

        scraper = _make_detail_scraper(EMPTY_SHELL_FRAGMENT)   # stale page (dead session)
        old = PJUDSession.create(rut="16021492-9", cookies=[], lawyer_id=1)
        new = PJUDSession.create(rut="16021492-9", cookies=[], lawyer_id=1)
        scraper._page_session_key = id(old)
        fresh_page = _make_detail_page(REAL_MODAL)
        scraper._get_page = AsyncMock(return_value=fresh_page)
        p1, p2 = _detail_patches()

        with p1, p2:
            result = await scraper.get_case_detail(session=new, case_token="tok")

        scraper._get_page.assert_awaited_once_with(new)
        scraper._parse_case_detail_html.assert_called_once()
        assert result == "parsed"

    @pytest.mark.asyncio
    async def test_reuses_page_for_the_same_session(self):
        from app.services.pjud_session import PJUDSession

        scraper = _make_detail_scraper(REAL_MODAL)
        session = PJUDSession.create(rut="16021492-9", cookies=[], lawyer_id=1)
        scraper._page_session_key = id(session)
        scraper._get_page = AsyncMock()
        p1, p2 = _detail_patches()

        with p1, p2:
            await scraper.get_case_detail(session=session, case_token="tok")

        scraper._get_page.assert_not_awaited()
        scraper._parse_case_detail_html.assert_called_once()

    @pytest.mark.asyncio
    async def test_reuses_injected_page_when_its_session_is_unknown(self):
        """A page injected/attached without a recorded session key (tests, CDP
        attach) keeps being used as before."""
        from app.services.pjud_session import PJUDSession

        scraper = _make_detail_scraper(REAL_MODAL)   # no _page_session_key set
        session = PJUDSession.create(rut="16021492-9", cookies=[], lawyer_id=1)
        scraper._get_page = AsyncMock()
        p1, p2 = _detail_patches()

        with p1, p2:
            await scraper.get_case_detail(session=session, case_token="tok")

        scraper._get_page.assert_not_awaited()


class TestDetailBreakerIgnoresSessionErrors:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("exc_kind", ["session_expired", "not_authenticated", "shape"])
    async def test_session_errors_do_not_count_as_failures(self, cb_settings, exc_kind):
        from app.scrapper.pjud.exceptions import (
            SessionExpiredError,
            SessionNotAuthenticatedError,
            ShapeChallengeError,
        )
        from app.scrapper.pjud.resilience.circuit_breaker import get_circuit_breaker
        from app.scrapper.pjud.resilience.integration import resilient_call

        def make_exc():
            if exc_kind == "session_expired":
                return SessionExpiredError("Sesión PJUD vencida")
            if exc_kind == "not_authenticated":
                return SessionNotAuthenticatedError(INDEX_URL, False, True)
            return ShapeChallengeError(INDEX_URL, False, False, "TSPD_101 challenge")

        op = f"detail-{uuid.uuid4()}"

        async def expired():
            raise make_exc()

        # Well past the threshold (2): the breaker must still be closed.
        for _ in range(5):
            with pytest.raises(type(make_exc())):
                await resilient_call(op, expired)

        status = get_circuit_breaker(f"pjud-{op}").get_status()
        assert status["state"] == "closed"
        assert status["failure_count"] == 0

        async def ok():
            return "ok"

        assert await resilient_call(op, ok) == "ok"

    @pytest.mark.asyncio
    async def test_transient_navigation_still_opens_the_breaker(self, cb_settings):
        """Regression guard for the documented decision: a page that did not load
        IS the outage signal the breaker exists for."""
        from app.scrapper.pjud.exceptions import CircuitOpenError, TransientNavigationError
        from app.scrapper.pjud.resilience.circuit_breaker import get_circuit_breaker
        from app.scrapper.pjud.resilience.integration import resilient_call

        op = f"detail-{uuid.uuid4()}"

        async def down():
            raise TransientNavigationError(url=INDEX_URL, reason="net::ERR_CONNECTION_RESET")

        for _ in range(2):
            with pytest.raises(TransientNavigationError):
                await resilient_call(op, down)

        assert get_circuit_breaker(f"pjud-{op}").get_status()["state"] == "open"
        with pytest.raises(CircuitOpenError):
            await resilient_call(op, down)


# ===========================================================================
# login_with_token — login-page diagnostics
# ===========================================================================

def _make_login_page(post_login_url: str, post_login_content: str) -> AsyncMock:
    async def evaluate(expr, *args, **kwargs):
        if 'input[type="hidden"]' in expr:
            return {"name": "ACCESO", "value": "eyJhbGciOiJIUzI1NiJ9.e30.sig"}
        if args:  # login-POST builder is called with a dict argument
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


def _make_login_scraper(page):
    from app.scrapper.pjud.civil import CivilScraper

    scraper = CivilScraper(headless=True)
    scraper._get_page = AsyncMock(return_value=page)
    scraper._context = MagicMock()
    scraper._context.cookies = AsyncMock(return_value=[])
    return scraper


STUCK_LOGIN_HTML = (
    "<html><head><style>.x{color:red}</style><script>var secretJs = 'sup3rSecretPw';</script></head>"
    "<body><div class='alert'>Estimado usuario: su cuenta requiere actualización de datos. "
    "Intente nuevamente más tarde.</div></body></html>"
)


class TestLoginWithTokenLoginPageDiagnostics:
    @pytest.mark.asyncio
    async def test_unrecognised_stuck_page_logs_snippet_and_raises_login_page_error(self):
        from app.scrapper.pjud.exceptions import InvalidCredentialsError, LoginPageError

        page = _make_login_page(LOGIN_URL, STUCK_LOGIN_HTML)
        scraper = _make_login_scraper(page)
        p1, p2 = _detail_patches()

        with p1, p2, patch("app.scrapper.pjud.base.logger") as mock_logger:
            with pytest.raises(LoginPageError) as exc_info:
                await scraper.login_with_token("16021492-9", "sup3rSecretPw", "tok")

        assert not isinstance(exc_info.value, InvalidCredentialsError)
        # The message carries PJUD's visible text (bounded) so it reaches
        # sync_history.error_message via the scheduler.
        assert "Intente nuevamente" in str(exc_info.value)
        assert len(str(exc_info.value)) <= 300
        # Diagnostic WARNING: RUT + visible text, never scripts nor the password.
        warnings = _warning_messages(mock_logger)
        assert any("16021492" in m and "Intente nuevamente" in m for m in warnings)
        assert all("sup3rSecretPw" not in m and "color:red" not in m for m in warnings)
        assert "sup3rSecretPw" not in str(exc_info.value)
        scraper._context.cookies.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_recognised_credential_message_still_raises_invalid_credentials(self):
        from app.scrapper.pjud.exceptions import InvalidCredentialsError

        page = _make_login_page(LOGIN_URL, "<html><div class='alert'>Clave incorrecta</div></html>")
        scraper = _make_login_scraper(page)
        p1, p2 = _detail_patches()

        with p1, p2:
            with pytest.raises(InvalidCredentialsError):
                await scraper.login_with_token("16021492-9", "secret", "tok")


class TestClassifyLoginFailureNewMarkers:
    @pytest.mark.parametrize(
        "content",
        [
            "<div>Clave incorrecta</div>",
            "<div>CLAVE INCORRECTA</div>",
            "<div>Contraseña incorrecta</div>",
            "<div>Contrasena incorrecta</div>",
            "<div>Usuario o clave inválidos</div>",
            "<div>Datos incorrectos</div>",
            "<div>Usuario bloqueado</div>",
            "<div>Cuenta bloqueada</div>",
            "<div>Usuario no existe</div>",
            "<div>El usuario no se encuentra registrado</div>",
            "<div>RUT no registrado en el sistema</div>",
        ],
    )
    def test_conservative_spanish_markers_are_invalid_credentials(self, content):
        from app.scrapper.pjud.base import classify_login_failure

        invalid, snippet = classify_login_failure(content)
        assert invalid is True
        assert snippet

    @pytest.mark.parametrize(
        "content",
        [
            "<html><body>Ingrese su RUT y clave para continuar</body></html>",
            "<div>Error de validación. Intente nuevamente.</div>",
            "<script>if (usuario.bloqueado) alert('cuenta bloqueada');</script><div>Bienvenido</div>",
        ],
    )
    def test_neutral_text_and_scripts_are_not_invalid_credentials(self, content):
        from app.scrapper.pjud.base import classify_login_failure

        assert classify_login_failure(content)[0] is False

    def test_snippet_keeps_original_casing_and_accents(self):
        from app.scrapper.pjud.base import classify_login_failure

        invalid, snippet = classify_login_failure("<div>Contraseña Incorrecta para el usuario</div>")
        assert invalid is True
        assert "Contraseña Incorrecta" in snippet


# ===========================================================================
# Clave Única — stuck on the PJUD login page is a LoginPageError too
# ===========================================================================

class TestClaveUnicaLoginPage:
    @pytest.fixture
    def mock_registry(self):
        with patch("app.scrapper.pjud.clave_unica.SelectorRegistry") as MockRegistry:
            registry = MagicMock()
            registry.load = MagicMock()
            registry.get = MagicMock(side_effect=lambda comp, sel: f"#{sel}")
            MockRegistry.return_value = registry
            yield registry

    def _make_page(self, content: str, url: str) -> AsyncMock:
        page = AsyncMock()
        page.url = url
        page.content = AsyncMock(return_value=content)
        page.evaluate = AsyncMock(
            return_value={"misCausas": False, "logout": False, "welcome": False}
        )
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

    @pytest.mark.asyncio
    async def test_unclassified_on_login_url_raises_login_page_error(self, mock_registry):
        from app.scrapper.pjud.clave_unica import (
            ClaveUnicaAuth,
            ClaveUnicaAuthError,
            ClaveUnicaCredentials,
        )
        from app.scrapper.pjud.exceptions import InvalidCredentialsError, LoginPageError

        auth = ClaveUnicaAuth()
        auth._registry = mock_registry
        credentials = ClaveUnicaCredentials(rut="12345678-9", password="s3cret")
        page = self._make_page(
            content="<html><div>Su cuenta requiere actualización. Intente nuevamente.</div></html>",
            url=LOGIN_URL,
        )

        limiter_patch, _ = _detail_patches()  # bypass the shared login token bucket
        with limiter_patch, patch("app.scrapper.pjud.clave_unica.logger") as mock_logger:
            with pytest.raises(LoginPageError) as exc_info:
                await auth.login(page, credentials, lawyer_id=1)

        # Backwards compatible: still a ClaveUnicaAuthError for existing callers.
        assert isinstance(exc_info.value, ClaveUnicaAuthError)
        assert not isinstance(exc_info.value, InvalidCredentialsError)
        assert "Intente nuevamente" in str(exc_info.value)
        warnings = _warning_messages(mock_logger)
        assert any("Intente nuevamente" in m for m in warnings)
        assert all("s3cret" not in m for m in warnings)
