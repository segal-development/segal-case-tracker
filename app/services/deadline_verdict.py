"""Verdict of a procedural plazo: was the obligation met, and was it on time?

This is a different axis from ``CaseDeadline.status``. ``status`` is the
lifecycle of the row (active / superseded / expired) and is also what an
auditor sets by hand (cumplido / no_cumplido). The verdict answers "did the
obligation get fulfilled, when, and with what evidence" and must survive the
row being superseded, which is exactly the moment the engine used to lose it.

Verdict values (``CaseDeadline.verdict``; NULL means "sin determinar"):

  cumplido              the act exists and is dated on or before due_date
  fuera_de_plazo        the act is dated MORE than the publication margin
                        after due_date (see "Publication margin" below)
  registro_tardio       the act is registered after due_date but within the
                        publication margin: inside the measurement error, so we
                        cannot say it was late. The expediente must be checked
  no_cumplido           no act and due_date already passed
  presentado_sin_ancla  the act exists but there is no anchor (no due_date) to
                        say whether it was on time. NOT "cumplido", NOT
                        "no_cumplido". A persisted CaseDeadline always has a
                        due_date, so this value is only reachable for cases
                        with a filing but no plazo row (case-level readers).

Publication margin (``DEADLINE_PUBLICATION_MARGIN_DAYS``, calendar days)
------------------------------------------------------------------------
The "date of the act" is ``Movement.movement_date``, which is when PJUD
PUBLISHED the escrito, not when it was filed. Measured on QA:

* Escritos de excepciones carry NO ``Diligencia:`` date (0 of 3,270), unlike
  notifications, so there is no real date of the act to use.
* PJUD publishes late: over 3,985 notifications, it published after the fact in
  64% of cases, ~4 days on average, and never before.

So a brief filed on the LAST day of the plazo shows up in our data 2 to 4 days
late. When this verdict was first computed, 371 causas were marked
``fuera_de_plazo`` and 92% of them were 5 days late or less (76% only 1-2 days),
i.e. indistinguishable from publication lag. Claiming "filed late" there accuses
a lawyer who probably arrived on time. Inside the margin we therefore say
``registro_tardio`` ("we can't prove it"); only beyond it do we say
``fuera_de_plazo``.

The margin is counted in CALENDAR days because that is the unit the lag was
measured in (date difference between publication and act), and because it
absorbs weekends and holidays without depending on the holiday table. 5 days
covers 92% of the observed cases. Do not remove this: without it the verdict
states a business fact the data cannot support.

acreditar_poder_3d: two biases stack, both toward a false ``fuera_de_plazo``
------------------------------------------------------------------------------
(1) the same publication lag as above; (2) our due_date counts from the
resolution's own ``movement_date`` whereas the firm's spec runs the 3 days from
the NOTIFICATION of that resolution, which is later. The margin for this type is
therefore ``DEADLINE_PUBLICATION_MARGIN_DAYS + DEADLINE_PODER_NOTIFICATION_
ALLOWANCE_DAYS``. The first term was measured on other deadlines; the second is
a judgment, not a measurement (see config). Neither bias can ever produce a
``cumplido``: that needs the publication date to be <= due_date, and both
biases only make the data look LATER than reality. Two further rules:

* The act must be dated STRICTLY after the resolution that opened the plazo
  (``triggered_at``). A demand often says "Acredita Poder" itself, long before
  the order; counting it would be a false ``cumplido``. No ``triggered_at`` means
  no verdict.
* Absence of the act is ``no_cumplido`` only once the margin has also elapsed:
  an escrito filed on time may simply not be published yet.

DETECTORS is the registry of "how do we recognise the act that fulfils plazo
X". Only excepciones_8d and acreditar_poder_3d are implemented. Every other type
is deliberately left "sin determinar" until its signal is defined: an invented
verdict is worse than none, because it reads as a business fact ("the firm
missed it") that nobody checked.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Callable, Optional, Sequence

from app.config import settings
from app.core.deadlines_config import DeadlineType
from app.services.poder_deadline import is_compliance_movement


class Verdict(str, Enum):
    CUMPLIDO = "cumplido"
    FUERA_DE_PLAZO = "fuera_de_plazo"
    # Max 20 chars: CaseDeadline.verdict is String(20).
    REGISTRO_TARDIO = "registro_tardio"
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


def is_annulled(movement: object) -> bool:
    """True for a movement the court annulled ("[Nulo] ..."): never evidence."""
    return bool(_NULO_RE.search(getattr(movement, "description", None) or ""))


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
        and not is_annulled(movement)
    )


# A detector returns the movement that fulfils the plazo (the earliest valid
# one — if any filing is on time, the plazo was met), or None. ``triggered_at``
# is the date of the movement that opened the plazo, for detectors whose act is
# defined relative to it.
Detector = Callable[..., Optional[object]]


def _detect_excepciones(
    movements: Sequence[object], triggered_at: Optional[date] = None
) -> Optional[object]:
    filings = [m for m in movements if is_excepciones_filing(m)]
    return min(filings, key=_day) if filings else None


def _detect_poder(
    movements: Sequence[object], triggered_at: Optional[date] = None
) -> Optional[object]:
    """Earliest compliance movement dated STRICTLY after the resolution.

    Same wording and same strictness as ``find_poder_obligation`` (which sets the
    row's ``status``), so the verdict can only be stricter on timing, never
    contradict it on whether the act exists.
    """
    if triggered_at is None:
        return None
    acts = [m for m in movements if is_compliance_movement(m) and _day(m) > triggered_at]
    return min(acts, key=_day) if acts else None


# --- Escrito evidence (case_escritos) ---------------------------------------
#
# ``CaseEscrito.fecha_ingreso`` is the date the party FILED, which is the real
# date of the act. A movement carries the date the court (or PJUD) published or
# resolved it: median 0 days later, p90 15, worst 78. So an escrito is stronger
# evidence of WHEN than a movement, and it is only ever used to improve a verdict.
_OPONE_EXCEPCIONES_ESCRITO_RE = re.compile(r"opone\s+excepciones")
# Whole words, so "Demandante" never matches. The ejecutado files the excepciones
# whichever side the firm is on; which plazos are OURS is decided elsewhere
# (``actionable_sets`` / ``_is_ours``), not by who filed.
_DEBTOR_SOLICITANTE_RE = re.compile(r"\b(demandad[oa]s?|ejecutad[oa]s?)\b")


def _normalize(value: Optional[str]) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.lower().split())


def is_excepciones_escrito(escrito: object) -> bool:
    """True for a dated escrito in which the EJECUTADO opposes the execution."""
    return (
        getattr(escrito, "fecha_ingreso", None) is not None
        and bool(_OPONE_EXCEPCIONES_ESCRITO_RE.search(_normalize(getattr(escrito, "tipo_escrito", None))))
        and bool(_DEBTOR_SOLICITANTE_RE.search(_normalize(getattr(escrito, "solicitante", None))))
    )


# Which escritos fulfil which plazo. Only excepciones has a defined escrito
# signal; every other type ignores escritos (no invented evidence).
EscritoMatcher = Callable[[object], bool]
ESCRITO_MATCHERS: dict[str, EscritoMatcher] = {
    DeadlineType.EXCEPCIONES_8D.value: is_excepciones_escrito,
}


def verdict_uses_escritos(deadline_type: str) -> bool:
    """Whether ``evaluate_deadline`` can read escritos for this plazo type."""
    return deadline_type in ESCRITO_MATCHERS


DETECTORS: dict[str, Detector] = {
    DeadlineType.EXCEPCIONES_8D.value: _detect_excepciones,
    DeadlineType.ACREDITAR_PODER_3D.value: _detect_poder,
}

# Detectors that cannot answer without ``triggered_at``: with no anchor the
# honest result is "sin determinar", not "no act found".
_NEEDS_TRIGGER = frozenset({DeadlineType.ACREDITAR_PODER_3D.value})

# Types where "no act yet" must also wait out the margin before it is called
# no_cumplido (the act may be filed on time but not yet published).
_ABSENCE_WAITS_FOR_MARGIN = frozenset({DeadlineType.ACREDITAR_PODER_3D.value})


def default_margin_days(deadline_type: str) -> int:
    """Calendar days of tolerance for ``deadline_type`` (see module docstring)."""
    margin = settings.DEADLINE_PUBLICATION_MARGIN_DAYS
    if deadline_type == DeadlineType.ACREDITAR_PODER_3D.value:
        margin += max(settings.DEADLINE_PODER_NOTIFICATION_ALLOWANCE_DAYS, 0)
    return margin


# Quality order of a verdict: escrito evidence may only move a result UP.
_RANK = {
    None: -1,
    Verdict.NO_CUMPLIDO: 0,
    Verdict.PRESENTADO_SIN_ANCLA: 1,
    Verdict.FUERA_DE_PLAZO: 2,
    Verdict.REGISTRO_TARDIO: 3,
    Verdict.CUMPLIDO: 4,
}


def verdict_rank(value: Optional[object]) -> int:
    """Rank of a stored/computed verdict (``Verdict`` or its string); NULL is lowest."""
    if value is None:
        return _RANK[None]
    try:
        return _RANK[Verdict(value)]
    except ValueError:
        return _RANK[None]


def _evaluate_escritos(
    deadline_type: str,
    due_date: Optional[date],
    escritos: Optional[Sequence[object]],
    triggered_at: Optional[date],
) -> Optional[VerdictResult]:
    """Verdict from the filing date of a matching escrito, or None (no evidence).

    ``fecha_ingreso`` is the real date of the act, so the publication margin
    does not apply: on or before ``due_date`` is on time, after it is late. An
    escrito dated before ``triggered_at`` predates the plazo and is not evidence.
    """
    matcher = ESCRITO_MATCHERS.get(deadline_type)
    if matcher is None or not escritos:
        return None
    days = [
        _day_of_escrito(e) for e in escritos
        if matcher(e) and (triggered_at is None or _day_of_escrito(e) >= triggered_at)
    ]
    if not days:
        return None
    filed_on = min(days)
    if due_date is None:
        return VerdictResult(Verdict.PRESENTADO_SIN_ANCLA, None, filed_on)
    verdict = Verdict.CUMPLIDO if filed_on <= due_date else Verdict.FUERA_DE_PLAZO
    return VerdictResult(verdict, None, filed_on)


def _day_of_escrito(escrito: object) -> date:
    stamp = escrito.fecha_ingreso  # type: ignore[attr-defined]
    return stamp.date() if isinstance(stamp, datetime) else stamp


def evaluate_deadline(
    deadline_type: str,
    due_date: Optional[date],
    movements: Sequence[object],
    today: date,
    *,
    publication_margin_days: Optional[int] = None,
    triggered_at: Optional[date] = None,
    escritos: Optional[Sequence[object]] = None,
) -> VerdictResult:
    """Pure evaluation. Never reads the row's lifecycle ``status``.

    ``publication_margin_days`` defaults to the per-type setting; 0 disables it.

    ``escritos`` (``CaseEscrito``-like rows) are ADDITIVE evidence: the better of
    the movement-based and the escrito-based verdict wins, so passing them can
    only improve a result, never worsen it. With ``None``/empty the result is
    exactly the movement-only one.
    """
    from_movements = _evaluate_movements(
        deadline_type, due_date, movements, today,
        publication_margin_days=publication_margin_days, triggered_at=triggered_at,
    )
    from_escritos = _evaluate_escritos(deadline_type, due_date, escritos, triggered_at)
    if from_escritos is not None and _RANK[from_escritos.verdict] > _RANK[from_movements.verdict]:
        return from_escritos
    return from_movements


def _evaluate_movements(
    deadline_type: str,
    due_date: Optional[date],
    movements: Sequence[object],
    today: date,
    *,
    publication_margin_days: Optional[int] = None,
    triggered_at: Optional[date] = None,
) -> VerdictResult:
    detector = DETECTORS.get(deadline_type)
    if detector is None:
        return UNDETERMINED
    if deadline_type in _NEEDS_TRIGGER and triggered_at is None:
        return UNDETERMINED

    if publication_margin_days is None:
        publication_margin_days = default_margin_days(deadline_type)
    margin = timedelta(days=max(publication_margin_days, 0))

    act = detector(movements, triggered_at=triggered_at)
    if act is None:
        # Absence only proves a miss once the window has actually elapsed.
        if due_date is None:
            return UNDETERMINED
        deadline_end = due_date + (
            margin if deadline_type in _ABSENCE_WAITS_FOR_MARGIN else timedelta(0)
        )
        if deadline_end < today:
            return VerdictResult(Verdict.NO_CUMPLIDO)
        return UNDETERMINED

    acted_on = _day(act)
    movement_id = getattr(act, "id", None)
    if due_date is None:
        return VerdictResult(Verdict.PRESENTADO_SIN_ANCLA, movement_id, acted_on)
    if acted_on <= due_date:
        verdict = Verdict.CUMPLIDO
    elif acted_on <= due_date + margin:
        verdict = Verdict.REGISTRO_TARDIO
    else:
        verdict = Verdict.FUERA_DE_PLAZO
    return VerdictResult(verdict, movement_id, acted_on)
