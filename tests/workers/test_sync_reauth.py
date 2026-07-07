"""S3-T4: Tests for autonomous worker re-auth — _reauth function.

Tests branches (ADR-7, post organic-reCAPTCHA segunda-clave automation):
1. clave_unica nominal: ClaveUnicaAuth.login called, session stored.
2. captcha (segunda clave): decrypts the stored PJUD password, runs a headless
   CivilScraper, and calls login_with_segunda_clave — an ORGANIC reCAPTCHA v3
   token generated in-page (NO 2Captcha, never reintroduced). Same failure
   handling as clave_unica: InvalidCredentialsError alerts the supervisor
   once (de-dup), Shape/transient errors never alert.
3. No stored credentials: no crash, returns no_credentials.
4. Corrupt ciphertext: decrypt raises → returns decrypt_failed, never raises.

All tests mock Playwright, browser, and scraper — no live connections.
"""
from datetime import datetime

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.security import encrypt_pjud_password
from app.services.pjud_session import PJUDSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_lawyer(
    *,
    preferred_auth_method: object = "clave_unica",
    encrypted_clave_unica_password: object = None,
    encrypted_pjud_password: object = None,
    clave_unica_rut: str = "12345678-9",
    rut: str = "12345678-9",
    lawyer_id: int = 42,
    name: str = "Juan Pérez",
    credential_alert_sent_at: object = None,
) -> MagicMock:
    lawyer = MagicMock()
    lawyer.id = lawyer_id
    lawyer.rut = rut
    lawyer.name = name
    lawyer.preferred_auth_method = preferred_auth_method
    lawyer.encrypted_clave_unica_password = encrypted_clave_unica_password
    lawyer.encrypted_pjud_password = encrypted_pjud_password
    lawyer.clave_unica_rut = clave_unica_rut
    lawyer.credential_alert_sent_at = credential_alert_sent_at
    return lawyer


def _make_fake_session(lawyer_id: int = 42, auth_method: str = "clave_unica") -> PJUDSession:
    return PJUDSession.create(
        rut="12345678-9",
        cookies=[{"name": "PHPSESSID", "value": "abc", "domain": ".pjud.cl"}],
        lawyer_id=lawyer_id,
        auth_method=auth_method,
    )


# ---------------------------------------------------------------------------
# Clave Única path
# ---------------------------------------------------------------------------

class TestReauthClaveUnica:
    """AUTH-04 / AUTH-05: Clave Única re-auth always works (no captcha needed)."""

    @pytest.mark.asyncio
    async def test_nominal_calls_login_stores_session(self, fake_redis):
        """Expired session → clave_unica → ClaveUnicaAuth.login called → session persisted."""
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore

        enc_pass = encrypt_pjud_password("mypassword")
        lawyer = _make_lawyer(
            preferred_auth_method="clave_unica",
            encrypted_clave_unica_password=enc_pass,
        )
        store = SessionStore(redis_client=fake_redis)
        fake_session = _make_fake_session(lawyer_id=42)

        # Mock BrowserFactory as an async context manager
        mock_page = AsyncMock()
        mock_factory = MagicMock()
        mock_factory.new_page = AsyncMock(return_value=mock_page)
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_factory)
        mock_cm.__aexit__ = AsyncMock(return_value=None)
        mock_browser_class = MagicMock(return_value=mock_cm)

        # Mock ClaveUnicaAuth
        mock_auth = MagicMock()
        mock_auth.login = AsyncMock(return_value=fake_session)
        mock_auth_class = MagicMock(return_value=mock_auth)

        with (
            patch("app.scrapper.pjud.browser.BrowserFactory", mock_browser_class),
            patch("app.scrapper.pjud.clave_unica.ClaveUnicaAuth", mock_auth_class),
        ):
            session, reason = await _reauth(lawyer, store)

        assert session is not None
        assert reason is None
        mock_auth.login.assert_awaited_once()
        # Session must be persisted in the store
        saved = await store.get_session_by_lawyer(lawyer.id)
        assert saved is not None

    @pytest.mark.asyncio
    async def test_no_clave_unica_password_returns_no_credentials(self, fake_redis):
        """clave_unica method but no encrypted password → no_credentials, no exception."""
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore

        lawyer = _make_lawyer(
            preferred_auth_method="clave_unica",
            encrypted_clave_unica_password=None,
        )
        store = SessionStore(redis_client=fake_redis)

        session, reason = await _reauth(lawyer, store)

        assert session is None
        assert reason == "no_credentials"


