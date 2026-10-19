"""Semaforo scales per deadline and who is told at each level.

Source: "Flujo Excepciones Dilatorias" (Direccion Juridica, 2026-10-14).

Day N is the Nth business day AFTER the day the term started (the trigger day
is day 0 and is not counted), the same convention as ``add_business_days``. A
term that has an N-day scale is over on day N+1: there is no level after it.

Recipients are NOT hardcoded here: they come from settings
(``SEMAFORO_AMARILLO_EXTRA_LAWYER_IDS`` / ``SEMAFORO_ROJO_EXTRA_LAWYER_IDS``),
so a person can be swapped by editing the environment and restarting the
worker, without shipping code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class Level(str, Enum):
    VERDE = "verde"
    AMARILLO = "amarillo"
    ROJO = "rojo"


#: Lowest to highest urgency; ``max(..., key=URGENCY.index)`` picks the worst.
URGENCY = (Level.VERDE, Level.AMARILLO, Level.ROJO)

#: ``Alert.type`` of each level. Verde is grouped (activity); amarillo and rojo
#: are immediate and live in ``ACTIONABLE_ALERT_TYPES``.
ALERT_TYPE_BY_LEVEL = {
    Level.VERDE: "plazo_verde",
    Level.AMARILLO: "plazo_amarillo",
    Level.ROJO: "plazo_rojo",
}
LEVEL_BY_ALERT_TYPE = {v: k for k, v in ALERT_TYPE_BY_LEVEL.items()}
IMMEDIATE_ALERT_TYPES = frozenset(
    {ALERT_TYPE_BY_LEVEL[Level.AMARILLO], ALERT_TYPE_BY_LEVEL[Level.ROJO]}
)
GROUPED_ALERT_TYPE = ALERT_TYPE_BY_LEVEL[Level.VERDE]
SEMAFORO_ALERT_TYPES = frozenset(ALERT_TYPE_BY_LEVEL.values())

#: A FATAL plazo that expired with the firm not having acted. A channel parallel
#: to the semaforo, NOT a level: it is deliberately absent from
#: ``ALERT_TYPE_BY_LEVEL`` / ``SEMAFORO_ALERT_TYPES`` so the live-term scan and
#: dispatch never see it. Immediate and actionable, like amarillo / rojo.
EXPIRED_ALERT_TYPE = "plazo_vencido"


@dataclass(frozen=True)
class Scale:
    """Inclusive day ranges of each level for one term length."""

    key: str
    term_days: int
    verde: tuple[int, int]
    amarillo: tuple[int, int]
    rojo: tuple[int, int]

    def level_for_day(self, day: int) -> Optional[Level]:
        for level, (first, last) in (
            (Level.VERDE, self.verde),
            (Level.AMARILLO, self.amarillo),
            (Level.ROJO, self.rojo),
        ):
            if first <= day <= last:
                return level
        return None


# Semaforo 1 -- excepciones. Which of the two applies depends on where the
# requerimiento was made, a fact that exists in no source we have.
SCALE_EXCEPCIONES_4D = Scale(
    "excepciones_4d", 4, verde=(1, 2), amarillo=(3, 3), rojo=(4, 4)
)
SCALE_EXCEPCIONES_8D = Scale(
    "excepciones_8d", 8, verde=(1, 5), amarillo=(6, 7), rojo=(8, 8)
)
# Semaforo 2 -- ratificacion de firma / acreditar poder.
SCALE_PODER_3D = Scale(
    "acreditar_poder_3d", 3, verde=(1, 1), amarillo=(2, 2), rojo=(3, 3)
)

EXCEPCIONES_SCALE_BY_TERM = {
    4: SCALE_EXCEPCIONES_4D,
    8: SCALE_EXCEPCIONES_8D,
}

#: What each reading of the excepciones term means, for the copy.
EXCEPCIONES_TERM_PLACE = {
    4: "el requerimiento se hizo en la comuna asiento del tribunal",
    8: "el requerimiento se hizo fuera de la comuna asiento del tribunal, dentro del territorio",
}


def parse_lawyer_ids(raw: Optional[str], *, setting: str = "") -> list[int]:
    """Parse a comma-separated list of lawyer ids from the environment.

    An entry that is not an integer is logged at ERROR and skipped: dropping a
    typo'd recipient silently would be the worst way to lose an escalation.
    Duplicates collapse, order is kept.
    """
    ids: list[int] = []
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            value = int(token)
        except ValueError:
            logger.error(
                "%s: %r is not a lawyer id; ignored. Escalation recipients may be missing.",
                setting or "semaforo recipients",
                token,
            )
            continue
        if value not in ids:
            ids.append(value)
    return ids
