"""Verdict for "why does the system not show what the client reports?".

Pure classification, no I/O. The caller feeds in facts about one case row and
its assigned lawyer; this returns which of five verdicts applies plus a
user-facing explanation in neutral Spanish (tuteo).

Only the BOOLEAN "has a credential" ever enters here. The credential itself
(value, ciphertext, hash, length) must never reach this module.
"""
from datetime import datetime
from typing import Optional, Tuple

SIN_CREDENCIAL = "sin_credencial"
ABOGADO_INACTIVO = "abogado_inactivo"
NUNCA_REVISADA = "nunca_revisada"
LATENCIA = "latencia"
AL_DIA = "al_dia"


def days_since(moment: Optional[datetime], now: datetime) -> Optional[int]:
    """Whole days elapsed since ``moment`` (naive UTC), or None when never."""
    if moment is None:
        return None
    return (now - moment).days


def classify(
    *,
    has_lawyer: bool,
    lawyer_active: Optional[bool],
    lawyer_has_credential: Optional[bool],
    last_detail_checked_at: Optional[datetime],
    latency_days: int,
    now: datetime,
) -> Tuple[str, str]:
    """Return ``(verdict, detail)`` in precedence order.

    1. inactive lawyer  2. no credential  3. never checked  4. latency  5. up to date.
    """
    if has_lawyer and lawyer_active is False:
        return (
            ABOGADO_INACTIVO,
            "El abogado asignado ya no está activo, por lo que esta causa no se revisa. "
            "Reasígnala a un abogado activo.",
        )
    if not has_lawyer or not lawyer_has_credential:
        return (
            SIN_CREDENCIAL,
            "El abogado asignado no tiene credencial del PJUD cargada, por lo que esta "
            "causa no se revisa. Carga su credencial o reasigna la causa.",
        )
    dias = days_since(last_detail_checked_at, now)
    if dias is None:
        return (
            NUNCA_REVISADA,
            "Nunca se le ha descargado el detalle. Está en la cola de revisión y se "
            "revisará cuando le toque su turno.",
        )
    if dias > latency_days:
        return (
            LATENCIA,
            f"Se revisó por última vez hace {dias} días. Es posible que el movimiento "
            "ya esté en el PJUD y todavía no se refleje aquí.",
        )
    return (
        AL_DIA,
        f"Se revisó hace {dias} días. Si el movimiento no aparece, el PJUD aún no lo "
        "publicaba en esa revisión o el sistema no lo capturó. Conviene mirarlo en el PJUD.",
    )
