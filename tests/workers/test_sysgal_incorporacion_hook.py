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
