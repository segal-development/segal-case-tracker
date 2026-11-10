"""Scheduler hook that runs the Sysgal incorporation between lawyer units."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.workers import sync_scheduler as ss


@pytest.mark.asyncio
async def test_forbidden_egress_skips_without_opening_a_pjud_session():
    opener = AsyncMock()
    run = AsyncMock()
    with patch.object(ss, "egress_allowed", return_value=(False, "1.2.3.4", "forbidden")), \
         patch.object(ss, "_open_firm_consulta", opener), \
         patch("app.services.sysgal_incorporacion_pjud.run_incorporacion", run), \
         patch("app.services.app_settings.get_ritmo", return_value={"por_dia": 5, "por_hora": 5}):
        await ss._maybe_run_sysgal_incorporacion(MagicMock())

    opener.assert_not_called()
    run.assert_not_called()


@pytest.mark.asyncio
async def test_runs_with_a_lazy_firm_fetch_and_closes_it():
    fetcher = MagicMock()
    fetcher.fetch = AsyncMock()
    fetcher.aclose = AsyncMock()
    seen = {}

    async def fake_run(db, fetch, **kw):
        seen["fetch"] = fetch
        return {"estado": "ok"}

    with patch.object(ss, "egress_allowed", return_value=(True, "9.9.9.9", "allowed")), \
         patch.object(ss, "_open_firm_consulta", return_value=fetcher), \
         patch("app.services.sysgal_incorporacion_pjud.run_incorporacion", fake_run), \
         patch("app.services.app_settings.get_ritmo", return_value={"por_dia": 5, "por_hora": 5}):
        await ss._maybe_run_sysgal_incorporacion(MagicMock())

    assert seen["fetch"] is fetcher.fetch
    fetcher.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_failure_is_isolated():
    with patch.object(ss, "egress_allowed", side_effect=RuntimeError("boom")), \
         patch("app.services.app_settings.get_ritmo", return_value={"por_dia": 5, "por_hora": 5}):
        await ss._maybe_run_sysgal_incorporacion(MagicMock())  # must not raise


@pytest.mark.asyncio
async def test_off_by_default_does_no_egress_lookup_and_no_session():
    opener = AsyncMock()
    egress = MagicMock()
    with patch.object(ss, "egress_allowed", egress), patch.object(ss, "_open_firm_consulta", opener), \
         patch("app.services.app_settings.get_ritmo", return_value={"por_dia": 0, "por_hora": 10}):
        await ss._maybe_run_sysgal_incorporacion(MagicMock())

    egress.assert_not_called()
    opener.assert_not_called()


# --- Firm session = the firm lawyer's own PJUD session (captcha or Clave Única) ---------------

from types import SimpleNamespace  # noqa: E402

from app.scrapper.pjud.exceptions import ConsultaSessionExpired, LoginPageError  # noqa: E402


def _fake_scraper(results):
    sc = MagicMock()
    sc._browser = None
    sc.start = AsyncMock()
    sc.stop = AsyncMock()
    sc.consulta_by_rol = AsyncMock(side_effect=results)
    return sc


def _consulta(store, scraper):
    lawyer = SimpleNamespace(id=1, rut="11111111-1", preferred_auth_method="captcha")
    c = ss._LawyerSessionConsulta(lawyer, store, MagicMock())
    c._scraper = scraper
    return c


@pytest.mark.asyncio
async def test_stored_firm_session_is_reused_without_login():
    stored = object()
    store = MagicMock(get_session_by_lawyer=AsyncMock(return_value=stored))
    sc = _fake_scraper(["detalle"])
    reauth = AsyncMock()
    with patch.object(ss, "_reauth_with_network_retry", reauth), patch.object(ss, "_reauth", reauth):
        assert await _consulta(store, sc).fetch("C-1-2026", 90) == "detalle"

    reauth.assert_not_called()
    assert sc.consulta_by_rol.await_args.args[0] is stored


@pytest.mark.asyncio
async def test_no_stored_session_logs_in_once_through_the_lawyer_reauth():
    fresh = object()
    store = MagicMock(get_session_by_lawyer=AsyncMock(return_value=None))
    sc = _fake_scraper(["a", "b"])
    reauth = AsyncMock(return_value=(fresh, None))
    with patch.object(ss, "_reauth_with_network_retry", reauth):
        c = _consulta(store, sc)
        await c.fetch("C-1-2026", 90)
        await c.fetch("C-2-2026", 90)

    reauth.assert_awaited_once()
    assert sc.consulta_by_rol.await_args.args[0] is fresh


@pytest.mark.asyncio
async def test_expired_session_reauths_once_and_retries():
    old, fresh = object(), object()
    store = MagicMock(get_session_by_lawyer=AsyncMock(return_value=old))
    sc = _fake_scraper([ConsultaSessionExpired("dead"), "detalle"])
    reauth = AsyncMock(return_value=(fresh, None))
    with patch.object(ss, "_reauth", reauth):
        assert await _consulta(store, sc).fetch("C-1-2026", 90) == "detalle"

    reauth.assert_awaited_once()
    assert sc.consulta_by_rol.await_args.args[0] is fresh


@pytest.mark.asyncio
async def test_failed_login_is_a_login_rejection_and_fetches_nothing():
    store = MagicMock(get_session_by_lawyer=AsyncMock(return_value=None))
    sc = _fake_scraper([])
    with patch.object(ss, "_reauth_with_network_retry", AsyncMock(return_value=(None, "invalid_credentials"))):
        with pytest.raises(LoginPageError):
            await _consulta(store, sc).fetch("C-1-2026", 90)

    sc.consulta_by_rol.assert_not_called()


@pytest.mark.asyncio
async def test_close_stops_only_the_browser_and_keeps_the_lawyer_session():
    store = MagicMock(get_session_by_lawyer=AsyncMock(return_value=object()), adelete_session=AsyncMock())
    sc = _fake_scraper(["x"])
    c = _consulta(store, sc)
    await c.fetch("C-1-2026", 90)
    await c.aclose()

    sc.stop.assert_awaited_once()
    store.adelete_session.assert_not_called()


def test_opener_uses_the_firm_lawyer_session_when_no_env_override(monkeypatch):
    monkeypatch.delenv("CU_RUT", raising=False)
    monkeypatch.delenv("CU_PASSWORD", raising=False)
    firm = SimpleNamespace(id=1, rut="11111111-1", preferred_auth_method="captcha",
                           encrypted_clave_unica_password=None)
    db = MagicMock()
    db.get.return_value = firm
    with patch("app.api.deps.firm_lawyer_id", return_value=1), patch.object(ss, "get_session_store"):
        opened = ss._open_firm_consulta(db)

    assert isinstance(opened, ss._LawyerSessionConsulta)


def test_env_clave_unica_override_still_wins(monkeypatch):
    monkeypatch.setenv("CU_RUT", "22222222-2")
    monkeypatch.setenv("CU_PASSWORD", "x")
    assert isinstance(ss._open_firm_consulta(MagicMock()), ss._FirmConsulta)
