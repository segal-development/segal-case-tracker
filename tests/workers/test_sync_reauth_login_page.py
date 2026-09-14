"""Defect C (2026-09-14): the credential vault was blind to a rejected login.

``_reauth`` recorded ``validation_failed`` only on ``InvalidCredentialsError``;
a login that PJUD bounced back to its login page every cycle (a retryable
``LoginError``) recorded nothing, so the vault kept showing "Válida" for
lawyers whose logins were rejected for days.

Now a ``LoginPageError`` (raised by ``login_with_token`` / Clave Única when the
POST lands back on the login page with no recognised credential message)
records ``validation_failed`` with detail ``login_page`` on BOTH auth methods,
WITHOUT the supervisor email (reserved for ``InvalidCredentialsError``) and
WITHOUT touching ``credential_alert_sent_at``. A later successful re-auth
records ``validation_ok`` as before.

All tests mock Playwright, browser, and scraper — no live connections.
"""
from datetime import datetime

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.security import encrypt_pjud_password
from app.services.pjud_session import PJUDSession


def _make_lawyer(
    *,
    preferred_auth_method: str,
    encrypted_clave_unica_password: object = None,
    encrypted_pjud_password: object = None,
    credential_alert_sent_at: object = None,
) -> MagicMock:
    lawyer = MagicMock()
    lawyer.id = 42
    lawyer.rut = "16021492-9"
    lawyer.name = "Carla"
    lawyer.preferred_auth_method = preferred_auth_method
    lawyer.encrypted_clave_unica_password = encrypted_clave_unica_password
    lawyer.encrypted_pjud_password = encrypted_pjud_password
    lawyer.clave_unica_rut = "16021492-9"
    lawyer.credential_alert_sent_at = credential_alert_sent_at
    return lawyer


def _fake_session(auth_method: str) -> PJUDSession:
    return PJUDSession.create(
        rut="16021492-9",
        cookies=[{"name": "PHPSESSID", "value": "abc", "domain": ".pjud.cl"}],
        lawyer_id=42,
        auth_method=auth_method,
    )


def _mock_civil_scraper(login_side_effect=None, login_return_value=None):
    mock_scraper = MagicMock()
    mock_scraper.start = AsyncMock()
    mock_scraper.stop = AsyncMock()
    if login_side_effect is not None:
        mock_scraper.login_with_segunda_clave = AsyncMock(side_effect=login_side_effect)
    else:
        mock_scraper.login_with_segunda_clave = AsyncMock(return_value=login_return_value)
    return MagicMock(return_value=mock_scraper), mock_scraper


def _mock_browser_and_auth(login_side_effect=None, login_return_value=None):
    mock_page = AsyncMock()
    mock_factory = MagicMock()
    mock_factory.new_page = AsyncMock(return_value=mock_page)
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_factory)
    mock_cm.__aexit__ = AsyncMock(return_value=None)
    mock_browser_class = MagicMock(return_value=mock_cm)

    mock_auth = MagicMock()
    if login_side_effect is not None:
        mock_auth.login = AsyncMock(side_effect=login_side_effect)
    else:
        mock_auth.login = AsyncMock(return_value=login_return_value)
    mock_auth_class = MagicMock(return_value=mock_auth)
    return mock_browser_class, mock_auth_class


