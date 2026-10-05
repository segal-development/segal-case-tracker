"""Verdict of a procedural plazo: was the obligation met, and was it on time?

This is a different axis from ``CaseDeadline.status``. ``status`` is the
lifecycle of the row (active / superseded / expired) and is also what an
auditor sets by hand (cumplido / no_cumplido). The verdict answers "did the
obligation get fulfilled, when, and with what evidence" and must survive the
row being superseded, which is exactly the moment the engine used to lose it.

Verdict values (``CaseDeadline.verdict``; NULL means "sin determinar"):

  cumplido              the act exists and is dated on or before due_date
  fuera_de_plazo        the act exists but is dated after due_date
  no_cumplido           no act and due_date already passed
  presentado_sin_ancla  the act exists but there is no anchor (no due_date) to
                        say whether it was on time. NOT "cumplido", NOT
                        "no_cumplido". A persisted CaseDeadline always has a
                        due_date, so this value is only reachable for cases
                        with a filing but no plazo row (case-level readers).

DETECTORS is the registry of "how do we recognise the act that fulfils plazo
X". Only excepciones_8d is implemented. Every other type is deliberately left
"sin determinar" until its signal is defined: an invented verdict is worse than
none, because it reads as a business fact ("the firm missed it") that nobody
checked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Callable, Optional, Sequence

from app.core.deadlines_config import DeadlineType


class Verdict(str, Enum):
    CUMPLIDO = "cumplido"
    FUERA_DE_PLAZO = "fuera_de_plazo"
    NO_CUMPLIDO = "no_cumplido"
    PRESENTADO_SIN_ANCLA = "presentado_sin_ancla"


@dataclass(frozen=True)
class VerdictResult:
    """verdict None = sin determinar (nothing to persist)."""

    verdict: Optional[Verdict]
    movement_id: Optional[int] = None
    acted_on: Optional[date] = None


UNDETERMINED = VerdictResult(verdict=None)

# Annulled movements are prefixed "[Nulo]" and must never count.
_NULO_RE = re.compile(r"^\s*\[nulo\]", re.IGNORECASE)
_OPONE_EXCEPCIONES_RE = re.compile(r"^\s*opone\s+excepciones", re.IGNORECASE)


def _day(movement: object) -> date:
    mv_dt = movement.movement_date  # type: ignore[attr-defined]
    return mv_dt.date() if isinstance(mv_dt, datetime) else mv_dt


def is_excepciones_filing(movement: object) -> bool:
    """True only for the debtor's own filing opposing the execution.

    ``Movement.procedure`` is PJUD's "Trámite" column and says WHO acted:
    ``Escrito`` is the party's filing; ``Resolución`` is the court's ruling on
    it (same "Opone excepciones" text, but NOT the presentation). Here we
    REQUIRE ``Escrito`` (unlike poder_deadline, which excludes it) because the
    act we want is by definition the party's.
    """
    description = getattr(movement, "description", None) or ""
    procedure = (getattr(movement, "procedure", None) or "").strip().lower()
    return (
        procedure == "escrito"
        and bool(_OPONE_EXCEPCIONES_RE.search(description))
        and not _NULO_RE.search(description)
    )


# A detector returns the movement that fulfils the plazo (the earliest valid
# one — if any filing is on time, the plazo was met), or None.
Detector = Callable[[Sequence[object]], Optional[object]]


def _detect_excepciones(movements: Sequence[object]) -> Optional[object]:
    filings = [m for m in movements if is_excepciones_filing(m)]
    return min(filings, key=_day) if filings else None


DETECTORS: dict[str, Detector] = {
    DeadlineType.EXCEPCIONES_8D.value: _detect_excepciones,
}


def evaluate_deadline(
    deadline_type: str,
    due_date: Optional[date],
    movements: Sequence[object],
    today: date,
) -> VerdictResult:
    """Pure evaluation. Never reads the row's lifecycle ``status``."""
    detector = DETECTORS.get(deadline_type)
    if detector is None:
        return UNDETERMINED

    act = detector(movements)
    if act is None:
        # Absence only proves a miss once the window has actually elapsed.
        if due_date is not None and due_date < today:
            return VerdictResult(Verdict.NO_CUMPLIDO)
        return UNDETERMINED

    acted_on = _day(act)
    movement_id = getattr(act, "id", None)
    if due_date is None:
        return VerdictResult(Verdict.PRESENTADO_SIN_ANCLA, movement_id, acted_on)
    verdict = Verdict.CUMPLIDO if acted_on <= due_date else Verdict.FUERA_DE_PLAZO
    return VerdictResult(verdict, movement_id, acted_on)
