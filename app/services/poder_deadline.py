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

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime

from app.config import settings

logger = logging.getLogger(__name__)

# Annulled movements are prefixed "[Nulo]" and must never trigger or fulfil.
_NULO_RE = re.compile(r"^\s*\[nulo\]", re.IGNORECASE)
_TRIGGER_RE = re.compile(r"Previo a proveer", re.IGNORECASE)
# Compliance: acreditar el poder, acompañar patrocinio, or delegar el poder.
_COMPLIANCE_RE = re.compile(
    r"Acredita Poder|Poder acreditado|Patrocinio y poder|Delega poder", re.IGNORECASE
)
# Extended triggers (forward-only, see ``PODER_EXTENDED_FROM``): the court's
# ``Mero trámite`` (accent-insensitive, matched on the normalized text) and
# ``Apercibimiento poder y/o título``, same effect as ``Previo a proveer``.
_EXTENDED_TRIGGER_RE = re.compile(r"^\s*(mero tramite|apercibimiento poder)", re.IGNORECASE)
# The court moved on to the traslado: the defense was not lost.
_TRASLADO_RE = re.compile(r"^\s*opone excepciones\s*-\s*traslado", re.IGNORECASE)
# Extra compliance wording, accepted only for the extended triggers.
_EXTENDED_COMPLIANCE_RE = re.compile(r"Cumple lo ordenado", re.IGNORECASE)


