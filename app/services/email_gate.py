"""Single gate for every outgoing e-mail.

Two settings, both comma-separated and read from the environment, restrict what
may leave: ``EMAIL_ALLOWED_KINDS`` (which kinds of e-mail) and
``EMAIL_ALLOWED_RECIPIENTS`` (which addresses, To and Cc alike). Empty means no
restriction. Every real send point asks ``email_allowed`` right before handing
the message to SMTP.

On top of them, a server-side kill switch in the DB (``goals.correos_apagados`` = 1)
blocks every e-mail from every process, including the station, whose environment
cannot be edited from the server. It is cached for ``SWITCH_TTL_SECONDS``; a failed
read keeps the last known value.
"""

import logging
import time

from app.config import settings

logger = logging.getLogger(__name__)

MOVIMIENTOS = "movimientos"
PLAZOS = "plazos"
PLAZOS_VERDE = "plazos_verde"
AGENDA = "agenda"
PLAZOS_FATALES_ESTUDIO = "plazos_fatales_estudio"
NOTIFICACIONES = "notificaciones"
SUPERVISOR = "supervisor"
SCRAPING = "scraping"


SWITCH_TTL_SECONDS = 60.0
_switch_cache: dict = {}


def _read_db_switch(db=None) -> bool:
    from app.services import app_settings

    if db is not None:
        return app_settings.get_int(db, app_settings.KEY_CORREOS_APAGADOS, 0) == 1
    from app.core.database import SessionLocal

    session = SessionLocal()
    try:
        return app_settings.get_int(session, app_settings.KEY_CORREOS_APAGADOS, 0) == 1
    finally:
        session.close()


def _db_switch_on() -> bool:
    now = time.monotonic()
    if "value" in _switch_cache and now - _switch_cache.get("at", 0.0) < SWITCH_TTL_SECONDS:
        return _switch_cache["value"]
    try:
        value = _read_db_switch()
    except Exception:  # noqa: BLE001 — keep the last known value
        logger.warning("Email gate: could not read the DB switch; keeping the last value")
        return _switch_cache.get("value", False)
    _switch_cache.update(value=value, at=now)
    return value


def _parse(raw: str) -> set[str]:
    return {item.strip().lower() for item in (raw or "").split(",") if item.strip()}


def email_allowed(kind: str, *addresses: str) -> bool:
    """True iff the DB switch is off and ``kind`` and every address pass the allow-lists."""
    if _db_switch_on():
        logger.info("Email suppressed by DB switch: kind=%s recipients=%d", kind, len(addresses))
        return False
    kinds = _parse(settings.EMAIL_ALLOWED_KINDS)
    recipients = _parse(settings.EMAIL_ALLOWED_RECIPIENTS)

    kind_ok = not kinds or (kind or "").strip().lower() in kinds
    recipients_ok = not recipients or all(
        (addr or "").strip().lower() in recipients for addr in addresses
    )
    if kind_ok and recipients_ok:
        return True

    logger.info(
        "Email suppressed by gate: kind=%s recipients=%d", kind, len(addresses)
    )
    return False