# ---------------------------------------------------------------------------
# Captcha path
# ---------------------------------------------------------------------------

def _mock_civil_scraper(login_side_effect=None, login_return_value=None):
    """Build a mocked CivilScraper class for the captcha/segunda-clave branch:
    start()/stop() no-ops, login_with_segunda_clave() either succeeds or raises."""
    mock_scraper = MagicMock()
    mock_scraper.start = AsyncMock()
    mock_scraper.stop = AsyncMock()
    if login_side_effect is not None:
        mock_scraper.login_with_segunda_clave = AsyncMock(side_effect=login_side_effect)
    else:
        mock_scraper.login_with_segunda_clave = AsyncMock(return_value=login_return_value)
    mock_scraper_class = MagicMock(return_value=mock_scraper)
    return mock_scraper_class, mock_scraper


class TestReauthCaptcha:
    """AUTH-06 (post organic-reCAPTCHA automation): segunda-clave re-auth now
    runs a headless CivilScraper and calls login_with_segunda_clave — an
    ORGANIC reCAPTCHA v3 token generated in-page. NO 2Captcha involved."""

    @pytest.mark.asyncio
    async def test_no_encrypted_password_returns_no_credentials(self, fake_redis):
        """No stored encrypted_pjud_password → no_credentials, no crash, no scraper started."""
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore

        lawyer = _make_lawyer(
            preferred_auth_method="captcha",
            encrypted_pjud_password=None,
        )
        store = SessionStore(redis_client=fake_redis)

        session, reason = await _reauth(lawyer, store)

        assert session is None
        assert reason == "no_credentials"

    @pytest.mark.asyncio
    async def test_organic_login_succeeds_stores_session(self, fake_redis):
        """Nominal: decrypts password, logs in via organic reCAPTCHA, session stored."""
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore

        enc_pass = encrypt_pjud_password("pjudpass")
        lawyer = _make_lawyer(
            preferred_auth_method="captcha",
            encrypted_pjud_password=enc_pass,
            credential_alert_sent_at=datetime(2026, 7, 1, 12, 0, 0),
        )
        store = SessionStore(redis_client=fake_redis)
        fake_session = _make_fake_session(lawyer_id=lawyer.id, auth_method="captcha")

        mock_scraper_class, mock_scraper = _mock_civil_scraper(login_return_value=fake_session)

        with patch("app.scrapper.pjud.civil.CivilScraper", mock_scraper_class):
            session, reason = await _reauth(lawyer, store)

        assert session is fake_session
        assert reason is None
        mock_scraper.login_with_segunda_clave.assert_awaited_once_with(lawyer.rut, "pjudpass")
        mock_scraper.stop.assert_awaited_once()
        saved = await store.get_session_by_lawyer(lawyer.id)
        assert saved is not None
        # A successful re-auth clears any prior credential-alert de-dup marker.
        assert lawyer.credential_alert_sent_at is None

    @pytest.mark.asyncio
    async def test_invalid_credentials_sends_alert_once_and_sets_timestamp(self, fake_redis):
        """PJUD rejects the segunda-clave RUT/password → supervisor alerted once."""
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore
        from app.scrapper.pjud.exceptions import InvalidCredentialsError

        enc_pass = encrypt_pjud_password("pjudpass")
        lawyer = _make_lawyer(
            preferred_auth_method="captcha",
            encrypted_pjud_password=enc_pass,
            credential_alert_sent_at=None,
        )
        store = SessionStore(redis_client=fake_redis)

        mock_scraper_class, mock_scraper = _mock_civil_scraper(
            login_side_effect=InvalidCredentialsError("PJUD rejected credentials")
        )

        with (
            patch("app.scrapper.pjud.civil.CivilScraper", mock_scraper_class),
            patch(
                "app.workers.sync_scheduler.send_supervisor_credential_alert",
                new_callable=AsyncMock,
            ) as mock_alert,
        ):
            mock_alert.return_value = True
            session, reason = await _reauth(lawyer, store)

        assert session is None
        assert reason == "invalid_credentials"
        mock_alert.assert_awaited_once()
        assert lawyer.credential_alert_sent_at is not None
        mock_scraper.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_second_consecutive_invalid_credentials_does_not_resend_alert(self, fake_redis):
        """De-dup: credential_alert_sent_at already set -> no second email."""
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore
        from app.scrapper.pjud.exceptions import InvalidCredentialsError

        already_sent_at = datetime(2026, 7, 1, 12, 0, 0)
        enc_pass = encrypt_pjud_password("pjudpass")
        lawyer = _make_lawyer(
            preferred_auth_method="captcha",
            encrypted_pjud_password=enc_pass,
            credential_alert_sent_at=already_sent_at,
        )
        store = SessionStore(redis_client=fake_redis)

        mock_scraper_class, _mock_scraper = _mock_civil_scraper(
            login_side_effect=InvalidCredentialsError("PJUD rejected credentials")
        )

        with (
            patch("app.scrapper.pjud.civil.CivilScraper", mock_scraper_class),
            patch(
                "app.workers.sync_scheduler.send_supervisor_credential_alert",
                new_callable=AsyncMock,
            ) as mock_alert,
        ):
            session, reason = await _reauth(lawyer, store)

        assert session is None
        assert reason == "invalid_credentials"
        mock_alert.assert_not_awaited()
        assert lawyer.credential_alert_sent_at == already_sent_at

    @pytest.mark.asyncio
    async def test_shape_challenge_does_not_send_alert(self, fake_redis):
        """A Shape block is NOT a credential problem -> no supervisor alert."""
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore
        from app.scrapper.pjud.exceptions import ShapeChallengeError

        enc_pass = encrypt_pjud_password("pjudpass")
        lawyer = _make_lawyer(
            preferred_auth_method="captcha",
            encrypted_pjud_password=enc_pass,
            credential_alert_sent_at=None,
        )
        store = SessionStore(redis_client=fake_redis)

        mock_scraper_class, _mock_scraper = _mock_civil_scraper(
            login_side_effect=ShapeChallengeError(
                url="https://oficinajudicialvirtual.pjud.cl/home/index.php",
                jquery_present=False,
                looks_like_login=True,
                marker="TSPD_101",
            )
        )

        with (
            patch("app.scrapper.pjud.civil.CivilScraper", mock_scraper_class),
            patch(
                "app.workers.sync_scheduler.send_supervisor_credential_alert",
                new_callable=AsyncMock,
            ) as mock_alert,
        ):
            session, reason = await _reauth(lawyer, store)

        assert session is None
        assert reason is not None and reason.startswith("reauth_failed")
        mock_alert.assert_not_awaited()
        assert lawyer.credential_alert_sent_at is None

    @pytest.mark.asyncio
    async def test_transient_error_does_not_send_alert(self, fake_redis):
        """A generic/transient error is NOT a credential problem -> no alert."""
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore

        enc_pass = encrypt_pjud_password("pjudpass")
        lawyer = _make_lawyer(
            preferred_auth_method="captcha",
            encrypted_pjud_password=enc_pass,
            credential_alert_sent_at=None,
        )
        store = SessionStore(redis_client=fake_redis)

        mock_scraper_class, _mock_scraper = _mock_civil_scraper(
            login_side_effect=Exception("network blip")
        )

        with (
            patch("app.scrapper.pjud.civil.CivilScraper", mock_scraper_class),
            patch(
                "app.workers.sync_scheduler.send_supervisor_credential_alert",
                new_callable=AsyncMock,
            ) as mock_alert,
        ):
            session, reason = await _reauth(lawyer, store)

        assert session is None
        mock_alert.assert_not_awaited()
        assert lawyer.credential_alert_sent_at is None

    @pytest.mark.asyncio
    async def test_corrupt_ciphertext_returns_decrypt_failed(self, fake_redis):
        """Corrupt encrypted_pjud_password -> decrypt raises -> decrypt_failed, never raises."""
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore

        lawyer = _make_lawyer(
            preferred_auth_method="captcha",
            encrypted_pjud_password="not-valid-fernet-ciphertext",
        )
        store = SessionStore(redis_client=fake_redis)

        session, reason = await _reauth(lawyer, store)

        assert session is None
        assert reason == "decrypt_failed"