class TestReauthLoginPageCaptcha:
    @pytest.mark.asyncio
    async def test_login_page_records_vault_failure_without_alert(self, fake_redis):
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore
        from app.scrapper.pjud.exceptions import LoginPageError

        lawyer = _make_lawyer(
            preferred_auth_method="captcha",
            encrypted_pjud_password=encrypt_pjud_password("pjudpass"),
            credential_alert_sent_at=None,
        )
        store = SessionStore(redis_client=fake_redis)
        mock_scraper_class, mock_scraper = _mock_civil_scraper(
            login_side_effect=LoginPageError(
                "PJUD no estableció la sesión (sigue en la página de login): Intente nuevamente"
            )
        )

        with (
            patch("app.scrapper.pjud.civil.CivilScraper", mock_scraper_class),
            patch(
                "app.workers.sync_scheduler.send_supervisor_credential_alert",
                new_callable=AsyncMock,
            ) as mock_alert,
            patch("app.workers.sync_scheduler._audit_validation") as mock_audit,
        ):
            session, reason = await _reauth(lawyer, store)

        assert session is None
        assert reason is not None
        assert "Intente nuevamente" in reason  # PJUD's text reaches the scheduler
        mock_alert.assert_not_awaited()
        assert lawyer.credential_alert_sent_at is None
        mock_audit.assert_called_once()
        audit_args, audit_kwargs = mock_audit.call_args
        assert audit_args[0] is lawyer
        assert audit_args[1] == "pjud"
        assert audit_kwargs["ok"] is False
        assert audit_kwargs["detail"] == "login_page"
        mock_scraper.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_login_page_leaves_existing_alert_marker_untouched(self, fake_redis):
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore
        from app.scrapper.pjud.exceptions import LoginPageError

        marker = datetime(2026, 9, 1, 12, 0, 0)
        lawyer = _make_lawyer(
            preferred_auth_method="captcha",
            encrypted_pjud_password=encrypt_pjud_password("pjudpass"),
            credential_alert_sent_at=marker,
        )
        store = SessionStore(redis_client=fake_redis)
        mock_scraper_class, _ = _mock_civil_scraper(login_side_effect=LoginPageError("login page"))

        with (
            patch("app.scrapper.pjud.civil.CivilScraper", mock_scraper_class),
            patch(
                "app.workers.sync_scheduler.send_supervisor_credential_alert",
                new_callable=AsyncMock,
            ) as mock_alert,
            patch("app.workers.sync_scheduler._audit_validation"),
        ):
            session, _reason = await _reauth(lawyer, store)

        assert session is None
        mock_alert.assert_not_awaited()
        assert lawyer.credential_alert_sent_at == marker

    @pytest.mark.asyncio
    async def test_generic_transient_error_still_records_nothing(self, fake_redis):
        """Regression: a Shape block / network error is not a credential verdict."""
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore
        from app.scrapper.pjud.exceptions import ShapeChallengeError

        lawyer = _make_lawyer(
            preferred_auth_method="captcha",
            encrypted_pjud_password=encrypt_pjud_password("pjudpass"),
        )
        store = SessionStore(redis_client=fake_redis)
        mock_scraper_class, _ = _mock_civil_scraper(
            login_side_effect=ShapeChallengeError("u", False, True, "TSPD_101")
        )

        with (
            patch("app.scrapper.pjud.civil.CivilScraper", mock_scraper_class),
            patch(
                "app.workers.sync_scheduler.send_supervisor_credential_alert",
                new_callable=AsyncMock,
            ) as mock_alert,
            patch("app.workers.sync_scheduler._audit_validation") as mock_audit,
        ):
            session, reason = await _reauth(lawyer, store)

        assert session is None
        assert reason.startswith("reauth_failed")
        mock_alert.assert_not_awaited()
        mock_audit.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_credentials_path_unchanged(self, fake_redis):
        """Regression: wrong clave still alerts once + records invalid_credentials."""
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore
        from app.scrapper.pjud.exceptions import InvalidCredentialsError

        lawyer = _make_lawyer(
            preferred_auth_method="captcha",
            encrypted_pjud_password=encrypt_pjud_password("pjudpass"),
            credential_alert_sent_at=None,
        )
        store = SessionStore(redis_client=fake_redis)
        mock_scraper_class, _ = _mock_civil_scraper(
            login_side_effect=InvalidCredentialsError("PJUD rejected credentials: Clave incorrecta")
        )

        with (
            patch("app.scrapper.pjud.civil.CivilScraper", mock_scraper_class),
            patch(
                "app.workers.sync_scheduler.send_supervisor_credential_alert",
                new_callable=AsyncMock,
            ) as mock_alert,
            patch("app.workers.sync_scheduler._audit_validation") as mock_audit,
        ):
            mock_alert.return_value = True
            session, reason = await _reauth(lawyer, store)

        assert session is None
        assert reason == "invalid_credentials"
        mock_alert.assert_awaited_once()
        assert lawyer.credential_alert_sent_at is not None
        mock_audit.assert_called_once()
        assert mock_audit.call_args.kwargs["detail"] == "invalid_credentials"

    @pytest.mark.asyncio
    async def test_later_successful_reauth_records_validation_ok(self, fake_redis):
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore

        lawyer = _make_lawyer(
            preferred_auth_method="captcha",
            encrypted_pjud_password=encrypt_pjud_password("pjudpass"),
        )
        store = SessionStore(redis_client=fake_redis)
        mock_scraper_class, _ = _mock_civil_scraper(login_return_value=_fake_session("captcha"))

        with (
            patch("app.scrapper.pjud.civil.CivilScraper", mock_scraper_class),
            patch("app.workers.sync_scheduler._audit_validation") as mock_audit,
        ):
            session, reason = await _reauth(lawyer, store)

        assert session is not None
        assert reason is None
        mock_audit.assert_called_once()
        assert mock_audit.call_args.args[1] == "pjud"
        assert mock_audit.call_args.kwargs["ok"] is True


