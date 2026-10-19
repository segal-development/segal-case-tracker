"""Where a plazo stands on its semaforo, and what the alert may say about it.

Pure (no DB, no I/O). The orchestration lives in ``semaforo_alerts``.

THE TERM WE DO NOT KNOW
-----------------------
For excepciones the term is 4 or 8 business days depending on where the
requerimiento was made (comuna asiento del tribunal vs fuera de ella, dentro
del territorio). That fact -- ``lugar_requerimiento`` -- is in no source we
have: not in the PJUD movements, not in the notifications, not in a column.
Today every excepciones plazo is therefore UNCONFIRMED.

An unconfirmed plazo is not assigned a term. It is assessed as BOTH readings:

* ``readings`` keeps one ``Reading`` per candidate term, each with its own
  due date, so the alert can print both and assert neither;
* ``level`` is the most urgent level among the readings still possible, so
  the alarm is never later than the earliest possible deadline;
* a reading whose last day has gone by stays listed with ``level=None``
  ("if it was 4 days, it already expired") instead of disappearing;
* ``slot`` names the combination of live readings. It is the dedupe key: the
  same level can legitimately appear twice (amarillo on day 3 because of the
  4-day reading, again on day 6 because of the 8-day one) and both are real.

A ``confirmed_term_days`` of 4 or 8 collapses it to a single reading.
``confirmed_excepciones_term`` is the one seam where a future source plugs in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from app.core.semaforo_config import (
    EXCEPCIONES_SCALE_BY_TERM,
    SCALE_APELACION_5D,
    SCALE_PODER_3D,
    URGENCY,
    Level,
    Scale,
)
from app.services.business_days import add_business_days, business_days_elapsed

EXCEPCIONES = "excepciones_8d"  # DeadlineType.EXCEPCIONES_8D.value (name is historic)
PODER = "acreditar_poder_3d"  # DeadlineType.ACREDITAR_PODER_3D.value
APELACION = "apelacion_5d"  # DeadlineType.APELACION_5D.value


@dataclass(frozen=True)
class Reading:
    """One possible term for a plazo."""

    term_days: int
    due_date: date
    #: None once ``due_date`` has gone by (that reading is over).
    level: Optional[Level]


@dataclass(frozen=True)
class Assessment:
    deadline_type: str
    triggered_at: date
    #: Day N counted from the trigger (the trigger day itself counts as day 1).
    day: int
    #: True: ONE known term. False: several possible terms, none confirmed.
    confirmed: bool
    readings: tuple[Reading, ...]
    #: Most urgent level among the live readings; None when none is live.
    level: Optional[Level]
    #: Dedupe slot, e.g. "8d", "3d", "5d", "u4+8", "u8" (u = unconfirmed).
    slot: str

    @property
    def live_readings(self) -> tuple[Reading, ...]:
        return tuple(r for r in self.readings if r.level is not None)

    @property
    def earliest_live_due(self) -> Optional[date]:
        live = self.live_readings
        return min(r.due_date for r in live) if live else None


def confirmed_excepciones_term(deadline: Any) -> Optional[int]:
    """The confirmed excepciones term (4 or 8), or None when unknown.

    Always None today: ``lugar_requerimiento`` exists in no source. When one
    appears (a column, a parsed receptor stamp), return its term here and
    nothing else changes -- the alert copy switches from "sin confirmar" to
    the single known term on its own.
    """
    return None


def _reading(scale: Scale, triggered_at: date, today: date, day: int) -> Reading:
    due = add_business_days(triggered_at, scale.term_days)
    # ``today > due`` and not ``day > term``: a Sunday after the due date adds
    # no business day, but the term is over all the same.
    level = None if today > due else scale.level_for_day(day)
    return Reading(term_days=scale.term_days, due_date=due, level=level)


def assess(
    deadline_type: str,
    triggered_at: date,
    today: date,
    confirmed_term_days: Optional[int] = None,
) -> Optional[Assessment]:
    """Assess one plazo on ``today``. None for a type without a semaforo.

    Raises ValueError for a ``confirmed_term_days`` that is not 4 or 8: an
    invented term is exactly what this module exists to prevent.
    """
    # Day 0 (the plazo started today) is day 1 for the scale: the window is open
    # and the lawyer must hear about it, not wait for tomorrow.
    day = max(1, business_days_elapsed(triggered_at, today))

    if deadline_type == PODER:
        readings = (_reading(SCALE_PODER_3D, triggered_at, today, day),)
        return Assessment(
            deadline_type, triggered_at, day, True, readings, readings[0].level, "3d"
        )

    if deadline_type == APELACION:
        readings = (_reading(SCALE_APELACION_5D, triggered_at, today, day),)
        return Assessment(
            deadline_type, triggered_at, day, True, readings, readings[0].level, "5d"
        )

    if deadline_type != EXCEPCIONES:
        return None

    if confirmed_term_days is not None:
        scale = EXCEPCIONES_SCALE_BY_TERM.get(confirmed_term_days)
        if scale is None:
            raise ValueError(
                f"excepciones term must be 4 or 8 business days, got {confirmed_term_days!r}"
            )
        readings = (_reading(scale, triggered_at, today, day),)
        return Assessment(
            deadline_type,
            triggered_at,
            day,
            True,
            readings,
            readings[0].level,
            f"{confirmed_term_days}d",
        )

    readings = tuple(
        _reading(EXCEPCIONES_SCALE_BY_TERM[term], triggered_at, today, day)
        for term in sorted(EXCEPCIONES_SCALE_BY_TERM)
    )
    live = [r for r in readings if r.level is not None]
    level = max((r.level for r in live), key=URGENCY.index) if live else None
    slot = "u" + "+".join(str(r.term_days) for r in live) if live else "u"
    return Assessment(deadline_type, triggered_at, day, False, readings, level, slot)
