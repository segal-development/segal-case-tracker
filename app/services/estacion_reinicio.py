"""Remote SOFT reset of the scraping station (odd/tasks/estacion-reinicio-remoto.md).

An admin records a request in ``goals``; the station (the only process that scrapes PJUD)
polls it at a safe point of its scheduler and resets its in-memory state: circuit breakers,
rate limiter buckets and the session-store singleton. It never exits the process and never
touches pace, budgets, the egress guard or credentials. Nothing here raises.
"""

import importlib
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.services import app_settings
from app.services.app_settings import KEY_REINICIO_HECHO, KEY_REINICIO_SOLICITADO

logger = logging.getLogger(__name__)


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def _read(db: Session) -> tuple[int, int]:
    return (
        app_settings.get_int(db, KEY_REINICIO_SOLICITADO, 0),
        app_settings.get_int(db, KEY_REINICIO_HECHO, 0),
    )


def estado(db: Session) -> dict:
    solicitado, hecho = _read(db)
    return {
        "solicitado_at": solicitado or None,
        "hecho_at": hecho or None,
        "pendiente": solicitado > hecho,
    }


def pendiente(db: Session) -> bool:
    solicitado, hecho = _read(db)
    return solicitado > hecho


def solicitar(db: Session, now: datetime) -> dict:
    """Record a request (flush only; the caller commits). Keeps the first time while pending."""
    solicitado, hecho = _read(db)
    if solicitado <= hecho:
        # Never equal to hecho_at, or a request made in the same second would not be pending.
        app_settings.set_int(db, KEY_REINICIO_SOLICITADO, max(_epoch(now), hecho + 1))
    return estado(db)


async def _reset_breakers() -> int:
    count = 0
    for mod_name in (
        "app.scrapper.pjud.resilience.integration",
        "app.scrapper.pjud.resilience.circuit_breaker",
    ):
        mod = importlib.import_module(mod_name)
        for cb in list(mod._circuit_breakers.values()):
            await cb.reset()
            count += 1
    return count


async def _reset_limiters() -> int:
    count = 0
    for mod_name in (
        "app.scrapper.pjud.resilience.rate_limiter",
        "app.scrapper.pjud.resilience.integration",
    ):
        mod = importlib.import_module(mod_name)
        for rl in list(mod._rate_limiters.values()):
            await rl.reset()
            count += 1
    return count


def _drop_sessions() -> bool:
    """Drop the process-wide SessionStore singleton (Redis-stored sessions are left alone)."""
    from app.services import session_store

    had = session_store._session_store is not None
    session_store._session_store = None
    return had


async def _reiniciar_suave() -> dict:
    """Run every reset step in isolation: one failing step never blocks the others."""
    out: dict = {}
    for name, step in (
        ("breakers", _reset_breakers),
        ("limiters", _reset_limiters),
    ):
        try:
            out[name] = await step()
        except Exception:
            out[name] = "error"
            logger.exception("Estacion reinicio: falló el paso %s (continúa)", name)
    try:
        out["sesiones"] = _drop_sessions()
    except Exception:
        out["sesiones"] = "error"
        logger.exception("Estacion reinicio: falló el paso sesiones (continúa)")
    return out


async def aplicar_si_pendiente(
    db: Session, process_started_at: datetime, now: datetime
) -> str:
    """Apply a pending request once. Returns sin_pendiente | ignorado | reiniciado | error."""
    try:
        solicitado, hecho = _read(db)
        if solicitado <= hecho:
            return "sin_pendiente"
        hecho_nuevo = max(_epoch(now), solicitado)
        if solicitado < _epoch(process_started_at):
            # A fresh process is already clean: mark it done without resetting.
            app_settings.set_int(db, KEY_REINICIO_HECHO, hecho_nuevo)
            db.commit()
            return "ignorado"
        resumen = await _reiniciar_suave()
        app_settings.set_int(db, KEY_REINICIO_HECHO, hecho_nuevo)
        db.commit()
        logger.info("Estacion reinicio suave aplicado (solicitado_at=%s): %s", solicitado, resumen)
        return "reiniciado"
    except Exception:
        logger.exception("Estacion reinicio: error aplicando la solicitud (no fatal)")
        try:
            db.rollback()
        except Exception:
            logger.exception("Estacion reinicio: rollback falló")
        return "error"
