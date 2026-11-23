"""Soft reset of the scraping station on admin request (odd/tasks/estacion-reinicio-remoto.md S3-S6)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

import importlib

from app.scrapper.pjud.resilience import integration as integ
from app.scrapper.pjud.resilience.circuit_breaker import CircuitState
from app.services import app_settings, estacion_reinicio as er
from app.services import session_store

# resilience/__init__ re-exports a *function* named circuit_breaker: import the module explicitly.
cb_mod = importlib.import_module("app.scrapper.pjud.resilience.circuit_breaker")

T0 = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
STARTED = T0 - timedelta(hours=1)


def _epoch(dt):
    return int(dt.timestamp())


@pytest.fixture(autouse=True)
def _clean_registries():
    saved = (dict(integ._circuit_breakers), dict(cb_mod._circuit_breakers))
    yield
    integ._circuit_breakers.clear()
    integ._circuit_breakers.update(saved[0])
    cb_mod._circuit_breakers.clear()
    cb_mod._circuit_breakers.update(saved[1])


def test_solicitar_records_time_and_is_pending(db):
    estado = er.solicitar(db, T0)
    assert estado == {"solicitado_at": _epoch(T0), "hecho_at": None, "pendiente": True}
    assert er.pendiente(db) is True
    assert app_settings.get_int(db, app_settings.KEY_REINICIO_SOLICITADO, 0) == _epoch(T0)


def test_solicitar_is_idempotent_while_pending(db):
    er.solicitar(db, T0)
    estado = er.solicitar(db, T0 + timedelta(minutes=5))
    assert estado["solicitado_at"] == _epoch(T0)
    assert estado["pendiente"] is True


def test_estado_without_requests(db):
    assert er.estado(db) == {"solicitado_at": None, "hecho_at": None, "pendiente": False}
    assert er.pendiente(db) is False


@pytest.mark.asyncio
async def test_aplicar_resets_open_and_half_open_breakers_and_marks_done(db):
    a = integ.get_competency_circuit_breaker("civil")
    b = cb_mod.get_circuit_breaker("pjud-detail")
    a._state = CircuitState.OPEN
    a._failure_count = 5
    b._state = CircuitState.HALF_OPEN
    b._half_open_calls = 3
    er.solicitar(db, T0)

    res = await er.aplicar_si_pendiente(db, STARTED, T0 + timedelta(seconds=30))

    assert res == "reiniciado"
    assert a.state == CircuitState.CLOSED and a._failure_count == 0
    assert b.state == CircuitState.CLOSED and b._half_open_calls == 0
    estado = er.estado(db)
    assert estado["hecho_at"] == _epoch(T0 + timedelta(seconds=30))
    assert estado["pendiente"] is False


@pytest.mark.asyncio
async def test_aplicar_acts_once_per_request(db):
    er.solicitar(db, T0)
    assert await er.aplicar_si_pendiente(db, STARTED, T0 + timedelta(seconds=5)) == "reiniciado"
    cb = integ.get_competency_circuit_breaker("laboral")
    cb._state = CircuitState.OPEN
    assert await er.aplicar_si_pendiente(db, STARTED, T0 + timedelta(seconds=10)) == "sin_pendiente"
    assert cb.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_new_request_after_done_is_pending_again(db):
    er.solicitar(db, T0)
    await er.aplicar_si_pendiente(db, STARTED, T0)  # done in the same second as the request
    assert er.pendiente(db) is False
    estado = er.solicitar(db, T0)
    assert estado["pendiente"] is True


@pytest.mark.asyncio
async def test_request_older_than_process_start_is_marked_done_without_reset(db):
    er.solicitar(db, T0)
    cb = integ.get_competency_circuit_breaker("penal")
    cb._state = CircuitState.OPEN
    late_start = T0 + timedelta(minutes=10)

    res = await er.aplicar_si_pendiente(db, late_start, late_start + timedelta(seconds=5))

    assert res == "ignorado"
    assert cb.state == CircuitState.OPEN
    assert er.pendiente(db) is False


@pytest.mark.asyncio
async def test_reset_drops_session_store_singleton_and_resets_limiters(db):
    session_store._session_store = object()
    limiter = integ.get_competency_rate_limiter("civil")
    limiter._tokens = 0.0
    er.solicitar(db, T0)
    await er.aplicar_si_pendiente(db, STARTED, T0)
    assert session_store._session_store is None
    assert limiter._tokens == float(limiter.config.burst)


@pytest.mark.asyncio
async def test_exception_inside_reset_is_contained_and_request_marked_done(db, caplog):
    er.solicitar(db, T0)
    with patch.object(er, "_reset_breakers", AsyncMock(side_effect=RuntimeError("boom"))):
        res = await er.aplicar_si_pendiente(db, STARTED, T0)  # must not raise
    assert res == "reiniciado"
    assert er.pendiente(db) is False
    assert "boom" in caplog.text or "falló" in caplog.text


@pytest.mark.asyncio
async def test_never_raises_even_if_db_fails():
    class Broken:
        def query(self, *a, **k):
            raise RuntimeError("db down")

        def rollback(self):
            pass

    assert await er.aplicar_si_pendiente(Broken(), STARTED, T0) == "error"


@pytest.mark.asyncio
async def test_logs_one_info_line(db, caplog):
    import logging

    er.solicitar(db, T0)
    with caplog.at_level(logging.INFO, logger=er.logger.name):
        await er.aplicar_si_pendiente(db, STARTED, T0)
    lines = [r for r in caplog.records if r.name == er.logger.name and r.levelno == logging.INFO]
    assert len(lines) == 1
