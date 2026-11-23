"""Scheduler hook: the soft reset runs only in the scraping station, between units."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.workers import sync_scheduler as ss


@pytest.mark.asyncio
async def test_station_applies_pending_request():
    apply = AsyncMock(return_value="reiniciado")
    db = MagicMock()
    with patch.object(ss.settings, "PJUD_SCRAPING_ENABLED", True), \
         patch("app.services.estacion_reinicio.aplicar_si_pendiente", apply):
        await ss._maybe_reiniciar_estacion(db)
    apply.assert_awaited_once()
    assert apply.await_args.args[0] is db
    assert apply.await_args.args[1] == ss._PROCESS_STARTED_AT


@pytest.mark.asyncio
async def test_vm_process_does_not_act():
    apply = AsyncMock()
    with patch.object(ss.settings, "PJUD_SCRAPING_ENABLED", False), \
         patch("app.services.estacion_reinicio.aplicar_si_pendiente", apply):
        await ss._maybe_reiniciar_estacion(MagicMock())
    apply.assert_not_called()


@pytest.mark.asyncio
async def test_failure_is_isolated():
    with patch.object(ss.settings, "PJUD_SCRAPING_ENABLED", True), \
         patch("app.services.estacion_reinicio.aplicar_si_pendiente", AsyncMock(side_effect=RuntimeError("x"))):
        await ss._maybe_reiniciar_estacion(MagicMock())  # must not raise


@pytest.mark.asyncio
async def test_hook_is_called_between_units_and_at_cycle_start():
    calls = []

    async def fake_hook(db):
        calls.append("hook")

    fake_sync = AsyncMock(return_value={"success": True})
    fake_service = MagicMock()
    fake_service.needs_sync.return_value = True
    timer = MagicMock()
    timer.started_at = 0
    timer.totals = {}
    with patch.object(ss, "_maybe_reiniciar_estacion", fake_hook), \
         patch.object(ss, "_active_lawyer_ids_by_staleness", return_value=[1, 2]), \
         patch.object(ss, "SessionLocal", return_value=MagicMock()), \
         patch.object(ss, "SyncService", return_value=fake_service), \
         patch.object(ss, "sync_lawyer_cases", fake_sync), \
         patch.object(ss, "_wait_for_allowed_egress", AsyncMock()), \
         patch.object(ss, "_maybe_run_sysgal_incorporacion", AsyncMock()), \
         patch.object(ss, "_maybe_sync_sysgal_causas"), patch.object(ss, "_maybe_take_avance_snapshot"), \
         patch.object(ss, "_maybe_sync_sysgal_cuotas"), patch.object(ss, "_maybe_take_cartera_snapshot"), \
         patch.object(ss, "_run_semaforo_alerts_safely"), \
         patch.object(ss.asyncio, "sleep", AsyncMock()), \
         patch.object(ss.settings, "PJUD_SCRAPING_ENABLED", True):
        try:
            await ss._run_sync_cycle(timer)
        except Exception:
            pass
    assert len(calls) >= 2  # at least the cycle start + a unit boundary
