"""A transient network failure must not cost a lawyer's whole cycle.

Measured 2026-10-20. The Mac that runs the station resolves the PJUD domain
intermittently: 115 ERR_NAME_NOT_RESOLVED that morning, in bursts of minutes
with working windows between them (09:06-09:22 failing, 09:44 a login
succeeded, 09:59-10:00 failing again). Each burst cost ELEVEN of the thirteen
credentialed lawyers their entire sync, and the cycle closed in 17m38s instead
of the usual three hours.

Daily cost over the preceding weeks: 4 to 11 lawyers lost per day.

#165 already established the pattern for the listing: a transient failure
retries instead of discarding thousands of fetched cases. This applies it to
the login, where today a 40-second DNS hiccup discards three hours of work.

What must NEVER retry: invalid credentials, missing credentials, a decrypt
failure, or a stuck login page. Retrying those wastes a session against PJUD
and, for invalid credentials, can lock an account.
"""

from unittest.mock import AsyncMock, patch

import pytest

import app.workers.sync_scheduler as scheduler


@pytest.fixture
def no_sleep(monkeypatch):
    """Never actually wait between attempts."""
    slept: list[float] = []

    async def _fake(seconds):
        slept.append(seconds)

    monkeypatch.setattr(scheduler.asyncio, "sleep", _fake)
    return slept


class TestReauthRetriesOnlyNetworkFailures:
    @pytest.mark.asyncio
    async def test_a_dns_failure_retries_and_the_lawyer_is_not_lost(
        self, db, no_sleep, monkeypatch
    ):
        intentos = []

        async def _reauth(lawyer, store):
            intentos.append(1)
            if len(intentos) == 1:
                return None, (
                    "reauth_failed: Page.goto: net::ERR_NAME_NOT_RESOLVED at "
                    "https://oficinajudicialvirtual.pjud.cl/home"
                )
            return object(), None  # la ventana buena

        sesion, reason = await scheduler._reauth_with_network_retry(
            lawyer=object(), store=object(), reauth=_reauth
        )
        assert len(intentos) == 2
        assert sesion is not None
        assert reason is None
        assert no_sleep, "debe esperar entre intentos, no martillar"

    @pytest.mark.asyncio
    async def test_invalid_credentials_never_retries(self, db, no_sleep):
        intentos = []

        async def _reauth(lawyer, store):
            intentos.append(1)
            return None, "invalid_credentials"

        sesion, reason = await scheduler._reauth_with_network_retry(
            lawyer=object(), store=object(), reauth=_reauth
        )
        assert len(intentos) == 1, "reintentar esto puede bloquear la cuenta"
        assert sesion is None
        assert reason == "invalid_credentials"

    @pytest.mark.asyncio
    async def test_a_stuck_login_page_never_retries(self, db, no_sleep):
        intentos = []

        async def _reauth(lawyer, store):
            intentos.append(1)
            return None, "reauth_failed: stuck on login page (algo)"

        await scheduler._reauth_with_network_retry(
            lawyer=object(), store=object(), reauth=_reauth
        )
        assert len(intentos) == 1

    @pytest.mark.asyncio
    async def test_it_gives_up_after_the_configured_attempts(self, db, no_sleep):
        intentos = []

        async def _reauth(lawyer, store):
            intentos.append(1)
            return None, "reauth_failed: net::ERR_CONNECTION_RESET"

        sesion, reason = await scheduler._reauth_with_network_retry(
            lawyer=object(), store=object(), reauth=_reauth
        )
        assert len(intentos) == scheduler.REAUTH_NETWORK_ATTEMPTS
        assert sesion is None
        assert "ERR_CONNECTION_RESET" in reason
