"""Detection of the "ratificar firma / acreditar patrocinio y poder" obligation
(pure, no DB).

The court's ``Previo a proveer`` resolution (PJUD nomenclature "[760]Previo a
proveer") orders to ratify the signature / acreditar patrocinio y poder within
3 días hábiles (``DeadlineType.ACREDITAR_PODER_3D``). Trigger evidence, real
PJUD resolutions sent by Dirección Jurídica (30º Civil de Santiago C-8844-2026
and 2º Letras Civil de Antofagasta C-2491-2026):

  "En atención a la modificación introducida por la Ley N° 21.394 al artículo 7
   de la Ley N° 20.886, previo a resolver, ratifíquese la firma electrónica
   simple ante el señor Secretario del tribunal... Cumpla lo ordenado, dentro
   del tercer día, bajo apercibimiento de tener por no presentado el escrito."

  "Atendido lo dispuesto en el artículo 7 del Código de Procedimiento Civil,
   ratifíquense la firma de don [X]... De conformidad lo dispone el artículo 49
   del Código de Procedimiento Civil apercíbase al demandado para que en el
   plazo de tres días hábiles señale domicilio conocido..."

Both close with "notificada por el estado diario, con la misma fecha que fue
suscrita", which confirms anchoring to ``movement_date`` (via ``anchor_date``).

It is NOT ``Apercibimiento poder y/o título`` (a different movement: 6,339 cases
in QA vs 2,174 here, only 1,919 overlap — never unify them without measuring).
It runs IN PARALLEL with
the procedural state (it coexists with excepciones in thousands of real cases),
so it is detected here, on the raw movements, instead of through a
``ClassifierRule`` — see ``PARALLEL_DEADLINES`` in ``app/core/deadlines_config``
for why, and for the trigger to pay that debt.

Anchoring: it is a COURT resolution, not a receptor notification, so it carries
no ``Diligencia:``; callers anchor with ``anchor_date``, which falls back to
``movement_date``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

# Annulled movements are prefixed "[Nulo]" and must never trigger or fulfil.
_NULO_RE = re.compile(r"^\s*\[nulo\]", re.IGNORECASE)
_TRIGGER_RE = re.compile(r"Previo a proveer", re.IGNORECASE)
# Compliance: acreditar el poder, acompañar patrocinio, or delegar el poder.
_COMPLIANCE_RE = re.compile(
    r"Acredita Poder|Poder acreditado|Patrocinio y poder|Delega poder", re.IGNORECASE
)


@dataclass(frozen=True)
class PoderObligation:
    """The latest valid "Previo a proveer" and whether it was later complied with."""

    trigger: object  # the "Previo a proveer" movement
    fulfilled: bool


def _day(movement: object) -> date:
    mv_dt = movement.movement_date  # type: ignore[attr-defined]
    return mv_dt.date() if isinstance(mv_dt, datetime) else mv_dt


def _description(movement: object) -> str:
    return getattr(movement, "description", None) or ""


def _is_court_act(movement: object) -> bool:
    """False for an ``Escrito`` — a party's filing, never the tribunal's order.

    ``Movement.procedure`` is PJUD's "Trámite" column (Resolución | Escrito). We
    EXCLUDE ``Escrito`` instead of requiring ``Resolución`` because 56 of the
    2,662 real ``Previo a proveer`` movements have it blank.
    """
    procedure = getattr(movement, "procedure", None) or ""
    return procedure.strip().lower() != "escrito"


def find_poder_obligation(movements: list) -> PoderObligation | None:
    """Return the latest valid ``Previo a proveer``, or None when there is none.

    ``fulfilled`` is True when a valid (non-``[Nulo]``) compliance movement is
    dated STRICTLY after the resolution. A same-day movement is not enough
    (it cannot be told apart from the demand's own filings), and an earlier one
    never counts.
    """
    triggers = [
        m
        for m in movements
        if _TRIGGER_RE.search(_description(m))
        and not _NULO_RE.search(_description(m))
        and _is_court_act(m)
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


# ---------------------------------------------------------------------------
# Whose obligation is it? (HEURISTIC)
#
# The "Previo a proveer" text does not name its addressee. In a juicio ejecutivo
# the excepciones are the ejecutado's, so when the firm is on the ejecutado's
# side (checked by the caller) and an excepciones filing exists BEFORE the
# resolution, we treat the ratification as ours.
#
# HEURISTIC = "an [Escrito] 'Opone excepciones' exists at any earlier point (by
# date, ties by id)". We first required it to be the movement IMMEDIATELY before
# the resolution (the court answers the escrito just before it: "A folio 8:"),
# but measured on QA that missed 414 of 1,358 causas (30%): another escrito had
# been filed in between. Reasoning for widening: if that in-between escrito is
# OURS, the duty to ratify is ours whichever of our escritos the resolution
# answers. We would be wrong only if the in-between escrito were the
# counterparty's, which cannot be told apart from the movement. That risk was
# accepted on purpose: on a FATAL plazo a false positive costs an email, a
# false negative costs the defense.
#
# PRECISE PATH (not implemented): parse the folio from the resolution's PDF text
# and match it to the movement with that folio. Extracted text exists for only
# ~40% of cases (1,053 of 2,663; 837 mention the folio), so it is a later
# refinement, not a replacement.
# ---------------------------------------------------------------------------
_EXCEPCIONES_RE = re.compile(r"^\s*opone excepciones", re.IGNORECASE)


def _order_key(movement: object) -> tuple:
    return (_day(movement), getattr(movement, "id", None) or 0)


def is_excepciones_filing(movement: object) -> bool:
    """An ``Escrito`` "Opone excepciones" (the party's filing, not the ruling)."""
    procedure = (getattr(movement, "procedure", None) or "").strip().lower()
    return (
        procedure == "escrito"
        and bool(_EXCEPCIONES_RE.search(_description(movement)))
        and not _NULO_RE.search(_description(movement))
    )


def responds_to_excepciones_filing(movements: list, trigger: object) -> bool:
    """True when an excepciones filing precedes the resolution (any distance)."""
    key = _order_key(trigger)
    return any(
        m is not trigger and _order_key(m) < key and is_excepciones_filing(m)
        for m in movements
    )
