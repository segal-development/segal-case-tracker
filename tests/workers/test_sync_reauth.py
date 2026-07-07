"""S3-T4: Tests for autonomous worker re-auth — _reauth function.

Tests four branches (ADR-7 + FIX-1, post 2Captcha removal):
1. clave_unica nominal: ClaveUnicaAuth.login called, session stored.
2. captcha: always returns captcha_no_2captcha_key — the 2Captcha integration
   was removed (dead automated-scraping path; policy: never 2Captcha), so this
   auth method can no longer be re-authenticated autonomously and is skipped
   gracefully regardless of stored credentials.
3. No stored credentials: no crash, returns no_credentials.
4. Corrupt ciphertext: decrypt raises → returns decrypt_failed, never raises.

All tests mock Playwright, browser, and scraper — no live connections.
"""
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
) -> MagicMock:
    lawyer = MagicMock()
    lawyer.id = lawyer_id
    lawyer.rut = rut
    lawyer.preferred_auth_method = preferred_auth_method
    lawyer.encrypted_clave_unica_password = encrypted_clave_unica_password
    lawyer.encrypted_pjud_password = encrypted_pjud_password
    lawyer.clave_unica_rut = clave_unica_rut
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

class TestReauthCaptcha:
    """AUTH-06: Captcha re-auth is unconditionally skipped — 2Captcha was removed
    (dead automated-scraping path; policy: never 2Captcha). No solver import,
    no crash — just a graceful skip regardless of stored credentials."""

    @pytest.mark.asyncio
    async def test_captcha_always_returns_skip_reason_with_credentials(self, fake_redis):
        """Even with a stored encrypted password, captcha re-auth always skips gracefully."""
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore

        enc_pass = encrypt_pjud_password("pjudpass")
        lawyer = _make_lawyer(
            preferred_auth_method="captcha",
            encrypted_pjud_password=enc_pass,
        )
        store = SessionStore(redis_client=fake_redis)

        session, reason = await _reauth(lawyer, store)

        assert session is None
        assert reason == "captcha_no_2captcha_key"

    @pytest.mark.asyncio
    async def test_captcha_always_returns_skip_reason_without_credentials(self, fake_redis):
        """No stored encrypted password either → still the same graceful skip, no exception."""
        from app.workers.sync_scheduler import _reauth
        from app.services.session_store import SessionStore

        lawyer = _make_lawyer(
            preferred_auth_method="captcha",
            encrypted_pjud_password=None,
        )
        store = SessionStore(redis_client=fake_redis)

        session, reason = await _reauth(lawyer, store)

        assert session is None
        assert reason == "captcha_no_2captcha_key"


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