# ---------------------------------------------------------------------------
# Decrypt-failure guard (FIX 1)
# ---------------------------------------------------------------------------

class TestReauthDecryptFailure:
    """FIX-1: corrupt ciphertext must never propagate — _reauth returns (None, 'decrypt_failed')."""

    @pytest.mark.asyncio
    async def test_clave_unica_corrupt_ciphertext_returns_decrypt_failed(self, fake_redis):
        """stored encrypted_clave_unica_password is garbage → decrypt raises → (None, 'decrypt_failed')."""
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore

        lawyer = _make_lawyer(
            preferred_auth_method="clave_unica",
            encrypted_clave_unica_password="not-valid-fernet-ciphertext",
        )
        store = SessionStore(redis_client=fake_redis)

        session, reason = await _reauth(lawyer, store)

        assert session is None
        assert reason == "decrypt_failed"


# ---------------------------------------------------------------------------
# No credentials fallback
# ---------------------------------------------------------------------------

class TestReauthNoCredentials:
    """AUTH-07: Lawyer has no usable credentials → skip, no exception."""

    @pytest.mark.asyncio
    async def test_no_preferred_method_returns_no_credentials(self, fake_redis):
        """No auth method set → no_credentials, no exception raised."""
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore

        lawyer = _make_lawyer(
            preferred_auth_method=None,
            encrypted_pjud_password=None,
            encrypted_clave_unica_password=None,
        )
        store = SessionStore(redis_client=fake_redis)

        session, reason = await _reauth(lawyer, store)

        assert session is None
        assert reason == "no_credentials"


