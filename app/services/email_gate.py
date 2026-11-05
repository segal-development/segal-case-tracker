"""Single gate for every outgoing e-mail.

Two settings, both comma-separated and read from the environment, restrict what
may leave: ``EMAIL_ALLOWED_KINDS`` (which kinds of e-mail) and
``EMAIL_ALLOWED_RECIPIENTS`` (which addresses, To and Cc alike). Empty means no
restriction. Every real send point asks ``email_allowed`` right before handing
the message to SMTP.
"""

import logging

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


def _parse(raw: str) -> set[str]:
    return {item.strip().lower() for item in (raw or "").split(",") if item.strip()}


def email_allowed(kind: str, *addresses: str) -> bool:
    """True iff ``kind`` and every address pass the configured allow-lists."""
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
