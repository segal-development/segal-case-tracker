"""Scheduler hook for the daily PJUD public "Búsqueda por Fecha"."""

from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.models.busqueda_fecha_run import BusquedaFechaRun
from app.workers import sync_scheduler as ss

WED = datetime(2026, 11, 25, 9, 0)  # Wednesday


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(ss, "_last_publico_fecha_day", None, raising=False)
    monkeypatch.setattr(settings, "PUBLICO_FECHA_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "PUBLICO_FECHA_AFTER_HOUR", 7, raising=False)
    monkeypatch.setattr(settings, "PUBLICO_FECHA_CORTES", "90,91", raising=False)
    monkeypatch.setattr(settings, "PJUD_SCRAPING_ENABLED", True)


async def _hook(db, now=WED, egress=(True, "9.9.9.9", "allowed")):
    run = AsyncMock(return_value={})
    with patch.object(ss, "_chile_now", return_value=now), \
         patch.object(ss, "egress_allowed", return_value=egress) as eg, \
         patch.object(ss, "_run_publico_fecha", run):
        await ss._maybe_run_publico_fecha(db)
    return run, eg


def _done(db, fecha, corte):
    db.add(BusquedaFechaRun(fecha=fecha, corte=corte, status="done", started_at=datetime(2026, 1, 1)))
    db.commit()


def test_defaults():
    f = settings.model_fields
    assert f["PUBLICO_FECHA_ENABLED"].default is False
    assert f["PUBLICO_FECHA_AFTER_HOUR"].default == 7
    assert f["PUBLICO_FECHA_CORTES"].default == "90,91"
    assert f["PUBLICO_FECHA_MAX_DETALLES"].default == 150


@pytest.mark.asyncio
async def test_disabled_does_nothing(db, monkeypatch):
    monkeypatch.setattr(settings, "PUBLICO_FECHA_ENABLED", False)
    run, eg = await _hook(db)
    run.assert_not_called()
    eg.assert_not_called()


@pytest.mark.asyncio
async def test_scraping_off_does_nothing(db, monkeypatch):
    monkeypatch.setattr(settings, "PJUD_SCRAPING_ENABLED", False)
    run, eg = await _hook(db)
    run.assert_not_called()
    eg.assert_not_called()


@pytest.mark.asyncio
async def test_egress_refused_does_not_run(db):
    run, _ = await _hook(db, egress=(False, "1.2.3.4", "forbidden"))
    run.assert_not_called()


@pytest.mark.asyncio
async def test_waits_until_after_hour(db):
    run, _ = await _hook(db, now=datetime(2026, 11, 25, 6, 59))
    run.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("hh,mm", [(3, 30), (4, 0), (4, 29)])
async def test_restart_window_skips(db, monkeypatch, hh, mm):
    monkeypatch.setattr(settings, "PUBLICO_FECHA_AFTER_HOUR", 0)
    run, eg = await _hook(db, now=datetime(2026, 11, 25, hh, mm))
    run.assert_not_called()
    eg.assert_not_called()


@pytest.mark.asyncio
async def test_first_run_searches_yesterday_only_per_corte(db):
    run, _ = await _hook(db)
    calls = [(c.args[1:]) for c in run.await_args_list]
    y = date(2026, 11, 24)
    assert calls == [(y, y, ["90"]), (y, y, ["91"])]


def _calls(run):
    return [(c.args[1], c.args[2], c.args[3]) for c in run.await_args_list]


def _run_row(db, fecha, corte, status):
    db.add(BusquedaFechaRun(fecha=fecha, corte=corte, status=status, started_at=datetime(2026, 1, 1)))
    db.commit()


@pytest.mark.asyncio
async def test_weekend_catch_up_range_after_last_done_day(db):
    monday = datetime(2026, 11, 23, 9, 0)
    _done(db, date(2026, 11, 20), "90")  # Friday done (first run ever for 90)
    _done(db, date(2026, 11, 22), "91")  # Sunday done (first run ever for 91)
    run, _ = await _hook(db, now=monday)
    assert _calls(run) == [(date(2026, 11, 21), date(2026, 11, 22), ["90"])]  # 91 is done through yesterday


@pytest.mark.asyncio
async def test_partial_day_is_retried_even_when_a_later_day_is_done(db):
    _run_row(db, date(2026, 11, 22), "90", "partial")
    _done(db, date(2026, 11, 23), "90")
    run, _ = await _hook(db, now=WED)  # yesterday = 24th
    assert [c for c in _calls(run) if c[2] == ["90"]] == [
        (date(2026, 11, 22), date(2026, 11, 22), ["90"]),
        (date(2026, 11, 24), date(2026, 11, 24), ["90"]),
    ]


@pytest.mark.asyncio
async def test_failed_day_is_retried_on_later_days(db):
    _run_row(db, date(2026, 11, 23), "90", "failed")
    run, _ = await _hook(db, now=WED)
    assert (date(2026, 11, 23), date(2026, 11, 24), ["90"]) in _calls(run)


@pytest.mark.asyncio
async def test_days_older_than_seven_are_never_retried(db):
    _run_row(db, date(2026, 11, 10), "90", "partial")  # first run ever, now 14 days old
    _done(db, date(2026, 11, 20), "90")
    run, _ = await _hook(db, now=WED)  # yesterday = 24 → window 18..24
    assert [c for c in _calls(run) if c[2] == ["90"]] == [
        (date(2026, 11, 18), date(2026, 11, 19), ["90"]),
        (date(2026, 11, 21), date(2026, 11, 24), ["90"]),
    ]


@pytest.mark.asyncio
async def test_never_searches_before_the_first_run_recorded_for_the_corte(db):
    _done(db, date(2026, 11, 22), "90")
    run, _ = await _hook(db, now=WED)
    assert _calls(run) == [(date(2026, 11, 23), date(2026, 11, 24), ["90"]), (date(2026, 11, 24), date(2026, 11, 24), ["91"])]


@pytest.mark.asyncio
async def test_already_done_through_yesterday_is_not_redone(db):
    _done(db, date(2026, 11, 24), "90")
    _done(db, date(2026, 11, 24), "91")
    run, _ = await _hook(db)
    run.assert_not_called()


@pytest.mark.asyncio
async def test_partial_day_is_not_counted_as_done(db):
    db.add(BusquedaFechaRun(fecha=date(2026, 11, 24), corte="90", status="partial", started_at=datetime(2026, 1, 1)))
    db.commit()
    _done(db, date(2026, 11, 24), "91")
    run, _ = await _hook(db)
    assert [c.args[3] for c in run.await_args_list] == [["90"]]


@pytest.mark.asyncio
async def test_runs_once_per_chile_day(db):
    run, _ = await _hook(db)
    assert run.await_count == 2
    run2, _ = await _hook(db)  # same process, same day
    run2.assert_not_called()


@pytest.mark.asyncio
async def test_failure_is_isolated_and_retried_next_cycle(db):
    run = AsyncMock(side_effect=RuntimeError("boom"))
    with patch.object(ss, "_chile_now", return_value=WED), \
         patch.object(ss, "egress_allowed", return_value=(True, "1.1.1.1", "allowed")), \
         patch.object(ss, "_run_publico_fecha", run):
        await ss._maybe_run_publico_fecha(db)  # must not raise
    assert ss._last_publico_fecha_day is None