class TestReauthLoginPageClaveUnica:
    @pytest.mark.asyncio
    async def test_login_page_records_vault_failure_without_alert(self, fake_redis):
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore
        from app.scrapper.pjud.clave_unica import ClaveUnicaLoginPageError

        lawyer = _make_lawyer(
            preferred_auth_method="clave_unica",
            encrypted_clave_unica_password=encrypt_pjud_password("mypassword"),
            credential_alert_sent_at=None,
        )
        store = SessionStore(redis_client=fake_redis)
        mock_browser_class, mock_auth_class = _mock_browser_and_auth(
            login_side_effect=ClaveUnicaLoginPageError("Login failed: sigue en login")
        )

        with (
            patch("app.scrapper.pjud.browser.BrowserFactory", mock_browser_class),
            patch("app.scrapper.pjud.clave_unica.ClaveUnicaAuth", mock_auth_class),
            patch(
                "app.workers.sync_scheduler.send_supervisor_credential_alert",
                new_callable=AsyncMock,
            ) as mock_alert,
            patch("app.workers.sync_scheduler._audit_validation") as mock_audit,
        ):
            session, reason = await _reauth(lawyer, store)

        assert session is None
        assert reason is not None
        mock_alert.assert_not_awaited()
        assert lawyer.credential_alert_sent_at is None
        mock_audit.assert_called_once()
        audit_args, audit_kwargs = mock_audit.call_args
        assert audit_args[1] == "clave_unica"
        assert audit_kwargs["ok"] is False
        assert audit_kwargs["detail"] == "login_page"

    @pytest.mark.asyncio
    async def test_later_successful_reauth_records_validation_ok(self, fake_redis):
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore

        lawyer = _make_lawyer(
            preferred_auth_method="clave_unica",
            encrypted_clave_unica_password=encrypt_pjud_password("mypassword"),
        )
        store = SessionStore(redis_client=fake_redis)
        mock_browser_class, mock_auth_class = _mock_browser_and_auth(
            login_return_value=_fake_session("clave_unica")
        )

        with (
            patch("app.scrapper.pjud.browser.BrowserFactory", mock_browser_class),
            patch("app.scrapper.pjud.clave_unica.ClaveUnicaAuth", mock_auth_class),
            patch("app.workers.sync_scheduler._audit_validation") as mock_audit,
        ):
            session, reason = await _reauth(lawyer, store)

        assert session is not None
        assert reason is None
        mock_audit.assert_called_once()
        assert mock_audit.call_args.args[1] == "clave_unica"
        assert mock_audit.call_args.kwargs["ok"] is True
