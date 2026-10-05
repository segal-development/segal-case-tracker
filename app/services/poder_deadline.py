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