# ---------------------------------------------------------------------------
# Supervisor credential-change alert wiring + de-dup
# ---------------------------------------------------------------------------

def _mock_browser_and_auth(login_side_effect=None, login_return_value=None):
    """Build the (browser_class_patch, auth_class_patch) mocks for a clave_unica
    ClaveUnicaAuth.login() call, either succeeding or raising."""
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


class TestReauthCredentialAlert:
    """S-CCS: InvalidCredentialsError triggers exactly one supervisor alert
    per credential-failure episode; Shape/transient failures never alert;
    a successful re-auth clears the de-dup marker."""

    @pytest.mark.asyncio
    async def test_invalid_credentials_sends_alert_once_and_sets_timestamp(self, fake_redis):
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore
        from app.scrapper.pjud.exceptions import InvalidCredentialsError

        enc_pass = encrypt_pjud_password("mypassword")
        lawyer = _make_lawyer(
            preferred_auth_method="clave_unica",
            encrypted_clave_unica_password=enc_pass,
            credential_alert_sent_at=None,
        )
        store = SessionStore(redis_client=fake_redis)

        mock_browser_class, mock_auth_class = _mock_browser_and_auth(
            login_side_effect=InvalidCredentialsError("PJUD rejected credentials")
        )

        with (
            patch("app.scrapper.pjud.browser.BrowserFactory", mock_browser_class),
            patch("app.scrapper.pjud.clave_unica.ClaveUnicaAuth", mock_auth_class),
            patch(
                "app.workers.sync_scheduler.send_supervisor_credential_alert",
                new_callable=AsyncMock,
            ) as mock_alert,
        ):
            mock_alert.return_value = True
            session, reason = await _reauth(lawyer, store)

        assert session is None
        assert reason == "invalid_credentials"
        mock_alert.assert_awaited_once()
        assert lawyer.credential_alert_sent_at is not None

    @pytest.mark.asyncio
    async def test_second_consecutive_failure_does_not_resend_alert(self, fake_redis):
        """De-dup: credential_alert_sent_at already set -> no second email."""
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore
        from app.scrapper.pjud.exceptions import InvalidCredentialsError

        already_sent_at = datetime(2026, 7, 1, 12, 0, 0)
        enc_pass = encrypt_pjud_password("mypassword")
        lawyer = _make_lawyer(
            preferred_auth_method="clave_unica",
            encrypted_clave_unica_password=enc_pass,
            credential_alert_sent_at=already_sent_at,
        )
        store = SessionStore(redis_client=fake_redis)

        mock_browser_class, mock_auth_class = _mock_browser_and_auth(
            login_side_effect=InvalidCredentialsError("PJUD rejected credentials")
        )

        with (
            patch("app.scrapper.pjud.browser.BrowserFactory", mock_browser_class),
            patch("app.scrapper.pjud.clave_unica.ClaveUnicaAuth", mock_auth_class),
            patch(
                "app.workers.sync_scheduler.send_supervisor_credential_alert",
                new_callable=AsyncMock,
            ) as mock_alert,
        ):
            session, reason = await _reauth(lawyer, store)

        assert session is None
        assert reason == "invalid_credentials"
        mock_alert.assert_not_awaited()
        assert lawyer.credential_alert_sent_at == already_sent_at

    @pytest.mark.asyncio
    async def test_shape_challenge_does_not_send_alert(self, fake_redis):
        """A Shape block is NOT a credential problem -> no supervisor alert."""
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore
        from app.scrapper.pjud.exceptions import ShapeChallengeError

        enc_pass = encrypt_pjud_password("mypassword")
        lawyer = _make_lawyer(
            preferred_auth_method="clave_unica",
            encrypted_clave_unica_password=enc_pass,
            credential_alert_sent_at=None,
        )
        store = SessionStore(redis_client=fake_redis)

        mock_browser_class, mock_auth_class = _mock_browser_and_auth(
            login_side_effect=ShapeChallengeError(
                url="https://oficinajudicialvirtual.pjud.cl/home/index.php",
                jquery_present=False,
                looks_like_login=True,
                marker="TSPD_101",
            )
        )

        with (
            patch("app.scrapper.pjud.browser.BrowserFactory", mock_browser_class),
            patch("app.scrapper.pjud.clave_unica.ClaveUnicaAuth", mock_auth_class),
            patch(
                "app.workers.sync_scheduler.send_supervisor_credential_alert",
                new_callable=AsyncMock,
            ) as mock_alert,
        ):
            session, reason = await _reauth(lawyer, store)

        assert session is None
        mock_alert.assert_not_awaited()
        assert lawyer.credential_alert_sent_at is None

    @pytest.mark.asyncio
    async def test_transient_error_does_not_send_alert(self, fake_redis):
        """A generic/transient error is NOT a credential problem -> no alert."""
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore

        enc_pass = encrypt_pjud_password("mypassword")
        lawyer = _make_lawyer(
            preferred_auth_method="clave_unica",
            encrypted_clave_unica_password=enc_pass,
            credential_alert_sent_at=None,
        )
        store = SessionStore(redis_client=fake_redis)

        mock_browser_class, mock_auth_class = _mock_browser_and_auth(
            login_side_effect=Exception("network blip")
        )

        with (
            patch("app.scrapper.pjud.browser.BrowserFactory", mock_browser_class),
            patch("app.scrapper.pjud.clave_unica.ClaveUnicaAuth", mock_auth_class),
            patch(
                "app.workers.sync_scheduler.send_supervisor_credential_alert",
                new_callable=AsyncMock,
            ) as mock_alert,
        ):
            session, reason = await _reauth(lawyer, store)

        assert session is None
        mock_alert.assert_not_awaited()
        assert lawyer.credential_alert_sent_at is None

    @pytest.mark.asyncio
    async def test_successful_reauth_clears_alert_timestamp(self, fake_redis):
        """A successful login clears any prior de-dup marker."""
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore

        enc_pass = encrypt_pjud_password("mypassword")
        lawyer = _make_lawyer(
            preferred_auth_method="clave_unica",
            encrypted_clave_unica_password=enc_pass,
            credential_alert_sent_at=datetime(2026, 7, 1, 12, 0, 0),
        )
        store = SessionStore(redis_client=fake_redis)
        fake_session = _make_fake_session(lawyer_id=lawyer.id)

        mock_browser_class, mock_auth_class = _mock_browser_and_auth(
            login_return_value=fake_session
        )

        with (
            patch("app.scrapper.pjud.browser.BrowserFactory", mock_browser_class),
            patch("app.scrapper.pjud.clave_unica.ClaveUnicaAuth", mock_auth_class),
            patch(
                "app.workers.sync_scheduler.send_supervisor_credential_alert",
                new_callable=AsyncMock,
            ) as mock_alert,
        ):
            session, reason = await _reauth(lawyer, store)

        assert session is not None
        assert reason is None
        mock_alert.assert_not_awaited()
        assert lawyer.credential_alert_sent_at is None
