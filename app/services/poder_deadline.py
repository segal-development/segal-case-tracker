"""Detection of the "acreditar patrocinio y poder" obligation (pure, no DB).

The court's ``Apercibimiento poder y/o título`` resolution opens a 3 días
hábiles window (``DeadlineType.ACREDITAR_PODER_3D``). It runs IN PARALLEL with
the procedural state (it coexists with excepciones in thousands of real cases),
so it is detected here, on the raw movements, instead of through a
``ClassifierRule`` — see ``PARALLEL_DEADLINES`` in ``app/core/deadlines_config``
for why, and for the trigger to pay that debt.

Anchoring: the apercibimiento is a COURT resolution and never carries
``Diligencia:`` (0 of ~7,080 in QA), so callers anchor with ``anchor_date``,
which falls back to ``movement_date``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

# Annulled movements are prefixed "[Nulo]" and must never trigger or fulfil.
_NULO_RE = re.compile(r"^\s*\[nulo\]", re.IGNORECASE)
_APERCIBIMIENTO_RE = re.compile(r"Apercibimiento poder y/o t[ií]tulo", re.IGNORECASE)
# Compliance: acreditar el poder, acompañar patrocinio, or delegar el poder.
_COMPLIANCE_RE = re.compile(
    r"Acredita Poder|Poder acreditado|Patrocinio y poder|Delega poder", re.IGNORECASE
)


@dataclass(frozen=True)
class PoderObligation:
    """The latest valid apercibimiento and whether it was later complied with."""

    trigger: object  # the apercibimiento movement
    fulfilled: bool


def _day(movement: object) -> date:
    mv_dt = movement.movement_date  # type: ignore[attr-defined]
    return mv_dt.date() if isinstance(mv_dt, datetime) else mv_dt


def _description(movement: object) -> str:
    return getattr(movement, "description", None) or ""


def find_poder_obligation(movements: list) -> PoderObligation | None:
    """Return the latest valid apercibimiento, or None when there is none.

    ``fulfilled`` is True when a valid (non-``[Nulo]``) compliance movement is
    dated STRICTLY after the apercibimiento. A same-day movement is not enough
    (it cannot be told apart from the demand's own filings), and an earlier one
    never counts.
    """
    triggers = [
        m
        for m in movements
        if _APERCIBIMIENTO_RE.search(_description(m)) and not _NULO_RE.search(_description(m))
    ]
    if not triggers:
        return None
    trigger = max(triggers, key=_day)
    trigger_day = _day(trigger)
    fulfilled = any(
        _COMPLIANCE_RE.search(_description(m))
        and not _NULO_RE.search(_description(m))
        and _day(m) > trigger_day
        for m in movements
    )
    return PoderObligation(trigger=trigger, fulfilled=fulfilled)
