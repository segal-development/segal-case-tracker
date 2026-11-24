"""Rejection hooks reuse the existing detectors/exceptions; they only count."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.security import encrypt_pjud_password
from app.scrapper.pjud import base
from app.scrapper.pjud.exceptions import ShapeChallengeError
from app.services import pjud_metrics as pm
from app.services import sync_service
from app.workers.sync_scheduler import _reauth


@pytest.fixture(autouse=True)
def _clean():
    pm.reset_state()
    yield
    pm.reset_state()


def _kinds():
    return sorted((k[3], k[4]) for k in pm._counts)


def test_shape_helper_counts_shape_rejection():
    base._record_shape_rejection("detail")
    assert _kinds() == [("detail", "shape_challenge")]


def test_shape_helper_never_raises(monkeypatch):
    monkeypatch.setattr(pm, "record_rejection", lambda *a: 1 / 0)
    base._record_shape_rejection("detail")


def test_connection_reset_counted_only_for_that_error():
    sync_service._count_connection_reset(RuntimeError("net::ERR_CONNECTION_RESET at x"))
    sync_service._count_connection_reset(RuntimeError("net::ERR_NAME_NOT_RESOLVED"))
    assert _kinds() == [("detail", "connection_reset")]


# --- login Shape block through _reauth is counted exactly once ------------

def _lawyer(method):
    lw = MagicMock()
    lw.id = 42
    lw.rut = "16021492-9"
    lw.preferred_auth_method = method
    lw.encrypted_clave_unica_password = encrypt_pjud_password("x")
    lw.encrypted_pjud_password = encrypt_pjud_password("x")
    lw.clave_unica_rut = "16021492-9"
    lw.credential_alert_sent_at = None
    return lw


def _shape():
    return ShapeChallengeError(url="u", jquery_present=False, looks_like_login=True, marker="m")


async def _reauth_clave_unica(exc_factory):
    async def login(*a, **k):
        raise exc_factory()

    factory = MagicMock()
    factory.new_page = AsyncMock(return_value=AsyncMock())
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=factory)
    cm.__aexit__ = AsyncMock(return_value=None)
    auth = MagicMock()
    auth.login = login
    with patch("app.scrapper.pjud.browser.BrowserFactory", MagicMock(return_value=cm)), \
         patch("app.scrapper.pjud.clave_unica.ClaveUnicaAuth", MagicMock(return_value=auth)), \
         patch("app.workers.sync_scheduler._audit_validation"):
        return await _reauth(_lawyer("clave_unica"), MagicMock())


@pytest.mark.asyncio
async def test_login_shape_already_counted_at_the_raise_site_is_not_recounted():
    def counted():
        return base._counted_shape("login", _shape())

    session, _ = await _reauth_clave_unica(counted)
    assert session is None
    assert _kinds() == [("login", "shape_challenge")]
    assert list(pm._counts.values()) == [1]


@pytest.mark.asyncio
async def test_clave_unica_shape_not_counted_elsewhere_still_counts_once():
    session, _ = await _reauth_clave_unica(_shape)
    assert session is None
    assert _kinds() == [("login", "shape_challenge")]
    assert list(pm._counts.values()) == [1]