def parse_extended_from(value: str | None) -> date | None:
    """The cutoff of the extended trigger, or None when it is OFF.

    Empty = OFF. An invalid value is also OFF (with a WARNING): a typo in the
    environment must never crash the engine, nor silently turn the rule on.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        logger.warning(
            "Invalid PODER_EXTENDED_FROM=%r (expected YYYY-MM-DD): extended poder trigger is OFF",
            raw,
        )
        return None


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


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


def is_compliance_movement(movement: object) -> bool:
    """A valid (non-``[Nulo]``) movement that fulfils the obligation by wording.

    The single definition of "compliance": the engine's ``fulfilled`` flag and the
    verdict detector must agree on it, or a row could read ``cumplido`` by one
    and not by the other. Dating (strictly after the resolution) is the caller's.
    """
    description = _description(movement)
    return bool(_COMPLIANCE_RE.search(description)) and not _NULO_RE.search(description)


def is_previo_a_proveer(movement: object) -> bool:
    """A valid court ``Previo a proveer`` (the original trigger)."""
    description = _description(movement)
    return (
        bool(_TRIGGER_RE.search(description))
        and not _NULO_RE.search(description)
        and _is_court_act(movement)
    )


def is_extended_trigger(movement: object) -> bool:
    """A valid court ``Mero trámite`` / ``Apercibimiento poder`` (wording only).

    A text that also says ``Previo a proveer`` stays a ``Previo a proveer``.
    """
    description = _description(movement)
    return (
        bool(_EXTENDED_TRIGGER_RE.search(_fold(description)))
        and not _TRIGGER_RE.search(description)
        and not _NULO_RE.search(description)
        and _is_court_act(movement)
    )


def is_extended_compliance_movement(movement: object) -> bool:
    """``Cumple lo ordenado``: compliance accepted only for the extended triggers."""
    description = _description(movement)
    return bool(_EXTENDED_COMPLIANCE_RE.search(description)) and not _NULO_RE.search(
        description
    )


def _is_traslado_resolution(movement: object) -> bool:
    description = _description(movement)
    return (
        bool(_TRASLADO_RE.search(description))
        and not _NULO_RE.search(description)
        and _is_court_act(movement)
    )


# Stages that come AFTER the traslado al ejecutante. A Mero tramite dictated in one of them (or
# once the case already went through one) does not threaten the defense: the window of the
# excepciones opuestas is over. PJUD's own stage names, matched accent-insensitively on the start.
_LATER_STAGES = (
    "contestacion excepciones",
    "termino probatorio",
    "sentencia",
    "impugnacion de sentencia",
    "apremio",
)


def _in_later_stage(movement: object) -> bool:
    stage = _fold((getattr(movement, "stage", None) or "").strip()).lower()
    return bool(stage) and stage.startswith(_LATER_STAGES)


def _court_moved_past_window(movements: list, trigger: object) -> bool:
    """True when the trigger itself, or any movement that precedes it, is in a stage after the
    traslado. Real case C-12767-2025: a sentencia stage Mero tramite and no literal
    'Opone excepciones-Traslado' movement, so the traslado check alone missed it."""
    if _in_later_stage(trigger):
        return True
    key = _order_key(trigger)
    return any(_in_later_stage(m) and _order_key(m) < key for m in movements)


def _extended_trigger_applies(
    movements: list, movement: object, extended_from: date | None
) -> bool:
    """Whether ``movement`` (an extended trigger by wording) opens the plazo.

    Needs the cutoff, an ``Opone excepciones`` escrito before it and no
    ``Opone excepciones-Traslado`` resolution on or before its day.
    """
    if extended_from is None or not is_extended_trigger(movement):
        return False
    day = _day(movement)
    if day < extended_from:
        return False
    if not responds_to_excepciones_filing(movements, movement):
        return False
    if _court_moved_past_window(movements, movement):
        return False
    return not any(_is_traslado_resolution(m) and _day(m) <= day for m in movements)


_CUTOFF_FROM_SETTINGS: object = object()


def find_poder_obligation(
    movements: list, extended_from: date | None | object = _CUTOFF_FROM_SETTINGS
) -> PoderObligation | None:
    """Return the latest valid trigger, or None when there is none.

    Triggers: ``Previo a proveer`` (whole history) and, from the
    ``PODER_EXTENDED_FROM`` cutoff on, ``Mero trámite`` / ``Apercibimiento
    poder`` (see ``_extended_trigger_applies``). ``extended_from`` overrides the
    setting (None = extension OFF).

    ``fulfilled`` is True when a valid (non-``[Nulo]``) compliance movement is
    dated STRICTLY after the resolution. A same-day movement is not enough
    (it cannot be told apart from the demand's own filings), and an earlier one
    never counts. An extended trigger also accepts ``Cumple lo ordenado``, and
    when the court moves on to the traslado after it without compliance the
    defense was not lost: there is no obligation (None), so the engine closes
    the row through its usual "superseded" path.
    """
    cutoff = (
        parse_extended_from(settings.PODER_EXTENDED_FROM)
        if extended_from is _CUTOFF_FROM_SETTINGS
        else extended_from
    )
    triggers = [
        m
        for m in movements
        if is_previo_a_proveer(m) or _extended_trigger_applies(movements, m, cutoff)
    ]
    if not triggers:
        return None
    # Ties: a ``Previo a proveer`` outranks an extended trigger of the same day
    # (its stricter compliance wording is the safe reading).
    trigger = max(triggers, key=lambda m: (_day(m), not is_extended_trigger(m)))
    trigger_day = _day(trigger)
    extended = is_extended_trigger(trigger)
    fulfilled = any(
        (
            is_compliance_movement(m)
            or (extended and is_extended_compliance_movement(m))
        )
        and _day(m) > trigger_day
        for m in movements
    )
    if (
        extended
        and not fulfilled
        and any(_is_traslado_resolution(m) and _day(m) > trigger_day for m in movements)
    ):
        return None
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


def obligation_is_ours(movements: list, source_movement_id: object, side: str) -> bool:
    """Whether the plazo opened by movement ``source_movement_id`` is the firm's own.

    Pure part of ``sync_service.poder_obligation_is_ours`` (same heuristic, no
    query, no due-date check): the trigger must be among ``movements``, an
    excepciones filing must precede it and the firm must not be the demandante.
    """
    if source_movement_id is None:
        return False
    trigger = next((m for m in movements if getattr(m, "id", None) == source_movement_id), None)
    if trigger is None or not responds_to_excepciones_filing(movements, trigger):
        return False
    return side != "demandante"


# ---------------------------------------------------------------------------
# Display of the court acts dictated inside the excepciones window
# ---------------------------------------------------------------------------
_WINDOW_RESOLUTIONS_CAP = 8
_WINDOW_ACTION_BUSINESS_DAYS = 3


def window_resolutions(movements: list, today: date) -> list[dict]:
    """Court acts from the first ``Opone excepciones`` up to the first traslado.

    A display of what the court dictated, not the plazo engine: no cutoff, no
    "no later stage" and no ownership checks. Each ``requires_action`` act is
    judged against the compliance dated strictly after ITSELF.
    """
    ordered = sorted(movements, key=_order_key)
    first_filing = next((m for m in ordered if is_excepciones_filing(m)), None)
    if first_filing is None:
        return []
    start = _day(first_filing)

    acts: list = []
    for m in ordered:
        if _day(m) < start or not _is_court_act(m) or _NULO_RE.search(_description(m)):
            continue
        if is_compliance_movement(m) or is_extended_compliance_movement(m):
            continue
        acts.append(m)
        if _is_traslado_resolution(m):
            break

    return [_window_item(m, ordered, today) for m in acts[-_WINDOW_RESOLUTIONS_CAP:]]


def _window_item(movement: object, ordered: list, today: date) -> dict:
    from app.services.business_days import add_business_days

    day = _day(movement)
    item = {
        "date": day.isoformat(),
        "description": _description(movement).strip(),
        "requires_action": False,
        "status": "informativa",
        "due_date": None,
        "resolved_on": None,
        "resolved_by": None,
    }
    if not (is_previo_a_proveer(movement) or is_extended_trigger(movement)):
        return item

    due = add_business_days(day, _WINDOW_ACTION_BUSINESS_DAYS)
    item["requires_action"] = True
    item["due_date"] = due.isoformat()

    compliance = next(
        (
            m
            for m in ordered
            if _day(m) > day
            and (is_compliance_movement(m) or is_extended_compliance_movement(m))
        ),
        None,
    )
    moved_on = next((m for m in ordered if _day(m) > day and _is_traslado_resolution(m)), None)
    if compliance is not None:
        item["status"] = "cumplida"
        item["resolved_on"] = _day(compliance).isoformat()
        item["resolved_by"] = _description(compliance).strip()
    elif moved_on is not None:
        item["status"] = "avanzo"
        item["resolved_on"] = _day(moved_on).isoformat()
    elif due < today:
        item["status"] = "vencida"
    else:
        item["status"] = "pendiente"
    return item
